"""Configuration loading utilities."""

import json
import os
import shutil
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from nanobot.config.defaults import apply_missing_defaults
from nanobot.config.schema import Config

CONFIG_VERSION = 2


def get_config_path() -> Path:
    """Get the default configuration file path."""
    return get_data_dir() / "config.json"


def get_data_dir() -> Path:
    """Get the nanobot data directory."""
    from nanobot.utils.helpers import get_data_path
    return get_data_path()


def _load_dotenv() -> None:
    """Load ~/.nanobot/.env (or $NANOBOT_HOME/.env) into os.environ.

    Existing environment variables are never overwritten — real env vars
    always take priority over the .env file.
    """
    nanobot_home = os.environ.get("NANOBOT_HOME", "").strip()
    base = Path(nanobot_home) if nanobot_home else Path.home() / ".nanobot"
    env_file = base / ".env"
    if not env_file.exists():
        return
    try:
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip()
            # Strip surrounding quotes (single or double)
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
                value = value[1:-1]
            if key and key not in os.environ:
                os.environ[key] = value
    except OSError:
        pass


def _apply_env_overrides(config: Config) -> Config:
    """Patch api_keys and tokens from environment variables.

    Environment variables (including those loaded from .env) take priority
    over values already present in config.json. This means the .env file
    is the canonical source for secrets; config.json can have empty keys.
    """
    from nanobot.providers.registry import PROVIDERS
    from nanobot.config.schema import ProviderConfig

    # Provider API keys — iterate registry so every provider is covered
    for spec in PROVIDERS:
        if not spec.env_key:
            continue
        provider = getattr(config.providers, spec.name, None)
        if not isinstance(provider, ProviderConfig):
            continue
        env_val = os.environ.get(spec.env_key, "").strip()
        if env_val:
            provider.api_key = env_val

    # ElevenLabs — not in PROVIDERS, handled separately
    elevenlabs_key = os.environ.get("ELEVENLABS_API_KEY", "").strip()
    if elevenlabs_key:
        config.providers.elevenlabs.api_key = elevenlabs_key

    # Channel tokens
    tg_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if tg_token:
        config.channels.telegram.token = tg_token

    wa_bridge_token = os.environ.get("WHATSAPP_BRIDGE_TOKEN", "").strip()
    if wa_bridge_token:
        config.channels.whatsapp.bridge_token = wa_bridge_token

    # Tool API keys
    search_key = os.environ.get("BRAVE_SEARCH_API_KEY", "").strip()
    if search_key:
        config.tools.web.search.api_key = search_key

    return config


def load_config(config_path: Path | None = None) -> Config:
    """Load configuration from file or create default.

    Loading order:
      1. Parse ~/.nanobot/.env and inject into os.environ (if not already set).
      2. Load config.json, applying schema migration.
      3. Override empty api_key / token fields with env var values.

    Args:
        config_path: Optional path to config file. Uses default if not provided.

    Returns:
        Loaded configuration object.
    """
    _load_dotenv()

    path = config_path or get_config_path()

    if path.exists():
        try:
            with open(path) as f:
                raw = json.load(f)

            migrated_raw, changed = _migrate_config_with_change(raw)
            validated = Config.model_validate(convert_keys(migrated_raw))
            if changed:
                _backup_config(path)
                _atomic_write_config(path, validated)
            return _apply_env_overrides(validated)
        except (json.JSONDecodeError, ValueError) as e:
            print(f"Warning: Failed to load config from {path}: {e}")
            print("Using default configuration.")

    return _apply_env_overrides(Config())


def save_config(config: Config, config_path: Path | None = None) -> None:
    """Save configuration to file.

    Args:
        config: Configuration to save.
        config_path: Optional path to save to. Uses default if not provided.
    """
    path = config_path or get_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    _atomic_write_config(path, config)


def _migrate_config(data: dict[str, Any]) -> dict[str, Any]:
    """Backward-compatible migration helper returning migrated payload only."""
    migrated, _ = _migrate_config_with_change(data)
    return migrated


def _migrate_config_with_change(data: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Migrate old config formats to current schema version.

    Returns:
        (migrated_data, changed)
    """
    if not isinstance(data, dict):
        raise ValueError("Config root must be a JSON object")

    original = json.dumps(data, sort_keys=True, separators=(",", ":"))
    data = json.loads(json.dumps(data))

    # Legacy key migrations on camelCase payload before snake_case normalization.
    # Move tools.exec.restrictToWorkspace → tools.restrictToWorkspace
    tools = data.get("tools", {})
    if not isinstance(tools, dict):
        tools = {}
        data["tools"] = tools

    exec_cfg = tools.get("exec", {})
    if not isinstance(exec_cfg, dict):
        exec_cfg = {}
        tools["exec"] = exec_cfg

    if "restrictToWorkspace" in exec_cfg and "restrictToWorkspace" not in tools:
        tools["restrictToWorkspace"] = exec_cfg.pop("restrictToWorkspace")

    # Legacy shortcuts → tools.exec.isolation.*
    isolation_cfg = exec_cfg.get("isolation")
    if not isinstance(isolation_cfg, dict):
        isolation_cfg = {}
        exec_cfg["isolation"] = isolation_cfg

    if "isolationEnabled" in exec_cfg and "enabled" not in isolation_cfg:
        isolation_cfg["enabled"] = exec_cfg.pop("isolationEnabled")

    if "isolationBackend" in exec_cfg and "backend" not in isolation_cfg:
        isolation_cfg["backend"] = exec_cfg.pop("isolationBackend")

    if "allowlist" in isolation_cfg and "allowlistPath" not in isolation_cfg:
        isolation_cfg["allowlistPath"] = isolation_cfg.pop("allowlist")

    channels = data.get("channels")
    if isinstance(channels, dict):
        wa = channels.get("whatsapp")
        if isinstance(wa, dict):
            bridge_url = wa.get("bridgeUrl")
            if isinstance(bridge_url, str) and bridge_url.strip():
                parsed = urlparse(bridge_url)
                if "bridgeHost" not in wa and parsed.hostname:
                    wa["bridgeHost"] = parsed.hostname
                if "bridgePort" not in wa and parsed.port is not None:
                    wa["bridgePort"] = parsed.port

    # Normalize to snake_case for semantic migrations.
    snake = convert_keys(data)
    if not isinstance(snake, dict):
        raise ValueError("Config migration produced invalid root payload")

    version = snake.get("config_version")
    try:
        version_num = int(version) if version is not None else 1
    except (TypeError, ValueError):
        version_num = 1

    if version_num < 2:
        runtime = snake.get("runtime")
        if not isinstance(runtime, dict):
            runtime = {}
            snake["runtime"] = runtime
        wa_runtime = runtime.get("whatsapp_bridge")
        if not isinstance(wa_runtime, dict):
            wa_runtime = {}
            runtime["whatsapp_bridge"] = wa_runtime

        channels_cfg = snake.get("channels")
        whatsapp_cfg = channels_cfg.get("whatsapp") if isinstance(channels_cfg, dict) else {}
        if not isinstance(whatsapp_cfg, dict):
            whatsapp_cfg = {}

        wa_runtime.setdefault("host", whatsapp_cfg.get("bridge_host", "127.0.0.1"))
        wa_runtime.setdefault("port", whatsapp_cfg.get("bridge_port", 3001))
        wa_runtime.setdefault("token", whatsapp_cfg.get("bridge_token", ""))
        wa_runtime.setdefault("auto_repair", whatsapp_cfg.get("bridge_auto_repair", True))
        wa_runtime.setdefault(
            "startup_timeout_ms",
            whatsapp_cfg.get("bridge_startup_timeout_ms", 15000),
        )
        wa_runtime.setdefault(
            "max_payload_bytes",
            whatsapp_cfg.get("max_payload_bytes", 262144),
        )

    # Collapse deprecated memory2 config into single memory config.
    memory_cfg = snake.get("memory")
    memory2_cfg = snake.get("memory2")
    if isinstance(memory2_cfg, dict):
        if not isinstance(memory_cfg, dict):
            memory_cfg = {}
            snake["memory"] = memory_cfg
        for key in ("enabled", "mode", "db_path", "capture", "recall", "embedding", "scoring", "acl", "wal"):
            if key in memory2_cfg:
                memory_cfg[key] = memory2_cfg[key]
        snake.pop("memory2", None)

    apply_missing_defaults(snake)
    snake["config_version"] = CONFIG_VERSION

    migrated = convert_to_camel(snake)
    changed = original != json.dumps(migrated, sort_keys=True, separators=(",", ":"))
    return migrated, changed


def _backup_config(path: Path) -> None:
    """Create timestamped backup of config before migration rewrite."""
    if not path.exists():
        return
    timestamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    backup = path.with_name(f"{path.stem}.backup.{timestamp}{path.suffix}")
    shutil.copy2(path, backup)
    try:
        backup.chmod(0o600)
    except OSError:
        pass


def _atomic_write_config(path: Path, config: Config) -> None:
    """Atomically write config as camelCase JSON with secure permissions."""
    path.parent.mkdir(parents=True, exist_ok=True)
    data = convert_to_camel(config.model_dump())
    fd, tmp_raw = tempfile.mkstemp(prefix=f".{path.name}.tmp-", dir=str(path.parent), text=True)
    tmp_path = Path(tmp_raw)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        try:
            tmp_path.chmod(0o600)
        except OSError:
            pass
        os.replace(tmp_path, path)
        try:
            path.chmod(0o600)
        except OSError:
            pass
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def convert_keys(data: Any) -> Any:
    """Convert camelCase keys to snake_case for Pydantic."""
    if isinstance(data, dict):
        return {camel_to_snake(k): convert_keys(v) for k, v in data.items()}
    if isinstance(data, list):
        return [convert_keys(item) for item in data]
    return data


def convert_to_camel(data: Any) -> Any:
    """Convert snake_case keys to camelCase."""
    if isinstance(data, dict):
        return {snake_to_camel(k): convert_to_camel(v) for k, v in data.items()}
    if isinstance(data, list):
        return [convert_to_camel(item) for item in data]
    return data


def camel_to_snake(name: str) -> str:
    """Convert camelCase to snake_case."""
    result = []
    for i, char in enumerate(name):
        if char.isupper() and i > 0:
            result.append("_")
        result.append(char.lower())
    return "".join(result)


def snake_to_camel(name: str) -> str:
    """Convert snake_case to camelCase."""
    components = name.split("_")
    return components[0] + "".join(x.title() for x in components[1:])
