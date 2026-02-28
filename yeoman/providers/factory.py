"""Factory helpers for task-specific provider construction."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from nanobot.providers.litellm_provider import LiteLLMProvider

if TYPE_CHECKING:
    from nanobot.config.schema import Config
    from nanobot.providers.base import LLMProvider


@dataclass(slots=True)
class ProviderFactory:
    """Build scoped provider instances for routed task models."""

    config: "Config"

    def create_chat_provider(self, model: str) -> "LLMProvider":
        """Create a provider bound to the supplied model route."""
        provider_cfg = self.config.get_provider(model)
        api_key = provider_cfg.api_key if provider_cfg and provider_cfg.api_key else None
        api_base = provider_cfg.api_base if provider_cfg else None
        extra_headers = provider_cfg.extra_headers if provider_cfg else None
        return LiteLLMProvider(
            api_key=api_key,
            api_base=api_base,
            default_model=model,
            extra_headers=extra_headers,
        )
