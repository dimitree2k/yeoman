from __future__ import annotations

import pytest
from yeoman_gateway.app import bootstrap
from yeoman_gateway.processing import model_route
from yeoman_gateway.processing.models import ShortReplyClaim
from yeoman_gateway.short_reply.reactor import ShortReplyReactor
from yeoman_shared.config.schema import Config, ModelProfile


class _Store:
    def recent_reactions(self, **kwargs):
        return ()

    def claim_short_reply(self, **kwargs):
        return ShortReplyClaim("claimed")

    def complete_short_reply(self, **kwargs):
        return None


def _config(mode: str) -> Config:
    config = Config()
    config.processing.enabled = True
    config.processing.chats = ["whatsapp:managed@g.us"]
    config.processing.short_reply = config.processing.short_reply.model_copy(
        update={"mode": mode, "route": "reaction.decide"}
    )
    config.models.profiles["reaction_decide"] = ModelProfile(
        kind="chat",
        model="openai/gpt-6-luna",
        provider="openrouter",
        max_tokens=48,
        temperature=0.0,
        timeout_ms=6000,
        fallback=[],
        reasoning=None,
    )
    config.models.routes["reaction.decide"] = "reaction_decide"
    return config


def _fail_if_built(*, config, route_key):
    raise AssertionError(f"unexpected route build: {route_key}")


def test_no_reactor_when_feature_is_off_processing_is_disabled_or_store_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(model_route, "RouteClient", _fail_if_built)
    assert bootstrap._build_short_reply_reactor(_config("off"), _Store()) is None

    disabled = _config("shadow")
    disabled.processing.enabled = False
    assert bootstrap._build_short_reply_reactor(disabled, _Store()) is None
    assert bootstrap._build_short_reply_reactor(_config("shadow"), None) is None


def test_a_missing_route_disables_the_reactor(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(model_route, "RouteClient", _fail_if_built)
    config = _config("shadow")
    config.models.routes.pop("reaction.decide")
    assert bootstrap._build_short_reply_reactor(config, _Store()) is None


def test_a_route_to_a_missing_profile_disables_the_reactor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(model_route, "RouteClient", _fail_if_built)
    config = _config("shadow")
    config.models.routes["reaction.decide"] = "missing_profile"
    assert bootstrap._build_short_reply_reactor(config, _Store()) is None


@pytest.mark.parametrize(
    ("profile_name", "model", "provider"),
    [
        ("reaction_decide", "openai/gpt-6-luna", "other"),
        ("reaction_decide", "other/model", "openrouter"),
        ("another_profile", "openai/gpt-6-luna", "openrouter"),
    ],
)
def test_an_unapproved_profile_disables_the_reactor(
    monkeypatch: pytest.MonkeyPatch, profile_name: str, model: str, provider: str
) -> None:
    monkeypatch.setattr(model_route, "RouteClient", _fail_if_built)
    config = _config("shadow")
    config.models.routes["reaction.decide"] = profile_name
    config.models.profiles[profile_name] = ModelProfile(
        kind="chat",
        model=model,
        provider=provider,
        max_tokens=48,
        temperature=0.0,
        timeout_ms=6000,
        fallback=[],
        reasoning=None,
    )
    assert bootstrap._build_short_reply_reactor(config, _Store()) is None


def test_an_unapproved_configured_route_disables_the_reactor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(model_route, "RouteClient", _fail_if_built)
    config = _config("shadow")
    config.processing.short_reply = config.processing.short_reply.model_copy(
        update={"route": "other.route"}
    )
    assert bootstrap._build_short_reply_reactor(config, _Store()) is None


def test_route_unavailable_disables_the_reactor(monkeypatch: pytest.MonkeyPatch) -> None:
    def unavailable(*, config, route_key):
        raise model_route.RouteUnavailableError("unavailable")

    monkeypatch.setattr(model_route, "RouteClient", unavailable)
    assert bootstrap._build_short_reply_reactor(_config("shadow"), _Store()) is None


def test_resolved_model_mismatch_disables_the_reactor(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Client:
        model = "other/model"

        def __init__(self, *, config, route_key):
            assert route_key == "reaction.decide"

    monkeypatch.setattr(model_route, "RouteClient", _Client)
    assert bootstrap._build_short_reply_reactor(_config("shadow"), _Store()) is None


def test_a_configured_route_builds_the_reactor_with_owner_vocabulary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, str] = {}

    class _Client:
        def __init__(self, *, config, route_key):
            seen["route"] = route_key
            self.model = "openai/gpt-6-luna"
            self.route_key = route_key

    monkeypatch.setattr(model_route, "RouteClient", _Client)
    config = _config("shadow")
    config.processing.reaction_emojis = ["👍", "🤙"]
    reactor = bootstrap._build_short_reply_reactor(config, _Store())

    assert isinstance(reactor, ShortReplyReactor)
    assert seen["route"] == "reaction.decide"
    assert reactor.mode_for("whatsapp", "managed@g.us") == "shadow"
    assert reactor.mode_for("whatsapp", "unmanaged@g.us") == "off"
    assert reactor._decider._vocabulary == ("👍", "🤙")


def test_empty_short_reply_chats_stays_inside_processing_managed_chats(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Client:
        model = "openai/gpt-6-luna"

        def __init__(self, *, config, route_key):
            assert route_key == "reaction.decide"

    monkeypatch.setattr(model_route, "RouteClient", _Client)
    config = _config("shadow")
    reactor = bootstrap._build_short_reply_reactor(config, _Store())
    assert reactor.mode_for("whatsapp", "managed@g.us") == "shadow"
    assert reactor.mode_for("whatsapp", "unmanaged@g.us") == "off"


def test_nonempty_short_reply_chats_are_intersected_with_managed_chats(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Client:
        model = "openai/gpt-6-luna"

        def __init__(self, *, config, route_key):
            assert route_key == "reaction.decide"

    monkeypatch.setattr(model_route, "RouteClient", _Client)
    config = _config("shadow")
    config.processing.chats = [
        "whatsapp:managed@g.us",
        "whatsapp:also-managed@g.us",
    ]
    config.processing.short_reply = config.processing.short_reply.model_copy(
        update={"chats": ["whatsapp:managed@g.us", "whatsapp:unmanaged@g.us"]}
    )
    reactor = bootstrap._build_short_reply_reactor(config, _Store())

    assert reactor.mode_for("whatsapp", "managed@g.us") == "shadow"
    assert reactor.mode_for("whatsapp", "also-managed@g.us") == "off"
    assert reactor.mode_for("whatsapp", "unmanaged@g.us") == "off"


def test_no_reactor_when_effective_chat_intersection_is_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(model_route, "RouteClient", _fail_if_built)
    config = _config("shadow")
    config.processing.short_reply = config.processing.short_reply.model_copy(
        update={"chats": ["whatsapp:unmanaged@g.us"]}
    )
    assert bootstrap._build_short_reply_reactor(config, _Store()) is None


def test_no_reactor_when_there_are_no_processing_managed_chats(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(model_route, "RouteClient", _fail_if_built)
    config = _config("shadow")
    config.processing.chats = []
    assert bootstrap._build_short_reply_reactor(config, _Store()) is None


def test_the_orchestrator_hands_the_reactor_to_the_middleware() -> None:
    import inspect

    from yeoman_gateway.core.orchestrator import Orchestrator

    assert "short_reply_reactor" in inspect.signature(Orchestrator.__init__).parameters
