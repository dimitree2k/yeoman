"""Policy file loading utilities."""

from __future__ import annotations

import json
from pathlib import Path

from loguru import logger

from nanobot.policy.schema import PolicyConfig


def get_policy_path() -> Path:
    """Get the default policy file path."""
    return Path.home() / ".nanobot" / "policy.json"


def load_policy(path: Path | None = None) -> PolicyConfig:
    """Load policy file from disk. Returns default policy when missing."""
    policy_path = path or get_policy_path()
    if not policy_path.exists():
        return PolicyConfig()
    with open(policy_path) as f:
        data = json.load(f)
    return PolicyConfig.model_validate(data)


def save_policy(policy: PolicyConfig, path: Path | None = None) -> None:
    """Save policy file to disk."""
    policy_path = path or get_policy_path()
    policy_path.parent.mkdir(parents=True, exist_ok=True)
    with open(policy_path, "w") as f:
        json.dump(policy.model_dump(by_alias=True, exclude_none=True), f, indent=2)


def ensure_policy_file(path: Path | None = None) -> Path:
    """Create policy file if missing and return its path."""
    policy_path = path or get_policy_path()
    if not policy_path.exists():
        save_policy(PolicyConfig(), policy_path)
    return policy_path


def warn_legacy_allow_from(config: object) -> None:
    """Warn when deprecated channels.*.allowFrom is still configured."""
    channels = getattr(config, "channels", None)
    if channels is None:
        return

    for channel_name in ("telegram", "whatsapp", "discord", "feishu"):
        channel_cfg = getattr(channels, channel_name, None)
        if channel_cfg is None:
            continue
        allow_from = getattr(channel_cfg, "allow_from", [])
        if allow_from:
            logger.warning(
                f"channels.{channel_name}.allowFrom is deprecated and ignored; use ~/.nanobot/policy.json"
            )
