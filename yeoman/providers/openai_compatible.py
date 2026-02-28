"""Helpers for OpenAI-compatible HTTP credentials (audio, embeddings, etc.)."""

from __future__ import annotations

from dataclasses import dataclass

from nanobot.config.schema import Config


@dataclass(frozen=True, slots=True)
class OpenAICompatibleCredentials:
    api_key: str
    api_base: str | None
    extra_headers: dict[str, str] | None
    source: str


def resolve_openai_compatible_credentials(config: Config) -> OpenAICompatibleCredentials | None:
    """Return the best OpenAI-compatible credentials payload available in config.

    Precedence:
      1) providers.openai
      2) providers.openrouter (OpenAI-compatible gateway; uses /api/v1)
      3) providers.aihubmix (OpenAI-compatible gateway)
      4) providers.vllm (user-provided OpenAI-compatible base)
    """
    openai = config.providers.openai
    if openai.api_key.strip():
        return OpenAICompatibleCredentials(
            api_key=openai.api_key.strip(),
            api_base=openai.api_base,
            extra_headers=openai.extra_headers,
            source="providers.openai",
        )

    openrouter = config.providers.openrouter
    if openrouter.api_key.strip():
        return OpenAICompatibleCredentials(
            api_key=openrouter.api_key.strip(),
            api_base=openrouter.api_base or "https://openrouter.ai/api/v1",
            extra_headers=openrouter.extra_headers,
            source="providers.openrouter",
        )

    aihubmix = config.providers.aihubmix
    if aihubmix.api_key.strip():
        return OpenAICompatibleCredentials(
            api_key=aihubmix.api_key.strip(),
            api_base=aihubmix.api_base or "https://aihubmix.com/v1",
            extra_headers=aihubmix.extra_headers,
            source="providers.aihubmix",
        )

    vllm = config.providers.vllm
    if vllm.api_key.strip() and (vllm.api_base or "").strip():
        return OpenAICompatibleCredentials(
            api_key=vllm.api_key.strip(),
            api_base=vllm.api_base,
            extra_headers=vllm.extra_headers,
            source="providers.vllm",
        )

    return None

