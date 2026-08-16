"""Temporary private reply handoffs created by bot-initiated DMs."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from loguru import logger

from yeoman_gateway.policy.identity import normalize_sender_list


@dataclass(frozen=True, slots=True)
class PrivateHandoff:
    id: str
    channel: str
    target_chat_id: str
    target_sender_id: str
    target_aliases: tuple[str, ...]
    origin_chat_id: str
    origin_label: str
    created_at: datetime
    expires_at: datetime
    remaining_replies: int


@dataclass(frozen=True, slots=True)
class _PrivateHandoffConsumption:
    handoff_id: str
    turn_id: str
    expires_at: datetime


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _parse_dt(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _aliases(channel: str, *values: str) -> tuple[str, ...]:
    normalized = normalize_sender_list(channel, [v for v in values if str(v or "").strip()])
    return tuple(sorted(normalized))


class PrivateHandoffStore:
    """Small JSON store for short-lived off-chat reply grants."""

    def __init__(
        self,
        path: Path,
        *,
        ttl: timedelta = timedelta(hours=24),
        max_replies: int = 5,
    ) -> None:
        self.path = path
        self.ttl = ttl
        self.max_replies = max(1, int(max_replies))

    def open(
        self,
        *,
        channel: str,
        target_chat_id: str,
        target_sender_id: str = "",
        origin_chat_id: str,
        origin_label: str = "",
        now: datetime | None = None,
    ) -> PrivateHandoff:
        current = (now or _utc_now()).astimezone(UTC)
        target_aliases = _aliases(channel, target_chat_id, target_sender_id)
        handoff = PrivateHandoff(
            id=uuid.uuid4().hex,
            channel=channel,
            target_chat_id=target_chat_id,
            target_sender_id=target_sender_id,
            target_aliases=target_aliases,
            origin_chat_id=origin_chat_id,
            origin_label=origin_label or origin_chat_id,
            created_at=current,
            expires_at=current + self.ttl,
            remaining_replies=self.max_replies,
        )
        loaded, consumptions = self._load_state(now=current, strict=True)
        records = [
            record
            for record in loaded
            if not (
                record.channel == handoff.channel
                and record.target_chat_id == handoff.target_chat_id
                and record.origin_chat_id == handoff.origin_chat_id
            )
        ]
        records.append(handoff)
        self._save(records, consumptions)
        return handoff

    def find_active(
        self,
        *,
        channel: str,
        chat_id: str,
        sender_id: str = "",
        now: datetime | None = None,
    ) -> PrivateHandoff | None:
        current = (now or _utc_now()).astimezone(UTC)
        incoming_aliases = set(_aliases(channel, chat_id, sender_id))
        records = self._load(now=current)
        for record in records:
            if record.channel != channel or record.remaining_replies <= 0:
                continue
            if incoming_aliases.intersection(record.target_aliases):
                return record
        return None

    def consume_reply(self, handoff_id: str, *, now: datetime | None = None) -> PrivateHandoff | None:
        current = (now or _utc_now()).astimezone(UTC)
        updated: list[PrivateHandoff] = []
        consumed: PrivateHandoff | None = None
        records, consumptions = self._load_state(now=current, strict=True)
        for record in records:
            if record.id != handoff_id:
                updated.append(record)
                continue
            remaining = max(0, record.remaining_replies - 1)
            consumed = PrivateHandoff(
                id=record.id,
                channel=record.channel,
                target_chat_id=record.target_chat_id,
                target_sender_id=record.target_sender_id,
                target_aliases=record.target_aliases,
                origin_chat_id=record.origin_chat_id,
                origin_label=record.origin_label,
                created_at=record.created_at,
                expires_at=record.expires_at,
                remaining_replies=remaining,
            )
            if remaining > 0:
                updated.append(consumed)
        self._save(updated, consumptions)
        return consumed

    def consume_reply_once(
        self,
        handoff_id: str,
        turn_id: str,
        *,
        now: datetime | None = None,
    ) -> PrivateHandoff | None:
        """Consume one reply for a grant, durably idempotent per turn."""
        current = (now or _utc_now()).astimezone(UTC)
        records, consumptions = self._load_state(now=current, strict=True)
        if any(
            consumption.handoff_id == handoff_id
            and consumption.turn_id == turn_id
            for consumption in consumptions
        ):
            return next(
                (record for record in records if record.id == handoff_id),
                None,
            )

        updated: list[PrivateHandoff] = []
        consumed: PrivateHandoff | None = None
        consumption_expiry: datetime | None = None
        for record in records:
            if record.id != handoff_id:
                updated.append(record)
                continue
            remaining = max(0, record.remaining_replies - 1)
            consumed = PrivateHandoff(
                id=record.id,
                channel=record.channel,
                target_chat_id=record.target_chat_id,
                target_sender_id=record.target_sender_id,
                target_aliases=record.target_aliases,
                origin_chat_id=record.origin_chat_id,
                origin_label=record.origin_label,
                created_at=record.created_at,
                expires_at=record.expires_at,
                remaining_replies=remaining,
            )
            consumption_expiry = record.expires_at
            if remaining > 0:
                updated.append(consumed)

        if consumed is None or consumption_expiry is None:
            return None
        consumptions.append(
            _PrivateHandoffConsumption(
                handoff_id=handoff_id,
                turn_id=turn_id,
                expires_at=consumption_expiry,
            )
        )
        self._save(updated, consumptions)
        return consumed

    def _load(self, *, now: datetime | None = None) -> list[PrivateHandoff]:
        records, _consumptions = self._load_state(now=now)
        return records

    def _load_state(
        self,
        *,
        now: datetime | None = None,
        strict: bool = False,
    ) -> tuple[list[PrivateHandoff], list[_PrivateHandoffConsumption]]:
        current = (now or _utc_now()).astimezone(UTC)
        if not self.path.exists():
            return [], []
        try:
            raw = json.loads(self.path.read_text())
        except Exception as exc:
            if strict:
                raise
            logger.warning("private handoff store load failed: {}", exc)
            return [], []
        if (
            strict
            and isinstance(raw, dict)
            and raw.get("version", 1) not in {1, 2}
        ):
            raise ValueError("unsupported private handoff store version")
        items = raw.get("handoffs") if isinstance(raw, dict) else raw
        if not isinstance(items, list):
            if strict:
                raise ValueError("private handoff store requires a handoffs list")
            return [], []
        is_legacy = not isinstance(raw, dict) or raw.get("version", 1) == 1
        records: list[PrivateHandoff] = []
        for item in items:
            if not isinstance(item, dict):
                if strict:
                    raise ValueError("private handoff record must be an object")
                continue
            record = self._from_json(item, allow_legacy_id=is_legacy)
            if record is None:
                if strict:
                    raise ValueError("invalid private handoff record")
                continue
            if record.expires_at <= current or record.remaining_replies <= 0:
                continue
            records.append(record)
        consumptions: list[_PrivateHandoffConsumption] = []
        raw_consumptions = (
            raw.get("consumptions", [])
            if isinstance(raw, dict) and raw.get("version") == 2
            else []
        )
        if isinstance(raw_consumptions, list):
            for item in raw_consumptions:
                if not isinstance(item, dict):
                    if strict:
                        raise ValueError(
                            "private handoff consumption must be an object"
                        )
                    continue
                expires_at = _parse_dt(item.get("expires_at"))
                handoff_id = str(item.get("handoff_id") or "").strip()
                turn_id = str(item.get("turn_id") or "").strip()
                if (
                    expires_at is None
                    or not handoff_id
                    or not turn_id
                ):
                    if strict:
                        raise ValueError(
                            "invalid private handoff consumption"
                        )
                    continue
                if expires_at <= current:
                    continue
                consumptions.append(
                    _PrivateHandoffConsumption(
                        handoff_id=handoff_id,
                        turn_id=turn_id,
                        expires_at=expires_at,
                    )
                )
        elif strict:
            raise ValueError(
                "private handoff store requires a consumptions list"
            )
        return records, consumptions

    def _save(
        self,
        records: list[PrivateHandoff],
        consumptions: list[_PrivateHandoffConsumption],
    ) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 2,
            "handoffs": [self._to_json(record) for record in records],
            "consumptions": [
                {
                    "handoff_id": consumption.handoff_id,
                    "turn_id": consumption.turn_id,
                    "expires_at": consumption.expires_at.isoformat(),
                }
                for consumption in consumptions
            ],
        }
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True))
        tmp.replace(self.path)

    def _from_json(
        self,
        item: dict[str, Any],
        *,
        allow_legacy_id: bool,
    ) -> PrivateHandoff | None:
        created_at = _parse_dt(item.get("created_at"))
        expires_at = _parse_dt(item.get("expires_at"))
        if created_at is None or expires_at is None:
            return None
        channel = str(item.get("channel") or "").strip()
        target_chat_id = str(item.get("target_chat_id") or "").strip()
        origin_chat_id = str(item.get("origin_chat_id") or "").strip()
        if not channel or not target_chat_id or not origin_chat_id:
            return None
        target_sender_id = str(item.get("target_sender_id") or "").strip()
        handoff_id = str(item.get("id") or "").strip()
        if not handoff_id:
            if not allow_legacy_id:
                return None
            handoff_id = f"{channel}:{target_chat_id}:{origin_chat_id}"
        aliases = item.get("target_aliases")
        if not isinstance(aliases, list):
            aliases = list(_aliases(channel, target_chat_id, target_sender_id))
        return PrivateHandoff(
            id=handoff_id,
            channel=channel,
            target_chat_id=target_chat_id,
            target_sender_id=target_sender_id,
            target_aliases=tuple(str(alias) for alias in aliases if str(alias).strip()),
            origin_chat_id=origin_chat_id,
            origin_label=str(item.get("origin_label") or origin_chat_id),
            created_at=created_at,
            expires_at=expires_at,
            remaining_replies=max(0, int(item.get("remaining_replies") or 0)),
        )

    @staticmethod
    def _to_json(record: PrivateHandoff) -> dict[str, Any]:
        return {
            "id": record.id,
            "channel": record.channel,
            "target_chat_id": record.target_chat_id,
            "target_sender_id": record.target_sender_id,
            "target_aliases": list(record.target_aliases),
            "origin_chat_id": record.origin_chat_id,
            "origin_label": record.origin_label,
            "created_at": record.created_at.isoformat(),
            "expires_at": record.expires_at.isoformat(),
            "remaining_replies": record.remaining_replies,
        }
