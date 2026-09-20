"""Public, immutable contracts of the person-knowledge module.

Everything in this module is part of the public boundary: runtime consumers import
``yeoman_gateway.knowledge.api`` and ``yeoman_gateway.knowledge.models`` only.  They
never see tables, connections or scope keys.

Design rules encoded here (see the private design spec):

* A person id is not a security principal.  ``PersonResolution`` carries a person
  identity and never owner authority.
* Speaker, reported speaker, subject, participant and mention are *roles at a
  statement*.  There is no single ``about_person_id``.
* Audience and read permission are never derived from person links.
* ``Trusted*`` types are built by trusted adapters (policy, channel, archive) and are
  re-validated inside the service; the type name is not a security proof.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Final

__all__ = [
    "CHANGE_STATUSES",
    "CONVERSATION_ORIGINS",
    "CONVERSATION_RELATIONS",
    "CONVERSATION_STATUSES",
    "ERROR_CODES",
    "MAX_NAME_LENGTH",
    "MAX_PEOPLE_PER_STATEMENT",
    "MAX_SOURCES_PER_STATEMENT",
    "PERSON_ROLES",
    "READ_PURPOSES",
    "STATEMENT_ATTRIBUTIONS",
    "STATEMENT_STATUSES",
    "CaptureJobReceipt",
    "CaptureResult",
    "ChangeReceipt",
    "ConversationMembership",
    "ConversationMembershipReceipt",
    "ConversationMergeReceipt",
    "ConversationReferenceReceipt",
    "ConversationRelation",
    "ConversationSplitReceipt",
    "ConversationView",
    "EndpointResolution",
    "Identifier",
    "KnowledgeContext",
    "KnowledgeError",
    "KnowledgeStats",
    "MaintenanceReport",
    "PersonLinkCandidate",
    "PersonProfile",
    "PersonResolution",
    "RecallQuery",
    "SourceRef",
    "StatementCandidate",
    "StatementPage",
    "StatementSummary",
    "TrustedAdminContext",
    "TrustedCaptureContext",
    "TrustedIdentityObservation",
    "TrustedReadContext",
    "ValidationError",
]

# ── enumerations kept as plain string tuples (validated, not enforced by typing) ──

PERSON_ROLES: Final[tuple[str, ...]] = (
    "speaker",
    "reported_speaker",
    "subject",
    "participant",
    "mentioned",
)

STATEMENT_ATTRIBUTIONS: Final[tuple[str, ...]] = (
    "transport",
    "explicit",
    "extracted",
    "confirmed",
)

STATEMENT_STATUSES: Final[tuple[str, ...]] = (
    "assertion",
    "confirmed",
    "superseded",
    "revoked",
    "expired",
)

CHANGE_STATUSES: Final[tuple[str, ...]] = (
    "resolved",
    "unresolved",
    "ambiguous",
    "conflict",
    "denied",
)

READ_PURPOSES: Final[tuple[str, ...]] = ("reply", "proactive", "profile", "admin")

#: How one source revision came to belong to a conversation thread.  A thread is a
#: subject-matter statement, so membership origin stays explicit and auditable.
CONVERSATION_ORIGINS: Final[tuple[str, ...]] = (
    "explicit_reply",
    "explicit_quote",
    "manual",
    "split",
    "merge",
)

#: The complete relation vocabulary.  Deliberately tiny: an explicit reply link is a
#: relation candidate, never a proof of identical topic.
CONVERSATION_RELATIONS: Final[tuple[str, ...]] = (
    "branches_from",
    "merged_into",
    "related_to",
)

CONVERSATION_STATUSES: Final[tuple[str, ...]] = ("open", "closed", "merged")

ERROR_CODES: Final[tuple[str, ...]] = (
    "invalid_input",
    "unresolved",
    "ambiguous",
    "identity_conflict",
    "unauthorized",
    "stale_revision",
    "dependent_merge",
    "denied_unknown_basis",
    "source_revoked",
    "storage_unavailable",
    "migration_required",
    "schema_incompatible",
)

NAME_SOURCES: Final[tuple[str, ...]] = ("owner_confirmed", "self_reported", "observed")

MAX_NAME_LENGTH: Final[int] = 200
MAX_PEOPLE_PER_STATEMENT: Final[int] = 64
MAX_SOURCES_PER_STATEMENT: Final[int] = 32
MIN_RECALL_LIMIT: Final[int] = 1
MAX_RECALL_LIMIT: Final[int] = 50
MAX_PAGE_LIMIT: Final[int] = 100

_IDENTIFIER_KINDS: Final[frozenset[str]] = frozenset(
    {
        "phone_jid",
        "lid",
        "telegram_id",
        "telegram_username",
        "signal_id",
        "discord_id",
        "slack_id",
        "email",
        "handle",
    }
)

_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class KnowledgeError(Exception):
    """A domain error with a stable reason code from :data:`ERROR_CODES`."""

    def __init__(self, code: str, message: str = "") -> None:
        if code not in ERROR_CODES:
            raise ValueError(f"unknown knowledge error code: {code!r}")
        super().__init__(message or code)
        self.code = code
        self.message = message or code

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"{self.code}: {self.message}"


class ValidationError(KnowledgeError):
    """Invalid caller input.  Never silently truncated."""

    def __init__(self, message: str = "") -> None:
        super().__init__("invalid_input", message)


# ── validation helpers ───────────────────────────────────────────────────────


def _require_id(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{field_name} must be a non-empty string")
    text = value.strip()
    if len(text) > 512:
        raise ValidationError(f"{field_name} is too long")
    if _CONTROL_CHARS.search(text):
        raise ValidationError(f"{field_name} contains control characters")
    return text


def _optional_id(value: object, field_name: str) -> str | None:
    if value is None:
        return None
    return _require_id(value, field_name)


def validate_name(value: object, field_name: str = "name") -> str:
    """Names are unicode text without control characters, at most 200 characters."""
    if not isinstance(value, str):
        raise ValidationError(f"{field_name} must be a string")
    text = value.strip()
    if not text:
        raise ValidationError(f"{field_name} must not be empty")
    if _CONTROL_CHARS.search(text):
        raise ValidationError(f"{field_name} contains control characters")
    if len(text) > MAX_NAME_LENGTH:
        raise ValidationError(f"{field_name} exceeds {MAX_NAME_LENGTH} characters")
    return text


def _require_choice(value: object, allowed: tuple[str, ...], field_name: str) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise ValidationError(f"{field_name} must be one of {allowed}")
    return value


def _require_int(value: object, field_name: str, *, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError(f"{field_name} must be an integer")
    if minimum is not None and value < minimum:
        raise ValidationError(f"{field_name} must be >= {minimum}")
    return int(value)


def _require_confidence(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError("confidence must be a number")
    number = float(value)
    if not math.isfinite(number) or not 0.0 <= number <= 1.0:
        raise ValidationError("confidence must be finite and within [0, 1]")
    return number


def normalize_identifier_value(kind: str, value: str) -> str:
    """Normalize only *known variants of the same identifier type*.

    Nothing here guesses a channel or a kind from a bare number: the caller has to
    name both.  WhatsApp JIDs are lower-cased and the device suffix is dropped;
    phone-like tokens keep their digits; everything else is only trimmed.
    """
    text = value.strip()
    if not text:
        raise ValidationError("identifier value must not be empty")
    if _CONTROL_CHARS.search(text) or any(char.isspace() for char in text):
        raise ValidationError("identifier value contains whitespace or control characters")
    if kind in ("phone_jid", "lid"):
        lowered = text.lower()
        local, _, domain = lowered.partition("@")
        local = local.split(":", 1)[0]
        if domain:
            if domain != domain.strip("@"):
                raise ValidationError("identifier value has a malformed domain")
            return f"{local}@{domain}"
        return local
    if kind == "email":
        if text.count("@") != 1:
            raise ValidationError("identifier value is not an email address")
        return text.lower()
    if kind in ("telegram_id", "signal_id", "discord_id", "slack_id"):
        return text.lstrip("@").strip()
    if kind == "telegram_username":
        return text.lstrip("@").strip().lower()
    if kind == "email":
        return text.lower()
    return text


# ── identifier and trusted observations ──────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Identifier:
    """One platform identifier: channel plus kind plus normalized value."""

    channel: str
    kind: str
    value: str

    def __post_init__(self) -> None:
        channel = _require_id(self.channel, "channel").lower()
        kind = _require_id(self.kind, "kind").lower()
        if kind not in _IDENTIFIER_KINDS:
            raise ValidationError(f"unsupported identifier kind: {kind!r}")
        value = normalize_identifier_value(kind, _require_id(self.value, "identifier value"))
        object.__setattr__(self, "channel", channel)
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "value", value)

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.channel, self.kind, self.value)


@dataclass(frozen=True, slots=True)
class TrustedIdentityObservation:
    """A verified platform observation, issued by a channel/bridge adapter.

    ``mapping_verified`` means the adapter proved that all given identifiers belong to
    the same platform account (for example WhatsApp PN<->LID evidence).  It never
    means the observation was authorized to create owner rights.
    """

    identifiers: tuple[Identifier, ...]
    evidence_ref: str
    observed_name: str | None = None
    observed_at_ms: int = 0
    mapping_verified: bool = False
    channel_hint: str | None = None

    def __post_init__(self) -> None:
        identifiers = tuple(self.identifiers)
        if not identifiers:
            raise ValidationError("observation needs at least one identifier")
        if len(identifiers) > 8:
            raise ValidationError("observation has too many identifiers")
        if len({item.key for item in identifiers}) != len(identifiers):
            raise ValidationError("observation contains duplicate identifiers")
        object.__setattr__(self, "identifiers", identifiers)
        object.__setattr__(self, "evidence_ref", _require_id(self.evidence_ref, "evidence_ref"))
        if self.observed_name is not None:
            object.__setattr__(
                self, "observed_name", validate_name(self.observed_name, "observed_name")
            )
        object.__setattr__(
            self, "observed_at_ms", _require_int(self.observed_at_ms, "observed_at_ms", minimum=0)
        )


# ── resolution results ───────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class PersonResolution:
    """Outcome of resolving an observation to a person.  Carries no owner authority."""

    status: str
    person_id: str | None
    display_name: str | None
    identity_revision: int
    reason: str

    def __post_init__(self) -> None:
        _require_choice(self.status, CHANGE_STATUSES, "status")
        if self.person_id is not None:
            object.__setattr__(self, "person_id", _require_id(self.person_id, "person_id"))
        if self.display_name is not None:
            object.__setattr__(
                self, "display_name", validate_name(self.display_name, "display_name")
            )
        object.__setattr__(
            self,
            "identity_revision",
            _require_int(self.identity_revision, "identity_revision", minimum=0),
        )
        if not isinstance(self.reason, str):
            raise ValidationError("reason must be a string")

    @property
    def resolved(self) -> bool:
        return self.status == "resolved" and self.person_id is not None


@dataclass(frozen=True, slots=True)
class EndpointResolution:
    """One proven, permitted delivery endpoint of a person."""

    status: str
    person_id: str
    identifier: Identifier | None
    identity_revision: int
    reason: str

    def __post_init__(self) -> None:
        _require_choice(self.status, CHANGE_STATUSES, "status")
        object.__setattr__(self, "person_id", _require_id(self.person_id, "person_id"))
        object.__setattr__(
            self,
            "identity_revision",
            _require_int(self.identity_revision, "identity_revision", minimum=0),
        )
        if not isinstance(self.reason, str):
            raise ValidationError("reason must be a string")


# ── sources and statements ───────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class SourceRef:
    """A proven origin of a statement: archive event, revision and author principal."""

    event_id: str
    revision: int
    channel: str
    chat_id: str
    author_principal: str
    occurred_at_ms: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "event_id", _require_id(self.event_id, "event_id"))
        object.__setattr__(self, "revision", _require_int(self.revision, "revision", minimum=1))
        object.__setattr__(self, "channel", _require_id(self.channel, "channel").lower())
        object.__setattr__(self, "chat_id", _require_id(self.chat_id, "chat_id"))
        object.__setattr__(
            self,
            "author_principal",
            _require_id(self.author_principal, "author_principal"),
        )
        object.__setattr__(
            self,
            "occurred_at_ms",
            _require_int(self.occurred_at_ms, "occurred_at_ms", minimum=0),
        )

    @property
    def key(self) -> tuple[str, int]:
        return (self.event_id, self.revision)


@dataclass(frozen=True, slots=True)
class PersonLinkCandidate:
    """One person in one role at one statement, bound to one evidence source."""

    person_id: str
    role: str
    source: SourceRef
    attribution: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "person_id", _require_id(self.person_id, "person_id"))
        _require_choice(self.role, PERSON_ROLES, "role")
        _require_choice(self.attribution, STATEMENT_ATTRIBUTIONS, "attribution")
        if not isinstance(self.source, SourceRef):
            raise ValidationError("link source must be a SourceRef")


@dataclass(frozen=True, slots=True)
class StatementCandidate:
    """An untrusted proposal for a statement.

    ``people`` entries carry a ``person_id``; the runtime only ever hands the extractor
    person ids it offered for this source, and the service rejects anything else.  The
    audience is deliberately *not* part of this type: it comes from the archived
    evidence, never from a model.
    """

    content: str
    sources: tuple[SourceRef, ...]
    people: tuple[PersonLinkCandidate, ...] = ()
    extractor_version: str = "unknown"
    confidence: float = 0.0
    valid_until_ms: int | None = None
    unresolved_mentions: tuple[str, ...] = ()
    kind: str = "fact"
    sector: str = "semantic"

    def __post_init__(self) -> None:
        if not isinstance(self.content, str):
            raise ValidationError("content must be a string")
        content = self.content.strip()
        if not content:
            raise ValidationError("content must not be empty")
        if _CONTROL_CHARS.search(content):
            raise ValidationError("content contains control characters")
        object.__setattr__(self, "content", content)

        sources = tuple(self.sources)
        if not sources:
            raise ValidationError("a statement needs at least one source")
        if len(sources) > MAX_SOURCES_PER_STATEMENT:
            raise ValidationError(f"at most {MAX_SOURCES_PER_STATEMENT} sources per statement")
        if len({item.key for item in sources}) != len(sources):
            raise ValidationError("statement sources must be unique")
        object.__setattr__(self, "sources", sources)

        people = tuple(self.people)
        if len(people) > MAX_PEOPLE_PER_STATEMENT:
            raise ValidationError(f"at most {MAX_PEOPLE_PER_STATEMENT} person links per statement")
        if len({(item.person_id, item.role, item.source.key) for item in people}) != len(people):
            raise ValidationError("duplicate person links")
        object.__setattr__(self, "people", people)

        object.__setattr__(
            self, "extractor_version", _require_id(self.extractor_version, "extractor_version")
        )
        object.__setattr__(self, "confidence", _require_confidence(self.confidence))
        if self.valid_until_ms is not None:
            object.__setattr__(
                self,
                "valid_until_ms",
                _require_int(self.valid_until_ms, "valid_until_ms", minimum=0),
            )
        mentions = tuple(str(item) for item in self.unresolved_mentions)
        if len(mentions) > MAX_PEOPLE_PER_STATEMENT:
            raise ValidationError("too many unresolved mentions")
        for mention in mentions:
            validate_name(mention, "unresolved mention")
        object.__setattr__(self, "unresolved_mentions", mentions)
        object.__setattr__(self, "kind", _require_id(self.kind, "kind"))
        object.__setattr__(self, "sector", _require_id(self.sector, "sector"))


@dataclass(frozen=True, slots=True)
class TrustedCaptureContext:
    """Trusted facts about a capture request, assembled by the runtime.

    ``authorized_sources`` are the archive revisions the runtime may read for this
    request; the archived evidence itself carries the audience.
    """

    request_id: str
    policy_revision: int
    capture_basis: str
    authorized_sources: tuple[SourceRef, ...]
    actor_principal: str = ""
    authorized: bool = True
    #: An administrative capture is authorized by the actor's owner authority instead of
    #: by a channel-issued capture receipt.
    admin_initiated: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "request_id", _require_id(self.request_id, "request_id"))
        object.__setattr__(
            self,
            "policy_revision",
            _require_int(self.policy_revision, "policy_revision", minimum=0),
        )
        object.__setattr__(
            self, "capture_basis", _require_id(self.capture_basis, "capture_basis")
        )
        sources = tuple(self.authorized_sources)
        if len(sources) > MAX_SOURCES_PER_STATEMENT:
            raise ValidationError("too many authorized sources")
        object.__setattr__(self, "authorized_sources", sources)
        if self.actor_principal:
            object.__setattr__(
                self,
                "actor_principal",
                _require_id(self.actor_principal, "actor_principal"),
            )


@dataclass(frozen=True, slots=True)
class TrustedReadContext:
    """Trusted facts about a read request: who reads for whom, and where."""

    principal_id: str
    channel: str
    chat_id: str
    recipient_principals: frozenset[str] | None
    membership_revision: str | None
    policy_revision: int
    purpose: str
    now_ms: int
    is_direct: bool = False
    owner: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "principal_id", _require_id(self.principal_id, "principal_id")
        )
        object.__setattr__(self, "channel", _require_id(self.channel, "channel").lower())
        object.__setattr__(self, "chat_id", _require_id(self.chat_id, "chat_id"))
        if self.recipient_principals is not None:
            recipients = frozenset(
                _require_id(item, "recipient_principal") for item in self.recipient_principals
            )
            object.__setattr__(self, "recipient_principals", recipients)
        object.__setattr__(
            self,
            "policy_revision",
            _require_int(self.policy_revision, "policy_revision", minimum=0),
        )
        _require_choice(self.purpose, READ_PURPOSES, "purpose")
        object.__setattr__(self, "now_ms", _require_int(self.now_ms, "now_ms", minimum=0))
        if self.membership_revision is not None:
            object.__setattr__(
                self,
                "membership_revision",
                self.membership_revision,
            )

    @property
    def membership_known(self) -> bool:
        return self.recipient_principals is not None

    def scope_key(self) -> str:
        return f"channel:{self.channel}:chat:{self.chat_id}"


@dataclass(frozen=True, slots=True)
class TrustedAdminContext:
    """Trusted admin authorization.  Re-checked against real admin rights."""

    actor_principal: str
    policy_revision: int
    authorization_ref: str
    owner: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "actor_principal", _require_id(self.actor_principal, "actor_principal")
        )
        object.__setattr__(
            self,
            "policy_revision",
            _require_int(self.policy_revision, "policy_revision", minimum=0),
        )
        object.__setattr__(
            self,
            "authorization_ref",
            _require_id(self.authorization_ref, "authorization_ref"),
        )


# ── read results ─────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class RecallQuery:
    """A read request.  Bounds are enforced, never silently truncated."""

    text: str = ""
    person_ids: tuple[str, ...] = ()
    roles: tuple[str, ...] = ()
    limit: int = 10

    def __post_init__(self) -> None:
        if not isinstance(self.text, str):
            raise ValidationError("query text must be a string")
        if _CONTROL_CHARS.search(self.text):
            raise ValidationError("query text contains control characters")
        person_ids = tuple(_require_id(item, "person_id") for item in self.person_ids)
        if len(set(person_ids)) != len(person_ids):
            raise ValidationError("duplicate person ids in query")
        object.__setattr__(self, "person_ids", person_ids)
        roles = tuple(_require_choice(item, PERSON_ROLES, "role") for item in self.roles)
        object.__setattr__(self, "roles", roles)
        limit = _require_int(self.limit, "limit", minimum=MIN_RECALL_LIMIT)
        if limit > MAX_RECALL_LIMIT:
            raise ValidationError(f"limit must be <= {MAX_RECALL_LIMIT}")
        object.__setattr__(self, "limit", limit)


@dataclass(frozen=True, slots=True)
class KnowledgeContext:
    """Prompt-ready knowledge.  Only ``text`` may reach a model."""

    text: str = ""
    statement_ids: tuple[str, ...] = ()
    source_refs: tuple[SourceRef, ...] = ()
    identity_revision: int = 0
    acl_epoch: int = 0
    context_revision: str = ""
    reason: str = "ok"
    denied_count: int = 0

    @property
    def empty(self) -> bool:
        return not self.statement_ids


@dataclass(frozen=True, slots=True)
class PersonProfile:
    """A person plus the knowledge context a reader is allowed to see."""

    person: PersonResolution
    context: KnowledgeContext


# ── conversation threads ─────────────────────────────────────────────────────


def _require_offsets(value: object) -> tuple[int, int] | None:
    """Validate an optional ``(start, end)`` character span inside the source text."""
    if value is None:
        return None
    try:
        start, end = value  # type: ignore[misc]
    except (TypeError, ValueError):
        raise ValidationError("text offsets must be a (start, end) pair") from None
    start = _require_int(start, "text_start", minimum=0)
    end = _require_int(end, "text_end", minimum=0)
    if end < start:
        raise ValidationError("text offsets must satisfy end >= start")
    return (start, end)


@dataclass(frozen=True, slots=True)
class ConversationMembership:
    """One source revision inside one conversation.  Never carries the source text."""

    conversation_id: str
    source: SourceRef
    origin: str
    confidence: float
    classifier_version: str
    text_offsets: tuple[int, int] | None = None
    created_ms: int = 0

    def __post_init__(self) -> None:
        _require_choice(self.origin, CONVERSATION_ORIGINS, "origin")
        object.__setattr__(self, "confidence", _require_confidence(self.confidence))
        object.__setattr__(self, "text_offsets", _require_offsets(self.text_offsets))


@dataclass(frozen=True, slots=True)
class ConversationRelation:
    """A typed edge between two conversations.  Old ids always stay resolvable."""

    relation: str
    from_conversation_id: str
    to_conversation_id: str
    origin: str
    confidence: float
    classifier_version: str
    created_ms: int = 0

    def __post_init__(self) -> None:
        _require_choice(self.relation, CONVERSATION_RELATIONS, "relation")
        _require_choice(self.origin, CONVERSATION_ORIGINS, "origin")
        object.__setattr__(self, "confidence", _require_confidence(self.confidence))


@dataclass(frozen=True, slots=True)
class ConversationView:
    """A read projection derived from relational rows, gated by the Phase-1 read gate."""

    conversation_id: str
    workspace_id: str = ""
    scope_key: str = ""
    status: str = "open"
    merged_into: str | None = None
    redirected_from: str | None = None
    memberships: tuple[ConversationMembership, ...] = ()
    relations: tuple[ConversationRelation, ...] = ()
    withheld_memberships: int = 0
    reason: str = "ok"

    @property
    def empty(self) -> bool:
        return not self.memberships


@dataclass(frozen=True, slots=True)
class ConversationMembershipReceipt:
    """Outcome of attaching one source revision to a conversation."""

    conversation_id: str
    created_conversation: bool
    new_memberships: int
    origin: str
    confidence: float = 1.0
    classifier_version: str = ""
    text_offsets: tuple[int, int] | None = None


@dataclass(frozen=True, slots=True)
class ConversationReferenceReceipt:
    """Outcome of an explicit reply/quote link.  A relation candidate, not a merge."""

    conversation_id: str
    related_conversation_id: str
    relation_created: bool
    relations: tuple[ConversationRelation, ...] = ()


@dataclass(frozen=True, slots=True)
class ConversationSplitReceipt:
    """A new branch that leaves the prior conversation id and memberships intact."""

    conversation_id: str
    branched_from: str
    origin: str = "split"
    relations: tuple[ConversationRelation, ...] = ()


@dataclass(frozen=True, slots=True)
class ConversationMergeReceipt:
    """A merge that redirects the retired ids instead of rewriting history."""

    conversation_id: str
    merged_ids: tuple[str, ...] = ()
    relations: tuple[ConversationRelation, ...] = ()


@dataclass(frozen=True, slots=True)
class CaptureResult:
    """Outcome of a capture attempt: accepted statement ids and rejected reasons."""

    statement_ids: tuple[str, ...] = ()
    rejected: tuple[tuple[str, str], ...] = ()

    @property
    def ok(self) -> bool:
        return bool(self.statement_ids)


@dataclass(frozen=True, slots=True)
class ChangeReceipt:
    """Proof of a mutation, carrying the revisions it produced."""

    operation_id: str
    identity_revision: int
    acl_epoch: int
    changed_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class StatementSummary:
    """One statement row for admin inspection.  Content only for allowed admin output."""

    statement_id: str
    status: str
    content: str | None
    sources: tuple[SourceRef, ...] = ()
    people: tuple[PersonLinkCandidate, ...] = ()
    created_ms: int = 0
    updated_ms: int = 0
    valid_until_ms: int | None = None


@dataclass(frozen=True, slots=True)
class StatementPage:
    """A stable page of statements.  The cursor is bound to the caller's filter."""

    items: tuple[StatementSummary, ...] = ()
    next_cursor: str | None = None


@dataclass(frozen=True, slots=True)
class CaptureJobReceipt:
    """State of one persistent extraction job."""

    job_id: str
    state: str
    reason: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "job_id", _require_id(self.job_id, "job_id"))
        _require_choice(
            self.state,
            ("queued", "running", "done", "skipped", "cancelled", "failed"),
            "state",
        )


@dataclass(frozen=True, slots=True)
class MaintenanceReport:
    """Result of a maintenance run.  Counts and reason codes, never content."""

    examined: int = 0
    changed: int = 0
    denied: int = 0
    reason_counts: tuple[tuple[str, int], ...] = ()


@dataclass(frozen=True, slots=True)
class KnowledgeStats:
    """Redacted operational counters."""

    schema_version: int = 0
    people_count: int = 0
    statement_count: int = 0
    quarantined_count: int = 0
    pending_jobs: int = 0
    identity_revision: int = 0
    acl_epoch: int = 0
    state: str = "ready"
    reason: str = ""


@dataclass(frozen=True, slots=True)
class NameObservation:
    """Internal value object: one observed or confirmed name of a person."""

    person_id: str
    name: str
    source: str
    visibility: str = "public"
    first_seen_ms: int = 0
    last_seen_ms: int = 0
    observed_by: str = ""


@dataclass(frozen=True, slots=True)
class MergeRedirect:
    """One reversible merge redirect."""

    operation_id: str
    source_id: str
    target_id: str
    actor_principal: str
    authorization_ref: str
    created_ms: int
    active: bool = True
    undone_ms: int | None = None


@dataclass(frozen=True, slots=True)
class IdentifierBinding:
    """Internal value object for one identifier binding row."""

    person_id: str
    identifier: Identifier
    evidence_ref: str
    status: str = "active"
    mapping_verified: bool = False
    created_ms: int = 0
    updated_ms: int = 0


@dataclass(frozen=True, slots=True)
class StatementRecord:
    """Internal value object: the stored shell of a statement."""

    statement_id: str
    status: str
    content: str
    author_principal: str
    scope_key: str
    visibility_scope: str
    group_rule: str
    valid_from_ms: int
    extractor_version: str
    sources: tuple[SourceRef, ...] = ()
    audience: frozenset[str] = field(default_factory=frozenset)
    allowed_principals: frozenset[str] = field(default_factory=frozenset)
    valid_until_ms: int | None = None
    superseded_by: str | None = None
    revoked_at_ms: int | None = None
    created_ms: int = 0
    updated_ms: int = 0
    unresolved_mentions: tuple[str, ...] = ()
