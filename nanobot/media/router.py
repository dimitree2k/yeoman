"""Capability model routing with channel-aware overrides."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nanobot.config.schema import ModelProfile, ModelRoutingConfig


@dataclass(frozen=True, slots=True)
class ResolvedProfile:
    """Resolved route/profile payload used by media executors."""

    route_key: str
    profile_name: str
    kind: str
    model: str | None
    provider: str | None
    max_tokens: int | None
    temperature: float | None
    timeout_ms: int | None


class ModelRouter:
    """Resolve capability routes into concrete model profiles."""

    def __init__(self, routing: "ModelRoutingConfig") -> None:
        self._routing = routing

    def resolve(self, task_key: str, channel: str | None = None) -> ResolvedProfile:
        """Resolve a route key, preferring `channel.task_key` when present."""
        scoped_key = f"{channel}.{task_key}" if channel else None
        chosen_key: str | None = None
        if scoped_key and scoped_key in self._routing.routes:
            chosen_key = scoped_key
        elif task_key in self._routing.routes:
            chosen_key = task_key

        if chosen_key is None:
            raise KeyError(f"No model route configured for task '{task_key}'")

        profile_name = self._routing.routes[chosen_key]
        profile = self._routing.profiles.get(profile_name)
        if profile is None:
            raise KeyError(
                f"Route '{chosen_key}' points to missing profile '{profile_name}'"
            )
        return self._to_resolved(chosen_key, profile_name, profile)

    @staticmethod
    def _to_resolved(route_key: str, profile_name: str, profile: "ModelProfile") -> ResolvedProfile:
        return ResolvedProfile(
            route_key=route_key,
            profile_name=profile_name,
            kind=profile.kind,
            model=profile.model,
            provider=profile.provider,
            max_tokens=profile.max_tokens,
            temperature=profile.temperature,
            timeout_ms=profile.timeout_ms,
        )
