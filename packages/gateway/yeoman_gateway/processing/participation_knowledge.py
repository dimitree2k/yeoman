"""Bounded same-chat knowledge selection for Participation."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
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

#: The persisted evidence schema. A bump means an older snapshot is not readable.
KNOWLEDGE_EVIDENCE_VERSION = 1
MAX_EVIDENCE_READERS = 16
MAX_EVIDENCE_MEMBERS = 1024
MAX_EVIDENCE_REVISION_CHARS = 128
MAX_EVIDENCE_REASON_CHARS = 128

_EVIDENCE_KEYS = frozenset(
    {"version", "text", "query", "revision", "reason", "records", "readers", "reader_prerequisites"}
)
_RECORD_KEYS = frozenset({"kind", "record_id", "text", "refs"})
_PREREQUISITE_KEYS = frozenset({"recipient_principals", "current_members", "now_ms", "is_direct"})
_RECORD_KINDS = frozenset({"statement", "fact"})


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
    recipient_principals: tuple[str, ...]
    current_members: tuple[str, ...]
    now_ms: int
    is_direct: bool
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
        recipient_principals: tuple[str, ...] = (),
        current_members: tuple[str, ...] = (),
        now_ms: int = 0,
        is_direct: bool = False,
    ) -> None:
        snapshot = facts or FactRetrievalResult()
        object.__setattr__(self, "text", str(text))
        object.__setattr__(self, "statements", statements)
        object.__setattr__(self, "revision", str(revision))
        object.__setattr__(self, "reason", str(reason))
        object.__setattr__(
            self,
            "recipient_principals",
            tuple(sorted(str(item) for item in recipient_principals)),
        )
        object.__setattr__(
            self, "current_members", tuple(sorted(str(item) for item in current_members))
        )
        object.__setattr__(self, "now_ms", int(now_ms))
        object.__setattr__(self, "is_direct", bool(is_direct))
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
            max_chars=max(
                0,
                MAX_TEXT_CHARS - len("\n".join(statement_values)) - (1 if statement_values else 0),
            ),
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
            **_reader_prerequisites(read_context, fact_context),
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
            # An empty or inconsistent reader bundle is a refusal, not a no-hit: the
            # caller must be able to tell "nobody may read" from "nothing matched".
            return _empty("denied")
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

        The early return is for a genuinely empty selection only. A selection that
        carries records must always be re-read, even if its rendered text is empty:
        otherwise suppressing the text would silently skip the store read and report
        "unchanged" for a draft that claims it used knowledge.
        """
        if not selection.text and not selection.records:
            return selection
        ordered = _ordered_readers(readers)
        if not ordered:
            raise ParticipationKnowledgeInvalidatedError("no trusted readers remain")
        if tuple(reader_key(r.read_context, r.fact_context) for r in ordered) != tuple(
            selection.reader_keys
        ):
            raise ParticipationKnowledgeInvalidatedError("trigger author set changed")
        revalidated: list[ParticipationKnowledgeSelection] = []
        try:
            for reader in ordered:
                group_wide = not reader.read_context.is_direct
                statements = self._knowledge.revalidate(
                    selection.statements,
                    context=reader.read_context,
                    max_chars=MAX_TEXT_CHARS,
                    group_wide=group_wide,
                )
                scoped_fact_context = replace(reader.fact_context, group_wide=group_wide)
                facts = (
                    self._memory.revalidate_for_context(
                        selection.facts,
                        read_context=scoped_fact_context,
                        max_chars=max(
                            0,
                            MAX_TEXT_CHARS - len(statements.text) - (1 if statements.text else 0),
                        ),
                    )
                    if selection.facts.used_source_refs
                    else FactRetrievalResult()
                )
                revalidated.append(
                    self._combine(
                        statements,
                        facts,
                        reader.read_context,
                        reader.fact_context,
                        query=selection.query,
                    )
                )
        except Exception as exc:
            raise ParticipationKnowledgeInvalidatedError("revalidation unavailable") from exc

        current = (
            revalidated[0] if len(revalidated) == 1 else _combine_records(revalidated, ordered)
        )
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
            **_reader_prerequisites(read_context, fact_context),
        )


# -- durable evidence ------------------------------------------------------------------
#
# A delayed owner approval has to survive a process restart. The admission therefore
# carries this bounded, versioned snapshot of the exact knowledge it was influenced by,
# so a later revalidation can look for the same records again. The mapping is JSON-native,
# deterministic and private: it never contains a native message, a transcript or model
# output, and it is validated on the way out as strictly as on the way in.


class ParticipationKnowledgeEvidenceError(RuntimeError):
    """A persisted knowledge-evidence snapshot is missing, malformed or unsupported."""


def selection_to_mapping(selection: ParticipationKnowledgeSelection) -> dict[str, Any]:
    """Encode one selection as bounded, versioned, JSON-native evidence."""
    mapping: dict[str, Any] = {
        "version": KNOWLEDGE_EVIDENCE_VERSION,
        "text": str(selection.text),
        "query": str(selection.query),
        "revision": str(selection.revision),
        "reason": str(selection.reason),
        "records": [
            {
                "kind": str(record.kind),
                "record_id": str(record.record_id),
                "text": str(record.text),
                "refs": [[str(event_id), int(revision)] for event_id, revision in record.refs],
            }
            for record in selection.records
        ],
        "readers": [list(key) for key in selection.reader_keys],
        "reader_prerequisites": {
            "recipient_principals": list(selection.recipient_principals),
            "current_members": list(selection.current_members),
            "now_ms": int(selection.now_ms),
            "is_direct": bool(selection.is_direct),
        },
    }
    # Fail closed: evidence this codec cannot read back is never persisted.
    selection_from_mapping(mapping)
    return mapping


def selection_from_mapping(value: Mapping[str, Any]) -> ParticipationKnowledgeSelection:
    """Decode one evidence snapshot, validating every field explicitly."""
    if not isinstance(value, Mapping):
        raise ParticipationKnowledgeEvidenceError("knowledge evidence must be an object")
    version = value.get("version")
    if isinstance(version, bool) or not isinstance(version, int):
        raise ParticipationKnowledgeEvidenceError("knowledge evidence version is not an integer")
    if version != KNOWLEDGE_EVIDENCE_VERSION:
        raise ParticipationKnowledgeEvidenceError(
            f"unsupported knowledge evidence version: {version}"
        )
    _require_exact_keys(value, _EVIDENCE_KEYS, "knowledge evidence")
    text = _evidence_text(value["text"], "text", max_chars=MAX_TEXT_CHARS)
    query = _evidence_text(value["query"], "query", max_chars=MAX_QUERY_CHARS)
    revision = _evidence_text(value["revision"], "revision", max_chars=MAX_EVIDENCE_REVISION_CHARS)
    reason = _evidence_text(value["reason"], "reason", max_chars=MAX_EVIDENCE_REASON_CHARS)
    records = _evidence_records(value["records"])
    readers = _evidence_readers(value["readers"])
    recipients, members, now_ms, is_direct = _evidence_prerequisites(value["reader_prerequisites"])
    _validate_evidence_consistency(records, readers, recipients, members)

    statement_records = tuple(record for record in records if record.kind == "statement")
    fact_records = tuple(record for record in records if record.kind == "fact")
    statements = KnowledgeContext(
        text="\n".join(record.text for record in statement_records),
        statement_ids=tuple(record.record_id for record in statement_records),
        entry_texts=tuple(record.text for record in statement_records),
    )
    facts = FactRetrievalResult(
        text="\n".join(record.text for record in fact_records),
        used_source_refs={record.record_id: record.refs for record in fact_records},
    )
    return ParticipationKnowledgeSelection(
        text=text,
        statements=statements,
        facts=facts,
        revision=revision,
        reason=reason,
        records=records,
        reader_keys=readers,
        query=query,
        recipient_principals=recipients,
        current_members=members,
        now_ms=now_ms,
        is_direct=is_direct,
    )


def _require_exact_keys(value: Mapping[str, Any], keys: frozenset[str], label: str) -> None:
    present = set(value)
    missing = sorted(keys - present)
    if missing:
        raise ParticipationKnowledgeEvidenceError(f"{label} is missing keys: {', '.join(missing)}")
    unknown = sorted(str(key) for key in present - keys)
    if unknown:
        raise ParticipationKnowledgeEvidenceError(f"{label} has unknown keys: {', '.join(unknown)}")


def _evidence_text(value: Any, name: str, *, max_chars: int) -> str:
    if not isinstance(value, str):
        raise ParticipationKnowledgeEvidenceError(f"{name} must be a string")
    if len(value) > max_chars:
        raise ParticipationKnowledgeEvidenceError(f"{name} exceeds {max_chars} characters")
    return value


def _evidence_records(value: Any) -> tuple[ParticipationKnowledgeRecord, ...]:
    if not isinstance(value, (list, tuple)):
        raise ParticipationKnowledgeEvidenceError("records must be a list")
    if len(value) > MAX_ENTRIES:
        raise ParticipationKnowledgeEvidenceError(f"records exceed {MAX_ENTRIES} entries")
    records: list[ParticipationKnowledgeRecord] = []
    for item in value:
        if not isinstance(item, Mapping):
            raise ParticipationKnowledgeEvidenceError("a record must be an object")
        _require_exact_keys(item, _RECORD_KEYS, "record")
        kind = item["kind"]
        if kind not in _RECORD_KINDS:
            raise ParticipationKnowledgeEvidenceError("record kind is neither statement nor fact")
        record_id = item["record_id"]
        if not isinstance(record_id, str) or not record_id.strip():
            raise ParticipationKnowledgeEvidenceError("a record id is empty")
        text = _evidence_text(item["text"], "record text", max_chars=MAX_TEXT_CHARS)
        if not text.strip():
            raise ParticipationKnowledgeEvidenceError("a record text is empty")
        records.append(
            ParticipationKnowledgeRecord(
                kind=kind, record_id=record_id, text=text, refs=_evidence_refs(item["refs"])
            )
        )
    return tuple(records)


def _evidence_refs(value: Any) -> tuple[tuple[str, int], ...]:
    if not isinstance(value, (list, tuple)):
        raise ParticipationKnowledgeEvidenceError("record refs must be a list")
    refs: list[tuple[str, int]] = []
    for item in value:
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            raise ParticipationKnowledgeEvidenceError("a record ref must be an event/revision pair")
        event_id, revision = item
        if not isinstance(event_id, str) or not event_id.strip():
            raise ParticipationKnowledgeEvidenceError("a record ref event id is empty")
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
            raise ParticipationKnowledgeEvidenceError("a record ref revision is not a positive int")
        refs.append((event_id, revision))
    return tuple(refs)


def _evidence_readers(value: Any) -> tuple[tuple[str, str, str, str, str, str, int], ...]:
    if not isinstance(value, (list, tuple)):
        raise ParticipationKnowledgeEvidenceError("readers must be a list")
    if len(value) > MAX_EVIDENCE_READERS:
        raise ParticipationKnowledgeEvidenceError(
            f"readers exceed {MAX_EVIDENCE_READERS} identities"
        )
    readers: list[tuple[str, str, str, str, str, str, int]] = []
    for item in value:
        if not isinstance(item, (list, tuple)) or len(item) != 7:
            raise ParticipationKnowledgeEvidenceError("a reader identity must have 7 members")
        principal, channel, chat_id, membership, scope, purpose, policy_revision = item
        for label, member in (
            ("principal", principal),
            ("channel", channel),
            ("chat id", chat_id),
            ("chat scope key", scope),
            ("purpose", purpose),
        ):
            if not isinstance(member, str) or not member.strip():
                raise ParticipationKnowledgeEvidenceError(f"a reader {label} is missing")
        if not isinstance(membership, str):
            raise ParticipationKnowledgeEvidenceError(
                "a reader membership revision is not a string"
            )
        if (
            isinstance(policy_revision, bool)
            or not isinstance(policy_revision, int)
            or policy_revision < 0
        ):
            raise ParticipationKnowledgeEvidenceError("a reader policy revision is invalid")
        readers.append((principal, channel, chat_id, membership, scope, purpose, policy_revision))
    return tuple(readers)


def _evidence_prerequisites(
    value: Any,
) -> tuple[tuple[str, ...], tuple[str, ...], int, bool]:
    if not isinstance(value, Mapping):
        raise ParticipationKnowledgeEvidenceError("reader_prerequisites must be an object")
    _require_exact_keys(value, _PREREQUISITE_KEYS, "reader_prerequisites")
    recipients = _evidence_principals(value["recipient_principals"], "recipient_principals")
    members = _evidence_principals(value["current_members"], "current_members")
    now_ms = value["now_ms"]
    if isinstance(now_ms, bool) or not isinstance(now_ms, int) or now_ms < 0:
        raise ParticipationKnowledgeEvidenceError("reader_prerequisites now_ms is invalid")
    is_direct = value["is_direct"]
    if not isinstance(is_direct, bool):
        raise ParticipationKnowledgeEvidenceError("reader_prerequisites is_direct is not a bool")
    return recipients, members, now_ms, is_direct


def _evidence_principals(value: Any, name: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise ParticipationKnowledgeEvidenceError(f"{name} must be a list")
    if len(value) > MAX_EVIDENCE_MEMBERS:
        raise ParticipationKnowledgeEvidenceError(f"{name} exceeds {MAX_EVIDENCE_MEMBERS} members")
    principals: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ParticipationKnowledgeEvidenceError(f"{name} contains an invalid principal")
        principals.append(item)
    return tuple(sorted(set(principals)))


def _validate_evidence_consistency(
    records: tuple[ParticipationKnowledgeRecord, ...],
    readers: tuple[tuple[str, str, str, str, str, str, int], ...],
    recipients: tuple[str, ...],
    members: tuple[str, ...],
) -> None:
    """A real selection always names its readers and one shared audience."""
    if bool(readers) != bool(recipients):
        raise ParticipationKnowledgeEvidenceError(
            "reader prerequisites do not match the recorded readers"
        )
    if recipients != members:
        raise ParticipationKnowledgeEvidenceError("reader recipient and member sets disagree")
    if records and not readers:
        raise ParticipationKnowledgeEvidenceError("evidence records have no reader identity")


def _reader_prerequisites(
    read_context: TrustedReadContext, fact_context: FactReadContext
) -> dict[str, Any]:
    """The reader inputs a revalidation needs and a reader key does not carry."""
    return {
        "recipient_principals": tuple(
            sorted(str(item) for item in (read_context.recipient_principals or ()))
        ),
        "current_members": tuple(
            sorted(str(item) for item in (fact_context.current_members or ()))
        ),
        "now_ms": int(read_context.now_ms),
        "is_direct": bool(read_context.is_direct),
    }


def _records_with_refs(
    statements: KnowledgeContext,
) -> list[tuple[str, str, tuple[tuple[str, int], ...]]]:
    """Pair each rendered statement with refs explicitly attached by protected recall."""
    values = [line.strip() for line in _statement_lines(statements)]
    ids = tuple(statements.statement_ids[: len(values)])
    if len(ids) != len(values):
        return []
    refs_by_id = {
        str(statement_id): _sorted_refs([(str(ref.event_id), int(ref.revision)) for ref in refs])
        for statement_id, refs in statements.source_refs_by_statement
    }
    if any(not refs_by_id.get(str(statement_id)) for statement_id in ids):
        return []
    return [
        (str(statement_id), value, refs_by_id[str(statement_id)])
        for statement_id, value in zip(ids, values, strict=True)
    ]


def _selection_records(
    statements: KnowledgeContext, facts: FactRetrievalResult
) -> tuple[ParticipationKnowledgeRecord, ...]:
    """Freeze each selected entry as identity, rendered text and source revisions."""
    records: list[ParticipationKnowledgeRecord] = [
        ParticipationKnowledgeRecord(
            kind="statement",
            record_id=statement_id,
            text=value,
            refs=refs,
        )
        for statement_id, value, refs in _records_with_refs(statements)
    ]
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
    # Keep the actual protected source metadata, including each statement's association.
    source_map = dict(selections[0].statements.source_refs_by_statement)
    statement_source_refs = tuple(
        (
            record.record_id,
            tuple(
                ref
                for ref in source_map.get(record.record_id, ())
                if (str(ref.event_id), int(ref.revision)) in set(record.refs)
            ),
        )
        for record in statement_records
    )
    source_refs_list: list[SourceRef] = []
    for _statement_id, refs in statement_source_refs:
        for ref in refs:
            if ref not in source_refs_list:
                source_refs_list.append(ref)
    statements = KnowledgeContext(
        text="\n".join(record.text for record in statement_records),
        statement_ids=tuple(record.record_id for record in statement_records),
        source_refs=tuple(source_refs_list),
        source_refs_by_statement=statement_source_refs,
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
        **_reader_prerequisites(readers[0].read_context, readers[0].fact_context),
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
