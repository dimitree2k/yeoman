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
    "ALIAS_KINDS",
    "ALIAS_STATUSES",
    "ATTRIBUTE_KEYS",
    "ATTRIBUTE_POLARITIES",
    "BINDING_STATUSES",
    "CHANGE_STATUSES",
    "CONVERSATION_ORIGINS",
    "CONVERSATION_RELATIONS",
    "CONVERSATION_STATUSES",
    "DAY_MS",
    "DEFAULT_NAMESPACE",
    "EPISODE_CLOSURE_MS",
    "EPISODE_STATUSES",
    "ERROR_CODES",
    "GLOBAL_SCOPE_KEY",
    "MAX_ATTRIBUTE_VALUE_LENGTH",
    "MAX_NAME_LENGTH",
    "MAX_PEOPLE_PER_STATEMENT",
    "MAX_SOURCES_PER_STATEMENT",
    "PERSON_ROLES",
    "PERSON_ROLE_STATUSES",
    "READ_PURPOSES",
    "STATEMENT_ATTRIBUTIONS",
    "STATEMENT_STATUSES",
    "SUPERSESSION_REASONS",
    "TIME_BASES",
    "TIME_PRECISIONS",
    "AttributeCandidate",
    "AttributeValue",
    "CaptureJobReceipt",
    "CaptureJobRecord",
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
    "EpisodeBuildReport",
    "EpisodeSeed",
    "EpisodeSourceRef",
    "EpisodeView",
    "Identifier",
    "IdentifierBinding",
    "KnowledgeContext",
    "KnowledgeError",
    "KnowledgeStats",
    "MaintenanceReport",
    "PersonLinkCandidate",
    "PersonProfile",
    "PersonProfileView",
    "PersonResolution",
    "RecallQuery",
    "SourceRef",
    "StatementCandidate",
    "StatementPage",
    "StatementRecord",
    "StatementSummary",
    "TrustedAdminContext",
    "TrustedCaptureContext",
    "TrustedIdentityObservation",
    "TrustedReadContext",
    "ValidationError",
    "normalize_alias_value",
    "normalize_identifier_value",
    "validate_alias_kind",
    "validate_attribute_key",
    "validate_attribute_value",
    "validate_name",
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

DAY_MS: Final[int] = 86_400_000

#: "Approximately two months": context whose sources are all older than this may be
#: consolidated.  The boundary means consolidation, never deletion of the log.
EPISODE_CLOSURE_MS: Final[int] = 60 * DAY_MS

EPISODE_STATUSES: Final[tuple[str, ...]] = ("active", "superseded", "stale")

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

#: Binding lifecycle.  ``ended`` keeps the proven past period readable; only ``active``
#: authorizes current delivery and current person roles.
BINDING_STATUSES: Final[tuple[str, ...]] = ("active", "ended", "conflict", "withheld")

#: Status of one stored person role at one statement.  A statement keeps a role row even
#: when the mapping behind it could not be proven, so the cutover stays auditable.
PERSON_ROLE_STATUSES: Final[tuple[str, ...]] = ("active", "withheld", "conflict")

#: Machine-readable reason for replacing a statement.  ``unknown`` is the honest default
#: for legacy rows: it is never guessed from the status alone.
SUPERSESSION_REASONS: Final[tuple[str, ...]] = (
    "state_change",
    "correction",
    "quality_rejected",
    "unknown",
)

#: How a stated time relates to the source.  ``stored`` marks the technical fallback of a
#: row that carries no human-stated period at all.
TIME_BASES: Final[tuple[str, ...]] = ("explicit", "source_time", "unknown", "stored")

#: Precision of a stated time.  ``unknown`` must never be rendered as a certain day.
TIME_PRECISIONS: Final[tuple[str, ...]] = ("exact", "day", "month", "year", "approximate", "unknown")

ALIAS_KINDS: Final[tuple[str, ...]] = (
    "platform_display",
    "nickname",
    "short_name",
    "other_name",
)

ALIAS_STATUSES: Final[tuple[str, ...]] = ("observed", "candidate", "confirmed", "retired")

#: The scope key of an alias that was explicitly released for every context.  SQLite
#: treats NULLs as distinct in unique indexes, so a global scope needs a real value.
GLOBAL_SCOPE_KEY: Final[str] = "global"

#: V1 attribute vocabulary.  The extractor may not invent further keys.
ATTRIBUTE_KEYS: Final[tuple[str, ...]] = (
    "residence",
    "hobby",
    "interest",
    "preference",
    "description",
)

ATTRIBUTE_POLARITIES: Final[tuple[str, ...]] = ("positive", "negative")

MAX_NAME_LENGTH: Final[int] = 200
MAX_ATTRIBUTE_VALUE_LENGTH: Final[int] = 500

#: The namespace of a channel that carries a single platform account.  A channel adapter
#: states it explicitly; nothing derives it from a missing value.
DEFAULT_NAMESPACE: Final[str] = "default"
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
    return text


def validate_namespace(value: object, field_name: str = "namespace") -> str:
    """A namespace is a non-empty, bounded token that names one platform account.

    It is validated like an id because it becomes part of a uniqueness key.  An empty
    namespace must fail loudly: silently collapsing every account onto one key would let
    two unrelated platform accounts share an identifier binding.
    """
    return _require_id(value, field_name).lower()


def normalize_alias_value(value: object, field_name: str = "alias") -> str:
    """The search form of a name.  Search only: it never confirms an identity."""
    if not isinstance(value, str):
        raise ValidationError(f"{field_name} must be a string")
    text = value.strip()
    if not text:
        raise ValidationError(f"{field_name} must not be empty")
    if _CONTROL_CHARS.search(text):
        raise ValidationError(f"{field_name} contains control characters")
    if len(text) > MAX_NAME_LENGTH:
        raise ValidationError(f"{field_name} exceeds {MAX_NAME_LENGTH} characters")
    return " ".join(text.casefold().split())


def validate_alias_kind(value: object) -> str:
    return _require_choice(value, ALIAS_KINDS, "alias_kind")


def validate_attribute_key(value: object) -> str:
    return _require_choice(value, ATTRIBUTE_KEYS, "attribute_key")


def validate_attribute_value(value: object, field_name: str = "attribute value") -> str:
    """A structured attribute value is bounded text, never silently truncated."""
    if not isinstance(value, str):
        raise ValidationError(f"{field_name} must be a string")
    text = value.strip()
    if not text:
        raise ValidationError(f"{field_name} must not be empty")
    if _CONTROL_CHARS.search(text):
        raise ValidationError(f"{field_name} contains control characters")
    if len(text) > MAX_ATTRIBUTE_VALUE_LENGTH:
        raise ValidationError(f"{field_name} exceeds {MAX_ATTRIBUTE_VALUE_LENGTH} characters")
    return text


# ── identifier and trusted observations ──────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Identifier:
    """One platform identifier: channel plus namespace plus kind plus normalized value.

    ``namespace`` names the platform account the identifier belongs to.  Channel
    adapters always state it.  ``None`` means "not stated" and is normalized to
    ``DEFAULT_NAMESPACE`` for compatibility callers; an *empty* string is a typo and is
    rejected.  Product code never derives a namespace from a missing value.
    """

    channel: str
    kind: str
    value: str
    namespace: str | None = None

    def __post_init__(self) -> None:
        channel = _require_id(self.channel, "channel").lower()
        kind = _require_id(self.kind, "kind").lower()
        if kind not in _IDENTIFIER_KINDS:
            raise ValidationError(f"unsupported identifier kind: {kind!r}")
        value = normalize_identifier_value(kind, _require_id(self.value, "identifier value"))
        namespace = (
            DEFAULT_NAMESPACE if self.namespace is None else validate_namespace(self.namespace)
        )
        object.__setattr__(self, "channel", channel)
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "value", value)
        object.__setattr__(self, "namespace", namespace)

    @property
    def key(self) -> tuple[str, str, str]:
        """The legacy channel/kind/value key, kept for compatibility readers."""
        return (self.channel, self.kind, self.value)

    @property
    def full_key(self) -> tuple[str, str, str, str]:
        """The complete uniqueness key of a temporal binding.

        ``__post_init__`` always resolves the namespace to a concrete token, so the
        fallback here is unreachable and exists only for the type checker.
        """
        return (self.channel, self.kind, self.namespace or DEFAULT_NAMESPACE, self.value)


@dataclass(frozen=True, slots=True)
class TrustedIdentityObservation:
    """A verified platform observation, issued by a channel/bridge adapter.

    ``mapping_verified`` means the adapter proved that all given identifiers belong to
    the same platform account (for example WhatsApp PN<->LID evidence).  It never
    means the observation was authorized to create owner rights.

    ``account_namespace`` names the platform account the identifiers belong to.  Every
    identifier of one observation shares it when the caller does not set the namespace
    on the identifier itself.
    """

    identifiers: tuple[Identifier, ...]
    evidence_ref: str
    observed_name: str | None = None
    observed_at_ms: int = 0
    mapping_verified: bool = False
    channel_hint: str | None = None
    account_namespace: str = ""

    def __post_init__(self) -> None:
        identifiers = tuple(self.identifiers)
        if not identifiers:
            raise ValidationError("observation needs at least one identifier")
        if len(identifiers) > 8:
            raise ValidationError("observation has too many identifiers")
        if self.account_namespace:
            namespace = validate_namespace(self.account_namespace, "account_namespace")
            identifiers = tuple(
                item
                if item.namespace == namespace
                else Identifier(item.channel, item.kind, item.value, namespace)
                for item in identifiers
            )
            object.__setattr__(self, "account_namespace", namespace)
        if len({item.full_key for item in identifiers}) != len(identifiers):
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
    """The result of resolving one identifier, or one person's endpoint.

    ``person_id`` is ``None`` whenever there is no proven person to name.  A placeholder
    id instead would invite a caller to treat "unresolved" as a person.
    """

    status: str
    person_id: str | None
    identifier: Identifier | None
    identity_revision: int
    reason: str

    def __post_init__(self) -> None:
        _require_choice(self.status, CHANGE_STATUSES, "status")
        if self.person_id is not None:
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
    """One person in one role at one statement, bound to one evidence source.

    ``status`` keeps the cutover auditable: a role whose principal->person mapping could
    not be proven is stored as ``withheld`` with a ``resolution_reason`` instead of being
    silently dropped or silently confirmed.  Only ``active`` roles feed profiles.
    """

    person_id: str
    role: str
    source: SourceRef
    attribution: str
    status: str = "active"
    binding_id: str | None = None
    resolution_reason: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "person_id", _require_id(self.person_id, "person_id"))
        _require_choice(self.role, PERSON_ROLES, "role")
        _require_choice(self.attribution, STATEMENT_ATTRIBUTIONS, "attribution")
        _require_choice(self.status, PERSON_ROLE_STATUSES, "status")
        if not isinstance(self.source, SourceRef):
            raise ValidationError("link source must be a SourceRef")
        if self.binding_id is not None:
            object.__setattr__(
                self, "binding_id", _require_id(self.binding_id, "binding_id")
            )
        if not isinstance(self.resolution_reason, str):
            raise ValidationError("resolution_reason must be a string")


@dataclass(frozen=True, slots=True)
class AttributeValue:
    """One validated structured value of a statement facet.

    ``polarity`` is stored explicitly: absence of a value never means a negation.
    """

    text: str
    precision: str = "unknown"
    polarity: str = "positive"

    def __post_init__(self) -> None:
        object.__setattr__(self, "text", validate_attribute_value(self.text))
        _require_choice(self.precision, TIME_PRECISIONS, "precision")
        _require_choice(self.polarity, ATTRIBUTE_POLARITIES, "polarity")

    @property
    def value_key(self) -> str:
        """Deterministic search/dedupe key.  Never an identity proof."""
        return normalize_alias_value(self.text, "attribute value")


@dataclass(frozen=True, slots=True)
class AttributeCandidate:
    """One structured facet proposed together with its statement.

    It is never an independent write: the service publishes it in the same transaction
    as the statement, and only when an active ``subject`` role for ``person_id`` exists.
    """

    person_id: str
    attribute_key: str
    value: AttributeValue
    polarity: str = "positive"

    def __post_init__(self) -> None:
        object.__setattr__(self, "person_id", _require_id(self.person_id, "person_id"))
        object.__setattr__(
            self, "attribute_key", validate_attribute_key(self.attribute_key)
        )
        if not isinstance(self.value, AttributeValue):
            raise ValidationError("attribute value must be an AttributeValue")
        _require_choice(self.polarity, ATTRIBUTE_POLARITIES, "polarity")


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
    attributes: tuple[AttributeCandidate, ...] = ()
    time_basis: str = "unknown"
    time_precision: str = "unknown"

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

        attributes = tuple(self.attributes)
        if len(attributes) > MAX_PEOPLE_PER_STATEMENT:
            raise ValidationError("too many attribute candidates")
        if len({(item.person_id, item.attribute_key, item.value.value_key) for item in attributes}) != len(
            attributes
        ):
            raise ValidationError("duplicate attribute candidates")
        object.__setattr__(self, "attributes", attributes)

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
        _require_choice(self.time_basis, TIME_BASES, "time_basis")
        _require_choice(self.time_precision, TIME_PRECISIONS, "time_precision")


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
class PersonProfileView:
    """A deterministic, bounded person card.

    Never a stored document and never a model output: every line is derived from the
    stored, permitted rows, so the same rows always produce the same card.  ``truncated``
    marks a bounded selection - the stored values themselves are untouched.
    """

    person_id: str | None = None
    display_name: str | None = None
    card: str = ""
    statement_ids: tuple[str, ...] = ()
    source_refs: tuple[SourceRef, ...] = ()
    attributes: tuple[tuple[str, tuple[str, ...]], ...] = ()
    aliases: tuple[str, ...] = ()
    endpoints: tuple[Identifier, ...] = ()
    conflicts: tuple[str, ...] = ()
    identity_revision: int = 0
    acl_epoch: int = 0
    truncated: bool = False
    reason: str = "ok"
    denied_count: int = 0

    @property
    def entry_count(self) -> int:
        return len([line for line in self.card.splitlines() if line.strip()])

    @property
    def empty(self) -> bool:
        return not self.statement_ids and not self.attributes


@dataclass(frozen=True, slots=True)
class PersonProfile:

    """A person plus the knowledge context a reader is allowed to see."""

    person: PersonResolution
    context: KnowledgeContext


# ── episodes ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class EpisodeSourceRef:
    """One covered source revision, and the statement it supports in this episode."""

    event_id: str
    revision: int
    statement_id: str = ""
    channel: str = ""
    chat_id: str = ""
    occurred_ms: int = 0
    status: str = "active"


@dataclass(frozen=True, slots=True)
class EpisodeSeed:
    """One closed statement offered to the summarizer, with its own provenance."""

    statement_id: str
    content: str
    author_principal: str = ""
    confidence: float = 0.5
    occurred_ms: int = 0
    source_event_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class EpisodeView:
    """A derived statement about closed context.  Never independent human evidence."""

    episode_id: str
    workspace_id: str = ""
    scope_key: str = ""
    version: int = 1
    status: str = "active"
    text: str = ""
    model_version: str = ""
    prompt_version: str = ""
    uncertainty: float = 1.0
    source_count: int = 0
    sources: tuple[EpisodeSourceRef, ...] = ()
    supersedes: str | None = None
    created_ms: int = 0
    reason: str = "ok"

    @property
    def derived(self) -> bool:
        """An episode is a derivation, not a fresh human statement."""
        return True

    @property
    def stale(self) -> bool:
        return self.status == "stale"


@dataclass(frozen=True, slots=True)
class EpisodeBuildReport:
    """What one consolidation pass did."""

    created: int = 0
    reused: int = 0
    superseded: int = 0
    stale: int = 0
    skipped: int = 0
    episode_ids: tuple[str, ...] = ()


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
class CaptureJobRecord:
    """One persistent extraction job together with the sources it rests on.

    Internal worker read: it carries the job's sources so a worker can re-verify them
    against the proof owner before any provider call.  ``unresolved`` names stored source
    keys the authority no longer issues - a job with any of those may not run.
    """

    job_id: str
    state: str
    reason: str = ""
    scope_key: str = ""
    kind: str = "statement_extraction"
    extractor_version: str = ""
    attempts: int = 0
    due_ms: int = 0
    updated_ms: int = 0
    sources: tuple[SourceRef, ...] = ()
    unresolved: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "job_id", _require_id(self.job_id, "job_id"))
        _require_choice(
            self.state,
            ("queued", "running", "done", "skipped", "cancelled", "failed"),
            "state",
        )
        object.__setattr__(self, "sources", tuple(self.sources))
        object.__setattr__(self, "unresolved", tuple(str(item) for item in self.unresolved))


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
    """Internal value object: one observed or confirmed name of a person.

    ``status`` and ``address_allowed`` are the ethics of the row: recognising a name is
    not the same as being allowed to address the person with it.
    """

    person_id: str
    name: str
    source: str
    visibility: str = "public"
    first_seen_ms: int = 0
    last_seen_ms: int = 0
    observed_by: str = ""
    alias_kind: str = "other_name"
    normalized_alias: str = ""
    scope_key: str = "global"
    status: str = "observed"
    address_allowed: bool = False
    is_preferred: bool = False
    supporting_statement_id: str | None = None
    evidence_ref: str = ""
    valid_until_ms: int | None = None
    mapping_retracted: bool = False
    revision: int = 1

    @property
    def retired(self) -> bool:
        return self.status == "retired"

    @property
    def findable(self) -> bool:
        """A retracted *mapping* ("that was never me") also leaves the search.

        Withdrawing the address ("please stop calling me that") does not: the name stays
        findable inside its existing rights.
        """
        return not self.mapping_retracted

    @property
    def usable_as_address(self) -> bool:
        return (
            self.status in ("confirmed", "observed")
            and self.address_allowed
            and not self.mapping_retracted
            and self.valid_until_ms is None
        )


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
    """Internal value object for one temporal identifier binding.

    ``valid_from_ms`` may be ``0`` for "unknown start": a timestamp that was never
    observed proves nothing about the past and never authorizes a retroactive mapping.
    ``valid_until_ms`` of ``0`` means "still open"; an ``ended`` binding always carries
    a positive end.
    """

    person_id: str
    identifier: Identifier
    evidence_ref: str
    status: str = "active"
    mapping_verified: bool = False
    created_ms: int = 0
    updated_ms: int = 0
    binding_id: str = ""
    valid_from_ms: int = 0
    valid_until_ms: int = 0
    observed_at_ms: int = 0
    revision: int = 1

    def __post_init__(self) -> None:
        object.__setattr__(self, "person_id", _require_id(self.person_id, "person_id"))
        if not isinstance(self.identifier, Identifier):
            raise ValidationError("binding identifier must be an Identifier")
        object.__setattr__(self, "evidence_ref", _require_id(self.evidence_ref, "evidence_ref"))
        _require_choice(self.status, BINDING_STATUSES, "status")
        object.__setattr__(
            self, "revision", _require_int(self.revision, "revision", minimum=1)
        )
        for name in ("created_ms", "updated_ms", "valid_from_ms", "valid_until_ms", "observed_at_ms"):
            object.__setattr__(
                self, name, _require_int(getattr(self, name), name, minimum=0)
            )

    @property
    def open_ended(self) -> bool:
        return self.status == "active" and self.valid_until_ms == 0

    def covers(self, at_ms: int) -> bool:
        """True when the *proven period* contains ``at_ms``.

        A period is half-open ``[start, end)`` and must have a known start: an unknown
        start is knowledge time, not a proven historical start, so it covers nothing and
        authorizes nothing retroactively.

        An ``ended`` binding still covers its own past - that is exactly what makes
        "which person did this number belong to in March?" answerable.  ``conflict`` and
        ``withheld`` never cover anything, because the mapping itself is unproven.
        """
        if self.status not in ("active", "ended") or self.valid_from_ms <= 0:
            return False
        if at_ms < self.valid_from_ms:
            return False
        return self.valid_until_ms == 0 or at_ms < self.valid_until_ms


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
    revision: int = 1
    supersession_reason: str = ""
    time_basis: str = "unknown"
    time_precision: str = "unknown"
