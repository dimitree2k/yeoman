"""Builders for fake source homes used by the converter tests (no real data)."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from yeoman_gateway.history.attestations import write_seed
from yeoman_gateway.history.layer1 import Origin, backfill_line


def make_db(path: Path, ddl: str, rows: dict[str, list[dict[str, Any]]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(ddl)
    for table, items in rows.items():
        for item in items:
            cols = ", ".join(item)
            marks = ", ".join("?" for _ in item)
            conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", list(item.values()))
    conn.commit()
    conn.close()
    return path

def write_jsonl(path: Path, records: list[Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [r if isinstance(r, str) else json.dumps(r, ensure_ascii=False) for r in records]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path

INBOUND_DDL = """
CREATE TABLE inbound_messages (
 channel TEXT, chat_id TEXT, message_id TEXT, participant TEXT, sender_id TEXT, text TEXT,
 timestamp INTEGER, created_at TEXT, sender_name TEXT, reply_to_message_id TEXT
);
"""


SAMPLE_GROUP = "4917623568044-1542142755@g.us"
FRANK_LID, FRANK_PN = "46918273106072@lid", "4917632625469@s.whatsapp.net"
T0 = 1_791_000_000_000


def _raw(kind, type_, payload, *, direction="in", received=T0, corr=""):
    return {"account": "default", "archive_version": 1, "channel": "whatsapp", "chat_id": SAMPLE_GROUP,
            "correlation_id": corr, "direction": direction, "kind": kind, "media": None,
            "native": {"type": type_, "payload": payload}, "received_ms": received}


def _bf(store, kind, payload, *, provenance="native", ms=T0, certainty="provider_timestamp",
        direction="in", channel="whatsapp", chat=SAMPLE_GROUP, skip=None):
    return backfill_line(channel=channel, kind=kind, provenance=provenance, time_certainty=certainty,
                         occurred_ms=ms, direction=direction, chat_id=chat, payload=payload,
                         origin=Origin(store, "x", "t", "1"), original={}, skip_reason=skip)


def sample_layer1(tmp_path: Path) -> tuple[Path, Path]:
    """A live raw root and a dev root exercising every merge rule (no real data)."""
    g, live, dev = SAMPLE_GROUP, tmp_path / "live", tmp_path / "dev"
    write_jsonl(live / "whatsapp/2026-10.jsonl", [
        _raw("message", "message", {"chatJid": g, "messageId": "AC1", "participantJid": FRANK_LID,
                                    "senderPhoneJid": FRANK_PN, "senderId": "46918273106072",
                                    "senderName": "Frank Taeger", "text": "hallo", "timestamp": T0 // 1000}),
        _raw("outbound_request", "react", {"chatJid": g, "messageId": "AC1", "emoji": "😂"},
             direction="out", received=T0 + 1000, corr="r1"),
        _raw("outbound_result", "react", {}, direction="out", received=T0 + 1500, corr="r1"),
        _raw("reaction", "reaction", {"chatJid": g, "emoji": "😂", "removed": False, "senderId": g,
                                      "targetMessageId": "AC1"}, received=T0 + 2000),
        _raw("edit", "edit", {"chatJid": g, "messageId": "AC1", "participantJid": FRANK_LID,
                              "text": "hallo!", "timestamp": T0 // 1000 + 10}),
        _raw("edit", "edit", {"chatJid": g, "messageId": "AC1", "participantJid": FRANK_LID,
                              "text": "hallo!!", "timestamp": T0 // 1000 + 20}),
        _raw("receipt", "receipt", {"chatJid": g, "messageId": "AC1", "status": "read"}),
        '{"kind": "mess',
    ])
    write_jsonl(dev / "backfill/journal.jsonl", [
        _bf("journal", "message", {"chatJid": g, "messageId": "3EB0P", "fromAssistant": True,
                                   "addressee": "4915140189391", "payloadPurged": True},
            direction="out", ms=T0 + 60_000, certainty="capture_time_approx"),
    ])
    write_jsonl(dev / "backfill/reply_context.jsonl", [
        _bf("reply_context", "message", {"chatJid": g, "messageId": "AC1", "participantJid": FRANK_PN,
                                         "senderId": "4917632625469", "senderName": "Frank Taeger",
                                         "text": "hallo"}),
    ])
    write_jsonl(dev / "backfill/session_jsonl.jsonl", [
        _bf("session_jsonl", "message", {"chatJid": g, "fromAssistant": True, "text": "Antwort an Matthias"},
            provenance="verbatim_unverified", direction="out", ms=T0 + 30_000, certainty="capture_time_approx"),
        _bf("session_jsonl", "message", {"chatJid": g, "messageId": "AC2", "senderId": "4915253696948",
                                         "senderName": "Carschten", "generatedDescription": "Eine Stahlbrücke",
                                         "mediaKind": "image"}, provenance="verbatim_unverified"),
        _bf("session_jsonl", "message", {"chatJid": "453897507", "text": "privet"}, channel="telegram",
            chat="453897507", provenance="verbatim_unverified"),
        _bf("session_jsonl", "session_meta", {}, skip="tool_trace", provenance="derived_only"),
    ])
    write_jsonl(dev / "backfill/memory.jsonl", [
        _bf("memory", "message", {"chatJid": g, "messageId": "AC1", "senderId": "4917632625469", "text": "hallo"},
            provenance="verbatim_unverified", certainty="capture_time_approx", ms=T0 + 5000),
        _bf("memory", "message", {"chatJid": g, "senderId": "4917632625469", "text": "Februar-Nachricht"},
            provenance="verbatim_unverified", certainty="capture_time_approx", ms=1_771_000_000_000),
    ])
    write_jsonl(dev / "backfill/knowledge.jsonl", [
        _bf("knowledge", "contact_record", {"contactRef": "945ae43e", "displayName": "Frank Taeger",
                                            "createdMs": 1, "isOwner": False},
            provenance="derived_only", channel="any", ms=None, certainty="unknown"),
        _bf("knowledge", "identifier_record", {"contactRef": "945ae43e", "identifier": FRANK_PN,
                                               "source": "contact_identifiers"},
            provenance="derived_only", ms=None, certainty="unknown"),
    ])
    write_seed(dev)
    return live, dev
