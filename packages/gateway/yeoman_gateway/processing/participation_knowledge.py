"""Bounded same-chat knowledge selection for Participation."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import Any

from yeoman_gateway.knowledge._memory.shared_facts import FactReadContext, FactRetrievalResult
from yeoman_gateway.knowledge.models import (
    KnowledgeContext,
    RecallQuery,
    TrustedReadContext,
)

MAX_QUERY_CHARS = 600
MAX_ENTRIES = 3
MAX_TEXT_CHARS = 1200


@dataclass(frozen=True, slots=True, init=False)
class ParticipationKnowledgeSelection:
    """Prompt text plus private immutable evidence snapshots."""

    text: str
    statements: KnowledgeContext
    revision: str
    reason: str
    _fact_text: str
    _fact_refs: tuple[tuple[str, tuple[tuple[str, int], ...]], ...]
    _fact_denied_count: int

    def __init__(
        self,
        text: str = "",
        statements: KnowledgeContext = KnowledgeContext(),
        facts: FactRetrievalResult | None = None,
        revision: str = "",
        reason: str = "empty",
    ) -> None:
        snapshot = facts or FactRetrievalResult()
        object.__setattr__(self, "text", str(text))
        object.__setattr__(self, "statements", statements)
        object.__setattr__(self, "revision", str(revision))
        object.__setattr__(self, "reason", str(reason))
        object.__setattr__(self, "_fact_text", str(snapshot.text))
        object.__setattr__(
            self,
            "_fact_refs",
            tuple(
                (str(key), tuple((str(event_id), int(revision)) for event_id, revision in refs))
                for key, refs in snapshot.used_source_refs.items()
            ),
        )
        object.__setattr__(self, "_fact_denied_count", int(snapshot.denied_count))

    @property
    def facts(self) -> FactRetrievalResult:
        refs = MappingProxyType(dict(self._fact_refs))
        return FactRetrievalResult(
            text=self._fact_text,
            hits=(),
            used_source_refs=refs,
            denied_count=self._fact_denied_count,
        )


class ParticipationKnowledgeInvalidatedError(RuntimeError):
    """A previously supplied selection cannot be revalidated."""


class ParticipationKnowledgeSelector:
    def __init__(self, *, knowledge: Any, memory: Any) -> None:
        self._knowledge = knowledge
        self._memory = memory

    def select(
        self,
        *,
        query: str,
        read_context: TrustedReadContext,
        fact_context: FactReadContext,
    ) -> ParticipationKnowledgeSelection:
        text = _normalize_query(query)
        if not text:
            return _empty("empty")
        if not _contexts_match(read_context, fact_context):
            return _empty("denied")

        group_wide = not read_context.is_direct
        scoped_fact_context = replace(fact_context, group_wide=group_wide)
        try:
            statements = self._knowledge.recall(
                RecallQuery(text=text, limit=MAX_ENTRIES),
                context=read_context,
                max_chars=MAX_TEXT_CHARS,
                group_wide=group_wide,
                require_match=True,
            )
            statement_lines = _statement_lines(statements)
            remaining_entries = max(0, MAX_ENTRIES - len(statement_lines))
            remaining_chars = max(
                0, MAX_TEXT_CHARS - len(statements.text) - (1 if statements.text else 0)
            )
            facts = FactRetrievalResult()
            if remaining_entries and remaining_chars:
                facts = self._memory.retrieve_for_context(
                    query=text,
                    read_context=scoped_fact_context,
                    limit=remaining_entries,
                    lexical_only=True,
                    max_chars=remaining_chars,
                )
        except Exception:
            return _empty("error")

        statement_values = [line.strip() for line in statement_lines]
        statement_ids = tuple(statements.statement_ids[: len(statement_values)])
        if len(statement_ids) != len(statement_values):
            statement_values = []
            statement_ids = ()
            statements = KnowledgeContext(reason="unrendered_metadata")

        fact_text, facts = _bounded_facts(
            facts,
            exclude={_normalize_entry(item) for item in statement_values},
            remaining_entries=MAX_ENTRIES - len(statement_values),
            max_chars=max(0, MAX_TEXT_CHARS - len("\n".join(statement_values)) -
                          (1 if statement_values else 0)),
        )
        rendered = "\n".join([*statement_values, *([fact_text] if fact_text else [])])
        if statement_ids != statements.statement_ids:
            statements = replace(statements, statement_ids=statement_ids)
        reason = "selected" if rendered else "empty"
        return ParticipationKnowledgeSelection(
            text=rendered,
            statements=statements,
            facts=facts,
            revision=_revision(rendered, statements, facts, read_context),
            reason=reason,
        )

    def revalidate(
        self,
        selection: ParticipationKnowledgeSelection,
        *,
        read_context: TrustedReadContext,
        fact_context: FactReadContext,
    ) -> ParticipationKnowledgeSelection:
        if not selection.text:
            return selection
        if not _contexts_match(read_context, fact_context):
            raise ParticipationKnowledgeInvalidatedError("trusted reader context changed")
        group_wide = not read_context.is_direct
        scoped_fact_context = replace(fact_context, group_wide=group_wide)
        statements = self._knowledge.revalidate(
            selection.statements,
            context=read_context,
            max_chars=MAX_TEXT_CHARS,
            group_wide=group_wide,
        )
        facts = (
            self._memory.revalidate_for_context(
                selection.facts,
                read_context=scoped_fact_context,
                max_chars=max(0, MAX_TEXT_CHARS - len(statements.text)),
            )
            if selection.facts.used_source_refs
            else FactRetrievalResult()
        )
        return self._combine(statements, facts, read_context)

    @staticmethod
    def _combine(
        statements: KnowledgeContext,
        facts: FactRetrievalResult,
        read_context: TrustedReadContext,
    ) -> ParticipationKnowledgeSelection:
        statement_lines = _statement_lines(statements)
        statement_ids = tuple(statements.statement_ids[: len(statement_lines)])
        if len(statement_ids) != len(statement_lines):
            statements = KnowledgeContext(reason="unrendered_metadata")
            statement_lines = []
            statement_ids = ()
        fact_text, facts = _bounded_facts(
            facts,
            exclude={_normalize_entry(line) for line in statement_lines},
            remaining_entries=max(0, MAX_ENTRIES - len(statement_lines)),
            max_chars=max(0, MAX_TEXT_CHARS - len("\n".join(statement_lines)) -
                          (1 if statement_lines else 0)),
        )
        rendered = "\n".join([*statement_lines, *([fact_text] if fact_text else [])])
        if statement_ids != statements.statement_ids:
            statements = replace(statements, statement_ids=statement_ids)
        return ParticipationKnowledgeSelection(
            text=rendered,
            statements=statements,
            facts=facts,
            revision=_revision(rendered, statements, facts, read_context),
            reason="selected" if rendered else "empty",
        )


def _contexts_match(
    read_context: TrustedReadContext, fact_context: FactReadContext
) -> bool:
    recipients = read_context.recipient_principals
    members = fact_context.current_members
    expected_scope = f"channel:{read_context.channel}:chat:{read_context.chat_id}"
    return bool(
        read_context.purpose == "proactive"
        and not read_context.owner
        and not fact_context.owner
        and recipients
        and read_context.membership_revision
        and read_context.principal_id in recipients
        and members is not None
        and recipients == members
        and fact_context.principal_id == read_context.principal_id
        and fact_context.chat_scope_key == expected_scope
    )


def _normalize_query(query: str) -> str:
    lines = [" ".join(line.split()) for line in str(query or "").splitlines()]
    bounded: list[str] = []
    for line in (line for line in lines if line):
        candidate = " ".join((*bounded, line))
        if len(candidate) > MAX_QUERY_CHARS:
            break
        bounded.append(line)
    return " ".join(bounded)


def _statement_lines(result: KnowledgeContext) -> list[str]:
    if not result.text:
        return []
    if result.entry_texts and len(result.entry_texts) == len(result.statement_ids):
        return list(result.entry_texts)
    lines = [line for line in result.text.splitlines() if line.strip()]
    return lines if len(lines) == len(result.statement_ids) else []


def _normalize_entry(text: str) -> str:
    return re.sub(r"\s+", " ", text.removeprefix("- ").strip()).casefold()


def _bounded_facts(
    result: FactRetrievalResult,
    *,
    exclude: set[str],
    remaining_entries: int,
    max_chars: int,
) -> tuple[str, FactRetrievalResult]:
    if remaining_entries <= 0 or max_chars <= 0:
        return "", FactRetrievalResult(denied_count=result.denied_count)
    refs = result.used_source_refs
    candidates: list[tuple[str, str, tuple[tuple[str, int], ...]]] = []
    if result.hits:
        for hit in result.hits:
            fact_id = str(hit.entry.id)
            if fact_id not in refs:
                continue
            content = str(hit.entry.content or "").strip()
            if not content or _normalize_entry(content) in exclude:
                continue
            candidates.append((fact_id, content, tuple(refs[fact_id])))

    lines: list[str] = []
    selected: dict[str, tuple[tuple[str, int], ...]] = {}
    total = 0
    for fact_id, content, source_refs in candidates:
        line = f"- {content}"
        size = len(line) + (1 if lines else 0)
        if len(lines) >= remaining_entries or total + size > max_chars:
            break
        lines.append(line)
        total += size
        selected[fact_id] = source_refs
    return "\n".join(lines), FactRetrievalResult(
        text="\n".join(lines), used_source_refs=selected, denied_count=result.denied_count
    )


def _revision(
    text: str,
    statements: KnowledgeContext,
    facts: FactRetrievalResult,
    read_context: TrustedReadContext,
) -> str:
    payload = {
        "text": text,
        "statements": list(statements.statement_ids),
        "statement_sources": sorted((item.event_id, item.revision) for item in statements.source_refs),
        "identity_revision": statements.identity_revision,
        "facts": sorted((key, value) for key, value in facts.used_source_refs.items()),
        "principal": read_context.principal_id,
        "channel": read_context.channel,
        "chat": read_context.chat_id,
        "recipients": sorted(read_context.recipient_principals or ()),
        "membership": read_context.membership_revision,
        "policy": read_context.policy_revision,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]


def _empty(reason: str) -> ParticipationKnowledgeSelection:
    return ParticipationKnowledgeSelection(reason=reason)
