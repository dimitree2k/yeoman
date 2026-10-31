"""Policy opt-in, IPC validation and the best-effort outbound metadata hook."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from yeoman_gateway.ipc.gateway_socket import GatewaySocket
from yeoman_gateway.policy.engine import PolicyEngine
from yeoman_gateway.policy.schema import PolicyConfig
from yeoman_gateway.processing.dispatch import IntentEffectRouter
from yeoman_gateway.processing.models import EffectEnvelope, EffectTarget, TextPayload
from yeoman_gateway.reception.outbound import ReceptionOutboundRecorder
from yeoman_gateway.reception.store import SentimentStore

CHAT = "120363000000000042@g.us"
OTHER = "120363000000000043@g.us"


def _policy() -> PolicyConfig:
    return PolicyConfig.model_validate({
        "channels": {
            "whatsapp": {
                "chats": {
                    CHAT: {"receptionMetrics": True, "comment": "Ente"},
                    OTHER: {"comment": "Finanzgruppe"},
                }
            }
        }
    })


def test_reception_flag_resolves_per_chat_only(tmp_path: Path) -> None:
    engine = PolicyEngine(_policy(), tmp_path)
    assert engine.reception_enabled("whatsapp", CHAT) is True
    assert engine.reception_enabled("whatsapp", OTHER) is False
    assert engine.reception_chats("whatsapp") == (CHAT,)
    assert engine.resolve_policy("whatsapp", CHAT).reception_metrics is True
    assert engine.resolve_policy("whatsapp", OTHER).reception_metrics is False


@pytest.mark.asyncio
async def test_ipc_sentiment_run_validates_its_arguments(tmp_path: Path) -> None:
    seen: list[dict[str, str]] = []

    async def handler(args):
        seen.append(dict(args))
        return {"status": "ok", "weeks": 1, "labels": 2}

    socket = GatewaySocket(path=tmp_path / "gateway.sock", sentiment_run_handler=handler)
    assert await socket._dispatch({
        "cmd": "sentiment_run", "args": {"since": "2026-09-01", "chat": CHAT},
    }) == {"status": "ok", "weeks": 1, "labels": 2}
    assert seen == [{"since": "2026-09-01", "chat": CHAT}]
    for args in (
        {"since": "2026-09-01"},
        {"since": "2026-09-01", "chat": "no-jid"},
        {"since": "2026-09-01", "chat": CHAT, "extra": "x"},
        {"since": "", "chat": CHAT},
    ):
        reply = await socket._dispatch({"cmd": "sentiment_run", "args": args})
        assert reply["code"] == "INVALID_SENTIMENT"


def _envelope(*, reply_to: str | None = None, trace: str = "outbound:1") -> EffectEnvelope:
    return EffectEnvelope(
        effect_id="effect-1", operation_key="operation-1",
        payload=TextPayload(text="synthetic", reply_to=reply_to),
        target=EffectTarget(channel="whatsapp", chat_id=CHAT), trace_id=trace,
    )


def test_outbound_recorder_records_provider_id_path_and_persona(tmp_path: Path) -> None:
    store = SentimentStore(tmp_path / "ops" / "sentiment.db")
    recorder = ReceptionOutboundRecorder(
        store=store, enabled=lambda chat_id: chat_id == CHAT,
        persona_sha=lambda chat_id: "persona-sha",
    )
    result = SimpleNamespace(
        transport_receipt=SimpleNamespace(provider_message_id="provider-9")
    )
    recorder(_envelope(reply_to="native-7"), None, result, 123)
    meta = store.outbound_meta(CHAT)
    assert meta["provider-9"]["path"] == "direct_reply"
    assert meta["provider-9"]["persona_sha"] == "persona-sha"
    # Disabled chats and receipts without a provider id write nothing.
    recorder(_envelope(), None, result, 124)
    store.record_outbound(
        chat_id=OTHER, message_id="x", sent_ms=1, path="other", persona_sha="s"
    )
    before = set(store.outbound_meta())
    recorder(_envelope(), None, SimpleNamespace(transport_receipt=None), 125)
    assert set(store.outbound_meta()) == before
    store.close()


def test_outbound_recorder_never_raises_on_a_broken_store(tmp_path: Path) -> None:
    class Broken:
        def record_outbound(self, **kwargs):
            raise RuntimeError("disk gone")

    recorder = ReceptionOutboundRecorder(
        store=Broken(), enabled=lambda chat_id: True, persona_sha=lambda chat_id: "sha"
    )
    recorder(
        _envelope(), None,
        SimpleNamespace(transport_receipt=SimpleNamespace(provider_message_id="p")), 1,
    )


def test_router_swallows_reception_hook_failures() -> None:
    router = object.__new__(IntentEffectRouter)

    def exploding(*args, **kwargs):
        raise RuntimeError("metadata failed")

    router._reception_recorder = exploding
    router._record_reception_metadata(_envelope(), None, object(), now=1)
