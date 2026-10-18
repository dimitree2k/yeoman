import ast
import json
from pathlib import Path
from typing import Callable

import pytest
from yeoman_shared.config.loader import load_config, save_config
from yeoman_shared.config.schema import Config
from yeoman_shared.utils.helpers import get_yeoman_home

PATH_DEFAULTS: list[tuple[dict, Callable[[Config], Path], str]] = [
    (
        {"channels": {"whatsapp": {"auth_dir": "~/.yeoman/secrets/whatsapp-auth"}}},
        lambda config: config.channels.whatsapp.resolved_auth_dir,
        "secrets/whatsapp-auth",
    ),
    (
        {
            "channels": {
                "whatsapp": {"media": {"incoming_dir": "~/.yeoman/var/media/incoming/whatsapp"}}
            }
        },
        lambda config: config.channels.whatsapp.media.incoming_path,
        "var/media/incoming/whatsapp",
    ),
    (
        {
            "channels": {
                "whatsapp": {"media": {"outgoing_dir": "~/.yeoman/var/media/outgoing/whatsapp"}}
            }
        },
        lambda config: config.channels.whatsapp.media.outgoing_path,
        "var/media/outgoing/whatsapp",
    ),
    (
        {"agents": {"defaults": {"workspace": "~/.yeoman/workspace"}}},
        lambda config: config.workspace_path,
        "workspace",
    ),
    (
        {"ipc": {"gateway_socket_path": "~/.yeoman/run/gateway.sock"}},
        lambda config: config.ipc.resolved_gateway_socket_path,
        "run/gateway.sock",
    ),
    (
        {"ipc": {"overseer_socket_path": "~/.yeoman/run/overseer.sock"}},
        lambda config: config.ipc.resolved_overseer_socket_path,
        "run/overseer.sock",
    ),
    (
        {"memory": {"db_path": "~/.yeoman/data/memory/memory.db"}},
        lambda config: config.memory.resolved_db_path,
        "data/memory/memory.db",
    ),
    (
        {"knowledge": {"db_path": "~/.yeoman/data/knowledge/knowledge.db"}},
        lambda config: config.knowledge.resolved_db_path,
        "data/knowledge/knowledge.db",
    ),
    (
        {"knowledge": {"legacy_contacts_paths": ["~/.yeoman/data/contacts/contacts.db"]}},
        lambda config: config.knowledge.resolved_legacy_contacts_paths[0],
        "data/contacts/contacts.db",
    ),
    (
        {"knowledge": {"legacy_memory_paths": ["~/.yeoman/data/memory/memory.db"]}},
        lambda config: config.knowledge.resolved_legacy_memory_paths[0],
        "data/memory/memory.db",
    ),
]


@pytest.mark.parametrize(("explicit", "path", "suffix"), PATH_DEFAULTS)
def test_path_defaults_follow_home_and_explicit_values_win(
    explicit: dict,
    path: Callable[[Config], Path],
    suffix: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    legacy_home = tmp_path / "legacy-user"
    monkeypatch.setenv("HOME", str(legacy_home))
    monkeypatch.delenv("YEOMAN_HOME", raising=False)
    assert get_yeoman_home() == legacy_home / ".yeoman"
    assert path(Config()) == legacy_home / ".yeoman" / suffix

    runtime_home = tmp_path / "runtime-home"
    monkeypatch.setenv("YEOMAN_HOME", str(runtime_home))
    assert path(Config()) == runtime_home / suffix

    config_path = tmp_path / "explicit.json"
    config_path.write_text(json.dumps(explicit), encoding="utf-8")
    explicit_config = load_config(config_path)
    assert path(explicit_config) == legacy_home / ".yeoman" / suffix


def test_migration_inserted_paths_keep_default_provenance(monkeypatch, tmp_path: Path) -> None:
    runtime_home = tmp_path / "runtime-home"
    monkeypatch.setenv("YEOMAN_HOME", str(runtime_home))
    config_path = tmp_path / "config.json"
    config_path.write_text('{"channels":{"whatsapp":{}},"memory":{}}', encoding="utf-8")

    config = load_config(config_path)
    assert "incoming_dir" not in config.channels.whatsapp.media.model_fields_set
    assert "db_path" not in config.memory.model_fields_set
    assert (
        config.channels.whatsapp.media.incoming_path == runtime_home / "var/media/incoming/whatsapp"
    )
    assert config.memory.resolved_db_path == runtime_home / "data/memory/memory.db"
    reloaded = load_config(config_path)
    assert reloaded.channels.whatsapp.media.incoming_path == (
        runtime_home / "var/media/incoming/whatsapp"
    )
    assert reloaded.memory.resolved_db_path == runtime_home / "data/memory/memory.db"


def test_save_omits_unset_defaults_and_keeps_explicit_paths(monkeypatch, tmp_path: Path) -> None:
    runtime_home = tmp_path / "runtime-home"
    monkeypatch.setenv("YEOMAN_HOME", str(runtime_home))
    saved_path = tmp_path / "saved.json"
    save_config(Config(), saved_path)
    saved = json.loads(saved_path.read_text(encoding="utf-8"))

    assert "authDir" not in saved["channels"]["whatsapp"]
    assert "incomingDir" not in saved["channels"]["whatsapp"]["media"]
    assert "outgoingDir" not in saved["channels"]["whatsapp"]["media"]
    assert "workspace" not in saved["agents"]["defaults"]
    assert "gatewaySocketPath" not in saved["ipc"]
    assert "overseerSocketPath" not in saved["ipc"]
    assert "dbPath" not in saved["memory"]
    assert "dbPath" not in saved["knowledge"]
    assert "legacyContactsPaths" not in saved["knowledge"]
    assert "legacyMemoryPaths" not in saved["knowledge"]
    assert str(runtime_home) not in saved_path.read_text(encoding="utf-8")

    explicit_path = tmp_path / "explicit-saved.json"
    save_config(
        Config.model_validate(
            {"channels": {"whatsapp": {"auth_dir": "~/.yeoman/secrets/whatsapp-auth"}}}
        ),
        explicit_path,
    )
    explicit_saved = json.loads(explicit_path.read_text(encoding="utf-8"))
    assert explicit_saved["channels"]["whatsapp"]["authDir"] == ("~/.yeoman/secrets/whatsapp-auth")


def test_schema_has_no_hardcoded_yeoman_path_defaults() -> None:
    schema_path = Path(__file__).parents[2] / "packages/shared/yeoman_shared/config/schema.py"
    tree = ast.parse(schema_path.read_text(encoding="utf-8"))
    literals = {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.value.startswith("~/.yeoman")
    }
    assert literals == {
        "~/.yeoman/secrets/whatsapp-auth",
        "~/.yeoman/workspace",
        "~/.yeoman/run/gateway.sock",
        "~/.yeoman/run/overseer.sock",
    }
