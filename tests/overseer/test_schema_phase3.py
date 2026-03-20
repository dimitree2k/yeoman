# tests/overseer/test_schema_phase3.py
from yeoman_overseer.runbook.schema import SafetyConfig, RunbookFrontmatter, TriggerConfig


def test_safety_config_shell_timeout_default():
    s = SafetyConfig()
    assert s.shell_timeout_s == 60


def test_safety_config_shell_timeout_custom():
    s = SafetyConfig(shell_timeout_s=120)
    assert s.shell_timeout_s == 120


def test_runbook_shell_timeout_parses():
    import yaml
    raw = yaml.safe_load("""
name: ops-cleanup
domain: ops
trigger:
  kind: cron
  expr: "0 2 * * 0"
safety:
  shell_timeout_s: 90
""")
    fm = RunbookFrontmatter(**raw)
    assert fm.safety.shell_timeout_s == 90
