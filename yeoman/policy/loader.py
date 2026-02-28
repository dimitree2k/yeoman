"""Policy file loading utilities."""

from __future__ import annotations

import json
from pathlib import Path

from nanobot.policy.schema import PolicyConfig


def get_policy_path() -> Path:
    """Get the default policy file path."""
    from nanobot.utils.helpers import get_data_path
    return get_data_path() / "policy.json"


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
        json.dump(
            policy.model_dump(by_alias=True, exclude_none=True),
            f,
            indent=2,
            ensure_ascii=False,
        )
    tmp_path.replace(policy_path)


def ensure_policy_file(path: Path | None = None) -> Path:
    """Create policy file if missing and return its path."""
    policy_path = path or get_policy_path()
    if not policy_path.exists():
        save_policy(PolicyConfig(), policy_path)
    return policy_path
