"""Small immutable records shared by the offline history adapters."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class NormalizedEvent:
    normalization_version: int
    event_id: str
    revision: int | str
    channel: str | None
    account: str | None
    chat_id: str | None
    native_id: str | None
    kind: str
    direction: str
    sender_raw: str | None
    principal: str | None
    observed_ms: int | None
    occurred_ms: int | None
    time_certainty: str
    original_timestamp: Any
    time_metadata: dict[str, Any]
    text: str | None
    text_hash: str | None
    media_kind: str | None
    media_missing: bool
    reply_target: Any
    edit_target: Any
    delete_target: Any
    source_id: str
    bundle_version: int | None
    source_hash: str | None
    locator: dict[str, Any] | str
    source_refs: tuple[Any, ...] = ()
    copies: tuple[dict[str, Any], ...] = ()
    known_event_ids: tuple[str, ...] = ()
    provenance_class: str = "unknown"
    verbatim_unverified: bool = False
    chat_kind: str | None = None
    native_evidence: tuple[dict[str, Any], ...] = ()
    retention_status: str = "retained"
    source_kind: str = "unknown"
    native_type: str | None = None
    transport_receipt: str | None = None
    source_authority: str | None = None
    sender_id_raw: str | None = None
    participant_jid_raw: str | None = None
    payload_purged_ms: Any = None
    source_role: str | None = None
    name_observations: tuple[dict[str, Any], ...] = ()

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-friendly copy without exposing mutable internal containers."""
        result = asdict(self)
        result["source_refs"] = list(self.source_refs)
        result["copies"] = list(self.copies)
        result["known_event_ids"] = list(self.known_event_ids)
        result["native_evidence"] = list(self.native_evidence)
        result["name_observations"] = list(self.name_observations)
        return result
