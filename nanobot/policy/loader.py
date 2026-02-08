"""Policy file loading utilities."""

from __future__ import annotations

import json
from pathlib import Path

from loguru import logger

from nanobot.policy.schema import ChannelPolicy, PolicyConfig, WhoCanTalkPolicyOverride


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
    tmp_path = policy_path.with_suffix(f"{policy_path.suffix}.tmp")
    with open(tmp_path, "w") as f:
        json.dump(policy.model_dump(by_alias=True, exclude_none=True), f, indent=2)
    tmp_path.replace(policy_path)


def ensure_policy_file(path: Path | None = None) -> Path:
    """Create policy file if missing and return its path."""
    policy_path = path or get_policy_path()
    if not policy_path.exists():
        save_policy(PolicyConfig(), policy_path)
    return policy_path


def warn_legacy_allow_from(config_path: Path | None = None) -> None:
    """Warn when removed channels.*.allowFrom is still present in config.json."""
    legacy = load_legacy_allow_from(config_path)
    for channel_name, allow_from in legacy.items():
        if not allow_from:
            continue
        logger.warning(
            f"channels.{channel_name}.allowFrom was removed from config schema; "
            "use ~/.nanobot/policy.json (or run `nanobot policy migrate-allowfrom`)"
        )


def load_legacy_allow_from(config_path: Path | None = None) -> dict[str, list[str]]:
    """Read legacy channels.*.allowFrom directly from raw config.json."""
    if config_path is None:
        from nanobot.config.loader import get_config_path

        config_path = get_config_path()
    if not config_path.exists():
        return {}

    try:
        with open(config_path) as f:
            raw = json.load(f)
    except Exception:
        return {}

    channels = raw.get("channels")
    if not isinstance(channels, dict):
        return {}

    legacy: dict[str, list[str]] = {}
    for channel_name in ("telegram", "whatsapp", "discord", "feishu"):
        channel_cfg = channels.get(channel_name)
        if not isinstance(channel_cfg, dict):
            continue
        allow_from_raw = channel_cfg.get("allowFrom")
        if not isinstance(allow_from_raw, list):
            continue
        values = [str(v).strip() for v in allow_from_raw if str(v).strip()]
        if values:
            legacy[channel_name] = values
    return legacy


def migrate_allow_from(
    policy: PolicyConfig,
    legacy_allow_from: dict[str, list[str]],
) -> tuple[PolicyConfig, list[str], bool]:
    """Migrate legacy channels.*.allowFrom entries into policy channel defaults."""
    if not legacy_allow_from:
        return policy, ["no legacy allowFrom entries found"], False

    notes: list[str] = []
    changed = False

    for channel_name in ("telegram", "whatsapp"):
        allow_from = [str(v).strip() for v in legacy_allow_from.get(channel_name, []) if str(v).strip()]
        if not allow_from:
            continue

        channel_policy = policy.channels.get(channel_name)
        if channel_policy is None:
            channel_policy = ChannelPolicy()
            policy.channels[channel_name] = channel_policy

        current_who = channel_policy.default.who_can_talk
        if current_who and (
            (current_who.mode is not None and current_who.mode != "everyone")
            or (current_who.senders is not None and len(current_who.senders) > 0)
        ):
            notes.append(
                f"skipped channels.{channel_name}.allowFrom migration; "
                f"channels.{channel_name}.default.whoCanTalk already configured"
            )
            continue

        channel_policy.default.who_can_talk = WhoCanTalkPolicyOverride(
            mode="allowlist",
            senders=allow_from,
        )
        notes.append(
            f"migrated channels.{channel_name}.allowFrom -> channels.{channel_name}.default.whoCanTalk"
        )
        changed = True

    for unsupported in ("discord", "feishu"):
        allow_from = [str(v).strip() for v in legacy_allow_from.get(unsupported, []) if str(v).strip()]
        if allow_from:
            notes.append(
                f"channels.{unsupported}.allowFrom found but policy engine currently applies to telegram/whatsapp only"
            )

    return policy, notes, changed
