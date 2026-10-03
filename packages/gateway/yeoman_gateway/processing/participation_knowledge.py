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
    SourceRef,
    TrustedReadContext,
)

MAX_QUERY_CHARS = 600
MAX_ENTRIES = 3
MAX_TEXT_CHARS = 1200


@dataclass(frozen=True, slots=True)
class ParticipationKnowledgeRecord:
    """One selected entry: stable identity, its exact rendered text and its revisions.

    Identity alone is not enough to re-admit an entry after a delayed approval, and
    text alone is not enough to prove it is the same record. Both are carried, with
    the source revisions that make a changed record detectable.
    """

    kind: str
    record_id: str
    text: str
    refs: tuple[tuple[str, int], ...]


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
    _records: tuple[ParticipationKnowledgeRecord, ...]
    _reader_keys: tuple[tuple[str, str, str, str, str, str, int], ...]
    _query: str

    def __init__(
        self,
        text: str = "",
        statements: KnowledgeContext = KnowledgeContext(),
        facts: FactRetrievalResult | None = None,
        revision: str = "",
        reason: str = "empty",
        records: tuple[ParticipationKnowledgeRecord, ...] = (),
        reader_keys: tuple[tuple[str, str, str, str, str, str, int], ...] = (),
        query: str = "",
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
        object.__setattr__(self, "_records", records)
        object.__setattr__(self, "_reader_keys", reader_keys)
        object.__setattr__(self, "_query", str(query))

    @property
    def records(self) -> tuple[ParticipationKnowledgeRecord, ...]:
        """Stable identity, rendered text and revisions of every selected record."""
        return self._records

    @property
    def reader_keys(self) -> tuple[tuple[str, str, str, str, str, str, int], ...]:
        """Identities of the readers the selection was computed for."""
        return self._reader_keys

    @property
    def query(self) -> str:
        """The bounded query this selection was computed from."""
        return self._query

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


@dataclass(frozen=True, slots=True)
class ParticipationKnowledgeReader:
    """One verified trigger author's protected read of the same chat."""

    read_context: TrustedReadContext
    fact_context: FactReadContext

    @property
    def principal_id(self) -> str:
        return str(self.read_context.principal_id)


@dataclass(frozen=True, slots=True)
class ParticipationKnowledgeReaders:
    """Controller-only bundle of the verified trigger author readers.

    Only the selection rendered from this bundle may reach a renderer; the bundle
    itself never leaves the controller.
    """

    readers: tuple[ParticipationKnowledgeReader, ...]

    @property
    def principals(self) -> tuple[str, ...]:
        return tuple(reader.principal_id for reader in self.readers)

    @property
    def single(self) -> ParticipationKnowledgeReader | None:
        return self.readers[0] if len(self.readers) == 1 else None

    def __bool__(self) -> bool:
        return bool(self.readers)


def reader_key(
    read_context: TrustedReadContext, fact_context: FactReadContext
) -> tuple[str, str, str, str, str, str, int]:
    """The reader-identity tuple a bundle shares and a selection records."""
    return (
        str(read_context.principal_id),
        str(read_context.channel),
        str(read_context.chat_id),
        str(read_context.membership_revision or ""),
        str(fact_context.chat_scope_key),
        str(read_context.purpose),
        int(read_context.policy_revision),
    )


def readers_from_single(
    read_context: TrustedReadContext, fact_context: FactReadContext
) -> ParticipationKnowledgeReaders:
    """Wrap one verified reader pair for the multi-reader entry points."""
    return ParticipationKnowledgeReaders(
        readers=(ParticipationKnowledgeReader(read_context, fact_context),)
    )


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
            records=_selection_records(statements, facts),
            reader_keys=(reader_key(read_context, fact_context),),
            query=text,
        )

    def select_for_readers(
        self, *, query: str, readers: ParticipationKnowledgeReaders
    ) -> ParticipationKnowledgeSelection:
        """Select once per verified trigger author and keep only the intersection.

        Every author is verified independently against the whole current recipient
        set, and only entries permitted *and* selected for all of them survive. This
        is deliberately conservative: it never unions authors' rights, and it never
        picks one privileged author. See the plan's documented intersection ceiling.
        """
        ordered = _ordered_readers(readers)
        if not ordered:
            return _empty("empty")
        if len(ordered) == 1:
            reader = ordered[0]
            return self.select(
                query=query,
                read_context=reader.read_context,
                fact_context=reader.fact_context,
            )
        selections: list[ParticipationKnowledgeSelection] = []
        for reader in ordered:
            selection = self.select(
                query=query,
                read_context=reader.read_context,
                fact_context=reader.fact_context,
            )
            if selection.reason == "error":
                return _empty("error")
            selections.append(selection)
        if not any(selection.text for selection in selections):
            return _empty("empty")
        return _combine_records(selections, ordered)

    def revalidate_for_readers(
        self,
        selection: ParticipationKnowledgeSelection,
        *,
        readers: ParticipationKnowledgeReaders,
    ) -> ParticipationKnowledgeSelection:
        """Re-read every original reader and require the same records back.

        A record that merely still exists is not enough: its rendered text and its
        source revisions have to match the persisted evidence, and every original
        reader has to still produce it. Anything else is invalidation.
        """
        if not selection.text:
            return selection
        ordered = _ordered_readers(readers)
        if not ordered:
            raise ParticipationKnowledgeInvalidatedError("no trusted readers remain")
        if tuple(reader_key(r.read_context, r.fact_context) for r in ordered) != tuple(
            selection.reader_keys
        ):
            raise ParticipationKnowledgeInvalidatedError("trigger author set changed")
        current = self.select_for_readers(
            query=selection.query, readers=ParticipationKnowledgeReaders(readers=ordered)
        )
        if current.reason == "error":
            raise ParticipationKnowledgeInvalidatedError("revalidation unavailable")
        if current.records != selection.records or current.text != selection.text:
            raise ParticipationKnowledgeInvalidatedError("selected knowledge changed")
        return current

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
        return self._combine(statements, facts, read_context, fact_context, query=selection.query)

    @staticmethod
    def _combine(
        statements: KnowledgeContext,
        facts: FactRetrievalResult,
        read_context: TrustedReadContext,
        fact_context: FactReadContext,
        *,
        query: str = "",
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
            records=_selection_records(statements, facts),
            reader_keys=(reader_key(read_context, fact_context),),
            query=query,
        )


def _selection_records(
    statements: KnowledgeContext, facts: FactRetrievalResult
) -> tuple[ParticipationKnowledgeRecord, ...]:
    """Freeze each selected entry as identity, rendered text and source revisions."""
    statement_values = [line.strip() for line in _statement_lines(statements)]
    statement_ids = tuple(statements.statement_ids[: len(statement_values)])
    refs_by_id: dict[str, list[tuple[str, int]]] = {}
    for ref in statements.source_refs:
        refs_by_id.setdefault(str(ref.event_id), []).append((str(ref.event_id), int(ref.revision)))
    records: list[ParticipationKnowledgeRecord] = []
    if len(statement_ids) == len(statement_values):
        for statement_id, value in zip(statement_ids, statement_values, strict=True):
            records.append(
                ParticipationKnowledgeRecord(
                    kind="statement",
                    record_id=str(statement_id),
                    text=value,
                    refs=_sorted_refs(refs_by_id.get(str(statement_id), ())),
                )
            )
    for fact_id, line in _fact_lines(facts):
        records.append(
            ParticipationKnowledgeRecord(
                kind="fact",
                record_id=str(fact_id),
                text=line,
                refs=_sorted_refs(list(facts.used_source_refs.get(fact_id, ()))),
            )
        )
    return tuple(records)


def _fact_lines(result: FactRetrievalResult) -> list[tuple[str, str]]:
    """Pair every rendered fact line with the fact id it came from."""
    lines = [line.strip() for line in str(result.text or "").splitlines() if line.strip()]
    ids = [str(key) for key in result.used_source_refs]
    if len(lines) != len(ids):
        return []
    return list(zip(ids, lines, strict=True))


def _sorted_refs(refs: list[tuple[str, int]]) -> tuple[tuple[str, int], ...]:
    return tuple(sorted(set(refs)))


def _ordered_readers(
    readers: ParticipationKnowledgeReaders,
) -> list[ParticipationKnowledgeReader]:
    """Deduplicate and order readers so author order can never change the outcome."""
    unique: dict[tuple[str, str, str, str, str, str, int], ParticipationKnowledgeReader] = {}
    scopes: set[tuple[str, str, str, str]] = set()
    for reader in readers.readers:
        if not _contexts_match(reader.read_context, reader.fact_context):
            return []
        scopes.add(
            (
                str(reader.read_context.chat_id),
                str(reader.read_context.membership_revision or ""),
                str(reader.fact_context.chat_scope_key),
                str(reader.read_context.purpose),
            )
        )
        unique.setdefault(reader_key(reader.read_context, reader.fact_context), reader)
    if len(scopes) != 1:
        return []
    return [unique[key] for key in sorted(unique)]


def _combine_records(
    selections: list[ParticipationKnowledgeSelection],
    readers: list[ParticipationKnowledgeReader],
) -> ParticipationKnowledgeSelection:
    """Keep only records every reader independently selected, identically rendered."""
    common: dict[str, ParticipationKnowledgeRecord] = {}
    for record in selections[0].records:
        if all(record in selection.records for selection in selections[1:]):
            common[record.record_id] = record
    if not common:
        return _empty("empty")
    ordered = [common[key] for key in sorted(common)]
    statement_records = [record for record in ordered if record.kind == "statement"]
    fact_records = [record for record in ordered if record.kind == "fact"]
    statements = KnowledgeContext(
        text="\n".join(record.text for record in statement_records),
        statement_ids=tuple(record.record_id for record in statement_records),
        source_refs=tuple(
            SourceRef(
                event_id,
                revision,
                str(readers[0].read_context.channel),
                str(readers[0].read_context.chat_id),
                "",
                int(readers[0].read_context.now_ms),
            )
            for record in statement_records
            for event_id, revision in record.refs
        ),
        context_revision=str(selections[0].statements.context_revision),
        identity_revision=int(selections[0].statements.identity_revision),
        acl_epoch=int(selections[0].statements.acl_epoch),
        entry_texts=tuple(record.text for record in statement_records),
    )
    fact_refs = {record.record_id: record.refs for record in fact_records}
    facts = FactRetrievalResult(
        text="\n".join(record.text for record in fact_records),
        used_source_refs=fact_refs,
        denied_count=int(selections[0].facts.denied_count),
    )
    text = "\n".join(record.text for record in ordered)
    reader_keys = tuple(
        reader_key(reader.read_context, reader.fact_context) for reader in readers
    )
    return ParticipationKnowledgeSelection(
        text=text,
        statements=statements,
        facts=facts,
        revision=_revision(text, statements, facts, readers[0].read_context, readers=reader_keys),
        reason="selected" if text else "empty",
        records=tuple(ordered),
        reader_keys=reader_keys,
        query=str(selections[0].query),
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
    *,
    readers: tuple[tuple[str, str, str, str, str, str, int], ...] = (),
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
    if readers:
        # A multi-author selection must change when the author set changes.
        payload["readers"] = [list(item) for item in readers]
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]


def _empty(reason: str) -> ParticipationKnowledgeSelection:
    return ParticipationKnowledgeSelection(reason=reason)
