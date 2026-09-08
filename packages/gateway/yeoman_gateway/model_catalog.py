"""Provider-specific capability facts. Refreshing these never changes profiles."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

OPENROUTER_MODELS = "https://openrouter.ai/api/v1/models"
EFFORTS = ["minimal", "low", "medium", "high", "xhigh", "max"]


class ModelCard(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider: str
    model: str
    reasoning: Literal["unknown", "unsupported", "optional", "mandatory"] = "unknown"
    efforts: list[str] = Field(default_factory=list)
    reasoning_budget: bool = False
    default_enabled: bool | None = None
    default_effort: str | None = None
    temperature: bool | None = None
    temperature_with_reasoning: bool | None = None
    max_output_tokens: int | None = None
    context_tokens: int | None = None
    tools: bool | None = None
    input_modalities: list[str] = Field(default_factory=list)
    alias_target: str | None = None
    source: str | None = None
    checked_at: str | None = None


class ModelCatalog(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: Literal[1] = 1
    cards: list[ModelCard] = Field(default_factory=list)

    def find(self, provider: str, model: str) -> ModelCard:
        return next((c for c in self.cards if (c.provider, c.model) == (provider, model)),
                    ModelCard(provider=provider, model=model))


def openrouter_card(data: dict[str, Any]) -> ModelCard:
    params = data.get("supported_parameters")
    reason = data.get("reasoning")
    status = "unknown"
    if isinstance(reason, dict):
        status = "mandatory" if reason.get("mandatory") else "optional"
    elif params is not None and not {"reasoning", "reasoning_effort"}.intersection(params):
        status = "unsupported"
    reason = reason if isinstance(reason, dict) else {}
    efforts = reason.get("supported_efforts", [])
    top = data.get("top_provider") or {}
    alias = data.get("alias_target") or {}
    return ModelCard(
        provider="openrouter", model=data["id"], reasoning=status,
        efforts=[e for e in (EFFORTS if efforts is None else efforts) if e != "none"],
        reasoning_budget=reason.get("supports_max_tokens", False),
        default_enabled=reason.get("default_enabled"), default_effort=reason.get("default_effort"),
        temperature="temperature" in params if params is not None else None,
        max_output_tokens=top.get("max_completion_tokens"), context_tokens=data.get("context_length"),
        tools="tools" in params if params is not None else None,
        input_modalities=(data.get("architecture") or {}).get("input_modalities", []),
        alias_target=alias.get("slug") if isinstance(alias, dict) else str(alias),
        source=OPENROUTER_MODELS, checked_at=datetime.now(UTC).isoformat(),
    )


def direct_card(provider: str, model: str) -> ModelCard:
    """Only documented, exact models; unknown models do not inherit family guesses."""
    name = model.removeprefix(provider + "/").removeprefix("zai/")
    facts: dict[str, Any] = {}
    if provider == "deepseek" and name in {"deepseek-v4-pro", "deepseek-v4-flash"}:
        facts = dict(reasoning="optional", efforts=["low", "high", "max"], default_enabled=True,
                     default_effort="high", temperature_with_reasoning=False,
                     source="https://api-docs.deepseek.com/guides/thinking_mode/")
    elif provider == "xiaomi" and name in {"mimo-v2.5", "mimo-v2.5-pro"}:
        facts = dict(reasoning="optional", default_enabled=True, temperature_with_reasoning=False,
                     source="https://platform.xiaomimimo.com/docs/en-US/usage-guide/passing-back-reasoning_content")
    elif provider == "zhipu" and name == "glm-5.1":
        facts = dict(reasoning="optional", default_enabled=True,
                     source="https://docs.z.ai/guides/capabilities/thinking")
    elif provider == "groq" and name in {"openai/gpt-oss-20b", "openai/gpt-oss-120b"}:
        facts = dict(reasoning="mandatory", efforts=["low", "medium", "high"], default_enabled=True,
                     default_effort="medium", temperature=True,
                     source="https://console.groq.com/docs/api-reference")
    if facts:
        facts["checked_at"] = "2026-09-08T00:00:00+00:00"
    return ModelCard(provider=provider, model=model, **facts)


def validate_reasoning(card: ModelCard, reasoning: dict[str, Any] | None) -> None:
    if not reasoning:
        return  # Omitted means provider default, including for unknown models.
    if card.reasoning in {"unknown", "unsupported"}:
        raise ValueError(f"Reasoning is {card.reasoning} for {card.provider}/{card.model}")
    if set(reasoning) - {"enabled", "effort", "max_tokens", "exclude"}:
        raise ValueError("Unknown reasoning parameter")
    for key in ("enabled", "exclude"):
        if key in reasoning and type(reasoning[key]) is not bool:
            raise ValueError(f"{key} must be boolean")
    if "exclude" in reasoning and card.provider != "openrouter":
        raise ValueError("exclude is only supported for OpenRouter")
    if reasoning.get("enabled") is False:
        if card.reasoning == "mandatory":
            raise ValueError("Cannot disable mandatory reasoning")
        if "effort" in reasoning or "max_tokens" in reasoning:
            raise ValueError("Disabled reasoning cannot have an effort or budget")
    if "effort" in reasoning and reasoning["effort"] not in card.efforts:
        raise ValueError(f"Invalid effort {reasoning['effort']!r}; choices: {', '.join(card.efforts) or 'none'}")
    if "max_tokens" in reasoning:
        value = reasoning["max_tokens"]
        if not card.reasoning_budget or type(value) is not int or value <= 0:
            raise ValueError("Reasoning budget unsupported or not a positive integer")
        if "effort" in reasoning:
            raise ValueError("Choose effort or reasoning budget, not both")


def reasoning_kwargs(provider: str, reasoning: dict[str, Any] | None) -> dict[str, Any]:
    if not reasoning:
        return {}
    if provider == "openrouter":
        return {"extra_body": {"reasoning": reasoning}}
    if provider in {"deepseek", "zhipu", "xiaomi"}:
        allowed = {"enabled", "effort"} if provider == "deepseek" else {"enabled"}
        if set(reasoning) - allowed:
            raise ValueError(f"Unsupported reasoning settings for {provider}")
        body: dict[str, Any] = {"thinking": {"type": "disabled" if reasoning.get("enabled") is False else "enabled"}}
        if "effort" in reasoning:
            # LiteLLM's DeepSeek mapping drops effort; preserve it in the native body.
            body["reasoning_effort"] = reasoning["effort"]
        return {"extra_body": body}
    if provider == "groq":
        if set(reasoning) - {"enabled", "effort"} or reasoning.get("enabled") is False:
            raise ValueError("Unsupported Groq reasoning settings")
        return {"reasoning_effort": reasoning["effort"]} if "effort" in reasoning else {}
    raise ValueError(f"No verified reasoning adapter for {provider}")
