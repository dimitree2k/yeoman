"""Tests for yeoman deploy utilities."""

import importlib.util
import os
import subprocess
from pathlib import Path


def _make_bridge(tmp_path: Path) -> Path:
    """Create a minimal bridge dir with src and dist."""
    bridge = tmp_path / "bridge"
    src = bridge / "src"
    dist = bridge / "dist"
    src.mkdir(parents=True)
    dist.mkdir(parents=True)
    (src / "server.ts").write_text("console.log('hello');")
    (src / "index.ts").write_text("export {};")
    (dist / "server.js").write_text("console.log('hello');")
    (dist / "index.js").write_text("// compiled")
    return bridge


class TestHashBridgeSources:
    def test_deterministic(self, tmp_path: Path) -> None:
        from yeoman_gateway.deploy import hash_bridge_sources

        bridge = _make_bridge(tmp_path)
        h1 = hash_bridge_sources(bridge / "src")
        h2 = hash_bridge_sources(bridge / "src")
        assert h1 == h2
        assert len(h1) == 64  # sha256 hex

    def test_changes_on_content_change(self, tmp_path: Path) -> None:
        from yeoman_gateway.deploy import hash_bridge_sources

        bridge = _make_bridge(tmp_path)
        h1 = hash_bridge_sources(bridge / "src")
        (bridge / "src" / "server.ts").write_text("console.log('changed');")
        h2 = hash_bridge_sources(bridge / "src")
        assert h1 != h2

    def test_ignores_non_ts(self, tmp_path: Path) -> None:
        from yeoman_gateway.deploy import hash_bridge_sources

        bridge = _make_bridge(tmp_path)
        h1 = hash_bridge_sources(bridge / "src")
        (bridge / "src" / "readme.md").write_text("docs")
        h2 = hash_bridge_sources(bridge / "src")
        assert h1 == h2

    def test_ignores_test_and_declaration_files(self, tmp_path: Path) -> None:
        from yeoman_gateway.deploy import hash_bridge_sources

        bridge = _make_bridge(tmp_path)
        h1 = hash_bridge_sources(bridge / "src")
        (bridge / "src" / "foo.test.ts").write_text("test")
        (bridge / "src" / "foo.d.ts").write_text("declare")
        h2 = hash_bridge_sources(bridge / "src")
        assert h1 == h2


class TestBridgeIsStale:
    def test_stale_when_no_dist(self, tmp_path: Path) -> None:
        from yeoman_gateway.deploy import bridge_is_stale

        bridge = _make_bridge(tmp_path)
        import shutil
        shutil.rmtree(bridge / "dist")
        assert bridge_is_stale(bridge / "src", bridge / "dist") is True

    def test_stale_when_no_hash_file(self, tmp_path: Path) -> None:
        from yeoman_gateway.deploy import bridge_is_stale

        bridge = _make_bridge(tmp_path)
        assert bridge_is_stale(bridge / "src", bridge / "dist") is True

    def test_stale_when_hash_mismatches(self, tmp_path: Path) -> None:
        from yeoman_gateway.deploy import bridge_is_stale

        bridge = _make_bridge(tmp_path)
        (bridge / "dist" / ".build-hash").write_text("old-hash")
        assert bridge_is_stale(bridge / "src", bridge / "dist") is True

    def test_not_stale_when_hash_matches(self, tmp_path: Path) -> None:
        from yeoman_gateway.deploy import bridge_is_stale, hash_bridge_sources

        bridge = _make_bridge(tmp_path)
        h = hash_bridge_sources(bridge / "src")
        (bridge / "dist" / ".build-hash").write_text(h)
        assert bridge_is_stale(bridge / "src", bridge / "dist") is False


class TestFindSourceRepo:
    def test_finds_via_env_var(self, tmp_path: Path, monkeypatch) -> None:
        from yeoman_gateway.deploy import find_source_repo

        toml = tmp_path / "pyproject.toml"
        toml.write_text('[tool.uv.workspace]\nmembers = []\n')
        monkeypatch.setenv("YEOMAN_SOURCE_DIR", str(tmp_path))
        assert find_source_repo() == tmp_path

    def test_returns_none_when_no_toml(self, tmp_path: Path, monkeypatch) -> None:
        from yeoman_gateway.deploy import find_source_repo

        monkeypatch.setenv("YEOMAN_SOURCE_DIR", str(tmp_path))
        assert find_source_repo() is None

    def test_returns_none_when_no_workspace_section(self, tmp_path: Path, monkeypatch) -> None:
        from yeoman_gateway.deploy import find_source_repo

        toml = tmp_path / "pyproject.toml"
        toml.write_text('[project]\nname = "foo"\n')
        monkeypatch.setenv("YEOMAN_SOURCE_DIR", str(tmp_path))
        assert find_source_repo() is None

    def test_returns_none_when_dir_missing(self, tmp_path: Path, monkeypatch) -> None:
        from yeoman_gateway.deploy import find_source_repo

        monkeypatch.setenv("YEOMAN_SOURCE_DIR", str(tmp_path / "nonexistent"))
        assert find_source_repo() is None


def test_deploy_dry_run_exits_zero() -> None:
    """Integration test: yeoman deploy --dry-run should succeed."""
    result = subprocess.run(
        ["yeoman", "deploy", "--dry-run"],
        capture_output=True,
        text=True,
        cwd=str(Path.home() / "Documents" / "yeoman"),
        env={**os.environ, "YEOMAN_SOURCE_DIR": str(Path.home() / "Documents" / "yeoman")},
    )
    assert result.returncode == 0, f"deploy --dry-run failed:\n{result.stderr}"


def test_whatsapp_qr_reconnect_script_resolves_repo_root() -> None:
    repo = Path(__file__).resolve().parents[2]
    script = repo / "scripts" / "whatsapp_qr_reconnect.py"
    spec = importlib.util.spec_from_file_location("whatsapp_qr_reconnect", script)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.repo_root() == repo


class _FakeSystemctl:
    """In-memory stand-in for ``systemctl --user`` on the deploy restart path."""

    def __init__(self, *, available: bool = True, active=(), fail_restart=()) -> None:
        self.available = available
        self.active = set(active)
        self.fail_restart = set(fail_restart)
        self.calls: list[list[str]] = []

    def is_available(self) -> bool:
        return self.available

    def is_active(self, unit: str) -> bool:
        self.calls.append(["is-active", unit])
        return unit in self.active

    def restart(self, unit: str) -> bool:
        self.calls.append(["restart", unit])
        if unit in self.fail_restart:
            return False
        self.active.add(unit)
        return True

    def stop(self, unit: str) -> bool:
        self.calls.append(["stop", unit])
        self.active.discard(unit)
        return True

    def actions(self, verb: str) -> list[str]:
        return [call[1] for call in self.calls if call[0] == verb]


def test_restart_uses_systemd_when_the_units_are_active(monkeypatch) -> None:
    """A systemd-managed install must be restarted through its own supervisor.

    Deciding from ``run/*.pid`` skipped every service, because systemd units never
    write those files - and the CLI fallback would have started a *second* process
    next to the unit's own.
    """
    from yeoman_gateway.cli import deploy_commands

    systemctl = _FakeSystemctl(active={
        "yeoman-gateway.service",
        "yeoman-bridge.service",
        "yeoman-overseer.service",
    })
    deploy_commands._restart_running_services(systemctl=systemctl)

    assert systemctl.actions("restart") == [
        "yeoman-gateway.service",
        "yeoman-bridge.service",
        "yeoman-overseer.service",
    ]


def test_bridge_is_restarted_after_the_gateway(monkeypatch) -> None:
    """Order matters: the gateway refreshes the bridge cache on its way up."""
    from yeoman_gateway.cli import deploy_commands

    systemctl = _FakeSystemctl(active={"yeoman-gateway.service", "yeoman-bridge.service"})
    deploy_commands._restart_running_services(systemctl=systemctl)

    restarted = systemctl.actions("restart")
    assert restarted.index("yeoman-gateway.service") < restarted.index("yeoman-bridge.service")


def test_inactive_unit_falls_back_to_the_pid_file_path(tmp_path, monkeypatch) -> None:
    """Without an active unit nothing is started that systemd does not manage."""
    from yeoman_gateway.cli import deploy_commands

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    monkeypatch.setenv("YEOMAN_HOME", str(tmp_path))
    systemctl = _FakeSystemctl(active=set())
    cli_calls: list[list[str]] = []
    monkeypatch.setattr(
        deploy_commands.subprocess,
        "run",
        lambda cmd, **kwargs: cli_calls.append(list(cmd)) or subprocess.CompletedProcess(cmd, 0, "", ""),
    )

    deploy_commands._restart_running_services(systemctl=systemctl)

    assert systemctl.actions("restart") == []
    assert cli_calls == []  # no pid file -> nothing to restart


def test_missing_systemd_keeps_the_pid_file_path(tmp_path, monkeypatch) -> None:
    from yeoman_gateway.cli import deploy_commands

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    monkeypatch.setenv("YEOMAN_HOME", str(tmp_path))
    systemctl = _FakeSystemctl(available=False)
    cli_calls: list[list[str]] = []
    monkeypatch.setattr(
        deploy_commands.subprocess,
        "run",
        lambda cmd, **kwargs: cli_calls.append(list(cmd)) or subprocess.CompletedProcess(cmd, 0, "", ""),
    )

    deploy_commands._restart_running_services(systemctl=systemctl)

    assert systemctl.calls == []
    assert cli_calls == []


def test_failed_restart_is_reported_without_claiming_success(monkeypatch, capsys) -> None:
    from yeoman_gateway.cli import deploy_commands

    systemctl = _FakeSystemctl(
        active={"yeoman-bridge.service"}, fail_restart={"yeoman-bridge.service"}
    )
    deploy_commands._restart_running_services(systemctl=systemctl)

    printed = capsys.readouterr().out
    assert "bridge" in printed
    assert "failed" in printed.lower()


def test_overseer_stopped_for_reinstall_comes_back_under_systemd(monkeypatch) -> None:
    """A CLI-started overseer next to an active unit leaves systemd blind to it.

    That is not a theoretical worry: the first deploy with the systemd-aware restart
    fell back to ``yeoman overseer start`` while the unit was stopped for the
    reinstall, and the service then ran unmanaged until the unit was started by hand.
    """
    from yeoman_gateway.cli import deploy_commands

    unit = "yeoman-overseer.service"
    systemctl = _FakeSystemctl(active={unit})

    # Step 4 of the deploy: take the overseer down before the tool env is replaced.
    owed = deploy_commands._stop_overseer_for_reinstall(systemctl=systemctl)
    assert owed is True
    assert systemctl.actions("stop") == [unit]
    assert systemctl.is_active(unit) is False

    # Step 6: the owed restart goes back through systemd, not through the CLI.
    cli_calls: list[list[str]] = []
    monkeypatch.setattr(
        deploy_commands.subprocess,
        "run",
        lambda cmd, **kwargs: cli_calls.append(list(cmd)) or subprocess.CompletedProcess(cmd, 0, "", ""),
    )
    deploy_commands._restart_running_services(True, systemctl=systemctl)

    assert systemctl.actions("restart") == [unit]
    assert systemctl.is_active(unit) is True
    assert cli_calls == []
