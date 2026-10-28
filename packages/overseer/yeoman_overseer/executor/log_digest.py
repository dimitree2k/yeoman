"""Daily log digest — content-free incident counts for the automatic log review.

Reads the Gateway's loguru log (current file plus rotated ``.gz`` files) from a
timestamp checkpoint and ``processing.db`` read-only, and writes one JSON digest
per run. Log lines carry text people wrote, so nothing from a message body is
kept: each line becomes a *signature* (level, module, function, the message's
leading words with numbers and ids stripped) plus allowlisted ``key=value``
fields whose values look like enum tokens.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import re
import sqlite3
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

SCHEMA_VERSION = 1
DEFAULT_LOOKBACK = timedelta(hours=24)
_TS_FORMAT = "%Y-%m-%d %H:%M:%S.%f"

_LINE_RE = re.compile(
    r"^(?P<ts>\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d{3}) \| (?P<level>[A-Z]+)\s*\| "
    r"(?P<module>[\w.]+):(?P<func>[\w<>]+):\d+ - (?P<msg>.*)$"
)
_TOOL_CALL_RE = re.compile(r"^Tool call: (?P<tool>[\w.-]+)\(")
_KV_RE = re.compile(r"\b(?P<key>[a-z_]+)=(?P<value>[^\s,;)]+)")
_SAFE_VALUE_RE = re.compile(r"^[A-Za-z][\w.\-/:]{0,47}$")
_ID_RE = re.compile(r"[0-9a-fA-F]{12,}|\d+")
# Only these keys keep their value in a signature; anything else may carry chat text.
SIGNATURE_KEYS = frozenset({
    "provider", "model", "type", "reason", "error_type", "tool", "kind",
    "capability", "mode", "status", "stage", "operation",
})
_INCIDENT_LEVELS = frozenset({"WARNING", "ERROR", "CRITICAL"})


@dataclass
class _Bucket:
    level: str
    module: str
    func: str
    count: int = 0
    first_seen: str = ""
    last_seen: str = ""


@dataclass
class LogScan:
    signatures: dict[str, _Bucket] = field(default_factory=dict)
    tool_calls: Counter[str] = field(default_factory=Counter)
    tool_failures: Counter[str] = field(default_factory=Counter)
    lines_read: int = 0
    last_ts: str = ""


@dataclass(frozen=True, slots=True)
class DigestResult:
    path: Path
    incidents: int
    new_incidents: int
    lines_read: int


def signature_of(level: str, module: str, func: str, msg: str) -> str:
    """Return a content-free signature for one log message."""
    head: list[str] = []
    for word in msg.split():
        if "=" in word or "(" in word or ":" in word or len(head) == 6:
            break
        head.append(_ID_RE.sub("N", word))
    fields = []
    for match in _KV_RE.finditer(msg):
        key, value = match["key"], match["value"]
        if key in SIGNATURE_KEYS and _SAFE_VALUE_RE.match(value):
            fields.append(f"{key}={_ID_RE.sub('N', value)}")
    text = " ".join(head)
    if fields:
        text = f"{text} {' '.join(sorted(set(fields)))}".strip()
    return f"{level} {module}:{func} {text}".strip()


def incident_id(signature: str) -> str:
    return hashlib.sha1(signature.encode("utf-8")).hexdigest()[:12]


def iter_log_files(log_dir: Path, *, since: datetime) -> list[Path]:
    """Rotated ``gateway.*.log.gz`` files touched after ``since``, oldest first, then the live log."""
    rotated = [
        path for path in sorted(log_dir.glob("gateway.*.log.gz"))
        if datetime.fromtimestamp(path.stat().st_mtime) >= since
    ]
    live = log_dir / "gateway.log"
    return rotated + ([live] if live.exists() else [])


def _read_lines(path: Path) -> Iterator[str]:
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8", errors="replace") as handle:
        yield from handle


def scan_logs(files: list[Path], *, since: datetime, until: datetime) -> LogScan:
    scan = LogScan()
    since_text = since.strftime(_TS_FORMAT)[:23]
    until_text = until.strftime(_TS_FORMAT)[:23]
    for path in files:
        for line in _read_lines(path):
            match = _LINE_RE.match(line.rstrip("\n"))
            if match is None:
                continue
            ts = match["ts"]
            if ts <= since_text or ts > until_text:
                continue
            scan.lines_read += 1
            scan.last_ts = max(scan.last_ts, ts)
            level, msg = match["level"], match["msg"]
            tool_call = _TOOL_CALL_RE.match(msg)
            if tool_call is not None:
                scan.tool_calls[tool_call["tool"]] += 1
                continue
            if level not in _INCIDENT_LEVELS:
                continue
            if msg.startswith("tool_result_failed "):
                fields = dict((m["key"], m["value"]) for m in _KV_RE.finditer(msg))
                scan.tool_failures[f"{fields.get('tool', '?')}:{fields.get('kind', '?')}"] += 1
            sig = signature_of(level, match["module"], match["func"], msg)
            bucket = scan.signatures.get(sig)
            if bucket is None:
                bucket = scan.signatures[sig] = _Bucket(
                    level=level, module=match["module"], func=match["func"], first_seen=ts
                )
            bucket.count += 1
            bucket.last_seen = ts
    return scan


def scan_processing_db(db_path: Path, *, since: datetime) -> dict[str, object]:
    """Aggregate decision, effect and reply-sorter outcomes; ids only, never payloads."""
    if not db_path.exists():
        return {"available": False}
    since_ms = int(since.timestamp() * 1000)
    uri = f"file:{db_path}?mode=ro"
    with sqlite3.connect(uri, uri=True, timeout=5.0) as conn:
        denied = conn.execute(
            "SELECT stage, reason, COUNT(*) FROM decisions "
            "WHERE created_ms > ? AND outcome = 'deny' GROUP BY 1, 2 ORDER BY 3 DESC",
            (since_ms,),
        ).fetchall()
        effects = conn.execute(
            "SELECT capability, state, COUNT(*) FROM effects "
            "WHERE created_ms > ? GROUP BY 1, 2 ORDER BY 1, 2",
            (since_ms,),
        ).fetchall()
        sorter = conn.execute(
            "SELECT status, COUNT(*) FROM short_reply_decisions "
            "WHERE created_ms > ? GROUP BY 1 ORDER BY 1",
            (since_ms,),
        ).fetchall()
        reactions = conn.execute(
            "SELECT channel, chat_id, message_id, emoji FROM short_reply_decisions "
            "WHERE created_ms > ? AND status = 'react' ORDER BY created_ms",
            (since_ms,),
        ).fetchall()
    return {
        "available": True,
        "denied_decisions": [
            {"stage": stage, "reason": _ID_RE.sub("N", reason), "count": count}
            for stage, reason, count in denied
        ],
        "effects": [
            {"capability": capability, "state": state, "count": count}
            for capability, state, count in effects
        ],
        "reply_sorter": {status: count for status, count in sorter},
        # The fixer may open these by id (authorized history read) to judge whether
        # a reaction was right; the digest itself holds no message text.
        "reply_sorter_reactions": [
            {"channel": channel, "chat_id": chat_id, "message_id": message_id, "emoji": emoji}
            for channel, chat_id, message_id, emoji in reactions
        ],
    }


def _incidents(scan: LogScan, db: dict[str, object]) -> list[dict[str, object]]:
    incidents: list[dict[str, object]] = []
    for sig, bucket in scan.signatures.items():
        incidents.append({
            "id": incident_id(sig),
            "kind": "log_signature",
            "signature": sig,
            "level": bucket.level,
            "module": bucket.module,
            "function": bucket.func,
            "count": bucket.count,
            "first_seen": bucket.first_seen,
            "last_seen": bucket.last_seen,
        })
    for key, count in scan.tool_failures.items():
        tool, _, kind = key.partition(":")
        sig = f"tool_failure {tool} {kind}"
        incidents.append({
            "id": incident_id(sig),
            "kind": "tool_failure",
            "signature": sig,
            "tool": tool,
            "failure_kind": kind,
            "count": count,
            "calls": scan.tool_calls.get(tool, 0),
        })
    for row in db.get("denied_decisions", []) or []:
        assert isinstance(row, dict)
        sig = f"decision_denied {row['stage']} {row['reason']}"
        incidents.append({
            "id": incident_id(sig),
            "kind": "decision_denied",
            "signature": sig,
            "count": row["count"],
        })
    incidents.sort(key=lambda item: (-int(str(item["count"])), str(item["signature"])))
    return incidents


def _load_json(path: Path, default: dict[str, object]) -> dict[str, object]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default
    return data if isinstance(data, dict) else default


def _write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)


def build_log_digest(
    *,
    log_dir: Path,
    processing_db: Path,
    out_dir: Path,
    now: datetime | None = None,
) -> DigestResult:
    """Scan everything since the last checkpoint and write ``digests/<date>.json``.

    ``index.json`` remembers every incident id across runs (first and last day,
    days seen), so the fixer can tell a new problem from a recurring one after
    the logs themselves have rotated away.
    """
    now = now or datetime.now()
    state_path = out_dir / "state.json"
    state = _load_json(state_path, {})
    since = now - DEFAULT_LOOKBACK
    if isinstance(state.get("last_ts"), str):
        try:
            since = max(datetime.strptime(str(state["last_ts"]), _TS_FORMAT), now - timedelta(days=14))
        except ValueError:
            pass

    scan = scan_logs(iter_log_files(log_dir, since=since), since=since, until=now)
    db = scan_processing_db(processing_db, since=since)
    incidents = _incidents(scan, db)

    index_path = out_dir / "index.json"
    index = _load_json(index_path, {"incidents": {}})
    known = index.setdefault("incidents", {})
    assert isinstance(known, dict)
    today = now.date().isoformat()
    new = 0
    for incident in incidents:
        entry = known.get(incident["id"])
        if entry is None:
            new += 1
            incident["new"] = True
            known[str(incident["id"])] = {
                "signature": incident["signature"],
                "first_day": today,
                "last_day": today,
                "days_seen": 1,
                "total": incident["count"],
            }
        else:
            incident["new"] = False
            incident["first_day"] = entry["first_day"]
            if entry["last_day"] != today:
                entry["days_seen"] = int(entry["days_seen"]) + 1
            entry["last_day"] = today
            entry["total"] = int(entry["total"]) + int(str(incident["count"]))
            incident["days_seen"] = entry["days_seen"]

    digest = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": now.isoformat(timespec="seconds"),
        "window": {"since": since.isoformat(timespec="seconds"), "until": now.isoformat(timespec="seconds")},
        "lines_read": scan.lines_read,
        "tool_calls": dict(scan.tool_calls.most_common()),
        "processing": db,
        "incidents": incidents,
    }
    path = out_dir / "digests" / f"{today}.json"
    if path.exists():
        path = out_dir / "digests" / f"{today}-{now.strftime('%H%M%S')}.json"
    _write_json(path, digest)
    _write_json(index_path, index)
    if scan.last_ts:
        state["last_ts"] = scan.last_ts + "000"
    else:
        state["last_ts"] = now.strftime(_TS_FORMAT)
    _write_json(state_path, state)
    return DigestResult(path=path, incidents=len(incidents), new_incidents=new, lines_read=scan.lines_read)
