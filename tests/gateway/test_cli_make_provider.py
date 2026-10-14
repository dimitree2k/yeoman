"""The assistant's default provider honours the provider its route pins."""

from __future__ import annotations

from yeoman_gateway.cli.core import make_provider
from yeoman_shared.config.schema import Config


def test_pinned_openrouter_is_not_lost_to_keyword_matching() -> None:
    config = Config.model_validate(
        {
            "models": {
                "routes": {"assistant.reply": "assistant"},
                "profiles": {
                    "assistant": {
                        "kind": "chat",
                        "model": "~z-ai/glm-flash-latest",
                        "provider": "openrouter",
                    }
                },
            },
        }
    )

    # Keys are injected from the secret store at load time, not validated from JSON.
    config.providers.openrouter.api_key = "sk-or-synthetic"
    config.providers.zhipu.api_key = "zhipu-synthetic"
    config.providers.zhipu.api_base = "https://zhipu.invalid/v4"

    provider = make_provider(config)

    assert provider.provider_name == "openrouter"
    assert provider.api_key == "sk-or-synthetic"
