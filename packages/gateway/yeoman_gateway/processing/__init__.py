"""State-aware message processing core (spec R01, R05, R06, R10).

This package is developed behind ``processing.enabled = false``. It adds durable
identities and idempotent effect states next to the existing archives; it is not a second
semantic memory database and it does not replace the inbound archive.
"""

from yeoman_gateway.processing.effects import EffectAuthorizer, EffectExecutor, EffectGateway
from yeoman_gateway.processing.models import (
    DAY_MS,
    CanonicalEvent,
    DecisionRecord,
    DeletePayload,
    EffectConflictError,
    EffectEnvelope,
    EffectEvidence,
    EffectReceipt,
    EffectState,
    EffectTarget,
    ExternalActionPayload,
    InvalidTransitionError,
    JournalConflictError,
    LineageView,
    MediaPayload,
    PolicySnapshot,
    ProcessingError,
    PurgeReport,
    ReactionPayload,
    RetentionSettings,
    StoredEffect,
    TextPayload,
    TurnRef,
    canonical_hash,
    canonical_json,
    payload_from_mapping,
    validate_transition,
)
from yeoman_gateway.processing.store import SCHEMA_VERSION, ProcessingStore

__all__ = [
    "DAY_MS",
    "SCHEMA_VERSION",
    "CanonicalEvent",
    "DecisionRecord",
    "DeletePayload",
    "EffectAuthorizer",
    "EffectConflictError",
    "EffectEnvelope",
    "EffectEvidence",
    "EffectExecutor",
    "EffectGateway",
    "EffectReceipt",
    "EffectState",
    "EffectTarget",
    "ExternalActionPayload",
    "InvalidTransitionError",
    "JournalConflictError",
    "LineageView",
    "MediaPayload",
    "PolicySnapshot",
    "ProcessingError",
    "ProcessingStore",
    "PurgeReport",
    "ReactionPayload",
    "RetentionSettings",
    "StoredEffect",
    "TextPayload",
    "TurnRef",
    "canonical_hash",
    "canonical_json",
    "payload_from_mapping",
    "validate_transition",
]
