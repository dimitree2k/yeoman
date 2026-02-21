"""Shared policy admin command service for DM and CLI surfaces."""

from __future__ import annotations

import asyncio
import json
import re
import threading
import time
import uuid
from collections import defaultdict, deque
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable

import websockets

from nanobot.config.loader import load_config
from nanobot.policy.admin.audit import PolicyAuditEntry, PolicyAuditStore
from nanobot.policy.admin.contracts import (
    PolicyActorContext,
    PolicyCommand,
    PolicyExecutionOptions,
    PolicyExecutionResult,
)
from nanobot.policy.admin.registry import PolicyCommandRegistry
from nanobot.policy.engine import PolicyEngine
from nanobot.policy.identity import normalize_identity_token
from nanobot.policy.loader import load_policy, save_policy
from nanobot.policy.schema import (
    BlockedSendersPolicyOverride,
    ChatPolicyOverride,
    PolicyConfig,
    WhenToReplyMode,
    WhenToReplyPolicyOverride,
    WhoCanTalkPolicyOverride,
)


class PolicyAdminService:
    """Executes policy admin commands against policy.json with guardrails."""

    def __init__(
        self,
        *,
        policy_path: Path,
        workspace: Path,
        known_tools: set[str],
        apply_channels: set[str],
        on_policy_applied: Callable[[PolicyConfig], None] | None = None,
        group_subject_resolver: Callable[[list[str]], dict[str, str]] | None = None,
    ) -> None:
        self._policy_path = policy_path
        self._workspace = workspace
        self._known_tools = set(known_tools)
        self._apply_channels = set(apply_channels)
        self._on_policy_applied = on_policy_applied
        self._registry = PolicyCommandRegistry()
        self._audit = PolicyAuditStore(policy_path)
        self._group_subject_resolver = group_subject_resolver
        self._bridge_subject_cache: dict[str, str] = {}
        self._rate_limit_windows: dict[str, deque[float]] = defaultdict(deque)

    @property
    def registry(self) -> PolicyCommandRegistry:
        return self._registry

    def usage(self) -> str:
        return "\n".join(self._registry.usage_lines())

    def execute_from_text(
        self,
        raw_text: str,
        *,
        actor: PolicyActorContext,
        options: PolicyExecutionOptions | None = None,
    ) -> PolicyExecutionResult:
        try:
            command = self._registry.parse_slash_command(raw_text)
        except ValueError as e:
            return self._result(
                outcome="invalid",
                actor=actor,
                command_name="",
                message=f"Invalid command: {e}",
            )
        return self.execute(command=command, actor=actor, options=options)

    def execute(
        self,
        *,
        command: PolicyCommand,
        actor: PolicyActorContext,
        options: PolicyExecutionOptions | None = None,
    ) -> PolicyExecutionResult:
        exec_opts = options or PolicyExecutionOptions()
        if command.namespace.strip().lower() != "policy":
            return self._result(
                outcome="invalid",
                actor=actor,
                command_name=command.subcommand,
                message=f"Unknown command '/{command.namespace}'. Try /policy help.",
                unknown_command=True,
                dry_run=exec_opts.dry_run,
            )

        subcommand = self._registry.normalize_subcommand(command.subcommand)
        argv, exec_opts = self._registry.split_options(command.argv, base=exec_opts)
        spec = self._registry.get_spec(subcommand)
        if spec is None:
            return self._result(
                outcome="invalid",
                actor=actor,
                command_name=subcommand,
                message=f"Unknown command '/policy {subcommand}'. Try /policy help.",
                unknown_command=True,
                dry_run=exec_opts.dry_run,
            )

        if actor.source == "dm" and not actor.is_owner:
            return self._result(
                outcome="denied",
                actor=actor,
                command_name=subcommand,
                message="Policy command denied.",
                dry_run=exec_opts.dry_run,
            )

        try:
            policy = load_policy(self._policy_path)
        except Exception as e:
            return self._result(
                outcome="error",
                actor=actor,
                command_name=subcommand,
                message=f"Failed to load policy: {e}",
                dry_run=exec_opts.dry_run,
            )

        rate_error = self._rate_limit_message(actor=actor, policy=policy)
        if rate_error is not None:
            return self._result(
                outcome="denied",
                actor=actor,
                command_name=subcommand,
                message=rate_error,
                dry_run=exec_opts.dry_run,
            )

        if spec.risky and policy.runtime.admin_require_confirm_for_risky and not exec_opts.confirm:
            return self._result(
                outcome="invalid",
                actor=actor,
                command_name=subcommand,
                message="Risky command requires --confirm (runtime.adminRequireConfirmForRisky=true).",
                dry_run=exec_opts.dry_run,
            )

        handlers: dict[str, Callable[[PolicyConfig, PolicyActorContext, tuple[str, ...], PolicyExecutionOptions, str], PolicyExecutionResult]] = {
            "help": self._handle_help,
            "list-groups": self._handle_list_groups,
            "resolve-group": self._handle_resolve_group,
            "status-group": self._handle_status_group,
            "explain-group": self._handle_explain_group,
            "allow-group": self._handle_allow_group,
            "block-group": self._handle_block_group,
            "set-when": self._handle_set_when,
            "set-persona": self._handle_set_persona,
            "clear-persona": self._handle_clear_persona,
            "block-sender": self._handle_block_sender,
            "unblock-sender": self._handle_unblock_sender,
            "list-blocked": self._handle_list_blocked,
            "history": self._handle_history,
            "rollback": self._handle_rollback,
        }
        handler = handlers[subcommand]
        return handler(policy, actor, argv, exec_opts, command.raw_text)

    def _rate_limit_message(self, *, actor: PolicyActorContext, policy: PolicyConfig) -> str | None:
        if actor.source != "dm":
            return None
        limit = int(policy.runtime.admin_command_rate_limit_per_minute)
        now = time.monotonic()
        key = f"{actor.source}:{normalize_identity_token(actor.sender_id) or actor.sender_id}"
        window = self._rate_limit_windows[key]
        while window and (now - window[0]) >= 60.0:
            window.popleft()
        if len(window) >= limit:
            return f"Policy command rate limit exceeded ({limit}/minute). Try again shortly."
        window.append(now)
        return None

    def _clone_policy(self, policy: PolicyConfig) -> PolicyConfig:
        return PolicyConfig.model_validate(policy.model_dump(by_alias=True, exclude_none=True))

    def _validate_policy(self, policy: PolicyConfig) -> None:
        engine = PolicyEngine(
            policy=policy,
            workspace=self._workspace,
            apply_channels=self._apply_channels,
        )
        engine.validate(self._known_tools)

    def _commit_policy(
        self,
        *,
        before: PolicyConfig,
        after: PolicyConfig,
        actor: PolicyActorContext,
        command_name: str,
        command_raw: str,
        dry_run: bool,
        is_rollback: bool = False,
        extra_error: str | None = None,
    ) -> PolicyExecutionResult:
        before_hash = self._audit.policy_hash(before)
        after_hash = self._audit.policy_hash(after)
        changed = before_hash != after_hash

        if not changed:
            return self._result(
                outcome="noop",
                actor=actor,
                command_name=command_name,
                message="No policy changes required.",
                mutated=False,
                before_hash=before_hash,
                after_hash=after_hash,
                dry_run=dry_run,
                is_rollback=is_rollback,
            )

        if dry_run:
            return self._result(
                outcome="applied",
                actor=actor,
                command_name=command_name,
                message=f"Dry-run: changes validated for {command_name}.",
                mutated=True,
                before_hash=before_hash,
                after_hash=after_hash,
                dry_run=True,
                is_rollback=is_rollback,
            )

        try:
            self._validate_policy(after)
        except Exception as e:
            return self._result(
                outcome="error",
                actor=actor,
                command_name=command_name,
                message=f"Failed to apply policy change: {e}",
                mutated=False,
                before_hash=before_hash,
                after_hash=after_hash,
                dry_run=dry_run,
                is_rollback=is_rollback,
            )

        change_id = uuid.uuid4().hex
        try:
            backup_ref = self._audit.write_backup(change_id, before)
        except Exception as e:
            return self._result(
                outcome="error",
                actor=actor,
                command_name=command_name,
                message=f"Failed to write policy backup: {e}",
                mutated=False,
                before_hash=before_hash,
                after_hash=after_hash,
                dry_run=dry_run,
                is_rollback=is_rollback,
            )

        try:
            save_policy(after, self._policy_path)
            if self._on_policy_applied is not None:
                self._on_policy_applied(after)
        except Exception as e:
            return self._result(
                outcome="error",
                actor=actor,
                command_name=command_name,
                message=f"Failed to write policy: {e}",
                mutated=False,
                before_hash=before_hash,
                after_hash=after_hash,
                backup_ref=backup_ref,
                audit_id=change_id,
                dry_run=dry_run,
                is_rollback=is_rollback,
            )

        audit_error = extra_error
        audit_write_failed = False
        entry = PolicyAuditEntry(
            id=change_id,
            timestamp=self._audit.now_iso(),
            actor_source=actor.source,
            actor_id=actor.sender_id,
            channel=actor.channel,
            chat_id=actor.chat_id,
            command_raw=command_raw,
            dry_run=dry_run,
            result="applied",
            before_hash=before_hash,
            after_hash=after_hash,
            backup_ref=backup_ref,
            error=audit_error,
        )
        try:
            self._audit.append(entry)
        except Exception:
            audit_write_failed = True

        message = "Policy updated successfully."
        if audit_write_failed:
            message += " Warning: audit write failed."

        return self._result(
            outcome="applied",
            actor=actor,
            command_name=command_name,
            message=message,
            mutated=True,
            before_hash=before_hash,
            after_hash=after_hash,
            backup_ref=backup_ref,
            audit_id=change_id,
            dry_run=dry_run,
            audit_write_failed=audit_write_failed,
            is_rollback=is_rollback,
        )

    def _result(
        self,
        *,
        outcome: str,
        actor: PolicyActorContext,
        command_name: str,
        message: str,
        mutated: bool = False,
        before_hash: str | None = None,
        after_hash: str | None = None,
        audit_id: str | None = None,
        backup_ref: str | None = None,
        dry_run: bool = False,
        unknown_command: bool = False,
        audit_write_failed: bool = False,
        is_rollback: bool = False,
        meta: dict[str, str] | None = None,
    ) -> PolicyExecutionResult:
        return PolicyExecutionResult(
            outcome=outcome,  # type: ignore[arg-type]
            message=message,
            mutated=mutated,
            before_hash=before_hash,
            after_hash=after_hash,
            audit_id=audit_id,
            backup_ref=backup_ref,
            command_name=command_name,
            source=actor.source,
            dry_run=dry_run,
            unknown_command=unknown_command,
            audit_write_failed=audit_write_failed,
            is_rollback=is_rollback,
            meta=meta or {},
        )

    def _parse_group_chat_id(self, value: str) -> str:
        chat_id = value.strip()
        if " " in chat_id or not chat_id.endswith("@g.us"):
            raise ValueError("chat id must be a WhatsApp group id ending in @g.us")
        return chat_id

    def _parse_when_mode(self, value: str) -> WhenToReplyMode:
        mode = value.strip().lower().replace("-", "_")
        aliases = {
            "mention": "mention_only",
            "mentions": "mention_only",
            "mentiononly": "mention_only",
            "allowed": "allowed_senders",
            "owner": "owner_only",
        }
        mode = aliases.get(mode, mode)
        valid = {"all", "mention_only", "allowed_senders", "owner_only", "off"}
        if mode not in valid:
            raise ValueError("mode must be one of: all, mention_only, allowed_senders, owner_only, off")
        return mode  # type: ignore[return-value]

    @staticmethod
    def _sender_keys(senders: list[str]) -> set[str]:
        return {normalize_identity_token(value) for value in senders if normalize_identity_token(value)}

    @staticmethod
    def _whatsapp_chat_override(policy: PolicyConfig, chat_id: str) -> ChatPolicyOverride:
        channel = policy.channels.get("whatsapp")
        if channel is None:
            raise ValueError("whatsapp channel is missing in policy")
        override = channel.chats.get(chat_id)
        if override is None:
            override = ChatPolicyOverride()
            channel.chats[chat_id] = override
        return override

    @staticmethod
    def _chat_alias(chat_id: str) -> str:
        import hashlib

        digest = hashlib.sha256(chat_id.encode("utf-8")).hexdigest()[:10]
        return f"g-{digest}"

    def _discover_groups(self, policy: PolicyConfig) -> dict[str, dict[str, Any]]:
        records: dict[str, dict[str, Any]] = {}

        def ensure(chat_id: str) -> dict[str, Any]:
            rec = records.get(chat_id)
            if rec is None:
                rec = {
                    "chat_id": chat_id,
                    "alias": self._chat_alias(chat_id),
                    "in_policy": False,
                    "comment": "",
                    "tags": [],
                    "seen_session": False,
                    "seen_log": False,
                    "seen_bridge": False,
                    "session_mtime": 0.0,
                }
                records[chat_id] = rec
            return rec

        wa = policy.channels.get("whatsapp")
        if wa is not None:
            for chat_id, override in wa.chats.items():
                if not isinstance(chat_id, str) or not chat_id.endswith("@g.us"):
                    continue
                rec = ensure(chat_id)
                rec["in_policy"] = True
                comment = (override.comment or "").strip()
                if comment:
                    rec["comment"] = comment
                tags: list[str] = []
                for raw in list(override.group_tags or []):
                    tag = str(raw or "").strip()
                    if tag and tag not in tags:
                        tags.append(tag)
                if tags:
                    rec["tags"] = tags

        base_dir = self._policy_path.parent
        sessions_dir = base_dir / "sessions"
        if sessions_dir.exists():
            for path in sessions_dir.glob("whatsapp_*@g.us.jsonl"):
                chat_id = path.name[len("whatsapp_") : -len(".jsonl")]
                if not chat_id.endswith("@g.us"):
                    continue
                rec = ensure(chat_id)
                rec["seen_session"] = True
                try:
                    rec["session_mtime"] = max(float(rec["session_mtime"]), path.stat().st_mtime)
                except OSError:
                    pass

        log_path = base_dir / "logs" / "gateway.log"
        if log_path.exists():
            try:
                with open(log_path, encoding="utf-8", errors="ignore") as f:
                    for line in f:
                        for chat_id in re.findall(r"chat=([0-9a-zA-Z-]+@g\.us)", line):
                            rec = ensure(chat_id)
                            rec["seen_log"] = True
            except OSError:
                pass

        for chat_id, subject in self._bridge_subject_cache.items():
            rec = ensure(chat_id)
            rec["seen_bridge"] = True
            if not str(rec.get("comment") or "").strip():
                rec["comment"] = subject

        resolver = self._group_subject_resolver or self._list_group_subjects_from_bridge
        bridge_names = resolver(list(records.keys()))
        for chat_id, subject in bridge_names.items():
            rec = ensure(chat_id)
            rec["seen_bridge"] = True
            self._bridge_subject_cache[chat_id] = subject
            if not str(rec.get("comment") or "").strip():
                rec["comment"] = subject

        return records

    def _match_group_query(self, query: str, records: dict[str, dict[str, Any]]) -> tuple[str | None, list[str]]:
        target = query.strip()
        if not target:
            return None, []

        if target in records:
            return target, []

        by_alias = [chat_id for chat_id, rec in records.items() if str(rec.get("alias", "")).strip() == target]
        if len(by_alias) == 1:
            return by_alias[0], []
        if len(by_alias) > 1:
            return None, by_alias

        by_tag = [
            chat_id
            for chat_id, rec in records.items()
            if target in {str(tag).strip() for tag in list(rec.get("tags", []))}
        ]
        if len(by_tag) == 1:
            return by_tag[0], []
        if len(by_tag) > 1:
            return None, by_tag

        exact_comment = [
            chat_id
            for chat_id, rec in records.items()
            if str(rec.get("comment", "")).strip() == target and str(rec.get("comment", "")).strip()
        ]
        if len(exact_comment) == 1:
            return exact_comment[0], []
        if len(exact_comment) > 1:
            return None, exact_comment

        lowered = target.lower()
        ci_comment = [
            chat_id
            for chat_id, rec in records.items()
            if lowered == str(rec.get("comment", "")).strip().lower() and str(rec.get("comment", "")).strip()
        ]
        if len(ci_comment) == 1:
            return ci_comment[0], []
        if len(ci_comment) > 1:
            return None, ci_comment

        ci_tag = [
            chat_id
            for chat_id, rec in records.items()
            if lowered in {str(tag).strip().lower() for tag in list(rec.get("tags", []))}
        ]
        if len(ci_tag) == 1:
            return ci_tag[0], []
        if len(ci_tag) > 1:
            return None, ci_tag

        bridge_hits = [
            chat_id for chat_id, subject in self._bridge_subject_cache.items() if subject.strip().lower() == lowered
        ]
        if len(bridge_hits) == 1:
            return bridge_hits[0], []
        if len(bridge_hits) > 1:
            return None, bridge_hits

        lowered_compact = re.sub(r"[\W_]+", "", lowered)
        if len(lowered_compact) >= 4:
            partial_hits: set[str] = set()

            def _matches_value(value: str) -> bool:
                raw = str(value or "").strip().lower()
                if not raw:
                    return False
                if lowered in raw:
                    return True
                raw_compact = re.sub(r"[\W_]+", "", raw)
                if not raw_compact:
                    return False
                return lowered_compact in raw_compact

            for chat_id, rec in records.items():
                alias = str(rec.get("alias") or "")
                comment = str(rec.get("comment") or "")
                subject = str(self._bridge_subject_cache.get(chat_id, "") or "")
                tags = [str(tag or "") for tag in list(rec.get("tags", []))]
                if (
                    _matches_value(alias)
                    or _matches_value(comment)
                    or _matches_value(subject)
                    or any(_matches_value(tag) for tag in tags)
                ):
                    partial_hits.add(chat_id)

            if len(partial_hits) == 1:
                return next(iter(partial_hits)), []
            if len(partial_hits) > 1:
                return None, sorted(partial_hits)

        return None, []

    def resolve_group_reference(
        self,
        query: str,
        *,
        policy: PolicyConfig | None = None,
    ) -> tuple[str | None, str | None]:
        """Resolve one WhatsApp group reference used by non-admin surfaces."""
        target = str(query or "").strip()
        if not target:
            return None, "group reference cannot be empty"
        if " " not in target and target.endswith("@g.us"):
            return target, None

        try:
            effective_policy = policy or load_policy(self._policy_path)
        except Exception as e:
            return None, f"failed to load policy: {e}"

        records = self._discover_groups(effective_policy)
        if not records:
            return None, "no WhatsApp groups discovered yet"

        resolved, ambiguous = self._match_group_query(target, records)
        if resolved is not None:
            return resolved, None
        if ambiguous:
            matches = ", ".join(ambiguous[:3])
            suffix = " ..." if len(ambiguous) > 3 else ""
            return None, f"group reference is ambiguous: {target} ({matches}{suffix})"
        return None, f"unknown group reference: {target}"

    @staticmethod
    def _source_layer(policy: PolicyConfig, chat_id: str, field_name: str) -> str:
        source = "default"
        wa = policy.channels.get("whatsapp")
        if wa is None:
            return source
        if getattr(wa.default, field_name) is not None:
            source = "channel"
        chat_override = wa.chats.get(chat_id)
        if chat_override is not None and getattr(chat_override, field_name) is not None:
            source = "chat"
        return source

    def _handle_help(
        self,
        policy: PolicyConfig,
        actor: PolicyActorContext,
        argv: tuple[str, ...],
        options: PolicyExecutionOptions,
        raw_text: str,
    ) -> PolicyExecutionResult:
        del policy, argv, options, raw_text
        return self._result(
            outcome="noop",
            actor=actor,
            command_name="help",
            message=self.usage(),
            dry_run=False,
        )

    def _handle_list_groups(
        self,
        policy: PolicyConfig,
        actor: PolicyActorContext,
        argv: tuple[str, ...],
        options: PolicyExecutionOptions,
        raw_text: str,
    ) -> PolicyExecutionResult:
        del options, raw_text
        if len(argv) > 1:
            return self._result(
                outcome="invalid",
                actor=actor,
                command_name="list-groups",
                message="Usage: /policy list-groups [query]",
            )

        query = argv[0].strip().lower() if len(argv) == 1 else ""
        records = self._discover_groups(policy)
        if not records:
            return self._result(
                outcome="noop",
                actor=actor,
                command_name="list-groups",
                message="No WhatsApp groups discovered yet.",
            )

        rows: list[dict[str, Any]] = []
        for rec in records.values():
            chat_id = str(rec["chat_id"])
            comment = str(rec["comment"] or "")
            tags_joined = " ".join(str(tag).lower() for tag in list(rec.get("tags", [])))
            if (
                query
                and query not in chat_id.lower()
                and query not in comment.lower()
                and query not in str(rec["alias"]).lower()
                and query not in tags_joined
            ):
                continue
            rows.append(rec)

        if not rows:
            return self._result(
                outcome="noop",
                actor=actor,
                command_name="list-groups",
                message=f"No WhatsApp groups matched '{query}'.",
            )

        rows.sort(
            key=lambda r: (
                0 if bool(r["in_policy"]) else 1,
                -float(r["session_mtime"]),
                str(r["chat_id"]),
            )
        )

        max_rows = 40
        shown = rows[:max_rows]
        lines = [f"Known WhatsApp groups: {len(rows)} (showing {len(shown)})"]
        for rec in shown:
            sources: list[str] = []
            if rec["in_policy"]:
                sources.append("policy")
            if rec["seen_session"]:
                sources.append("sessions")
            if rec["seen_log"]:
                sources.append("log")
            if rec.get("seen_bridge"):
                sources.append("bridge")
            source_text = "+".join(sources) if sources else "unknown"
            chat_id = str(rec["chat_id"])
            alias = str(rec["alias"])
            comment = str(rec["comment"] or "")
            tags = [str(tag).strip() for tag in list(rec.get("tags", [])) if str(tag).strip()]
            tags_suffix = f" | tags: {', '.join(tags)}" if tags else ""
            if comment:
                lines.append(f"- {alias} | {chat_id} | {source_text} | {comment}{tags_suffix}")
            else:
                lines.append(f"- {alias} | {chat_id} | {source_text}{tags_suffix}")

        if len(rows) > max_rows:
            lines.append(f"... and {len(rows) - max_rows} more")
        lines.append("Use: /policy resolve-group <name_or_id>")

        return self._result(
            outcome="noop",
            actor=actor,
            command_name="list-groups",
            message="\n".join(lines),
        )

    def _handle_resolve_group(
        self,
        policy: PolicyConfig,
        actor: PolicyActorContext,
        argv: tuple[str, ...],
        options: PolicyExecutionOptions,
        raw_text: str,
    ) -> PolicyExecutionResult:
        del options, raw_text
        if len(argv) != 1:
            return self._result(
                outcome="invalid",
                actor=actor,
                command_name="resolve-group",
                message="Usage: /policy resolve-group <name_or_id>",
            )

        query = argv[0].strip()
        if not query:
            return self._result(
                outcome="invalid",
                actor=actor,
                command_name="resolve-group",
                message="Usage: /policy resolve-group <name_or_id>",
            )

        records = self._discover_groups(policy)
        resolved, ambiguous = self._match_group_query(query, records)
        if resolved is not None:
            rec = records.get(resolved, {})
            alias = str(rec.get("alias") or self._chat_alias(resolved))
            comment = str(rec.get("comment") or "").strip()
            tags = [str(tag).strip() for tag in list(rec.get("tags", [])) if str(tag).strip()]
            tags_suffix = f" | tags: {', '.join(tags)}" if tags else ""
            suffix = f" | {comment}" if comment else ""
            return self._result(
                outcome="noop",
                actor=actor,
                command_name="resolve-group",
                message=f"Resolved '{query}' -> {resolved} ({alias}){suffix}{tags_suffix}",
            )

        if ambiguous:
            lines = [f"Ambiguous group reference '{query}'. Matches:"]
            for chat_id in ambiguous[:10]:
                rec = records.get(chat_id, {})
                alias = str(rec.get("alias") or self._chat_alias(chat_id))
                comment = str(rec.get("comment") or "").strip()
                if comment:
                    lines.append(f"- {alias} | {chat_id} | {comment}")
                else:
                    lines.append(f"- {alias} | {chat_id}")
            return self._result(
                outcome="invalid",
                actor=actor,
                command_name="resolve-group",
                message="\n".join(lines),
            )

        return self._result(
            outcome="invalid",
            actor=actor,
            command_name="resolve-group",
            message=f"No group matched '{query}'. Try /policy list-groups.",
        )

    def _resolve_existing_chat(self, policy: PolicyConfig, value: str) -> tuple[str | None, str | None]:
        candidate = value.strip()
        if not candidate:
            return None, "chat id cannot be empty"
        if candidate.endswith("@g.us"):
            return candidate, None

        records = self._discover_groups(policy)
        resolved, ambiguous = self._match_group_query(candidate, records)
        if resolved is not None:
            return resolved, None
        if ambiguous:
            return None, f"group reference is ambiguous: {candidate}"
        return None, f"unknown group reference: {candidate}"

    def _handle_status_group(
        self,
        policy: PolicyConfig,
        actor: PolicyActorContext,
        argv: tuple[str, ...],
        options: PolicyExecutionOptions,
        raw_text: str,
    ) -> PolicyExecutionResult:
        del options, raw_text
        if len(argv) != 1:
            return self._result(
                outcome="invalid",
                actor=actor,
                command_name="status-group",
                message="Usage: /policy status-group <chat_id@g.us>",
            )

        chat_id, err = self._resolve_existing_chat(policy, argv[0])
        if err is not None or chat_id is None:
            return self._result(
                outcome="invalid",
                actor=actor,
                command_name="status-group",
                message=f"Invalid status-group arguments: {err}",
            )

        engine = PolicyEngine(policy=policy, workspace=self._workspace, apply_channels=self._apply_channels)
        effective = engine.resolve_policy("whatsapp", chat_id)

        who_src = self._source_layer(policy, chat_id, "who_can_talk")
        when_src = self._source_layer(policy, chat_id, "when_to_reply")
        blocked_src = self._source_layer(policy, chat_id, "blocked_senders")
        tools_src = self._source_layer(policy, chat_id, "allowed_tools")
        persona_src = self._source_layer(policy, chat_id, "persona_file")

        lines = [
            chat_id,
            f"whoCanTalk={effective.who_can_talk_mode} (source={who_src})",
            f"whenToReply={effective.when_to_reply_mode} (source={when_src})",
            f"blockedSenders={','.join(effective.blocked_senders)} (source={blocked_src})",
            f"personaFile={effective.persona_file or '-'} (source={persona_src})",
            f"allowedTools.mode={effective.allowed_tools_mode} (source={tools_src})",
            f"allowedTools.tools={','.join(effective.allowed_tools_tools)}",
            f"allowedTools.deny={','.join(effective.allowed_tools_deny)}",
        ]
        return self._result(
            outcome="noop",
            actor=actor,
            command_name="status-group",
            message="\n".join(lines),
        )

    def _handle_explain_group(
        self,
        policy: PolicyConfig,
        actor: PolicyActorContext,
        argv: tuple[str, ...],
        options: PolicyExecutionOptions,
        raw_text: str,
    ) -> PolicyExecutionResult:
        del options, raw_text
        if len(argv) != 1:
            return self._result(
                outcome="invalid",
                actor=actor,
                command_name="explain-group",
                message="Usage: /policy explain-group <chat_id@g.us>",
            )

        chat_id, err = self._resolve_existing_chat(policy, argv[0])
        if err is not None or chat_id is None:
            return self._result(
                outcome="invalid",
                actor=actor,
                command_name="explain-group",
                message=f"Invalid explain-group arguments: {err}",
            )

        engine = PolicyEngine(policy=policy, workspace=self._workspace, apply_channels=self._apply_channels)
        effective = engine.resolve_policy("whatsapp", chat_id)

        lines = [
            f"Group explain: {chat_id}",
            "merge_trace=defaults -> channels.whatsapp.default -> channels.whatsapp.chats.<chat_id>",
            f"whoCanTalk.source={self._source_layer(policy, chat_id, 'who_can_talk')}",
            f"whenToReply.source={self._source_layer(policy, chat_id, 'when_to_reply')}",
            f"blockedSenders.source={self._source_layer(policy, chat_id, 'blocked_senders')}",
            f"allowedTools.source={self._source_layer(policy, chat_id, 'allowed_tools')}",
            f"personaFile.source={self._source_layer(policy, chat_id, 'persona_file')}",
            f"effective.whoCanTalk={effective.who_can_talk_mode}",
            f"effective.whenToReply={effective.when_to_reply_mode}",
            f"effective.blockedSenders={','.join(effective.blocked_senders)}",
            f"effective.personaFile={effective.persona_file or '-'}",
            f"effective.allowedTools.mode={effective.allowed_tools_mode}",
            f"effective.allowedTools.tools={','.join(effective.allowed_tools_tools)}",
            f"effective.allowedTools.deny={','.join(effective.allowed_tools_deny)}",
        ]

        return self._result(
            outcome="noop",
            actor=actor,
            command_name="explain-group",
            message="\n".join(lines),
        )

    def _handle_allow_group(
        self,
        policy: PolicyConfig,
        actor: PolicyActorContext,
        argv: tuple[str, ...],
        options: PolicyExecutionOptions,
        raw_text: str,
    ) -> PolicyExecutionResult:
        if len(argv) != 1:
            return self._result(
                outcome="invalid",
                actor=actor,
                command_name="allow-group",
                message="Usage: /policy allow-group <chat_id@g.us>",
                dry_run=options.dry_run,
            )

        try:
            chat_id = self._parse_group_chat_id(argv[0])
        except ValueError as e:
            return self._result(
                outcome="invalid",
                actor=actor,
                command_name="allow-group",
                message=f"Invalid allow-group arguments: {e}",
                dry_run=options.dry_run,
            )

        after = self._clone_policy(policy)
        override = self._whatsapp_chat_override(after, chat_id)
        override.who_can_talk = WhoCanTalkPolicyOverride(mode="everyone", senders=[])

        result = self._commit_policy(
            before=policy,
            after=after,
            actor=actor,
            command_name="allow-group",
            command_raw=raw_text,
            dry_run=options.dry_run,
        )
        if result.outcome == "applied":
            return replace(result, message=f"Policy updated for {chat_id}: whoCanTalk=everyone.")
        return result

    def _handle_block_group(
        self,
        policy: PolicyConfig,
        actor: PolicyActorContext,
        argv: tuple[str, ...],
        options: PolicyExecutionOptions,
        raw_text: str,
    ) -> PolicyExecutionResult:
        if len(argv) != 1:
            return self._result(
                outcome="invalid",
                actor=actor,
                command_name="block-group",
                message="Usage: /policy block-group <chat_id@g.us>",
                dry_run=options.dry_run,
            )

        try:
            chat_id = self._parse_group_chat_id(argv[0])
        except ValueError as e:
            return self._result(
                outcome="invalid",
                actor=actor,
                command_name="block-group",
                message=f"Invalid block-group arguments: {e}",
                dry_run=options.dry_run,
            )

        owner_senders = list(policy.owners.get("whatsapp", []))
        if not owner_senders:
            return self._result(
                outcome="invalid",
                actor=actor,
                command_name="block-group",
                message="Cannot block group: owners.whatsapp is empty in policy.",
                dry_run=options.dry_run,
            )

        after = self._clone_policy(policy)
        override = self._whatsapp_chat_override(after, chat_id)
        override.who_can_talk = WhoCanTalkPolicyOverride(mode="allowlist", senders=owner_senders)

        result = self._commit_policy(
            before=policy,
            after=after,
            actor=actor,
            command_name="block-group",
            command_raw=raw_text,
            dry_run=options.dry_run,
        )
        if result.outcome == "applied":
            return replace(result, message=f"Policy updated for {chat_id}: whoCanTalk=allowlist (owners only).")
        return result

    def _handle_set_when(
        self,
        policy: PolicyConfig,
        actor: PolicyActorContext,
        argv: tuple[str, ...],
        options: PolicyExecutionOptions,
        raw_text: str,
    ) -> PolicyExecutionResult:
        if len(argv) != 2:
            return self._result(
                outcome="invalid",
                actor=actor,
                command_name="set-when",
                message="Usage: /policy set-when <chat_id@g.us> <all|mention_only|allowed_senders|owner_only|off>",
                dry_run=options.dry_run,
            )

        try:
            chat_id = self._parse_group_chat_id(argv[0])
            mode = self._parse_when_mode(argv[1])
        except ValueError as e:
            return self._result(
                outcome="invalid",
                actor=actor,
                command_name="set-when",
                message=f"Invalid set-when arguments: {e}",
                dry_run=options.dry_run,
            )

        after = self._clone_policy(policy)
        override = self._whatsapp_chat_override(after, chat_id)
        override.when_to_reply = WhenToReplyPolicyOverride(mode=mode, senders=[])

        result = self._commit_policy(
            before=policy,
            after=after,
            actor=actor,
            command_name="set-when",
            command_raw=raw_text,
            dry_run=options.dry_run,
        )
        if result.outcome == "applied":
            return replace(result, message=f"Policy updated for {chat_id}: whenToReply={mode}.")
        return result

    def _handle_set_persona(
        self,
        policy: PolicyConfig,
        actor: PolicyActorContext,
        argv: tuple[str, ...],
        options: PolicyExecutionOptions,
        raw_text: str,
    ) -> PolicyExecutionResult:
        if len(argv) != 2:
            return self._result(
                outcome="invalid",
                actor=actor,
                command_name="set-persona",
                message="Usage: /policy set-persona <chat_id@g.us> <persona_path>",
                dry_run=options.dry_run,
            )

        try:
            chat_id = self._parse_group_chat_id(argv[0])
        except ValueError as e:
            return self._result(
                outcome="invalid",
                actor=actor,
                command_name="set-persona",
                message=f"Invalid set-persona arguments: {e}",
                dry_run=options.dry_run,
            )

        persona_path = argv[1].strip()
        if not persona_path:
            return self._result(
                outcome="invalid",
                actor=actor,
                command_name="set-persona",
                message="Invalid set-persona arguments: persona_path cannot be empty",
                dry_run=options.dry_run,
            )

        after = self._clone_policy(policy)
        override = self._whatsapp_chat_override(after, chat_id)
        override.persona_file = persona_path

        result = self._commit_policy(
            before=policy,
            after=after,
            actor=actor,
            command_name="set-persona",
            command_raw=raw_text,
            dry_run=options.dry_run,
        )
        if result.outcome == "applied":
            return replace(result, message=f"Policy updated for {chat_id}: personaFile={persona_path}.")
        return result

    def _handle_clear_persona(
        self,
        policy: PolicyConfig,
        actor: PolicyActorContext,
        argv: tuple[str, ...],
        options: PolicyExecutionOptions,
        raw_text: str,
    ) -> PolicyExecutionResult:
        if len(argv) != 1:
            return self._result(
                outcome="invalid",
                actor=actor,
                command_name="clear-persona",
                message="Usage: /policy clear-persona <chat_id@g.us>",
                dry_run=options.dry_run,
            )

        try:
            chat_id = self._parse_group_chat_id(argv[0])
        except ValueError as e:
            return self._result(
                outcome="invalid",
                actor=actor,
                command_name="clear-persona",
                message=f"Invalid clear-persona arguments: {e}",
                dry_run=options.dry_run,
            )

        after = self._clone_policy(policy)
        override = self._whatsapp_chat_override(after, chat_id)
        override.persona_file = None

        result = self._commit_policy(
            before=policy,
            after=after,
            actor=actor,
            command_name="clear-persona",
            command_raw=raw_text,
            dry_run=options.dry_run,
        )
        if result.outcome == "applied":
            return replace(result, message=f"Policy updated for {chat_id}: personaFile cleared (inherits channel/default policy).")
        return result

    def _handle_block_sender(
        self,
        policy: PolicyConfig,
        actor: PolicyActorContext,
        argv: tuple[str, ...],
        options: PolicyExecutionOptions,
        raw_text: str,
    ) -> PolicyExecutionResult:
        if len(argv) != 2:
            return self._result(
                outcome="invalid",
                actor=actor,
                command_name="block-sender",
                message="Usage: /policy block-sender <chat_id@g.us> <sender_id>",
                dry_run=options.dry_run,
            )

        try:
            chat_id = self._parse_group_chat_id(argv[0])
        except ValueError as e:
            return self._result(
                outcome="invalid",
                actor=actor,
                command_name="block-sender",
                message=f"Invalid block-sender arguments: {e}",
                dry_run=options.dry_run,
            )

        sender = argv[1].strip()
        sender_key = normalize_identity_token(sender)
        if not sender_key:
            return self._result(
                outcome="invalid",
                actor=actor,
                command_name="block-sender",
                message="Invalid block-sender arguments: sender_id cannot be empty",
                dry_run=options.dry_run,
            )

        after = self._clone_policy(policy)
        override = self._whatsapp_chat_override(after, chat_id)
        current = list(override.blocked_senders.senders) if override.blocked_senders else []
        keys = self._sender_keys(current)
        if sender_key not in keys:
            current.append(sender)
        override.blocked_senders = BlockedSendersPolicyOverride(senders=current)

        result = self._commit_policy(
            before=policy,
            after=after,
            actor=actor,
            command_name="block-sender",
            command_raw=raw_text,
            dry_run=options.dry_run,
        )
        if result.outcome == "applied":
            return replace(result, message=f"Policy updated for {chat_id}: blocked sender {sender}.")
        return result

    def _handle_unblock_sender(
        self,
        policy: PolicyConfig,
        actor: PolicyActorContext,
        argv: tuple[str, ...],
        options: PolicyExecutionOptions,
        raw_text: str,
    ) -> PolicyExecutionResult:
        if len(argv) != 2:
            return self._result(
                outcome="invalid",
                actor=actor,
                command_name="unblock-sender",
                message="Usage: /policy unblock-sender <chat_id@g.us> <sender_id>",
                dry_run=options.dry_run,
            )

        try:
            chat_id = self._parse_group_chat_id(argv[0])
        except ValueError as e:
            return self._result(
                outcome="invalid",
                actor=actor,
                command_name="unblock-sender",
                message=f"Invalid unblock-sender arguments: {e}",
                dry_run=options.dry_run,
            )

        sender = argv[1].strip()
        sender_key = normalize_identity_token(sender)
        if not sender_key:
            return self._result(
                outcome="invalid",
                actor=actor,
                command_name="unblock-sender",
                message="Invalid unblock-sender arguments: sender_id cannot be empty",
                dry_run=options.dry_run,
            )

        after = self._clone_policy(policy)
        override = self._whatsapp_chat_override(after, chat_id)
        current = list(override.blocked_senders.senders) if override.blocked_senders else []
        updated = [value for value in current if normalize_identity_token(value) != sender_key]
        override.blocked_senders = BlockedSendersPolicyOverride(senders=updated)

        result = self._commit_policy(
            before=policy,
            after=after,
            actor=actor,
            command_name="unblock-sender",
            command_raw=raw_text,
            dry_run=options.dry_run,
        )
        if result.outcome == "applied":
            return replace(result, message=f"Policy updated for {chat_id}: unblocked sender {sender}.")
        return result

    def _handle_list_blocked(
        self,
        policy: PolicyConfig,
        actor: PolicyActorContext,
        argv: tuple[str, ...],
        options: PolicyExecutionOptions,
        raw_text: str,
    ) -> PolicyExecutionResult:
        del options, raw_text
        if len(argv) != 1:
            return self._result(
                outcome="invalid",
                actor=actor,
                command_name="list-blocked",
                message="Usage: /policy list-blocked <chat_id@g.us>",
            )

        try:
            chat_id = self._parse_group_chat_id(argv[0])
        except ValueError as e:
            return self._result(
                outcome="invalid",
                actor=actor,
                command_name="list-blocked",
                message=f"Invalid list-blocked arguments: {e}",
            )

        override = self._whatsapp_chat_override(policy, chat_id)
        values = list(override.blocked_senders.senders) if override.blocked_senders else []
        if not values:
            msg = f"{chat_id}: blockedSenders is empty."
        else:
            lines = [f"{chat_id}: blockedSenders ({len(values)})"]
            for value in values:
                lines.append(f"- {value}")
            msg = "\n".join(lines)

        return self._result(
            outcome="noop",
            actor=actor,
            command_name="list-blocked",
            message=msg,
        )

    def _handle_history(
        self,
        policy: PolicyConfig,
        actor: PolicyActorContext,
        argv: tuple[str, ...],
        options: PolicyExecutionOptions,
        raw_text: str,
    ) -> PolicyExecutionResult:
        del policy, options, raw_text
        limit = 10
        if len(argv) > 1:
            return self._result(
                outcome="invalid",
                actor=actor,
                command_name="history",
                message="Usage: /policy history [limit]",
            )
        if len(argv) == 1:
            raw_limit = argv[0].strip()
            try:
                limit = max(1, min(100, int(raw_limit)))
            except ValueError:
                return self._result(
                    outcome="invalid",
                    actor=actor,
                    command_name="history",
                    message="Usage: /policy history [limit]",
                )

        rows = self._audit.read_recent(limit)
        if not rows:
            return self._result(
                outcome="noop",
                actor=actor,
                command_name="history",
                message="Policy history is empty.",
            )

        lines = [f"Policy history: {len(rows)} (latest first)"]
        for row in rows:
            command = row.command_raw.strip() or "(unknown command)"
            if len(command) > 80:
                command = command[:77] + "..."
            lines.append(f"- {row.id} | {row.timestamp} | {row.result} | {command}")
        lines.append("Use: /policy rollback <change_id> [--confirm]")

        return self._result(
            outcome="noop",
            actor=actor,
            command_name="history",
            message="\n".join(lines),
        )

    def _handle_rollback(
        self,
        policy: PolicyConfig,
        actor: PolicyActorContext,
        argv: tuple[str, ...],
        options: PolicyExecutionOptions,
        raw_text: str,
    ) -> PolicyExecutionResult:
        if len(argv) != 1:
            return self._result(
                outcome="invalid",
                actor=actor,
                command_name="rollback",
                message="Usage: /policy rollback <change_id> [--confirm] [--dry-run]",
                dry_run=options.dry_run,
                is_rollback=True,
            )

        target_change_id = argv[0].strip()
        if not target_change_id:
            return self._result(
                outcome="invalid",
                actor=actor,
                command_name="rollback",
                message="Usage: /policy rollback <change_id> [--confirm] [--dry-run]",
                dry_run=options.dry_run,
                is_rollback=True,
            )

        target = self._audit.find(target_change_id)
        if target is None:
            return self._result(
                outcome="invalid",
                actor=actor,
                command_name="rollback",
                message=f"Unknown change id: {target_change_id}",
                dry_run=options.dry_run,
                is_rollback=True,
            )
        if not target.backup_ref:
            return self._result(
                outcome="invalid",
                actor=actor,
                command_name="rollback",
                message=f"Change {target_change_id} has no rollback snapshot.",
                dry_run=options.dry_run,
                is_rollback=True,
            )

        try:
            restored = self._audit.load_backup(target.backup_ref)
        except Exception as e:
            return self._result(
                outcome="error",
                actor=actor,
                command_name="rollback",
                message=f"Failed to load rollback snapshot: {e}",
                dry_run=options.dry_run,
                is_rollback=True,
            )

        result = self._commit_policy(
            before=policy,
            after=restored,
            actor=actor,
            command_name="rollback",
            command_raw=raw_text,
            dry_run=options.dry_run,
            is_rollback=True,
            extra_error=f"rollback_target={target_change_id}",
        )
        if result.outcome == "applied":
            return replace(result, message=f"Rollback applied from change {target_change_id}.")
        return result

    def _list_group_subjects_from_bridge(self, ids: list[str]) -> dict[str, str]:
        target_ids = [cid for cid in ids if isinstance(cid, str) and cid.endswith("@g.us")]
        if not target_ids:
            return {}

        try:
            config = load_config()
        except Exception:
            return {}

        if not bool(getattr(config.channels.whatsapp, "enabled", False)):
            return {}

        token = str(getattr(config.channels.whatsapp, "bridge_token", "") or "").strip()
        if not token:
            return {}

        bridge_url = str(config.channels.whatsapp.resolved_bridge_url).strip()
        if not bridge_url:
            return {}

        async def _fetch(url: str, chat_ids: list[str], bridge_token: str) -> dict[str, str]:
            request_id = uuid.uuid4().hex
            payload = {
                "version": 2,
                "type": "list_groups",
                "token": bridge_token,
                "requestId": request_id,
                "accountId": "default",
                "payload": {"ids": chat_ids},
            }
            async with websockets.connect(url) as ws:
                await ws.send(json.dumps(payload))
                deadline = time.monotonic() + 5.0
                while True:
                    timeout = deadline - time.monotonic()
                    if timeout <= 0:
                        raise TimeoutError("bridge did not reply in time")
                    raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
                    data = json.loads(raw)
                    if data.get("version") != 2:
                        continue
                    if data.get("type") != "response":
                        continue
                    if data.get("requestId") != request_id:
                        continue
                    response_payload = data.get("payload")
                    if not isinstance(response_payload, dict):
                        raise RuntimeError("bridge response payload malformed")
                    if not bool(response_payload.get("ok")):
                        return {}
                    result = response_payload.get("result")
                    if not isinstance(result, dict):
                        return {}
                    groups = result.get("groups", [])
                    out: dict[str, str] = {}
                    if isinstance(groups, list):
                        for item in groups:
                            if not isinstance(item, dict):
                                continue
                            gid = str(item.get("chatJid", "")).strip()
                            subj = str(item.get("subject", "")).strip()
                            if gid and subj:
                                out[gid] = subj
                    return out

        result_holder: dict[str, str] = {}
        error_holder: dict[str, Exception] = {}

        def _runner() -> None:
            try:
                result_holder.update(asyncio.run(_fetch(bridge_url, target_ids, token)))
            except Exception as e:
                error_holder["error"] = e

        thread = threading.Thread(target=_runner, daemon=True)
        thread.start()
        thread.join(timeout=6.0)
        if thread.is_alive() or error_holder:
            return {}
        return result_holder
