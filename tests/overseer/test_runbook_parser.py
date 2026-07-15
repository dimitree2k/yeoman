"""Tests for runbook Markdown+YAML parser."""
from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest
from yeoman_overseer.executor.deterministic import parse_deterministic_actions
from yeoman_overseer.runbook.parser import parse_runbook, parse_runbook_dir

SAMPLE_RUNBOOK = dedent("""\
    ---
    name: gateway-health
    domain: health
    enabled: true
    version: 1
    trigger:
      kind: poll
      interval_s: 30
      condition:
        check: process_alive
        target: yeoman-gateway
        operator: "=="
        value: true
    escalate_to_llm: false
    safety:
      max_actions_per_hour: 10
      rollback: true
      cooldown_s: 300
    ---
    # Gateway Health
    ## Context
    The gateway is the core message processing service.
    ## Actions
    1. Check if process is alive via PID file
    2. If dead: restart via systemctl
    ## Escalation
    After 3 failed restarts, alert owner.
""")

STARTER_RUNBOOKS = Path(__file__).parents[2] / "packages" / "overseer" / "yeoman_overseer" / "starter_runbooks"
SYSTEMD_UNITS = Path(__file__).parents[2] / "packages" / "overseer" / "yeoman_overseer" / "systemd"

def test_parse_valid_runbook(tmp_path: Path) -> None:
    f = tmp_path / "health-gateway.md"
    f.write_text(SAMPLE_RUNBOOK)
    rb = parse_runbook(f)
    assert rb.meta.name == "gateway-health"
    assert rb.meta.domain == "health"
    assert rb.meta.trigger.kind == "poll"
    assert rb.body.startswith("# Gateway Health")
    assert rb.path == f

def test_parse_missing_frontmatter(tmp_path: Path) -> None:
    f = tmp_path / "bad.md"
    f.write_text("# No frontmatter here\nJust plain markdown.")
    with pytest.raises(ValueError, match="frontmatter"):
        parse_runbook(f)

def test_parse_invalid_yaml(tmp_path: Path) -> None:
    f = tmp_path / "bad.md"
    f.write_text("---\nname: [invalid yaml\n---\n# Body")
    with pytest.raises(ValueError, match="YAML|parse"):
        parse_runbook(f)

def test_parse_runbook_dir(tmp_path: Path) -> None:
    (tmp_path / "a.md").write_text(SAMPLE_RUNBOOK)
    disabled = SAMPLE_RUNBOOK.replace("enabled: true", "enabled: false")
    (tmp_path / "b.md").write_text(disabled)
    (tmp_path / "not-a-runbook.txt").write_text("ignore me")
    runbooks = parse_runbook_dir(tmp_path)
    assert len(runbooks) == 2
    names = {rb.meta.name for rb in runbooks}
    assert "gateway-health" in names

def test_parse_runbook_dir_empty(tmp_path: Path) -> None:
    assert parse_runbook_dir(tmp_path) == []

def test_health_bridge_alerts_when_whatsapp_protocol_is_disconnected() -> None:
    rb = parse_runbook(STARTER_RUNBOOKS / "health-bridge.md")

    assert rb.meta.version == 3
    assert rb.meta.safety.manual_reset_after_failures is True
    assert rb.meta.trigger.condition is not None
    assert rb.meta.trigger.condition.check == "whatsapp_bridge_connected"
    assert rb.meta.trigger.condition.target == "default"
    assert rb.meta.trigger.condition.operator == "=="
    assert rb.meta.trigger.condition.value is False

    actions = parse_deterministic_actions(rb.body)

    assert [action.action for action in actions] == ["alert", "restart_service"]
    assert actions[0].target == "owner"
    assert "WhatsApp bridge is disconnected" in actions[0].kwargs["message"]
    assert actions[1].target == "yeoman-bridge.service"

def test_bridge_systemd_unit_starts_index_with_runtime_env() -> None:
    unit = (SYSTEMD_UNITS / "yeoman-bridge.service").read_text()

    assert "ExecStart=/usr/bin/node %h/.yeoman/var/cache/bridge/dist/index.js" in unit
    assert "EnvironmentFile=-%h/.yeoman/.env" in unit
    assert "Environment=AUTH_DIR=%h/.yeoman/secrets/whatsapp-auth" in unit
    assert "Environment=MEDIA_INCOMING_DIR=%h/.yeoman/var/media/incoming/whatsapp" in unit
    assert "BindPaths=%h/.yeoman/var/media" in unit
