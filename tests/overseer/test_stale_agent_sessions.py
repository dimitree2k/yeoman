"""Tests for stale OS agent-session cleanup."""
from __future__ import annotations

from yeoman_overseer.executor.stale_agent_sessions import (
    ProcessInfo,
    find_stale_agent_session_roots,
)


def test_finds_old_mosh_agent_session_roots() -> None:
    processes = [
        ProcessInfo(pid=10, ppid=1, command="mosh-server", etimes=7200, args="mosh-server new"),
        ProcessInfo(pid=11, ppid=10, command="bash", etimes=7190, args="-bash"),
        ProcessInfo(pid=12, ppid=11, command="claude", etimes=7180, args="claude"),
        ProcessInfo(pid=20, ppid=1, command="mosh-server", etimes=90, args="mosh-server new"),
        ProcessInfo(pid=21, ppid=20, command="bash", etimes=80, args="-bash"),
        ProcessInfo(pid=22, ppid=21, command="codex", etimes=70, args="codex"),
    ]

    roots = find_stale_agent_session_roots(processes, min_age_seconds=3600)

    assert [root.pid for root in roots] == [10]


def test_keeps_young_agent_sessions() -> None:
    processes = [
        ProcessInfo(pid=10, ppid=1, command="mosh-server", etimes=3599, args="mosh-server new"),
        ProcessInfo(pid=11, ppid=10, command="bash", etimes=3590, args="-bash"),
        ProcessInfo(pid=12, ppid=11, command="codex", etimes=3580, args="codex"),
    ]

    roots = find_stale_agent_session_roots(processes, min_age_seconds=3600)

    assert roots == []


def test_keeps_non_agent_mosh_sessions() -> None:
    processes = [
        ProcessInfo(pid=10, ppid=1, command="mosh-server", etimes=7200, args="mosh-server new"),
        ProcessInfo(pid=11, ppid=10, command="bash", etimes=7190, args="-bash"),
        ProcessInfo(pid=12, ppid=11, command="vim", etimes=7180, args="vim notes.md"),
    ]

    roots = find_stale_agent_session_roots(processes, min_age_seconds=3600)

    assert roots == []
