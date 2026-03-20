"""Prune memory.db entries by age/salience -- snapshot-first."""
from __future__ import annotations

import shutil
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

from yeoman_overseer.audit.logger import AuditEntry


def prune_memory(
    *,
    age_days: int | None = None,
    salience_below: float | None = None,
    domain: str | None = None,
    ctx: object,
) -> dict:
    """Delete memory nodes matching criteria after snapshotting the DB first."""
    db_path: Path = ctx.memory_db

    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    snapshot = db_path.with_name(f"{db_path.name}.snapshot-{ts}")
    shutil.copy2(db_path, snapshot)

    cutoff_ts = time.time() - (age_days * 86400) if age_days is not None else None

    clauses: list[str] = []
    params: list = []

    if cutoff_ts is not None:
        clauses.append("created_at < ?")
        params.append(cutoff_ts)
    if salience_below is not None:
        clauses.append("salience < ?")
        params.append(salience_below)
    if domain is not None:
        clauses.append("domain = ?")
        params.append(domain)

    if not clauses:
        return {"ok": False, "error": "no criteria provided"}

    where = " AND ".join(clauses)
    conn = sqlite3.connect(db_path)
    try:
        cursor = conn.execute(f"DELETE FROM memory2_nodes WHERE {where}", params)
        rows_deleted = cursor.rowcount
        conn.commit()
    finally:
        conn.close()

    ctx.audit.append(AuditEntry(
        runbook=ctx.runbook_name,
        trigger="llm",
        action="prune_memory",
        target=str(db_path),
        result=f"deleted {rows_deleted} rows",
        duration_ms=0,
        escalated_to_llm=True,
        domain=ctx.domain,
    ))

    return {"ok": True, "rows_deleted": rows_deleted, "snapshot": str(snapshot)}
