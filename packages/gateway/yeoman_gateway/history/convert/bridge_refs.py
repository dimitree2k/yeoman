"""Bridge message references: encoded native WhatsApp messages the bridge kept for seven days."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from typing import Any

from ..ids import classify
from ..layer1 import Origin, backfill_line
from .common import compact, epoch_or_iso_to_ms

Decoder = Callable[[list[tuple[str, str]]], dict[str, dict[str, Any]]]

_BATCH_SCRIPT = r"""
import { proto } from '@whiskeysockets/baileys/WAProto/index.js';
import readline from 'node:readline';
const rl = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });
for await (const line of rl) {
  if (!line.trim()) continue;
  const { name, encoded } = JSON.parse(line);
  try {
    const bytes = Buffer.from(encoded, 'base64');
    if (bytes.length === 0) throw new Error('empty payload');
    const message = proto.WebMessageInfo.decode(bytes);
    const value = proto.WebMessageInfo.toObject(message, {
      longs: String, enums: String, bytes: String, defaults: false,
    });
    process.stdout.write(JSON.stringify({ name, value }) + '\n');
  } catch (error) {
    process.stdout.write(JSON.stringify({ name, error: String((error && error.message) || error) }) + '\n');
  }
}
"""
_MEDIA_KEYS = {"imageMessage": "image", "videoMessage": "video", "documentMessage": "document",
               "documentWithCaptionMessage": "document", "audioMessage": "audio",
               "stickerMessage": "sticker"}
_WRAPPERS = ("ephemeralMessage", "viewOnceMessage", "viewOnceMessageV2", "documentWithCaptionMessage")


def node_batch_decoder(bridge_package_dir: Path) -> Decoder:
    package = Path(bridge_package_dir).expanduser()

    def decode(items: list[tuple[str, str]]) -> dict[str, dict[str, Any]]:
        stdin = "".join(json.dumps({"name": n, "encoded": e}) + "\n" for n, e in items)
        result = subprocess.run(
            ["node", "--input-type=module", "-e", _BATCH_SCRIPT], cwd=package,
            input=stdin.encode("utf-8"), capture_output=True, timeout=1800, check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.decode("utf-8", errors="replace")[-2000:])
        decoded: dict[str, dict[str, Any]] = {}
        for line in result.stdout.decode("utf-8").splitlines():
            record = json.loads(line)
            decoded[record["name"]] = record
        return decoded

    return decode


def _unwrap(message: dict[str, Any]) -> dict[str, Any]:
    for wrapper in _WRAPPERS:
        inner = (message.get(wrapper) or {}).get("message")
        if isinstance(inner, dict):
            return _unwrap(inner)
    return message


def _content(message: dict[str, Any]) -> tuple[str | None, str | None, str | None]:
    message = _unwrap(message)
    if "conversation" in message:
        return message["conversation"], None, None
    extended = message.get("extendedTextMessage")
    if isinstance(extended, dict):
        return extended.get("text"), None, (extended.get("contextInfo") or {}).get("stanzaId")
    for key, kind in _MEDIA_KEYS.items():
        body = message.get(key)
        if isinstance(body, dict):
            return body.get("caption"), kind, (body.get("contextInfo") or {}).get("stanzaId")
    return None, None, None


def _sender(key: dict[str, Any]) -> tuple[str | None, str | None, str | None]:
    values = [key.get(k) for k in ("participant", "participantAlt", "participantPn", "participantLid")]
    if not any(values) and not key.get("fromMe"):
        values = [key.get("remoteJid"), key.get("remoteJidAlt")]
    values = [v for v in values if v]
    lid = next((v for v in values if (c := classify(v)) is not None and c.kind == "lid"), None)
    pn = next((v for v in values if (c := classify(v)) is not None and c.kind == "pn_jid"), None)
    return lid, pn, values[0] if values else None


def convert_bridge_refs(dirs: Sequence[Path], decode: Decoder | None) -> Iterator[dict[str, Any]]:
    files: dict[str, Path] = {}
    for folder in dirs:
        if folder.is_dir():
            for path in sorted(folder.glob("*.json")):
                files.setdefault(path.name, path)
    records = {name: json.loads(files[name].read_text(encoding="utf-8")) for name in sorted(files)}
    batch = [(n, r["encoded"]) for n, r in records.items()
             if isinstance(r.get("encoded"), str) and r["encoded"]]
    decoded = decode(batch) if decode is not None and batch else {}
    for name, record in records.items():
        yield _line(name, files[name], record, decoded.get(name), decode is not None)


def _line(name: str, path: Path, record: dict[str, Any], decoded: dict[str, Any] | None,
          has_decoder: bool) -> dict[str, Any]:
    origin = Origin("bridge_refs", path.as_posix(), "message_reference", name)
    chat = record.get("chatJid")

    def skip(reason: str) -> dict[str, Any]:
        stored, _ = epoch_or_iso_to_ms(record.get("storedAtMs"))
        return backfill_line(channel="whatsapp", kind="bridge_reference", provenance="native",
                             time_certainty="capture_time_approx" if stored else "unknown",
                             occurred_ms=stored, direction=None, chat_id=chat, payload={},
                             origin=origin, original=record, skip_reason=reason)

    if not isinstance(record.get("encoded"), str) or not record["encoded"]:
        return skip("encoded_missing")
    if not has_decoder:
        return skip("decoder_not_supplied")
    if decoded is None or "value" not in decoded:
        return skip("decode_failed")
    value = decoded["value"]
    key = value.get("key") or {}
    message = _unwrap(value.get("message") or {})
    chat = key.get("remoteJid") or chat
    from_me = bool(key.get("fromMe"))
    lid, pn, first = _sender(key)
    occurred_ms, certainty = epoch_or_iso_to_ms(value.get("messageTimestamp"))
    if occurred_ms is None:
        occurred_ms, _ = epoch_or_iso_to_ms(record.get("storedAtMs"))
        certainty = "capture_time_approx" if occurred_ms else "unknown"
    base: dict[str, Any] = {"chatJid": chat, "participantJid": lid, "senderPhoneJid": pn,
                            "senderId": first, "senderName": value.get("pushName"),
                            "fromAssistant": True if from_me else None}
    if "reactionMessage" in message:
        reaction = message["reactionMessage"] or {}
        emoji = reaction.get("text") or ""
        kind = "reaction"
        payload = compact({**base, "nativeEventId": key.get("id"),
                           "targetMessageId": (reaction.get("key") or {}).get("id"),
                           "emoji": emoji or None})
        payload["removed"] = emoji == ""
    elif "protocolMessage" in message:
        protocol = message["protocolMessage"] or {}
        target = (protocol.get("key") or {}).get("id")
        if protocol.get("type") == "REVOKE":
            kind, payload = "delete", compact({**base, "nativeEventId": key.get("id"),
                                                "targetMessageId": target})
        elif protocol.get("type") == "MESSAGE_EDIT":
            text, _, _ = _content(protocol.get("editedMessage") or {})
            kind, payload = "edit", compact({**base, "nativeEventId": key.get("id"),
                                              "targetMessageId": target, "text": text})
        else:
            return skip(f"protocol:{protocol.get('type')}")
    else:
        text, media, reply = _content(message)
        kind = "message"
        payload = compact({**base, "messageId": key.get("id"), "text": text, "mediaKind": media,
                           "replyToMessageId": reply})
    return backfill_line(channel="whatsapp", kind=kind, provenance="native", time_certainty=certainty,
                         occurred_ms=occurred_ms, direction="out" if from_me else "in", chat_id=chat,
                         payload=payload, origin=origin, original=record)
