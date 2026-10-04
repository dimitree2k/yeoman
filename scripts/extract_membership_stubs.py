"""Read preserved bridge references into a private, offline membership stub report."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from yeoman_gateway.knowledge._history_adapters import _decode_bridge

_COLD = Path("/home/dm/.yeoman/backups/preservation/2026-10-03-cold/data/bridge/whatsapp-message-references")
_ROLLING = Path("/home/dm/.yeoman/backups/preservation/bridge-references/files")
_OUTPUT = Path("/home/dm/.yeoman/.worktrees/membership-capture-runtime/session-context/2026-10-04-membership-capture/historical-membership-stubs-dry-run.json")
_ACTIONS = {
    "GROUP_PARTICIPANT_ADD": "add",
    "GROUP_PARTICIPANT_REMOVE": "remove",
    "GROUP_PARTICIPANT_LEAVE": "remove",
    "GROUP_PARTICIPANT_INVITE": "add",
    "GROUP_PARTICIPANT_PROMOTE": "promote",
    "GROUP_PARTICIPANT_DEMOTE": "demote",
}


def _checked_root(value: Path, suffix: tuple[str, ...]) -> Path:
    supplied = Path(value).expanduser()
    if ".." in supplied.parts:
        raise ValueError("path traversal in bridge reference source root")
    root = supplied.absolute()
    if tuple(root.parts[-len(suffix):]) != suffix or any(part in {"raw", "raw-spool"} for part in root.parts):
        raise ValueError("expected bridge reference source root")
    current = Path(root.anchor)
    for part in root.parts[1:]:
        current /= part
        if current.is_symlink():
            raise ValueError("symlink in bridge reference source path")
    if not root.is_dir():
        raise ValueError("bridge reference source root missing")
    return root


def _identifier(value: Any) -> str | None:
    return value if isinstance(value, str) and 0 < len(value) <= 256 and "@" in value else None


def _message_id(value: Any) -> str | None:
    return value if isinstance(value, str) and 0 < len(value) <= 256 else None


def extract_membership_stubs(cold_root: Path, rolling_root: Path, bridge_package_dir: Path) -> dict[str, Any]:
    """Scan direct JSON reference files without modifying either source tree."""
    roots = (
        ("cold", _checked_root(cold_root, ("data", "bridge", "whatsapp-message-references"))),
        ("rolling", _checked_root(rolling_root, ("bridge-references", "files"))),
    )
    counts = {
        "source_files": 0, "membership_stubs": 0, "non_membership": 0,
        "malformed": 0, "unreadable": 0, "decode_failures": 0,
        "duplicate_groups": 0, "conflict_groups": 0,
    }
    stubs: list[dict[str, Any]] = []
    decoded_cache: dict[str, tuple[Any, str | None]] = {}
    for label, root in roots:
        for path in sorted(root.iterdir()):
            if path.suffix != ".json":
                continue
            if path.is_symlink():
                raise ValueError("symlink in bridge reference source tree")
            if not path.is_file() or path.parent != root:
                continue
            counts["source_files"] += 1
            try:
                raw = path.read_bytes()
            except OSError:
                counts["unreadable"] += 1
                continue
            source_hash = hashlib.sha256(raw).hexdigest()
            try:
                record = json.loads(raw)
            except (UnicodeError, json.JSONDecodeError):
                counts["malformed"] += 1
                continue
            if not isinstance(record, dict) or not isinstance(record.get("encoded"), str):
                counts["malformed"] += 1
                continue
            encoded = record["encoded"]
            try:
                payload_hash = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
            except UnicodeError:
                counts["malformed"] += 1
                continue
            if payload_hash not in decoded_cache:
                try:
                    decoded_cache[payload_hash] = _decode_bridge(encoded, bridge_package_dir)
                except (UnicodeError, ValueError):
                    decoded_cache[payload_hash] = (None, "offline_decoder_failed")
            decoded, error = decoded_cache[payload_hash]
            if error or not isinstance(decoded, dict):
                counts["decode_failures"] += 1
                continue
            stub_type = decoded.get("messageStubType")
            if stub_type not in _ACTIONS:
                counts["non_membership"] += 1
                continue
            key = decoded.get("key") if isinstance(decoded.get("key"), dict) else {}
            participants = decoded.get("messageStubParameters")
            stubs.append({
                "source_locator": f"{label}/{path.name}",
                "source_sha256": source_hash,
                "payload_sha256": payload_hash,
                "chat_jid": _identifier(key.get("remoteJid")) or _identifier(record.get("chatJid")),
                "native_message_id": _message_id(key.get("id")) or _message_id(record.get("messageId")),
                "provider_timestamp": str(decoded["messageTimestamp"]) if isinstance(decoded.get("messageTimestamp"), (str, int)) and str(decoded["messageTimestamp"]).isdigit() else None,
                "stub_type": stub_type,
                "action": _ACTIONS[stub_type],
                "actor": _identifier(key.get("participant")) or _identifier(decoded.get("participant")),
                "participants": [jid for item in participants if (jid := _identifier(item))] if isinstance(participants, list) else [],
                "duplicate_locators": [],
                "conflicting_payloads": False,
            })
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in stubs:
        if row["chat_jid"] and row["native_message_id"]:
            groups.setdefault((row["chat_jid"], row["native_message_id"]), []).append(row)
    for rows in groups.values():
        if len(rows) < 2:
            continue
        counts["duplicate_groups"] += 1
        conflict = len({row["payload_sha256"] for row in rows}) > 1
        counts["conflict_groups"] += int(conflict)
        locators = [row["source_locator"] for row in rows]
        for row in rows:
            row["duplicate_locators"] = locators
            row["conflicting_payloads"] = conflict
    counts["membership_stubs"] = len(stubs)
    return {"counts": counts, "stubs": stubs}


if __name__ == "__main__":
    report = extract_membership_stubs(_COLD, _ROLLING, Path(__file__).resolve().parents[1] / "packages" / "bridge")
    _OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    _OUTPUT.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
