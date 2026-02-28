"""Capability model routing with channel-aware overrides and fallback chains.

Supports:
- Channel-scoped route overrides (e.g., `whatsapp.tts.speak`)
- Fallback chains per profile (try each fallback on 429/5xx)
- Cooldown tracking to avoid hammering degraded providers
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from yeoman.config.schema import ModelProfile, ModelRoutingConfig


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
    # Fallback chain (profile names) for this resolved profile
    fallback: tuple[str, ...] = ()
    # Cooldown seconds for this profile
    cooldown_seconds: int = 60


@dataclass
class CooldownEntry:
    """Tracks cooldown state for a profile."""

    until: float  # monotonic timestamp when cooldown expires
    error_type: str  # "429" or "5xx"


@dataclass
class RouterState:
    """Mutable state for cooldown tracking."""

    # profile_name -> CooldownEntry
    cooldowns: dict[str, CooldownEntry] = field(default_factory=dict)


class ModelRouter:
    """Resolve capability routes into concrete model profiles with fallback support.

    Usage:
        router = ModelRouter(routing_config)
        profile = router.resolve("tts.speak", channel="whatsapp")

        # On error, mark profile as degraded:
        router.mark_error(profile.profile_name, error_type="429")

        # Check if profile is in cooldown:
        if router.is_in_cooldown(profile.profile_name):
            # Try next fallback
            ...
    """

    def __init__(self, routing: "ModelRoutingConfig") -> None:
        self._routing = routing
        self._state = RouterState()

    def resolve(self, task_key: str, channel: str | None = None) -> ResolvedProfile:
        """Resolve a route key, preferring `channel.task_key` when present.

        Returns the first non-cooldown profile in the fallback chain,
        or the primary profile if all are in cooldown.
        """
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

        # If primary is not in cooldown, return it
        if not self.is_in_cooldown(profile_name):
            return self._to_resolved(chosen_key, profile_name, profile)

        # Primary is in cooldown; try fallbacks
        for fallback_name in profile.fallback:
            if not self.is_in_cooldown(fallback_name):
                fallback_profile = self._routing.profiles.get(fallback_name)
                if fallback_profile is not None:
                    return self._to_resolved(chosen_key, fallback_name, fallback_profile)

        # All in cooldown; return primary anyway
        return self._to_resolved(chosen_key, profile_name, profile)

    def resolve_primary(self, task_key: str, channel: str | None = None) -> ResolvedProfile:
        """Resolve the primary profile, ignoring cooldown state.

        Use this when you need to know the primary regardless of fallback.
        """
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

    def resolve_by_profile(self, profile_name: str) -> ResolvedProfile:
        """Resolve directly by profile name, bypassing route lookup.

        Raises KeyError if the profile does not exist.
        """
        profile = self._routing.profiles.get(profile_name)
        if profile is None:
            raise KeyError(f"No model profile named '{profile_name}'")
        return self._to_resolved(profile_name, profile_name, profile)

    def is_in_cooldown(self, profile_name: str) -> bool:
        """Check if a profile is currently in cooldown."""
        entry = self._state.cooldowns.get(profile_name)
        if entry is None:
            return False
        if time.monotonic() >= entry.until:
            # Cooldown expired; clean up
            del self._state.cooldowns[profile_name]
            return False
        return True

    def mark_error(self, profile_name: str, error_type: str) -> None:
        """Mark a profile as degraded due to an error.

        Args:
            profile_name: The profile that failed
            error_type: "429" for rate limit, "5xx" for server error
        """
        profile = self._routing.profiles.get(profile_name)
        cooldown_seconds = profile.cooldown_seconds if profile else 60

        self._state.cooldowns[profile_name] = CooldownEntry(
            until=time.monotonic() + cooldown_seconds,
            error_type=error_type,
        )

    def clear_cooldown(self, profile_name: str) -> None:
        """Manually clear cooldown for a profile (e.g., after successful request)."""
        self._state.cooldowns.pop(profile_name, None)

    def get_cooldown_state(self) -> dict[str, tuple[float, str]]:
        """Get current cooldown state for diagnostics.

        Returns:
            Dict mapping profile_name to (expires_at_monotonic, error_type)
        """
        now = time.monotonic()
        # Clean expired entries
        expired = [name for name, entry in self._state.cooldowns.items() if now >= entry.until]
        for name in expired:
            del self._state.cooldowns[name]

        return {
            name: (entry.until, entry.error_type)
            for name, entry in self._state.cooldowns.items()
        }

    def _to_resolved(self, route_key: str, profile_name: str, profile: "ModelProfile") -> ResolvedProfile:
        return ResolvedProfile(
            route_key=route_key,
            profile_name=profile_name,
            kind=profile.kind,
            model=profile.model,
            provider=profile.provider,
            max_tokens=profile.max_tokens,
            temperature=profile.temperature,
            timeout_ms=profile.timeout_ms,
            fallback=tuple(profile.fallback),
            cooldown_seconds=profile.cooldown_seconds,
        )
