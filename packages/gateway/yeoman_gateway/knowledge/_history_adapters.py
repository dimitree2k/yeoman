"""Read immutable preservation bundles into source-traceable history records.

This module is deliberately offline and read-only. It validates v2 bundle copies before
reading them, follows only manifest locators for reference-only sources, and never calls
the raw archive seeder or replay writer.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import shutil
import sqlite3
import subprocess
import tempfile
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any
from zoneinfo import ZoneInfo

from ._history_records import NormalizedEvent

_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_HASH = re.compile(r"^[0-9a-f]{64}$")
_LOCAL_ZONE = ZoneInfo("Europe/Berlin")
_SESSION_EPOCH_CUTOFF_MS = 315_532_800_000  # 1980-01-01
_DECODER_SCRIPT = r"""
import { proto } from '@whiskeysockets/baileys/WAProto/index.js';
let input = '';
for await (const chunk of process.stdin) input += chunk;
const bytes = Buffer.from(input.trim(), 'base64');
const message = proto.WebMessageInfo.decode(bytes);
const result = proto.WebMessageInfo.toObject(message, {
  longs: String, enums: String, bytes: String, defaults: false,
});
process.stdout.write(JSON.stringify(result));
"""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_relative(value: Any) -> PurePosixPath:
    if not isinstance(value, str) or not value or "\\" in value:
        raise ValueError("unsafe source locator")
    relative = PurePosixPath(value)
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        raise ValueError("unsafe source locator")
    return relative


def _checked_child(root: Path, relative: Any) -> Path:
    safe = _safe_relative(relative)
    candidate = root.joinpath(*safe.parts)
    current = root
    for part in safe.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError("symlink in source locator")
    try:
        candidate.resolve(strict=True).relative_to(root.resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise ValueError("source locator escapes its root") from exc
    if not candidate.is_file():
        raise ValueError("source locator is not a file")
    return candidate


def _manifest_path(collection: Path) -> tuple[Path, Path]:
    path = Path(collection).expanduser()
    if path.is_symlink():
        raise ValueError("collection path is a symlink")
    if path.is_file():
        manifest = path
    else:
        manifest = path / "manifest.json"
        if not manifest.is_file():
            versions = path / "versions"
            candidates = sorted(versions.glob("*/manifest.json")) if versions.is_dir() else []
            if len(candidates) != 1:
                raise ValueError("collection must identify one v2 manifest")
            manifest = candidates[0]
    if manifest.name != "manifest.json" or manifest.is_symlink():
        raise ValueError("invalid source bundle manifest path")
    return manifest.parent, manifest


def _read_manifest(collection: Path) -> tuple[Path, dict[str, Any]]:
    root, path = _manifest_path(collection)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("source bundle manifest is unreadable") from exc
    if not isinstance(payload, dict) or payload.get("source_bundle_manifest_version") != 2:
        raise ValueError("source bundle manifest must be v2")
    sources = payload.get("sources")
    if not isinstance(sources, list) or any(not isinstance(item, dict) for item in sources):
        raise ValueError("source bundle source list is invalid")
    ids = [item.get("source_id") for item in sources]
    if any(not isinstance(item, str) or not _SAFE_ID.fullmatch(item) for item in ids):
        raise ValueError("source bundle has an invalid source id")
    if len(ids) != len(set(ids)):
        raise ValueError("source bundle has duplicate source ids")
    return root, payload


def _copied_files(bundle: Path, entry: Mapping[str, Any]) -> list[tuple[str, Path, str]]:
    source_id = str(entry["source_id"])
    base = _checked_source_root(bundle, source_id)
    copied = entry.get("copied_files")
    if not isinstance(copied, Mapping):
        raise ValueError("copied source file list is invalid")
    result: list[tuple[str, Path, str]] = []
    declared: set[str] = set()
    for relative, details in copied.items():
        safe = _safe_relative(relative)
        if not isinstance(details, Mapping):
            raise ValueError("copied source file metadata is invalid")
        expected = details.get("copied_sha256")
        if not isinstance(expected, str) or not _HASH.fullmatch(expected):
            raise ValueError("copied source hash is invalid")
        path = _checked_child(base, safe.as_posix())
        if _sha256(path) != expected:
            raise ValueError("copied source hash mismatch")
        declared.add(safe.as_posix())
        result.append((safe.as_posix(), path, expected))
    actual: set[str] = set()
    if base.exists():
        for candidate in base.rglob("*"):
            if candidate.is_symlink():
                raise ValueError("symlink in copied source")
            if candidate.is_file():
                actual.add(candidate.relative_to(base).as_posix())
    if actual != declared:
        raise ValueError("copied source layout mismatch")
    return sorted(result)


def _checked_source_root(bundle: Path, source_id: str) -> Path:
    sources = bundle / "sources"
    if sources.is_symlink():
        raise ValueError("source root is a symlink")
    base = sources / source_id
    if base.is_symlink():
        raise ValueError("copied source is a symlink")
    try:
        base.resolve(strict=True).relative_to(sources.resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise ValueError("copied source escapes collection") from exc
    if not base.is_dir():
        raise ValueError("copied source is missing")
    return base


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _nonempty(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _first(mapping: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        value = mapping.get(key)
        if value is not None and value != "":
            return value
    return None


def _walk_text(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    if not isinstance(value, Mapping):
        return None
    for key in ("conversation", "text", "caption", "body", "content"):
        found = value.get(key)
        if isinstance(found, str):
            return found
    for key in (
        "extendedTextMessage",
        "imageMessage",
        "videoMessage",
        "documentMessage",
        "message",
        "ephemeralMessage",
        "viewOnceMessage",
        "editedMessage",
    ):
        child = value.get(key)
        if isinstance(child, Mapping):
            found = _walk_text(child)
            if found is not None:
                return found
    return None


def _payload_text(*values: Any) -> str | None:
    for value in values:
        found = _walk_text(value)
        if found is not None:
            return found
    return None


def _native_id(*values: Any) -> str | None:
    for value in values:
        mapping = _mapping(value)
        found = _nonempty(_first(mapping, "messageId", "message_id", "source_message_id"))
        if found:
            return found
        key = _mapping(mapping.get("key"))
        found = _nonempty(_first(key, "id", "messageId"))
        if found:
            return found
        message = _mapping(mapping.get("message"))
        found = _nonempty(_first(message, "message_id", "messageId"))
        if found:
            return found
    return None


def _media_kind(message: Mapping[str, Any]) -> str | None:
    for key in ("imageMessage", "videoMessage", "audioMessage", "documentMessage", "stickerMessage"):
        if key in message:
            return key.removesuffix("Message").lower()
    return None


def _source_ref(source_id: str, locator: Mapping[str, Any], *, event_id: str | None = None) -> dict[str, Any]:
    reference: dict[str, Any] = {"source_id": source_id, **dict(locator)}
    if event_id:
        reference["event_id"] = event_id
    return reference


def _generated_id(source_id: str, locator: Mapping[str, Any]) -> str:
    encoded = json.dumps([source_id, locator], sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return "history:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:32]


def _hash_text(value: str | None) -> str | None:
    return hashlib.sha256(value.encode("utf-8")).hexdigest() if value is not None else None


def _name_observations(name: Any, raw_identifier: Any, time: Mapping[str, Any] | None) -> tuple[dict[str, Any], ...]:
    observed = _nonempty(name)
    if observed is None:
        return ()
    timing = time or {}
    return ({
        "name": observed,
        "raw_identifier": _nonempty(raw_identifier),
        "occurred_ms": timing.get("occurred_ms"),
        "observed_ms": timing.get("observed_ms"),
        "time_certainty": timing.get("time_certainty") or "unknown",
        "provenance_class": "native",
    },)


def _time_ms(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        if not math.isfinite(float(value)):
            return None
        number = float(value)
        if abs(number) < 100_000_000_000:
            number *= 1000
        return int(number)
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    try:
        number = float(text)
    except ValueError:
        number = None
    if number is not None and math.isfinite(number):
        if abs(number) < 100_000_000_000:
            number *= 1000
        return int(number)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        return int(parsed.timestamp() * 1000)
    candidates: set[int] = set()
    for fold in (0, 1):
        aware = parsed.replace(tzinfo=_LOCAL_ZONE, fold=fold)
        utc_value = aware.astimezone(UTC)
        if utc_value.astimezone(_LOCAL_ZONE).replace(tzinfo=None) == parsed:
            candidates.add(int(utc_value.timestamp() * 1000))
    if len(candidates) != 1:
        return None
    return next(iter(candidates))


def normalize_time(value: Any, *, basis: str, source_kind: str) -> dict[str, Any]:
    """Normalize one timestamp while keeping capture and occurrence claims separate."""
    original = value
    normalized_basis = basis.strip().lower() if isinstance(basis, str) else "unknown"
    metadata: dict[str, Any] = {"source_basis": normalized_basis, "source_kind": source_kind}
    if value is None or normalized_basis in {"unknown", "none", ""}:
        return {
            "observed_ms": None,
            "occurred_ms": None,
            "time_certainty": "unknown",
            "original_timestamp": original,
            "time_metadata": metadata,
        }
    naive_local = False
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
            naive_local = parsed.tzinfo is None
        except ValueError:
            pass
    milliseconds = _time_ms(value)
    if naive_local:
        metadata["timezone_basis"] = "Europe/Berlin"
    if milliseconds is None:
        if naive_local:
            metadata["reason"] = "ambiguous_or_nonexistent_local_time"
            return {
                "observed_ms": None,
                "occurred_ms": None,
                "time_certainty": "ambiguous",
                "original_timestamp": original,
                "time_metadata": metadata,
            }
        metadata["reason"] = "unparseable"
        return {
            "observed_ms": None,
            "occurred_ms": None,
            "time_certainty": "unknown",
            "original_timestamp": original,
            "time_metadata": metadata,
        }
    if source_kind in {"session", "session_jsonl"} and milliseconds < _SESSION_EPOCH_CUTOFF_MS:
        metadata["reason"] = "epoch_1970_session_timestamp"
        return {
            "observed_ms": None,
            "occurred_ms": None,
            "time_certainty": "unknown",
            "original_timestamp": original,
            "time_metadata": metadata,
        }
    if normalized_basis in {"native", "native_timestamp", "occurred", "occurred_ms"}:
        certainty = "native"
        occurred_ms, observed_ms = milliseconds, None
    elif normalized_basis in {"provider_timestamp", "provider", "timestamp"}:
        certainty = "provider_timestamp"
        occurred_ms, observed_ms = milliseconds, None
    elif normalized_basis in {"capture_time_approx", "capture", "created_at_capture", "observed"}:
        certainty = "capture_time_approx"
        occurred_ms, observed_ms = None, milliseconds
    else:
        certainty = "unknown"
        occurred_ms, observed_ms = None, None
    return {
        "observed_ms": observed_ms,
        "occurred_ms": occurred_ms,
        "time_certainty": certainty,
        "original_timestamp": original,
        "time_metadata": metadata,
    }


def _event(
    *,
    source_id: str,
    source_hash: str | None,
    locator: dict[str, Any],
    channel: Any = None,
    account: Any = None,
    chat_id: Any = None,
    native_id: Any = None,
    event_id: Any = None,
    archive_native_id: Any = None,
    revision: Any = 1,
    kind: Any = "message",
    direction: Any = "unknown",
    sender_raw: Any = None,
    principal: Any = None,
    time: Mapping[str, Any] | None = None,
    text: Any = None,
    media_kind: Any = None,
    media_missing: bool = False,
    reply_target: Any = None,
    edit_target: Any = None,
    delete_target: Any = None,
    provenance_class: str = "unknown",
    chat_kind: Any = None,
    native_evidence: Any = None,
    retention_status: str = "retained",
    source_kind: str = "unknown",
    native_type: Any = None,
    known_event_ids: Sequence[str] = (),
    transport_receipt: str | None = None,
    source_authority: str | None = None,
    sender_id_raw: Any = None,
    participant_jid_raw: Any = None,
    payload_purged_ms: Any = None,
    source_role: Any = None,
    name_observations: Sequence[Mapping[str, Any]] = (),
) -> NormalizedEvent:
    actual_text = _text(text)
    actual_event_id = _nonempty(event_id) or _generated_id(source_id, locator)
    actual_channel = _nonempty(channel)
    actual_account = _nonempty(account)
    actual_chat_id = _nonempty(chat_id)
    actual_native_id = _nonempty(native_id)
    actual_kind = str(kind) if kind is not None else "unknown"
    actual_direction = str(direction) if direction is not None else "unknown"
    actual_source_role = _nonempty(source_role)
    actual_sender_id = _nonempty(sender_id_raw)
    actual_participant_jid = _nonempty(participant_jid_raw)
    references = (_source_ref(source_id, locator, event_id=actual_event_id),)
    copy = {
        "source_id": source_id,
        "source_hash": source_hash,
        "locator": dict(locator),
        "source_kind": source_kind,
        "provenance_class": provenance_class,
        "source_authority": source_authority,
        "channel": actual_channel,
        "account": actual_account,
        "chat_id": actual_chat_id,
        "native_id": actual_native_id,
        "kind": actual_kind,
        "direction": actual_direction,
        "revision": revision,
        "text_hash": _hash_text(actual_text),
        "sender_id_raw": actual_sender_id,
        "participant_jid_raw": actual_participant_jid,
        "payload_purged_ms": payload_purged_ms,
        "source_role": actual_source_role,
    }
    if _nonempty(archive_native_id):
        copy["archive_native_id"] = _nonempty(archive_native_id)
    for key, value in (("event_id", event_id), ("revision", revision)):
        if value is not None:
            copy[key] = str(value) if isinstance(value, (str, int)) else value
    time_data = dict(time or normalize_time(None, basis="unknown", source_kind=source_kind))
    revision_value: int | str = revision if isinstance(revision, (int, str)) and not isinstance(revision, bool) else 1
    evidence: tuple[dict[str, Any], ...]
    if isinstance(native_evidence, Mapping):
        evidence = (dict(native_evidence),)
    elif isinstance(native_evidence, Sequence) and not isinstance(native_evidence, (str, bytes)):
        evidence = tuple(dict(item) for item in native_evidence if isinstance(item, Mapping))
    else:
        evidence = ()
    return NormalizedEvent(
        normalization_version=1,
        event_id=actual_event_id,
        revision=revision_value,
        channel=actual_channel,
        account=actual_account,
        chat_id=actual_chat_id,
        native_id=actual_native_id,
        kind=actual_kind,
        direction=actual_direction,
        sender_raw=_nonempty(sender_raw),
        principal=_nonempty(principal),
        observed_ms=time_data.get("observed_ms"),
        occurred_ms=time_data.get("occurred_ms"),
        time_certainty=str(time_data.get("time_certainty") or "unknown"),
        original_timestamp=time_data.get("original_timestamp"),
        time_metadata=dict(time_data.get("time_metadata") or {}),
        text=actual_text,
        text_hash=_hash_text(actual_text),
        media_kind=_nonempty(media_kind),
        media_missing=bool(media_missing),
        reply_target=reply_target,
        edit_target=edit_target,
        delete_target=delete_target,
        source_id=source_id,
        bundle_version=2,
        source_hash=source_hash,
        locator=dict(locator),
        source_refs=references,
        copies=(copy,),
        known_event_ids=tuple(dict.fromkeys(item for item in known_event_ids if item)),
        provenance_class=provenance_class,
        verbatim_unverified=False,
        chat_kind=_nonempty(chat_kind),
        native_evidence=evidence,
        retention_status=retention_status,
        source_kind=source_kind,
        native_type=_nonempty(native_type),
        transport_receipt=transport_receipt,
        source_authority=source_authority,
        sender_id_raw=actual_sender_id,
        participant_jid_raw=actual_participant_jid,
        payload_purged_ms=payload_purged_ms,
        source_role=actual_source_role,
        name_observations=tuple(
            {
                **dict(item),
                "source_id": source_id,
                "locator": dict(locator),
                "provenance_class": provenance_class,
            }
            for item in name_observations
        ),
    )


def _row(row: sqlite3.Row) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}


def _json(value: Any) -> Any:
    if isinstance(value, Mapping) or isinstance(value, list):
        return value
    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8")
        except UnicodeDecodeError:
            return None
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return None
    return None


def _kind(value: Any, native_type: Any = None) -> str:
    original_value = _nonempty(value)
    original_native = _nonempty(native_type)
    mapping = {
        "MESSAGE": "message",
        "MESSAGE_CREATE": "message",
        "MESSAGE_EDIT": "edit",
        "EDIT": "edit",
        "MESSAGE_DELETE": "delete",
        "DELETE": "delete",
        "REACTION": "reaction",
        "MESSAGE_REACTION": "reaction",
        "RECEIPT": "receipt",
        "PARTICIPANT_ADD": "membership",
        "PARTICIPANT_REMOVE": "membership",
        "GROUP_PARTICIPANT_UPDATE": "membership",
    }

    def mapped(original: str | None) -> str | None:
        if original is None:
            return None
        return mapping.get(original.upper().replace("-", "_"))

    native_kind = mapped(original_native)
    value_kind = mapped(original_value)
    if native_kind in {"edit", "delete", "reaction", "receipt", "membership"}:
        return native_kind
    if value_kind is not None:
        return value_kind
    return original_value or original_native or "unknown"


def _chat_kind(*values: Any) -> str | None:
    for value in values:
        mapping = _mapping(value)
        found = _nonempty(_first(mapping, "chat_kind", "chatKind", "conversation_type", "conversationType"))
        if found:
            return found.lower()
        if mapping.get("isGroup") is True or mapping.get("is_group") is True:
            return "group"
        if mapping.get("isGroup") is False or mapping.get("is_group") is False:
            return "direct"
        jid = _nonempty(_first(mapping, "remoteJid", "remote_jid", "chatJid", "chat_jid", "chat_id", "chatId"))
        if jid:
            address = jid.casefold()
            if address.endswith("@g.us"):
                return "group"
            if address.endswith(("@s.whatsapp.net", "@c.us")):
                return "direct"
    return None


def _payload_account(*values: Any) -> str | None:
    for value in values:
        mapping = _mapping(value)
        found = _nonempty(_first(mapping, "account", "accountId", "account_id"))
        if found:
            return found
    return None


def _native_bridge_reference_tree(entry: Mapping[str, Any]) -> bool:
    source_path = entry.get("source_path")
    return (
        str(entry.get("source_class") or "").strip().casefold() == "native"
        and str(entry.get("kind") or "").strip().casefold() == "tree"
        and isinstance(source_path, str)
        and PurePosixPath(source_path).is_absolute()
        and PurePosixPath(source_path).name == "whatsapp-message-references"
    )


def _source_label(source_id: str, entry: Mapping[str, Any], relative: str) -> str:
    if _native_bridge_reference_tree(entry):
        return "bridge_reference"
    material = " ".join((source_id.lower(), str(entry.get("source_class") or "").lower(), relative.lower()))
    if "bridge" in material and "reference" in material:
        return "bridge_reference"
    if "raw" in material:
        return "raw_archive"
    if "session" in material:
        return "session_jsonl"
    if "inbound" in material or "reply_context" in material:
        return "inbound_jsonl"
    return "jsonl"


def _decode_bridge(encoded: Any, bridge_package_dir: Path | None) -> tuple[Mapping[str, Any] | None, str | None]:
    if not isinstance(encoded, str) or not encoded:
        return None, "encoded_payload_missing"
    try:
        data = base64_bytes(encoded)
    except ValueError:
        return None, "encoded_payload_invalid"
    if not data:
        return None, "encoded_payload_invalid"
    if bridge_package_dir is None:
        return None, "offline_decoder_not_supplied"
    package = Path(bridge_package_dir).expanduser()
    if not package.is_dir():
        return None, "offline_decoder_unavailable"
    try:
        result = subprocess.run(
            ["node", "--input-type=module", "-e", _DECODER_SCRIPT],
            cwd=package,
            input=encoded.encode("ascii"),
            capture_output=True,
            timeout=8,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None, "offline_decoder_unavailable"
    if result.returncode != 0 or len(result.stdout) > 2_000_000:
        return None, "offline_decoder_failed"
    try:
        value = json.loads(result.stdout)
    except (UnicodeError, json.JSONDecodeError):
        return None, "offline_decoder_failed"
    return (value, None) if isinstance(value, Mapping) else (None, "offline_decoder_failed")


def base64_bytes(value: str) -> bytes:
    import base64
    import binascii

    try:
        return base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("invalid base64") from exc


def _reference_event(
    record: Mapping[str, Any],
    *,
    source_id: str,
    source_hash: str,
    locator: dict[str, Any],
    source_kind: str,
    bridge_package_dir: Path | None,
) -> tuple[NormalizedEvent | None, str | None]:
    if source_kind == "bridge_reference":
        decoded, error = _decode_bridge(record.get("encoded"), bridge_package_dir)
        key = _mapping(_mapping(decoded).get("key"))
        remote = _nonempty(_first(key, "remoteJid", "remote_jid")) or _nonempty(record.get("chatJid"))
        native_id = _nonempty(_first(key, "id", "messageId")) or _nonempty(record.get("messageId"))
        message = _mapping(_mapping(decoded).get("message"))
        message_time = _first(_mapping(decoded), "messageTimestamp", "message_timestamp")
        timestamp = normalize_time(message_time, basis="provider_timestamp", source_kind="bridge_reference")
        text = _payload_text(message)
        unresolved_reason = error or ("decoded_payload_without_text" if decoded is not None and text is None else None)
        direction = "out" if key.get("fromMe") is True else "in"
        event = _event(
            source_id=source_id,
            source_hash=source_hash,
            locator=locator,
            channel="whatsapp",
            account=_payload_account(record, decoded),
            chat_id=remote,
            native_id=native_id,
            event_id=_first(_mapping(decoded), "eventId", "event_id"),
            kind="message",
            direction=direction,
            sender_raw=_first(key, "participant", "participantAlt"),
            time=timestamp,
            text=text,
            media_kind=_media_kind(message),
            provenance_class=("authored" if direction == "out" else "native") if decoded is not None else "unknown",
            chat_kind=_chat_kind({"chat_id": remote}),
            source_kind="bridge_reference",
            native_type="WebMessageInfo",
            known_event_ids=tuple(
                str(item) for item in (_first(_mapping(decoded), "eventId", "event_id"),) if item
            ),
            source_authority=(
                "none_authored_outbound_context_only"
                if direction == "out"
                else "native_payload"
            )
            if decoded is not None
            else "unverified_native_reference",
            name_observations=_name_observations(
                _first(message, "pushName", "push_name", "verifiedName", "senderName"),
                _first(key, "participant", "participantAlt"),
                timestamp,
            ),
        )
        return event, unresolved_reason
    native_id = _native_id(record)
    event_id = _first(record, "event_id", "eventId")
    text = _payload_text(record)
    if native_id is None and not _nonempty(event_id) and text is None:
        return None, "unsupported_referenced_record"
    time = normalize_time(_first(record, "original_time", "timestamp"), basis="provider_timestamp", source_kind=source_kind)
    event = _event(
        source_id=source_id,
        source_hash=source_hash,
        locator=locator,
        channel=_first(record, "channel", "chat_channel"),
        account=_first(record, "account", "account_id"),
        chat_id=_first(record, "chat_id", "chat"),
        native_id=native_id,
        event_id=event_id,
        kind=_kind(_first(record, "record_type", "kind")),
        direction="in",
        sender_raw=_first(record, "sender_raw", "from", "sender_id"),
        time=time,
        text=text,
        provenance_class="native",
        source_kind=source_kind,
        source_authority="reference_metadata_only",
        name_observations=_name_observations(
            _first(record, "sender_name", "push_name", "display_name", "name"),
            _first(record, "sender_raw", "from", "sender_id"),
            time,
        ),
    )
    return event, None


def _parse_json_line(
    value: Mapping[str, Any],
    *,
    source_id: str,
    source_hash: str,
    relative: str,
    line: int,
    entry: Mapping[str, Any],
    bridge_package_dir: Path | None,
) -> tuple[NormalizedEvent | None, str | None, bool]:
    source_kind = _source_label(source_id, entry, relative)
    locator: dict[str, Any] = {"file": relative, "line": line}
    if value.get("_type") == "metadata":
        return None, "metadata_header", False
    if source_kind == "bridge_reference":
        if not isinstance(value.get("encoded"), str) or not value["encoded"]:
            return None, "bridge_reference_encoded_missing", False
        event, error = _reference_event(
            value,
            source_id=source_id,
            source_hash=source_hash,
            locator=locator,
            source_kind="bridge_reference",
            bridge_package_dir=bridge_package_dir,
        )
        return event, error, error is not None
    if "encoded" in value and "chatJid" in value:
        event, error = _reference_event(
            value,
            source_id=source_id,
            source_hash=source_hash,
            locator=locator,
            source_kind="bridge_reference",
            bridge_package_dir=bridge_package_dir,
        )
        return event, error, error is not None
    if isinstance(value.get("native"), Mapping):
        native = _mapping(value.get("native"))
        payload = _mapping(native.get("payload"))
        native_type = _first(native, "type", "eventType")
        kind = _kind(_first(value, "kind"), native_type)
        message_id = _native_id(payload, native)
        event_id = _first(native, "eventId", "event_id", value.get("eventId"))
        media = _mapping(value.get("media"))
        native_time = _first(payload, "messageTimestamp", "timestamp", "occurredAt", "occurred_ms")
        time = normalize_time(native_time, basis="native", source_kind="raw_archive")
        received = _first(value, "received_ms", "receivedAt", "received_at_ms")
        if received is not None:
            observed = normalize_time(received, basis="capture_time_approx", source_kind="raw_archive")
            time = {**time, "observed_ms": observed["observed_ms"]}
        event = _event(
            source_id=source_id,
            source_hash=source_hash,
            locator=locator,
            channel=_first(value, "channel") or (PurePosixPath(relative).parts[2] if len(PurePosixPath(relative).parts) > 2 and PurePosixPath(relative).parts[:2] == ("data", "raw") else None),
            account=_first(value, "account", "account_id") or _payload_account(native, payload),
            chat_id=_first(value, "chat_id") or _first(payload, "remoteJid", "chatJid", "chat_id"),
            native_id=message_id,
            event_id=event_id,
            archive_native_id=event_id,
            revision=_first(value, "revision") or _first(payload, "revision", "editRevision") or 1,
            kind=kind,
            direction=_first(value, "direction") or "unknown",
            sender_raw=_first(payload, "senderId", "participantJid", "participant", "sender", "author", "from"),
            principal=_first(payload, "principal"),
            time=time,
            text=_payload_text(payload, native),
            media_kind=_first(media, "kind", "type") or _first(payload, "mediaKind", "media_kind"),
            media_missing=bool(value.get("media_missing", False)),
            reply_target=_first(payload, "replyToMessageId", "reply_to_message_id") or _mapping(payload.get("contextInfo")).get("stanzaId"),
            edit_target=_first(payload, "targetMessageId", "target_message_id", "messageId") if kind == "edit" else None,
            delete_target=_first(payload, "targetMessageId", "target_message_id", "messageId") if kind == "delete" else None,
            provenance_class="native",
            chat_kind=_chat_kind(value, payload, native),
            native_evidence=_first(value, "native_evidence"),
            source_kind="raw_archive",
            native_type=native_type,
            known_event_ids=(str(event_id),) if event_id else (),
            source_authority="native_envelope",
            sender_id_raw=_first(payload, "senderId"),
            participant_jid_raw=_first(payload, "participantJid"),
            name_observations=_name_observations(
                _first(payload, "pushName", "push_name", "verifiedName", "senderName"),
                _first(payload, "senderId", "participantJid", "participant", "from"),
                time,
            ),
        )
        return event, None, False
    if _is_session_or_inbound(value, source_kind, relative):
        session = source_kind == "session_jsonl"
        path_parts = PurePosixPath(relative).stem.partition("_")
        channel = _nonempty(value.get("channel")) or (path_parts[0] if path_parts[1] else None)
        chat_id = _nonempty(value.get("chat_id")) or (path_parts[2] if path_parts[1] else None)
        role = _nonempty(value.get("role")) or "user"
        direction = "out" if role.casefold() in {"assistant", "bot", "yeoman"} else "in"
        timestamp = _first(value, "timestamp", "original_time")
        time = normalize_time(timestamp, basis="provider_timestamp", source_kind="session_jsonl" if session else "inbound_jsonl")
        message_id = _native_id(value)
        content = _first(value, "text", "content", "message")
        if isinstance(content, Mapping):
            content = _payload_text(content)
        event = _event(
            source_id=source_id,
            source_hash=source_hash,
            locator=locator,
            channel=channel,
            account=_first(value, "account", "account_id"),
            chat_id=chat_id,
            native_id=message_id,
            revision=_first(value, "revision") or 1,
            kind=_kind(_first(value, "kind", "type") or "message"),
            direction=direction,
            sender_raw=_first(value, "sender_id", "from", "sender"),
            principal=_first(value, "principal"),
            time=time,
            text=_payload_text(content),
            reply_target=_first(value, "reply_to_message_id", "replyToMessageId"),
            provenance_class="native" if not session else "unknown",
            chat_kind=_chat_kind(value),
            source_kind="session_jsonl" if session else "inbound_jsonl",
            source_authority="none_for_authored_outbound" if direction == "out" else "source_copy",
            name_observations=_name_observations(
                _first(value, "sender_name", "display_name", "name"),
                _first(value, "sender_id", "from", "sender"),
                time,
            ),
        )
        return event, None, False
    return None, "unknown_jsonl_schema", False


def _is_session_or_inbound(value: Mapping[str, Any], source_kind: str, relative: str) -> bool:
    if source_kind in {"session_jsonl", "inbound_jsonl"}:
        return any(key in value for key in ("text", "content", "message", "role", "message_id"))
    return "message_id" in value and any(key in value for key in ("timestamp", "content", "text"))


def _read_jsonl(
    *,
    path: Path,
    relative: str,
    source_id: str,
    source_hash: str,
    entry: Mapping[str, Any],
    bridge_package_dir: Path | None,
    report: dict[str, Any],
    source_report: dict[str, Any],
) -> list[NormalizedEvent]:
    events: list[NormalizedEvent] = []
    try:
        handle = path.open("rb")
    except OSError:
        _omit(report, source_report, "unreadable_file")
        return events
    with handle:
        for line_number, raw in enumerate(handle, start=1):
            locator = {"file": relative, "line": line_number}
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                _omit(report, source_report, "invalid_utf8")
                _unresolved(report, source_report, source_id, locator, "invalid_utf8")
                continue
            try:
                value = json.loads(text)
            except json.JSONDecodeError:
                _omit(report, source_report, "malformed_json_line")
                _unresolved(report, source_report, source_id, locator, "malformed_json_line")
                continue
            if not isinstance(value, Mapping):
                _omit(report, source_report, "unknown_jsonl_schema")
                continue
            event, reason, unresolved = _parse_json_line(
                value,
                source_id=source_id,
                source_hash=source_hash,
                relative=relative,
                line=line_number,
                entry=entry,
                bridge_package_dir=bridge_package_dir,
            )
            if event is None:
                _omit(report, source_report, reason or "unknown_jsonl_schema")
                continue
            events.append(event)
            _parsed(report, source_report)
            if unresolved:
                _unresolved(report, source_report, source_id, locator, reason or "decode_unavailable")
    return events


def _is_sqlite(path: Path) -> bool:
    try:
        with path.open("rb") as source:
            return source.read(16) == b"SQLite format 3\x00"
    except OSError:
        return False


def _read_sqlite(
    *,
    path: Path,
    relative: str,
    source_id: str,
    source_hash: str,
    report: dict[str, Any],
    source_report: dict[str, Any],
) -> list[NormalizedEvent]:
    if not _is_sqlite(path):
        _omit(report, source_report, "unsupported_sqlite_format")
        return []
    events: list[NormalizedEvent] = []
    try:
        with tempfile.TemporaryDirectory(prefix="yeoman-history-db-") as scratch:
            copied_main = Path(scratch) / path.name
            shutil.copyfile(path, copied_main)
            # A static snapshot is a three-file unit. Copy every present sidecar and open
            # only this temporary copy so SQLite cannot checkpoint or alter source bytes.
            for suffix in ("-wal", "-shm", "-journal"):
                sibling = Path(f"{path}{suffix}")
                if sibling.is_file():
                    shutil.copyfile(sibling, Path(f"{copied_main}{suffix}"))
            uri = copied_main.as_uri() + "?mode=ro"
            connection = sqlite3.connect(uri, uri=True)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only=ON")
            try:
                tables = [
                    str(row[0])
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
                    )
                ]
                recognized = {"events", "inbound_messages", "memory2_nodes", "effects"}
                for table in tables:
                    quoted_table = '"' + table.replace('"', '""') + '"'
                    count = int(connection.execute(f"SELECT COUNT(*) FROM {quoted_table}").fetchone()[0])
                    if table not in recognized:
                        report["unsupported_tables"].setdefault(table, {"count": 0, "reason": "unsupported_table"})["count"] += count
                        _omit(report, source_report, "unsupported_table")
                        continue
                    source_report["tables"][table] = count
                    for row in connection.execute(f"SELECT rowid AS __history_rowid__, * FROM {quoted_table}"):
                        values = _row(row)
                        try:
                            event = _sqlite_row_event(
                                table=table,
                                values=values,
                                source_id=source_id,
                                source_hash=source_hash,
                                relative=relative,
                            )
                        except (TypeError, ValueError, OverflowError):
                            _omit(report, source_report, "unsupported_row_schema")
                            _unresolved(
                                report,
                                source_report,
                                source_id,
                                {"file": relative, "table": table, "rowid": values.get("__history_rowid__")},
                                "unsupported_row_schema",
                            )
                            continue
                        if event is not None:
                            events.append(event)
                            _parsed(report, source_report)
                            if event.retention_status == "erased":
                                report["denial_evidence_count"] += 1
                                source_report["denial_evidence_count"] += 1
            finally:
                connection.close()
    except sqlite3.Error:
        _omit(report, source_report, "sqlite_read_error")
        _unresolved(report, source_report, source_id, {"file": relative}, "sqlite_read_error")
    return events


def _sqlite_row_event(
    *, table: str, values: Mapping[str, Any], source_id: str, source_hash: str, relative: str
) -> NormalizedEvent | None:
    if table == "events":
        event_id = _nonempty(values.get("event_id"))
        rowid = event_id or str(values.get("__history_rowid__") or "unknown")
        payload = _json(values.get("payload_json"))
        payload_map = _mapping(payload)
        native_id = _nonempty(values.get("source_message_id")) or _native_id(payload_map, _mapping(payload_map.get("payload")))
        observed = normalize_time(values.get("created_ms"), basis="capture_time_approx", source_kind="journal")
        occurred = normalize_time(values.get("occurred_ms"), basis="native", source_kind="journal")
        time = {**occurred, "observed_ms": observed["observed_ms"]}
        text = _payload_text(payload_map)
        locator = {"file": relative, "table": table, "event_id": rowid}
        raw_kind = values.get("kind")
        native_type = _first(payload_map, "type", "event_type", "native_type")
        kind = _kind(raw_kind, native_type)
        return _event(
            source_id=source_id,
            source_hash=source_hash,
            locator=locator,
            channel=values.get("channel"),
            account=values.get("account") or _payload_account(payload_map),
            chat_id=values.get("chat_id"),
            native_id=native_id,
            event_id=event_id,
            revision=values.get("revision") or 1,
            kind=kind,
            direction=values.get("direction") or "unknown",
            sender_raw=_first(payload_map, "senderId", "participantJid", "sender", "sender_id", "participant", "author"),
            principal=values.get("principal") or payload_map.get("principal"),
            time=time,
            text=text,
            reply_target=_first(payload_map, "reply_to_message_id", "replyToMessageId"),
            edit_target=_first(values, "target_message_id") or _first(payload_map, "target_message_id", "targetMessageId", "messageId") if kind == "edit" else None,
            delete_target=_first(values, "target_message_id") or _first(payload_map, "target_message_id", "targetMessageId", "messageId") if kind == "delete" else None,
            provenance_class="native" if payload is not None else "unknown",
            chat_kind=_chat_kind(values, payload_map),
            native_evidence=payload_map.get("native_evidence"),
            source_kind="journal",
            native_type=native_type,
            known_event_ids=(event_id,) if event_id else (),
            source_authority=(
                "payload_purged"
                if values.get("payload_purged_ms") is not None
                else ("journal_event" if payload is not None else "payload_missing")
            ),
            sender_id_raw=_first(payload_map, "senderId"),
            participant_jid_raw=_first(payload_map, "participantJid"),
            payload_purged_ms=values.get("payload_purged_ms"),
            name_observations=_name_observations(
                _first(payload_map, "pushName", "push_name", "verifiedName", "senderName"),
                _first(payload_map, "senderId", "participantJid", "sender", "participant", "author"),
                time,
            ),
        )
    if table == "inbound_messages":
        native_id = _native_id(values)
        timestamp = values.get("timestamp")
        occurred = normalize_time(timestamp, basis="provider_timestamp", source_kind="inbound_messages")
        observed = normalize_time(values.get("created_at"), basis="created_at_capture", source_kind="inbound_messages")
        time = {**occurred, "observed_ms": observed["observed_ms"]}
        locator = {"file": relative, "table": table, "message_id": native_id or values.get("__history_rowid__")}
        content = _first(values, "text", "content")
        return _event(
            source_id=source_id,
            source_hash=source_hash,
            locator=locator,
            channel=values.get("channel"),
            account=values.get("account"),
            chat_id=values.get("chat_id"),
            native_id=native_id,
            kind="message",
            direction="in",
            sender_raw=_first(values, "senderId", "sender_id", "from"),
            time=time,
            text=content,
            media_kind=values.get("type") if values.get("type") not in {"text", None} else None,
            reply_target=values.get("reply_to_message_id"),
            provenance_class="native",
            chat_kind=_chat_kind(values),
            source_kind="inbound_messages",
            source_authority="inbound_archive_copy",
            sender_id_raw=_first(values, "senderId", "sender_id"),
            participant_jid_raw=_first(values, "participantJid", "participant_jid"),
            name_observations=_name_observations(
                _first(values, "sender_name", "push_name", "display_name", "name"),
                _first(values, "senderId", "sender_id", "from"),
                time,
            ),
        )
    if table == "memory2_nodes":
        node_id = _nonempty(values.get("id"))
        if not node_id:
            return None
        native_id = _nonempty(values.get("source_message_id"))
        kind = _nonempty(values.get("kind")) or "unknown"
        original_kind = kind.casefold()
        is_deleted = bool(values.get("is_deleted"))
        legacy_original = original_kind in {"utterance", "whatsapp_message"}
        source_role = _nonempty(values.get("source_role"))
        normalized_role = source_role.casefold() if source_role else None
        authored_legacy = legacy_original and normalized_role in {"assistant", "bot", "yeoman", "system", "tool"}
        legacy_candidate = legacy_original and (normalized_role is None or normalized_role in {"user", "human"})
        created = normalize_time(values.get("created_at"), basis="created_at_capture", source_kind="memory2_nodes")
        locator = {"file": relative, "table": table, "node_id": node_id}
        text = None if is_deleted else _first(values, "content", "text")
        return _event(
            source_id=source_id,
            source_hash=source_hash,
            locator=locator,
            channel=values.get("channel"),
            account=values.get("account"),
            chat_id=values.get("chat_id"),
            native_id=native_id,
            event_id=f"legacy-node:{node_id}",
            revision=1,
            kind="message" if legacy_original else kind,
            direction=("out" if authored_legacy else "in") if legacy_candidate or authored_legacy else "unknown",
            sender_raw=values.get("sender_id"),
            time=created,
            text=text,
            provenance_class=("authored" if authored_legacy else "legacy_unverified") if legacy_candidate or authored_legacy else "derived_only",
            retention_status="erased" if is_deleted else "retained",
            chat_kind=_chat_kind(values),
            source_kind="memory2_nodes",
            known_event_ids=(f"legacy-node:{node_id}",),
            source_authority=(
                "denied_erased"
                if is_deleted
                else (
                    "none_authored_outbound_context_only"
                    if authored_legacy
                    else ("legacy_candidate" if legacy_candidate else "derived_only")
                )
            ),
            source_role=source_role,
            name_observations=_name_observations(
                _first(values, "sender_name", "display_name", "name"),
                _first(values, "sender_id", "sender_raw"),
                created,
            ),
        )
    if table == "effects":
        effect_id = _nonempty(values.get("effect_id"))
        payload = _mapping(_json(values.get("payload_json")))
        target = _mapping(_json(values.get("target_json")))
        state = _nonempty(values.get("state")) or "unknown"
        created = normalize_time(values.get("created_ms"), basis="capture_time_approx", source_kind="effects")
        purged = values.get("payload_purged_ms") is not None
        text = None if purged else _payload_text(payload)
        locator = {"file": relative, "table": table, "effect_id": effect_id or values.get("__history_rowid__")}
        return _event(
            source_id=source_id,
            source_hash=source_hash,
            locator=locator,
            channel=_first(target, "channel") or values.get("channel"),
            account=_first(target, "account"),
            chat_id=_first(target, "chat_id", "to", "target"),
            event_id=None,
            kind="message" if text is not None else str(values.get("payload_kind") or "effect"),
            direction="out",
            sender_raw=values.get("principal"),
            time=created,
            text=text,
            provenance_class="authored",
            retention_status="purged" if purged else state,
            source_kind="effects",
            transport_receipt="not_claimed",
            source_authority="none_authored_outbound_context_only",
            payload_purged_ms=values.get("payload_purged_ms"),
        )
    return None


def _omit(report: dict[str, Any], source_report: dict[str, Any], reason: str, count: int = 1) -> None:
    source_report["omitted_counts"][reason] = source_report["omitted_counts"].get(reason, 0) + count
    report["omitted_counts"][reason] = report["omitted_counts"].get(reason, 0) + count


def _parsed(report: dict[str, Any], source_report: dict[str, Any]) -> None:
    source_report["parsed_count"] += 1
    report["parsed_count"] += 1


def _unresolved(
    report: dict[str, Any], source_report: dict[str, Any], source_id: str, locator: Mapping[str, Any], reason: str
) -> None:
    report["unresolved_count"] += 1
    source_report["unresolved_count"] += 1
    report["unresolved"].append({"source_id": source_id, "locator": dict(locator), "reason": reason})


def _reference_members(
    *, entry: Mapping[str, Any]
) -> list[tuple[str, Path, str, tuple[int, ...]]]:
    source_root = Path(str(entry.get("source_path") or "")).expanduser()
    if not str(entry.get("source_path") or "") or not source_root.is_absolute():
        raise ValueError("reference-only source path is missing")
    if source_root.is_symlink():
        raise ValueError("reference source is a symlink")
    refs = _mapping(entry.get("record_metadata")).get("records")
    if not isinstance(refs, list):
        return []
    stats = entry.get("reference_file_stats")
    if not isinstance(stats, Mapping):
        raise ValueError("reference source file hashes are missing")
    referenced_lines: dict[str, set[int]] = defaultdict(set)
    for record in refs:
        if not isinstance(record, Mapping):
            continue
        locator = _mapping(record.get("locator"))
        relative = _safe_relative(locator.get("file"))
        line = locator.get("line")
        if isinstance(line, bool) or not isinstance(line, int) or line < 1:
            raise ValueError("invalid reference locator line")
        key = relative.as_posix()
        file_details = stats.get(key)
        if not isinstance(file_details, Mapping):
            raise ValueError("reference locator has no declared file hash")
        expected = file_details.get("sha256")
        if not isinstance(expected, str) or not _HASH.fullmatch(expected):
            raise ValueError("reference source hash is invalid")
        referenced_lines[key].add(line)
    result: list[tuple[str, Path, str, tuple[int, ...]]] = []
    for key, lines in sorted(referenced_lines.items()):
        details = stats.get(key)
        assert isinstance(details, Mapping)
        expected = str(details["sha256"])
        path_root = source_root if source_root.is_dir() else source_root.parent
        path = _checked_child(path_root, key if source_root.is_dir() else source_root.name)
        if _sha256(path) != expected:
            raise ValueError("reference source hash mismatch")
        result.append((key, path, expected, tuple(sorted(lines))))
    return result


def _read_reference_only(
    *, entry: Mapping[str, Any], bridge_package_dir: Path | None, report: dict[str, Any], source_report: dict[str, Any]
) -> list[NormalizedEvent]:
    source_id = str(entry["source_id"])
    events: list[NormalizedEvent] = []
    source_class = " ".join(
        (
            source_id.lower(),
            str(entry.get("source_class") or "").lower(),
            str(entry.get("source_path") or "").lower(),
        )
    )
    bridge_reference_source = _native_bridge_reference_tree(entry) or "bridge" in source_class
    if "raw" not in source_class and not bridge_reference_source:
        _omit(report, source_report, "reference_only_metadata_not_replayable")
        return events
    for relative, path, digest, line_numbers in _reference_members(entry=entry):
        wanted = set(line_numbers)
        seen: set[int] = set()
        with path.open("rb") as source:
            for current, raw in enumerate(source, start=1):
                if current not in wanted:
                    continue
                line_number = current
                seen.add(line_number)
                try:
                    value = json.loads(raw)
                except (UnicodeError, json.JSONDecodeError):
                    _omit(report, source_report, "malformed_referenced_line")
                    _unresolved(report, source_report, source_id, {"file": relative, "line": line_number}, "malformed_referenced_line")
                    continue
                if not isinstance(value, Mapping):
                    _omit(report, source_report, "unsupported_referenced_record")
                    _unresolved(report, source_report, source_id, {"file": relative, "line": line_number}, "unsupported_referenced_record")
                    continue
                source_kind = "raw_archive" if "raw" in source_class else "bridge_reference"
                event, error, unresolved = _parse_json_line(
                    value,
                    source_id=source_id,
                    source_hash=digest,
                    relative=relative,
                    line=line_number,
                    entry=entry,
                    bridge_package_dir=bridge_package_dir,
                )
                if event is None and source_kind == "raw_archive":
                    event, error = _reference_event(
                        value,
                        source_id=source_id,
                        source_hash=digest,
                        locator={"file": relative, "line": line_number},
                        source_kind=source_kind,
                        bridge_package_dir=bridge_package_dir,
                    )
                    unresolved = error is not None
                if event is None:
                    reason = error or "unsupported_referenced_record"
                    _omit(report, source_report, reason)
                    _unresolved(report, source_report, source_id, {"file": relative, "line": line_number}, reason)
                    continue
                events.append(event)
                _parsed(report, source_report)
                if unresolved:
                    _unresolved(report, source_report, source_id, event.locator if isinstance(event.locator, Mapping) else {}, error or "decode_unavailable")
        for line_number in sorted(wanted - seen):
            locator = {"file": relative, "line": line_number}
            _omit(report, source_report, "referenced_line_missing")
            _unresolved(report, source_report, source_id, locator, "referenced_line_missing")
    return events


def _merge_copies(events: Sequence[NormalizedEvent]) -> list[NormalizedEvent]:
    """Coalesce only matching message copies; preserve conflicting/edit variants."""
    groups: dict[tuple[Any, ...], list[NormalizedEvent]] = defaultdict(list)
    separate: list[NormalizedEvent] = []
    for event in events:
        if event.source_kind == "memory2_nodes" or not event.native_id:
            separate.append(event)
            continue
        key: tuple[Any, ...] = (
            event.channel,
            event.account,
            event.chat_id,
            event.native_id,
            event.kind,
            event.revision,
        )
        if event.kind in {"edit", "delete", "reaction", "receipt", "membership"}:
            key = (*key, event.event_id)
        group = groups[key]
        if not group:
            group.append(event)
            continue
        compatible = next(
            (item for item in group if item.text_hash == event.text_hash or item.text is None or event.text is None),
            None,
        )
        if compatible is None:
            separate.append(event)
            continue
        if compatible is event:
            group.append(event)
            continue
        group[group.index(compatible)] = _merge_pair(compatible, event)
    merged = [event for group in groups.values() for event in group]
    merged.extend(separate)
    return sorted(merged, key=lambda event: (event.source_id, json.dumps(event.locator, sort_keys=True), event.event_id))


def _merge_pair(left: NormalizedEvent, right: NormalizedEvent) -> NormalizedEvent:
    left_is_live = left.source_kind == "journal" or bool(left.known_event_ids)
    right_is_live = right.source_kind == "journal" or bool(right.known_event_ids)
    primary, secondary = (left, right) if left_is_live or not right_is_live else (right, left)
    text = primary.text if primary.text is not None else secondary.text
    provenance = primary.provenance_class
    if provenance == "unknown" and secondary.provenance_class != "unknown":
        provenance = secondary.provenance_class
    payload_purged_ms = (
        primary.payload_purged_ms
        if primary.payload_purged_ms is not None
        else secondary.payload_purged_ms
    )
    return replace(
        primary,
        text=text,
        text_hash=_hash_text(text),
        occurred_ms=primary.occurred_ms if primary.occurred_ms is not None else secondary.occurred_ms,
        observed_ms=primary.observed_ms if primary.observed_ms is not None else secondary.observed_ms,
        source_refs=_unique_json((*primary.source_refs, *secondary.source_refs)),
        copies=(*primary.copies, *secondary.copies),
        known_event_ids=tuple(dict.fromkeys((*primary.known_event_ids, *secondary.known_event_ids))),
        native_evidence=_unique_json((*primary.native_evidence, *secondary.native_evidence)),
        name_observations=_unique_json((*primary.name_observations, *secondary.name_observations)),
        provenance_class=provenance,
        source_authority=(
            "payload_purged"
            if payload_purged_ms is not None
            else primary.source_authority or secondary.source_authority
        ),
        payload_purged_ms=payload_purged_ms,
    )


def _unique_json(values: Sequence[Any]) -> tuple[Any, ...]:
    result: list[Any] = []
    seen: set[str] = set()
    for value in values:
        key = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
        if key not in seen:
            result.append(value)
            seen.add(key)
    return tuple(result)


def legacy_fidelity(events: Sequence[NormalizedEvent]) -> list[NormalizedEvent]:
    """Classify legacy text only against independent original inbound human copies."""
    independent: dict[tuple[Any, ...], list[str]] = defaultdict(list)
    original_authorities = {
        "native_envelope",
        "native_payload",
        "inbound_archive_copy",
        "journal_event",
        "source_copy",
    }
    for event in events:
        if event.source_kind == "memory2_nodes":
            continue
        copies = event.copies or (
            {
                "channel": event.channel,
                "account": event.account,
                "chat_id": event.chat_id,
                "native_id": event.native_id,
                "kind": event.kind,
                "direction": event.direction,
                "text_hash": event.text_hash,
                "provenance_class": event.provenance_class,
                "source_authority": event.source_authority,
            },
        )
        for copy in copies:
            identity = (
                copy.get("channel"),
                copy.get("account"),
                copy.get("chat_id"),
                copy.get("native_id"),
            )
            if (
                identity[0] is not None
                and identity[2] is not None
                and identity[3] is not None
                and copy.get("kind") == "message"
                and copy.get("direction") == "in"
                and copy.get("provenance_class") == "native"
                and copy.get("source_authority") in original_authorities
                and isinstance(copy.get("text_hash"), str)
            ):
                independent[identity].append(copy["text_hash"])
    result: list[NormalizedEvent] = []
    for event in events:
        if event.source_kind != "memory2_nodes" or event.retention_status == "erased" or event.source_authority != "legacy_candidate":
            result.append(event)
            continue
        matches = independent.get((event.channel, event.account, event.chat_id, event.native_id), ())
        if event.text_hash is not None and any(text_hash == event.text_hash for text_hash in matches):
            result.append(replace(event, provenance_class="recovered_text", verbatim_unverified=False))
        elif matches:
            result.append(replace(event, provenance_class="derived_only", verbatim_unverified=False))
        else:
            result.append(replace(event, provenance_class="legacy_unverified", verbatim_unverified=True))
    return result


def read_catalogued_events(
    *, collection: Path, bridge_package_dir: Path | None = None
) -> tuple[list[NormalizedEvent], dict[str, Any]]:
    """Read verified available sources from one preservation v2 collection."""
    bundle, manifest = _read_manifest(collection)
    complete = manifest.get("complete") is True
    report: dict[str, Any] = {
        "schema": "history-adapter-report-v1",
        "manifest_version": 2,
        "complete_manifest": complete,
        "collection_verdict": "verified_available_sources" if complete else "partial_collection",
        "parsed_count": 0,
        "unresolved_count": 0,
        "denial_evidence_count": 0,
        "omitted_counts": {},
        "unsupported_tables": {},
        "unresolved": [],
        "sources": [],
    }
    events: list[NormalizedEvent] = []
    bridge_dir = Path(bridge_package_dir).expanduser() if bridge_package_dir is not None else None
    for entry in manifest["sources"]:
        source_id = str(entry["source_id"])
        status = entry.get("status")
        source_report: dict[str, Any] = {
            "source_id": source_id,
            "status": status,
            "parsed_count": 0,
            "unresolved_count": 0,
            "denial_evidence_count": 0,
            "omitted_counts": {},
            "tables": {},
        }
        report["sources"].append(source_report)
        if status == "reference_only":
            events.extend(
                _read_reference_only(
                    entry=entry,
                    bridge_package_dir=bridge_dir,
                    report=report,
                    source_report=source_report,
                )
            )
            continue
        if status != "copied":
            _omit(report, source_report, str(entry.get("reason_code") or f"source_{status or 'missing'}"))
            report["collection_verdict"] = "partial_collection"
            continue
        try:
            files = _copied_files(bundle, entry)
        except ValueError:
            raise
        _checked_source_root(bundle, source_id)
        sqlite_paths = {
            relative
            for relative, _path, _digest in files
            if Path(relative).suffix.lower() in {".db", ".sqlite", ".sqlite3"}
        }
        for relative, path, digest in files:
            lower = relative.lower()
            is_bridge_json = lower.endswith(".json") and (
                "bridge" in source_id.lower()
                or "whatsapp-message-references" in lower
                or _native_bridge_reference_tree(entry)
            )
            if lower.endswith((".jsonl", ".ndjson")) or is_bridge_json:
                events.extend(
                    _read_jsonl(
                        path=path,
                        relative=relative,
                        source_id=source_id,
                        source_hash=digest,
                        entry=entry,
                        bridge_package_dir=bridge_dir,
                        report=report,
                        source_report=source_report,
                    )
                )
            elif Path(relative).suffix.lower() in {".db", ".sqlite", ".sqlite3"} or Path(relative).name == "data.db":
                events.extend(
                    _read_sqlite(
                        path=path,
                        relative=relative,
                        source_id=source_id,
                        source_hash=digest,
                        report=report,
                        source_report=source_report,
                    )
                )
            elif any(
                relative == f"{main}{suffix}"
                for main in sqlite_paths
                for suffix in ("-wal", "-shm", "-journal")
            ):
                continue
            else:
                _omit(report, source_report, "unsupported_source_format")
        if entry.get("kind") == "static_sqlite_triple":
            source_report["static_sqlite_triple"] = True
    if not complete:
        report["collection_verdict"] = "partial_collection"
        _omit(report, {"omitted_counts": {}}, "collection_manifest_incomplete")
    normalized = legacy_fidelity(_merge_copies(events))
    return normalized, report
