"""Regression tests for provider registry routing and LiteLLMProvider env setup.

Protects against two bug classes that leaked memory-embedding calls to the
wrong endpoint:

1. ``find_by_model`` matching provider keywords as substrings anywhere in the
   model string. For names like ``groq/openai/gpt-oss-20b`` this returned the
   wrong spec because "openai" appears as a substring. The fix matches the
   first ``/``-separated segment when the model has a provider prefix.

2. ``LiteLLMProvider.__init__`` writing ``litellm.api_base`` as a module-level
   global. That leaked the primary chat provider's base URL into every other
   ``litellm.*`` call in the process (embeddings, classifier, etc.).
"""

from __future__ import annotations

import os
from types import SimpleNamespace

import litellm
import pytest
from yeoman_gateway.providers.factory import ProviderFactory
from yeoman_gateway.providers.litellm_provider import LiteLLMProvider
from yeoman_gateway.providers.registry import find_by_model
from yeoman_shared.config.schema import Config

# --------------------------------------------------------------------------
# find_by_model: prefixed model names resolve by the first /-segment only.
# --------------------------------------------------------------------------


def test_find_by_model_prefixed_groq_openai_picks_groq() -> None:
    """The Groq-hosted gpt-oss model name contains both 'groq' and 'openai'.
    Substring matching used to pick openai (which comes first in PROVIDERS)."""
    spec = find_by_model("groq/openai/gpt-oss-20b")
    assert spec is not None
    assert spec.name == "groq"


def test_find_by_model_prefixed_openai_embedding() -> None:
    spec = find_by_model("openai/text-embedding-3-small")
    assert spec is not None
    assert spec.name == "openai"


def test_find_by_model_prefixed_gemini() -> None:
    spec = find_by_model("gemini/gemini-pro")
    assert spec is not None
    assert spec.name == "gemini"


def test_find_by_model_unprefixed_legacy_match() -> None:
    """Unprefixed names still fall back to substring keyword matching."""
    assert find_by_model("claude-3-opus").name == "anthropic"
    assert find_by_model("gpt-4o-mini").name == "openai"


def test_find_by_model_unprefixed_mimo_picks_xiaomi() -> None:
    spec = find_by_model("mimo-v2.5-pro")
    assert spec is not None
    assert spec.name == "xiaomi"


def test_find_by_model_unknown_prefix_returns_none() -> None:
    """Explicit prefix that doesn't match any standard provider: don't guess."""
    assert find_by_model("unknown_vendor/some-model") is None


# --------------------------------------------------------------------------
# LiteLLMProvider does not leak api_base to the litellm module, and does not
# write the wrong env var for Groq-hosted OpenAI-named models.
# --------------------------------------------------------------------------


def test_litellm_provider_does_not_set_global_api_base(monkeypatch) -> None:
    """Previously, constructing a provider with an api_base mutated
    ``litellm.api_base``. That global state leaked into every other
    ``litellm.*`` call — most visibly, memory embeddings being routed to
    the primary chat provider's endpoint."""
    monkeypatch.setattr(litellm, "api_base", None, raising=False)
    LiteLLMProvider(
        api_key="sk-or-test",
        api_base="https://openrouter.ai/api/v1",
        default_model="openrouter/anthropic/claude-3-opus",
    )
    assert getattr(litellm, "api_base", None) is None


def test_litellm_provider_groq_model_does_not_pollute_openai_env(
    monkeypatch,
) -> None:
    """Classifier uses model 'groq/openai/gpt-oss-20b'. Before the fix,
    _setup_env routed the groq api_key into OPENAI_API_KEY because
    find_by_model matched 'openai' as a substring. Subsequent memory
    embedding calls then authenticated to OpenAI with the Groq key."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    LiteLLMProvider(
        api_key="gsk_classifier_key",
        api_base=None,
        default_model="groq/openai/gpt-oss-20b",
    )
    assert os.environ.get("GROQ_API_KEY") == "gsk_classifier_key"
    assert "OPENAI_API_KEY" not in os.environ


def test_provider_factory_uses_xiaomi_default_base_url() -> None:
    config = Config.model_validate({
        "providers": {
            "xiaomi": {
                "api_key": "mimo-test-key",
            }
        }
    })

    provider = ProviderFactory(config=config).create_chat_provider(
        "mimo-v2.5-pro",
        "xiaomi",
    )

    assert isinstance(provider, LiteLLMProvider)
    assert provider.api_key == "mimo-test-key"
    assert provider.api_base == "https://api.xiaomimimo.com/v1"
    assert provider._resolve_model("mimo-v2.5-pro") == "openai/mimo-v2.5-pro"


@pytest.mark.asyncio
async def test_litellm_provider_passes_api_key_and_base_per_call(monkeypatch) -> None:
    captured: dict[str, object] = {}

    async def fake_acompletion(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    finish_reason="stop",
                    message=SimpleNamespace(
                        content="hello from mimo",
                        reasoning_content=None,
                        tool_calls=[],
                    ),
                )
            ],
            usage=None,
        )

    monkeypatch.setattr(
        "yeoman_gateway.providers.litellm_provider.acompletion",
        fake_acompletion,
    )
    provider = LiteLLMProvider(
        api_key="mimo-test-key",
        api_base="https://api.xiaomimimo.com/v1",
        default_model="mimo-v2.5",
    )

    response = await provider.chat(
        [{"role": "user", "content": "say hi"}],
        model="mimo-v2.5",
        max_tokens=32,
        temperature=1.0,
    )

    assert response.content == "hello from mimo"
    assert captured["model"] == "openai/mimo-v2.5"
    assert captured["api_key"] == "mimo-test-key"
    assert captured["api_base"] == "https://api.xiaomimimo.com/v1"


def test_litellm_provider_preserves_reasoning_content_from_response() -> None:
    provider = LiteLLMProvider(default_model="deepseek-v4-flash")
    response = SimpleNamespace(
        choices=[
            SimpleNamespace(
                finish_reason="tool_calls",
                message=SimpleNamespace(
                    content="I need to search.",
                    reasoning_content="The user asked for current data, so I need a tool.",
                    tool_calls=[],
                ),
            )
        ],
        usage=None,
    )

    parsed = provider._parse_response(response)

    assert parsed.reasoning_content == "The user asked for current data, so I need a tool."
