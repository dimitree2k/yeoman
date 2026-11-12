"""Each route target gets the strictest structured-output format its provider accepts."""

import json
from types import SimpleNamespace

import pytest
from yeoman_gateway.processing.model_route import adapt_messages_and_format
from yeoman_gateway.providers.registry import supported_structured_output

SCHEMA = {
    "type": "json_schema",
    "json_schema": {"name": "x", "strict": True,
                    "schema": {"type": "object", "properties": {"id": {"type": "string"}}}},
}
MESSAGES = [{"role": "system", "content": "Decide."}, {"role": "user", "content": "data"}]


@pytest.mark.parametrize(
    ("provider", "model", "requested", "expected"),
    [
        ("deepseek", "deepseek-v4-flash", "json_schema_strict", "json_object"),
        ("deepseek", "deepseek-v4-flash", "json_object", "json_object"),
        ("deepseek", "deepseek-v4-flash", "prompt_only", "prompt_only"),
        ("openrouter", "z-ai/glm-5.3-flash", "json_schema_strict", "json_schema_strict"),
        (None, "deepseek-v4-pro", "json_schema_strict", "json_object"),
        ("openrouter", "z-ai/glm-5.3-flash", "nonsense", "json_schema_strict"),
    ],
)
def test_supported_mode_is_lowered_to_what_the_provider_accepts(
    provider, model, requested, expected
):
    assert supported_structured_output(provider, model, requested) == expected


def test_schema_passes_unchanged_to_a_schema_target():
    messages, fmt = adapt_messages_and_format(MESSAGES, SCHEMA, "json_schema_strict")
    assert fmt is SCHEMA and messages is MESSAGES


def test_schema_becomes_json_object_plus_written_schema():
    messages, fmt = adapt_messages_and_format(MESSAGES, SCHEMA, "json_object")
    assert fmt == {"type": "json_object"}
    assert json.dumps(SCHEMA["json_schema"]["schema"]) in messages[0]["content"]
    assert messages[0]["content"].startswith("Decide.")
    assert MESSAGES[0]["content"] == "Decide."  # the caller's list is untouched


def test_prompt_only_target_gets_no_format_but_the_instruction():
    messages, fmt = adapt_messages_and_format(MESSAGES, {"type": "json_object"}, "prompt_only")
    assert fmt is None
    assert "JSON object" in messages[0]["content"]


def test_without_system_message_an_instruction_is_prepended():
    messages, fmt = adapt_messages_and_format(
        [{"role": "user", "content": "data"}], SCHEMA, "json_object"
    )
    assert messages[0]["role"] == "system" and fmt == {"type": "json_object"}


def test_no_format_stays_no_format():
    assert adapt_messages_and_format(MESSAGES, None, "json_object") == (MESSAGES, None)


@pytest.mark.asyncio
async def test_fallback_target_gets_its_own_format():
    """A DeepSeek primary and a schema-capable fallback each get their own format."""
    from yeoman_gateway.processing.model_route import RouteClient, _RouteTarget

    seen = []

    class _Provider:
        def __init__(self, fail):
            self.fail = fail

        async def chat(self, **kwargs):
            seen.append(kwargs["response_format"])
            if self.fail:
                raise RuntimeError("down")
            return SimpleNamespace(content='{"id": "a"}', usage={}, finish_reason="stop",
                                   model="m", diagnostics={})

    client = RouteClient.__new__(RouteClient)
    client.route_key = "r"
    client.model = "deepseek-v4-flash"
    client.timeout_ms = 0
    client.reasoning = None
    client.structured_output = "json_object"
    client._primary = _RouteTarget("p", "deepseek-v4-flash", 0, None, _Provider(True),
                                   structured_output="json_object")
    fallback = _RouteTarget("f", "glm", 0, None, _Provider(False),
                            structured_output="json_schema_strict")
    client._fallback_names = ("f",)
    client._resolve_fallback = lambda name: fallback
    await client.chat_with_usage(MESSAGES, max_tokens=50, response_format=SCHEMA)
    assert seen == [{"type": "json_object"}, SCHEMA]
