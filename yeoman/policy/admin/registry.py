"""Shared parser and command registry for policy admin commands."""

from __future__ import annotations

import shlex
from dataclasses import dataclass

from yeoman.policy.admin.contracts import PolicyCommand, PolicyExecutionOptions


@dataclass(frozen=True, slots=True)
class PolicyCommandSpec:
    """Static metadata for one policy subcommand."""

    name: str
    mutating: bool
    risky: bool = False


class PolicyCommandRegistry:
    """Canonical slash command parser and policy command metadata registry."""

    def __init__(self) -> None:
        self._specs: dict[str, PolicyCommandSpec] = {
            "help": PolicyCommandSpec("help", mutating=False),
            "list-groups": PolicyCommandSpec("list-groups", mutating=False),
            "allow-group": PolicyCommandSpec("allow-group", mutating=True),
            "block-group": PolicyCommandSpec("block-group", mutating=True),
            "set-when": PolicyCommandSpec("set-when", mutating=True),
            "set-persona": PolicyCommandSpec("set-persona", mutating=True),
            "clear-persona": PolicyCommandSpec("clear-persona", mutating=True),
            "block-sender": PolicyCommandSpec("block-sender", mutating=True),
            "unblock-sender": PolicyCommandSpec("unblock-sender", mutating=True),
            "list-blocked": PolicyCommandSpec("list-blocked", mutating=False),
            "status-group": PolicyCommandSpec("status-group", mutating=False),
            "explain-group": PolicyCommandSpec("explain-group", mutating=False),
            "resolve-group": PolicyCommandSpec("resolve-group", mutating=False),
            "history": PolicyCommandSpec("history", mutating=False),
            "rollback": PolicyCommandSpec("rollback", mutating=True, risky=True),
        }
        self._aliases = {
            "groups": "list-groups",
            "resume-group": "allow-group",
            "pause-group": "block-group",
        }

    def parse_slash_command(self, raw_text: str) -> PolicyCommand:
        compact = raw_text.strip()
        if not compact.startswith("/"):
            raise ValueError("command must start with '/'")

        body = compact[1:].strip()
        if not body:
            raise ValueError("empty command")

        try:
            tokens = shlex.split(body)
        except ValueError as e:
            raise ValueError(f"invalid command syntax: {e}") from e

        if not tokens:
            raise ValueError("empty command")

        namespace = tokens[0].strip().lower()
        if not namespace:
            raise ValueError("missing command namespace")

        subcommand = "help"
        argv: tuple[str, ...] = ()
        if len(tokens) > 1:
            subcommand = tokens[1].strip().lower() or "help"
            argv = tuple(tokens[2:])

        return PolicyCommand(
            namespace=namespace,
            subcommand=subcommand,
            argv=argv,
            raw_text=compact,
        )

    def normalize_subcommand(self, name: str) -> str:
        key = (name or "").strip().lower()
        if not key:
            return "help"
        return self._aliases.get(key, key)

    def get_spec(self, subcommand: str) -> PolicyCommandSpec | None:
        return self._specs.get(self.normalize_subcommand(subcommand))

    def is_mutating(self, subcommand: str) -> bool:
        spec = self.get_spec(subcommand)
        return bool(spec and spec.mutating)

    def is_risky(self, subcommand: str) -> bool:
        spec = self.get_spec(subcommand)
        return bool(spec and spec.risky)

    def split_options(
        self,
        argv: tuple[str, ...],
        *,
        base: PolicyExecutionOptions | None = None,
    ) -> tuple[tuple[str, ...], PolicyExecutionOptions]:
        opts = base or PolicyExecutionOptions()
        raw: list[str] = []
        dry_run = opts.dry_run
        confirm = opts.confirm

        for token in argv:
            normalized = token.strip().lower()
            if normalized == "--dry-run":
                dry_run = True
                continue
            if normalized == "--confirm":
                confirm = True
                continue
            raw.append(token)

        return tuple(raw), PolicyExecutionOptions(dry_run=dry_run, confirm=confirm)

    def usage_lines(self) -> tuple[str, ...]:
        return (
            "Policy commands (owner DM only):",
            "/policy help",
            "/policy list-groups [query]",
            "/policy resolve-group <name_or_id>",
            "/policy status-group <chat_id@g.us>",
            "/policy explain-group <chat_id@g.us>",
            "/policy allow-group <chat_id@g.us> [--dry-run]",
            "/policy block-group <chat_id@g.us> [--dry-run]",
            "/policy set-when <chat_id@g.us> <all|mention_only|allowed_senders|owner_only|off> [--dry-run]",
            "/policy set-persona <chat_id@g.us> <persona_path> [--dry-run]",
            "/policy clear-persona <chat_id@g.us> [--dry-run]",
            "/policy block-sender <chat_id@g.us> <sender_id> [--dry-run]",
            "/policy unblock-sender <chat_id@g.us> <sender_id> [--dry-run]",
            "/policy list-blocked <chat_id@g.us>",
            "/policy history [limit]",
            "/policy rollback <change_id> [--confirm] [--dry-run]",
        )
