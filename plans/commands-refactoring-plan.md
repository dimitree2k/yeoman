# Commands.py Refactoring Plan

**File**: `yeoman/cli/commands.py` (1458 lines)  
**Goal**: Reduce size, eliminate duplication, improve maintainability

---

## Executive Summary

| Approach | Lines Reduced | New Files | Risk | Effort |
|----------|---------------|-----------|------|--------|
| Option A: Process Utils Only | ~120 lines | 1 | Low | Small |
| Option B: Process + Templates | ~180 lines | 2 | Low | Medium |
| Option C: Full Modular Split | ~200 lines | 6-8 | Medium | Large |

---

## Option A: Extract Process Management Only

### Create `yeoman/cli/process.py`

Extract common process management utilities used by both gateway and bridge:

```python
# yeoman/cli/process.py
"""Process management utilities for CLI commands."""

import os
import signal
import shutil
import subprocess
import time
from pathlib import Path
from typing import Callable

def pid_alive(pid: int) -> bool:
    """Check if a process is running."""
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False

def command_for_pid(pid: int) -> str:
    """Get command line for a process."""
    result = subprocess.run(
        ["ps", "-p", str(pid), "-o", "command="],
        capture_output=True, text=True, check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else ""

def process_cwd(pid: int) -> Path | None:
    """Get working directory for a process."""
    try:
        return Path(f"/proc/{pid}/cwd").resolve()
    except OSError:
        return None

def pid_has_env(pid: int, key: str, value: str | None = None) -> bool:
    """Check if process has specific environment variable."""
    # ... existing logic from lines 243-260

def tokenize_command(command: str) -> list[str]:
    """Safely tokenize a command string."""
    import shlex
    try:
        return shlex.split(command)
    except ValueError:
        return command.split()

def listener_pids_for_port(port: int) -> set[int]:
    """Find PIDs listening on a TCP port using lsof."""
    # ... existing logic from lines 912-935

class ProcessManager:
    """Generic process manager for daemon-style services."""
    
    def __init__(
        self,
        name: str,
        pid_file: Path,
        log_file: Path,
        is_match: Callable[[int], bool],
    ):
        self.name = name
        self.pid_file = pid_file
        self.log_file = log_file
        self.is_match = is_match
    
    def find_pids(self) -> list[int]:
        """Find all matching process PIDs."""
        ...
    
    def stop(self, timeout_s: float = 10.0) -> int:
        """Stop all matching processes."""
        ...
    
    def signal(self, pid: int, sig: int) -> None:
        """Send signal to a process (with process group handling)."""
        ...
```

### Functions to Extract from commands.py

| Function | Lines | Destination |
|----------|-------|-------------|
| [`_pid_alive()`](yeoman/cli/commands.py:219) | 219-226 | `pid_alive()` |
| [`_command_for_pid()`](yeoman/cli/commands.py:229) | 229-240 | `command_for_pid()` |
| [`_pid_has_env()`](yeoman/cli/commands.py:243) | 243-260 | `pid_has_env()` |
| [`_process_cwd()`](yeoman/cli/commands.py:881) | 881-888 | `process_cwd()` |
| [`_listener_pids_for_port()`](yeoman/cli/commands.py:912) | 912-935 | `listener_pids_for_port()` |
| New helper | - | `tokenize_command()` |

### ProcessManager Class Replaces

| Gateway Function | Bridge Function | Combined Into |
|------------------|-----------------|---------------|
| `_gateway_pid_path()` | `_bridge_pid_path()` | `ProcessManager.pid_file` |
| `_gateway_log_path()` | `_bridge_log_path()` | `ProcessManager.log_file` |
| `_find_gateway_pids()` | `_find_bridge_pids()` | `ProcessManager.find_pids()` |
| `_signal_gateway_pid()` | `_signal_bridge_pid()` | `ProcessManager.signal()` |
| `_stop_gateway_processes()` | `_stop_bridge_processes()` | `ProcessManager.stop()` |

### Usage in Refactored commands.py

```python
from yeoman.cli.process import ProcessManager, pid_alive, command_for_pid

# Gateway process manager
gateway_manager = ProcessManager(
    name="gateway",
    pid_file=get_data_dir() / "run" / "gateway.pid",
    log_file=get_data_dir() / "logs" / "gateway.log",
    is_match=lambda pid: _is_gateway_process_on_port(pid, port),
)

# Bridge process manager  
bridge_manager = ProcessManager(
    name="whatsapp-bridge",
    pid_file=get_data_dir() / "run" / "whatsapp-bridge.pid",
    log_file=get_data_dir() / "logs" / "whatsapp-bridge.log",
    is_match=lambda pid: _is_bridge_process(pid),
)

# Stop gateway
stopped = gateway_manager.stop()

# Find bridge PIDs
pids = bridge_manager.find_pids()
```

### Estimated Impact

- **Lines removed from commands.py**: ~120 lines
- **New file process.py**: ~150 lines (with docstrings and class structure)
- **Net change**: Slightly more total code, but much better organized
- **Duplication eliminated**: Gateway and bridge share same logic

---

## Option B: Process Utils + Templates

In addition to Option A, extract workspace templates.

### Create `yeoman/cli/templates.py`

```python
# yeoman/cli/templates.py
"""Workspace template files for yeoman initialization."""

WORKSPACE_TEMPLATES = {
    "AGENTS.md": """# Agent Instructions

You are a helpful AI assistant. Be concise, accurate, and friendly.

## Guidelines

- Always explain what you're doing before taking actions
- Ask for clarification when the request is ambiguous
- Use tools to help accomplish tasks
- Remember important information in your memory files
""",
    "SOUL.md": """# Soul

I am yeoman, a lightweight AI assistant.

## Personality

- Helpful and friendly
- Concise and to the point
- Curious and eager to learn

## Values

- Accuracy over speed
- User privacy and safety
- Transparency in actions
""",
    "USER.md": """# User

Information about the user goes here.

## Preferences

- Communication style: (casual/formal)
- Timezone: (your timezone)
- Language: (your preferred language)
""",
}

MEMORY_TEMPLATE = """# Long-term Memory

This file stores important information that should persist across sessions.

## User Information

(Important facts about the user)

## Preferences

(User preferences learned over time)

## Important Notes

(Things to remember)
"""

def create_workspace_files(workspace: Path) -> None:
    """Create default workspace template files."""
    from rich.console import Console
    console = Console()
    
    for filename, content in WORKSPACE_TEMPLATES.items():
        file_path = workspace / filename
        if not file_path.exists():
            file_path.write_text(content)
            console.print(f"  [dim]Created {filename}[/dim]")

    memory_dir = workspace / "memory"
    memory_dir.mkdir(exist_ok=True)
    memory_file = memory_dir / "MEMORY.md"
    if not memory_file.exists():
        memory_file.write_text(MEMORY_TEMPLATE)
        console.print("  [dim]Created memory/MEMORY.md[/dim]")
```

### Refactored `_create_workspace_templates()` in commands.py

```python
def _create_workspace_templates(workspace: Path):
    """Create default workspace template files."""
    from yeoman.cli.templates import create_workspace_files
    create_workspace_files(workspace)
```

### Estimated Impact

- **Additional lines removed**: ~60 lines
- **Total lines removed from commands.py**: ~180 lines
- **New file templates.py**: ~70 lines

---

## Option C: Full Modular Split

Reorganize into a `yeoman/cli/commands/` subdirectory:

```
yeoman/cli/
├── __init__.py          # Re-exports app
├── process.py           # Process management utilities
├── templates.py         # Workspace templates
└── commands/
    ├── __init__.py      # Creates and combines all apps
    ├── onboard.py       # onboard command (~50 lines)
    ├── gateway.py       # gateway command + helpers (~200 lines)
    ├── agent.py         # agent command (~60 lines)
    ├── channels.py      # channels commands + bridge (~300 lines)
    ├── policy.py        # policy commands (~100 lines)
    ├── cron.py          # cron commands (~150 lines)
    └── status.py        # status + logs commands (~80 lines)
```

### File Breakdown

| Module | Commands | Estimated Lines |
|--------|----------|-----------------|
| `onboard.py` | `onboard` | ~50 |
| `gateway.py` | `gateway` | ~200 |
| `agent.py` | `agent` | ~60 |
| `channels.py` | `channels status`, `channels login`, `bridge start/stop/restart/status` | ~300 |
| `policy.py` | `policy path`, `policy explain`, `policy migrate-allowfrom` | ~100 |
| `cron.py` | `cron list/add/remove/enable/run` | ~150 |
| `status.py` | `status`, `logs` | ~80 |
| **Total** | | **~940 lines** |

### commands/__init__.py Structure

```python
# yeoman/cli/commands/__init__.py
"""CLI commands for yeoman."""

import typer
from yeoman import __logo__

app = typer.Typer(
    name="yeoman",
    help=f"{__logo__} yeoman - Personal AI Assistant",
    no_args_is_help=True,
)

# Import and register command groups
from yeoman.cli.commands import onboard, gateway, agent, channels, policy, cron, status

app.command()(onboard.onboard)
app.command()(gateway.gateway)
app.command()(agent.agent)
app.add_typer(channels.channels_app, name="channels")
app.add_typer(policy.policy_app, name="policy")
app.add_typer(cron.cron_app, name="cron")
app.command()(status.status)
app.command()(status.logs)
```

### Estimated Impact

- **Original commands.py**: 1458 lines
- **New total across modules**: ~940 lines (excluding process.py and templates.py)
- **Reduction**: ~35% fewer lines in command code
- **Better organization**: Each file has a single responsibility

---

## Recommendation

**Start with Option A** (Process Utils Only) because:

1. **Low risk** - Only extracting utility functions, no command logic changes
2. **Immediate benefit** - Eliminates the most egregious duplication
3. **Foundation** - Creates infrastructure for future refactoring
4. **Easy to test** - Process utilities can be unit tested independently

Then consider Option B if you want cleaner template management.

Option C is a larger undertaking that could be done incrementally after A and B are complete.

---

## Implementation Order for Option A

1. Create `yeoman/cli/process.py` with:
   - `pid_alive()`
   - `command_for_pid()`
   - `process_cwd()`
   - `pid_has_env()`
   - `tokenize_command()`
   - `listener_pids_for_port()`
   - `ProcessManager` class

2. Update `yeoman/cli/commands.py`:
   - Add `from yeoman.cli.process import ...`
   - Replace `_gateway_*` and `_bridge_*` functions with `ProcessManager` instances
   - Remove duplicated helper functions

3. Add tests for `process.py`

4. Verify all CLI commands still work

---

## Questions to Consider

1. **Backward compatibility**: Are any of the `_gateway_*` or `_bridge_*` functions used externally (e.g., by plugins)? They appear to be private (underscore prefix), so likely safe.

2. **Platform support**: The process management uses Linux-specific paths (`/proc/{pid}/`). Is this CLI intended to work on macOS/Windows? Currently it would not.

3. **Test coverage**: Does the current code have tests? Refactoring is safer with tests in place.
