from __future__ import annotations

from typer.testing import CliRunner


def test_a2a_serve_delegates_to_standalone_relay(monkeypatch) -> None:
    from yeoman_gateway.a2a import relay
    from yeoman_gateway.cli.commands import app

    called = False

    def run() -> int:
        nonlocal called
        called = True
        return 0

    monkeypatch.setattr(relay, "main", run)

    result = CliRunner().invoke(app, ["a2a", "serve"])

    assert result.exit_code == 0, result.output
    assert called


def test_install_units_includes_a2a_relay(tmp_path, monkeypatch) -> None:
    import yeoman_overseer
    from yeoman_gateway.cli import overseer_commands

    monkeypatch.setattr(overseer_commands.Path, "home", classmethod(lambda cls: tmp_path))

    overseer_commands.install_units()

    installed = tmp_path / ".config/systemd/user/yeoman-a2a.service"
    installed_text = installed.read_text(encoding="utf-8")
    assert installed_text == (
        overseer_commands.Path(yeoman_overseer.__file__).parent
        .joinpath("systemd/yeoman-a2a.service")
        .read_text(encoding="utf-8")
    )
    assert "After=network-online.target yeoman-gateway.service" in installed_text
    assert "Wants=network-online.target" in installed_text
    assert "EnvironmentFile=%h/.yeoman/secrets/a2a.env" in installed_text
