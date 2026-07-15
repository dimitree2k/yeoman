"""Tests for yeoman deploy utilities."""

from pathlib import Path
import importlib.util


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


import os
import subprocess


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
