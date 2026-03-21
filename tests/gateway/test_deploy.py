"""Tests for yeoman deploy utilities."""

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
