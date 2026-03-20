"""Tool registry: definitions for the Anthropic API and dispatch map."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from yeoman_overseer.audit.logger import AuditLogger
from yeoman_overseer.comms.cascading import CascadingComms


@dataclass
class ToolContext:
    """Dependencies injected into every tool."""
    yeoman_home: Path
    source_dir: Path
    audit: AuditLogger
    comms: CascadingComms
    data_dir: Path


TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "read_file",
        "description": "Read a file under ~/.yeoman/ or ~/Documents/yeoman/. Sensitive paths (.env, secrets/, .git/) are blocked.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
    {
        "name": "query_db",
        "description": "Run a SELECT query on a SQLite database. Connection is read-only at the engine level.",
        "input_schema": {
            "type": "object",
            "properties": {
                "db_path": {"type": "string"},
                "query": {"type": "string"},
            },
            "required": ["db_path", "query"],
        },
    },
    {
        "name": "query_memory",
        "description": "Full-text search on the semantic memory database.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer", "default": 10},
            },
            "required": ["query"],
        },
    },
    {
        "name": "check_health",
        "description": "Run a built-in health check by name.",
        "input_schema": {
            "type": "object",
            "properties": {
                "check": {"type": "string"},
                "target": {"type": "string"},
            },
            "required": ["check", "target"],
        },
    },
    {
        "name": "git_log",
        "description": "Read recent git log from the source repo or internal overseer git.",
        "input_schema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string", "enum": ["source", "internal"]},
                "limit": {"type": "integer", "default": 20},
            },
            "required": ["repo"],
        },
    },
    {
        "name": "send_alert",
        "description": "Send an alert message via cascading comms (Telegram → SMTP → log).",
        "input_schema": {
            "type": "object",
            "properties": {"message": {"type": "string"}},
            "required": ["message"],
        },
    },
]


async def dispatch(name: str, args: dict[str, Any], ctx: ToolContext) -> Any:
    """Dispatch a tool call by name. Raises ValueError for unknown tools."""
    from yeoman_overseer.agent.tools import (
        check_health, git_log, query_db,
        query_memory, read_file, send_alert,
    )
    handlers: dict[str, Any] = {
        "read_file": read_file.execute,
        "query_db": query_db.execute,
        "query_memory": query_memory.execute,
        "check_health": check_health.execute,
        "git_log": git_log.execute,
        "send_alert": send_alert.execute,
    }
    if name not in handlers:
        raise ValueError(f"Unknown tool: {name!r}")
    result = handlers[name](args, ctx)
    if hasattr(result, "__await__"):  # handle async tool handlers (e.g. send_alert)
        result = await result
    return result
