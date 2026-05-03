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
    # Phase 3 additions
    sandbox: object | None = None       # Sandbox instance (or None if bwrap unavailable)
    shell_timeout_s: int = 60
    memory_db: Path | None = None       # used by prune_memory
    runbook_name: str = ""              # current runbook name for audit
    domain: str = ""                    # current runbook domain for audit
    git: object | None = None           # InternalGit for write_file/edit_file commits


TOOL_DEFINITIONS: list[dict[str, Any]] = [
    # Phase 2 tools
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
                "threshold": {"type": "number"},
                "query": {"type": "string"},
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
    # Phase 3 tools
    {
        "name": "write_file",
        "description": "Write content to a file. Path must be under ~/.yeoman/ or ~/Documents/yeoman/. Blocked: .git/, .env, secrets/, systemd/, runbooks/.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "edit_file",
        "description": "Replace a string in an existing file. Same path restrictions as write_file.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "old_string": {"type": "string"},
                "new_string": {"type": "string"},
            },
            "required": ["path", "old_string", "new_string"],
        },
    },
    {
        "name": "prune_memory",
        "description": "Delete memory.db entries matching age/salience/domain criteria. Always takes a snapshot first.",
        "input_schema": {
            "type": "object",
            "properties": {
                "age_days": {"type": "integer"},
                "salience_below": {"type": "number"},
                "domain": {"type": "string"},
            },
        },
    },
    {
        "name": "run_tests",
        "description": "Execute pytest inside the bubblewrap sandbox.",
        "input_schema": {
            "type": "object",
            "properties": {
                "source_root": {"type": "string"},
            },
        },
    },
    {
        "name": "git_revert",
        "description": "Revert a single commit in the internal overseer git by SHA.",
        "input_schema": {
            "type": "object",
            "properties": {
                "sha": {"type": "string"},
            },
            "required": ["sha"],
        },
    },
    {
        "name": "dry_run_runbook",
        "description": "Validate a runbook file without executing it.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "shell",
        "description": "Run a command inside the bubblewrap sandbox (no network, isolated tmpdir).",
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string"},
            },
            "required": ["command"],
        },
    },
]


async def dispatch(name: str, args: dict[str, Any], ctx: ToolContext) -> Any:
    """Dispatch a tool call by name. Raises ValueError for unknown tools."""
    from yeoman_overseer.agent.tools import (
        check_health,
        dry_run_runbook,
        edit_file,
        git_log,
        git_revert,
        prune_memory,
        query_db,
        query_memory,
        read_file,
        run_tests,
        send_alert,
        shell,
        write_file,
    )
    handlers: dict[str, Any] = {
        # Phase 2 tools
        "read_file": lambda a, c: read_file.execute(a, c),
        "query_db": lambda a, c: query_db.execute(a, c),
        "query_memory": lambda a, c: query_memory.execute(a, c),
        "check_health": lambda a, c: check_health.execute(a, c),
        "git_log": lambda a, c: git_log.execute(a, c),
        "send_alert": lambda a, c: send_alert.execute(a, c),
        # Phase 3 tools
        "write_file": lambda a, c: write_file.write_file(a["path"], a["content"], c),
        "edit_file": lambda a, c: edit_file.edit_file(a["path"], a["old_string"], a["new_string"], c),
        "prune_memory": lambda a, c: prune_memory.prune_memory(
            age_days=a.get("age_days"), salience_below=a.get("salience_below"),
            domain=a.get("domain"), ctx=c,
        ),
        "run_tests": lambda a, c: run_tests.run_tests(
            source_root=Path(a["source_root"]) if a.get("source_root") else None, ctx=c,
        ),
        "git_revert": lambda a, c: git_revert.git_revert(a["sha"], ctx=c),
        "dry_run_runbook": lambda a, c: dry_run_runbook.dry_run_runbook(a["path"], ctx=c),
        "shell": lambda a, c: shell.shell(a["command"], ctx=c),
    }
    if name not in handlers:
        raise ValueError(f"Unknown tool: {name!r}")
    result = handlers[name](args, ctx)
    if hasattr(result, "__await__"):  # handle async tool handlers (e.g. send_alert)
        result = await result
    return result
