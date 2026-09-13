from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from yeoman_gateway.a2a.contracts import ContractSchemas
from yeoman_gateway.bus.events import OutboundMessage
from yeoman_gateway.bus.queue import MessageBus
from yeoman_gateway.channels.whatsapp import WhatsAppChannel
from yeoman_gateway.ipc import a2a_invoke
from yeoman_gateway.ipc.a2a_invoke import process_a2a_invocation
from yeoman_gateway.ipc.gateway_socket import GatewaySocket
from yeoman_gateway.processing.dispatch import EffectNotDeliveredError
from yeoman_gateway.processing.models import EffectReceipt, TransportReceipt
from yeoman_shared.config.schema import WhatsAppConfig

_EFFECT_ID = "a2a-effect-50c84ffe7b1984e86d7f14c7397fee6ed7cf5b11"


def _input(**changes: object) -> dict[str, object]:
    value: dict[str, object] = {
        "recipient": {"type": "group", "alias": "team-example"},
        "content": [{"type": "text", "text": "Hello"}],
        "idempotency_key": "request-1",
    }
    value.update(changes)
    return value


class _Policy:
    def __init__(self, *, tools: frozenset[str] = frozenset({"message"})) -> None:
        self.tools = tools
        self.events: list[object] = []

    def evaluate(self, event: object) -> object:
        self.events.append(event)
        return SimpleNamespace(
            accept_message=True,
            should_respond=True,
            allowed_tools=self.tools,
            is_owner=False,
            reason="allowed",
        )


class _Resolver:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def __call__(self, kind: str, alias: str) -> tuple[str | None, str | None]:
        self.calls.append((kind, alias))
        if (kind, alias) == ("group", "team-example"):
            return "private-group@g.us", None
        if alias == "contact-alice":
            return None, "type_mismatch"
        return None, "unknown"


class _Effects:
    def __init__(self, receipt: EffectReceipt | None = None, *, unmanaged: bool = False) -> None:
        self.receipt = receipt or EffectReceipt(
            effect_id=_EFFECT_ID, state="sent", accepted=True
        )
        self.unmanaged = unmanaged
        self.calls: list[dict[str, object]] = []

    async def send(self, **kwargs: object) -> EffectReceipt | None:
        self.calls.append(kwargs)
        if self.unmanaged:
            raise EffectNotDeliveredError("private target leaked@example.test")
        return self.receipt


class _VoiceGenerator:
    def __init__(self, artifact: dict[str, object]) -> None:
        self.artifact = artifact
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def generate(
        self, effect_id: str, request: dict[str, object]
    ) -> dict[str, object]:
        self.calls.append((effect_id, request))
        return self.artifact


class _VoiceSender:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def __call__(self, **kwargs: object) -> EffectReceipt:
        self.calls.append(kwargs)
        return EffectReceipt(effect_id=str(kwargs["effect_id"]), state="sent", accepted=True)


class _EffectStore:
    def __init__(
        self,
        *,
        state: str = "sent",
        provider_message_id: str | None = "provider-1",
        delivery_status: str | None = None,
    ) -> None:
        self.state = state
        self.provider_message_id = provider_message_id
        self.delivery_status = delivery_status

    def get_effect(self, effect_id: str) -> object:
        return SimpleNamespace(effect_id=effect_id, state=self.state)

    def effect_transport_receipt(self, effect_id: str) -> TransportReceipt | None:
        if self.provider_message_id is None:
            return None
        return TransportReceipt(
            channel="whatsapp",
            chat_id="private-group@g.us",
            provider_message_id=self.provider_message_id,
            confirmed_ms=1,
        )

    def delivery_signals(self, *, chat_id: str, message_id: str) -> tuple[object, ...]:
        if self.delivery_status is None:
            return ()
        return (SimpleNamespace(payload={"status": self.delivery_status}),)


async def _invoke(**changes: object) -> tuple[dict[str, object], _Policy, _Resolver, _Effects]:
    policy = changes.pop("policy", _Policy())
    resolver = changes.pop("resolver", _Resolver())
    effects = changes.pop("effects", _Effects())
    store = changes.pop("effect_store", _EffectStore())
    arguments: dict[str, object] = {
        "peer": "hermes",
        "skill": "whatsapp.send",
        "input": _input(),
        "task_id": "task-1",
        "context_id": "context-1",
        "effect_id": _EFFECT_ID,
        "configured_peer": "hermes",
        "advertised_skills": frozenset({"whatsapp.send"}),
        "policy_adapter": policy,
        "recipient_resolver": resolver,
        "effects": effects,
        "effect_store": store,
        "sender_account": "default",
    }
    arguments.update(changes)
    result = await process_a2a_invocation(**arguments)
    return result, policy, resolver, effects


@pytest.mark.asyncio
async def test_valid_text_uses_exact_alias_policy_and_managed_effect() -> None:
    result, policy, resolver, effects = await _invoke()

    assert result["status"] == "completed"
    assert result["output"] == {
        "delivery_id": _EFFECT_ID,
        "status": "sent",
        "recipient": {"type": "group", "alias": "team-example"},
        "sender_account": "default",
        "provider_message_id": "provider-1",
    }
    assert result["correlation"] == {
        "task_id": "task-1",
        "context_id": "context-1",
        "idempotency_key": "request-1",
    }
    ContractSchemas.load().validate_result(result)
    ContractSchemas.load().validate_response("whatsapp.send", result["output"])
    assert resolver.calls == [("group", "team-example")]
    assert policy.events[0].chat_id == "private-group@g.us"
    assert policy.events[0].sender_id == "service:a2a"
    assert effects.calls == [
        {
            "source": "a2a",
            "operation_ref": _EFFECT_ID,
            "channel": "whatsapp",
            "chat_id": "private-group@g.us",
            "content": "Hello",
            "effect_id": _EFFECT_ID,
            "require_managed": True,
        }
    ]


@pytest.mark.asyncio
async def test_voice_generation_returns_internal_metadata_without_sending(
    tmp_path: Path,
) -> None:
    effect_id = "a2a-effect-" + hashlib.sha256(
        b"hermes\0media.voice.generate\0voice-1"
    ).hexdigest()[:40]
    artifact = {
        "path": str(tmp_path / f"{effect_id}.wav"),
        "mime_type": "audio/wav",
        "duration_ms": 100,
        "sha256": "a" * 64,
        "size_bytes": 1_644,
        "expires_at": 1_060.0,
        "filename": f"{effect_id}.wav",
    }
    generator = _VoiceGenerator(artifact)
    effects = _Effects()
    request = {"text": "Hello", "format": "wav", "idempotency_key": "voice-1"}

    result, *_ = await _invoke(
        skill="media.voice.generate",
        input=request,
        effect_id=effect_id,
        advertised_skills=frozenset({"media.voice.generate", "whatsapp.send"}),
        voice_generator=generator,
        effects=effects,
    )

    assert result == {
        "skill": "media.voice.generate",
        "status": "completed",
        "output": {"internal_artifact": artifact},
        "correlation": {
            "task_id": "task-1",
            "context_id": "context-1",
            "idempotency_key": "voice-1",
        },
    }
    assert generator.calls == [(effect_id, request)]
    assert effects.calls == []


@pytest.mark.asyncio
async def test_second_explicit_voice_send_uses_only_validated_artifact_path(
    tmp_path: Path,
) -> None:
    voice_path = tmp_path / "artifacts" / "voice.wav"
    voice_path.parent.mkdir()
    voice_path.write_bytes(b"voice")
    sha256 = hashlib.sha256(b"voice").hexdigest()
    uri = "https://relay.example.test/a2a/artifacts/opaque-1"
    request = _input(
        content=[
            {
                "type": "voice",
                "uri": uri,
                "mime_type": "audio/wav",
                "duration_ms": 100,
            }
        ]
    )
    effect_id = "a2a-effect-" + hashlib.sha256(
        b"hermes\0whatsapp.send\0request-1"
    ).hexdigest()[:40]
    sender = _VoiceSender()

    result, policy, resolver, effects = await _invoke(
        input=request,
        effect_id=effect_id,
        policy=_Policy(tools=frozenset({"send_voice"})),
        resolved_artifacts=[
            {
                "peer": "hermes",
                "uri": uri,
                "path": str(voice_path),
                "mime_type": "audio/wav",
                "duration_ms": 100,
                "sha256": sha256,
                "size_bytes": 5,
                "expires_at": 2_000.0,
            }
        ],
        artifact_root=voice_path.parent,
        clock=lambda: 1_000.0,
        voice_sender=sender,
    )

    assert result["status"] == "completed"
    assert resolver.calls == [("group", "team-example")]
    assert policy.events[0].content == ""
    assert effects.calls == []
    assert sender.calls == [
        {
            "operation_ref": effect_id,
            "chat_id": "private-group@g.us",
            "path": str(voice_path),
            "effect_id": effect_id,
        }
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("suffix", "mime_type"), [(".wav", "audio/wav"), (".mp3", "audio/mpeg")]
)
async def test_whatsapp_voice_artifacts_use_audio_command_mime(
    tmp_path: Path, suffix: str, mime_type: str
) -> None:
    path = tmp_path / f"voice{suffix}"
    path.write_bytes(b"audio")
    channel = WhatsAppChannel(
        WhatsAppConfig(media={"outgoing_dir": str(tmp_path)}), MessageBus()
    )
    channel._connected = True
    calls: list[tuple[str, dict[str, object]]] = []

    async def send(command: str, payload: dict[str, object], **kwargs: object) -> dict[str, str]:
        calls.append((command, payload))
        return {"messageId": "provider-1"}

    channel._send_command_with_retry = send  # type: ignore[method-assign]
    await channel.send(
        OutboundMessage(
            channel="whatsapp", chat_id="private-group@g.us", content="", media=[str(path)]
        )
    )

    assert calls[0][0] == "send_media"
    assert calls[0][1]["mimeType"] == mime_type


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("uri", "resolved"),
    [
        ("https://external.example/voice.wav", []),
        ("file:///private/voice.wav", []),
        (
            "https://relay.example.test/a2a/artifacts/opaque",
            [
                {
                    "peer": "hermes",
                    "uri": "https://relay.example.test/a2a/artifacts/opaque",
                    "path": "/tmp/artifacts/../private.wav",
                    "mime_type": "audio/wav",
                    "duration_ms": 100,
                    "sha256": "a" * 64,
                    "size_bytes": 5,
                    "expires_at": 2_000.0,
                }
            ],
        ),
    ],
)
async def test_voice_send_rejects_external_private_or_arbitrary_paths(
    tmp_path: Path, uri: str, resolved: list[dict[str, object]]
) -> None:
    sender = _VoiceSender()
    result, *_ = await _invoke(
        input=_input(
            content=[{"type": "voice", "uri": uri, "mime_type": "audio/wav"}]
        ),
        policy=_Policy(tools=frozenset({"send_voice"})),
        resolved_artifacts=resolved,
        artifact_root=tmp_path / "artifacts",
        clock=lambda: 1_000.0,
        voice_sender=sender,
    )

    assert result["status"] == "rejected"
    assert result["error"]["code"] == "ARTIFACT_DENIED"
    assert sender.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("changes", "code"),
    [
        ({"peer": "not-hermes"}, "PEER_UNAUTHORIZED"),
        ({"skill": "search.web"}, "SKILL_NOT_ADVERTISED"),
        ({"input": _input(extra=True)}, "INVALID_SKILL_INPUT"),
        ({"input": _input(sender={"account": "chosen"})}, "SENDER_NOT_SELECTABLE"),
        (
            {"input": _input(recipient={"type": "contact", "alias": "1555@s.whatsapp.net"})},
            "INVALID_SKILL_INPUT",
        ),
        (
            {"input": _input(recipient={"type": "contact", "alias": "49123456789"})},
            "INVALID_SKILL_INPUT",
        ),
        (
            {"input": _input(recipient={"type": "group", "alias": "missing-group"})},
            "RECIPIENT_UNKNOWN",
        ),
        (
            {"input": _input(recipient={"type": "group", "alias": "contact-alice"})},
            "RECIPIENT_TYPE_MISMATCH",
        ),
        (
            {
                "input": _input(
                    content=[
                        {
                            "type": "image",
                            "uri": "artifact://image/opaque",
                            "mime_type": "image/png",
                        }
                    ]
                )
            },
            "CONTENT_TYPE_DENIED",
        ),
        (
            {
                "input": _input(
                    content=[
                        {
                            "type": "file",
                            "uri": "artifact://file/opaque",
                            "mime_type": "application/pdf",
                        }
                    ]
                )
            },
            "CONTENT_TYPE_DENIED",
        ),
        (
            {
                "input": _input(
                    content=[
                        {
                            "type": "voice",
                            "uri": "artifact://voice/opaque",
                            "mime_type": "audio/ogg",
                        }
                    ]
                )
            },
            "CONTENT_TYPE_DENIED",
        ),
    ],
)
async def test_rejects_untrusted_or_disabled_input(
    changes: dict[str, object], code: str
) -> None:
    result, policy, _resolver, effects = await _invoke(**changes)

    assert result["status"] == "rejected"
    assert result["error"]["code"] == code
    ContractSchemas.load().validate_result(result)
    if code in {"PEER_UNAUTHORIZED", "SKILL_NOT_ADVERTISED", "INVALID_SKILL_INPUT"}:
        assert policy.events == []
    assert effects.calls == []
    serialized = str(result)
    assert "1555@s.whatsapp.net" not in serialized
    assert "49123456789" not in serialized


@pytest.mark.asyncio
async def test_target_policy_must_allow_text_tool() -> None:
    result, _policy, _resolver, effects = await _invoke(
        policy=_Policy(tools=frozenset())
    )

    assert result["status"] == "rejected"
    assert result["error"]["code"] == "TARGET_POLICY_DENIED"
    assert effects.calls == []


@pytest.mark.asyncio
async def test_unmanaged_target_is_sanitized_and_never_falls_back() -> None:
    result, _policy, _resolver, effects = await _invoke(
        effects=_Effects(unmanaged=True)
    )

    assert result["status"] == "rejected"
    assert result["error"] == {
        "code": "MANAGED_EFFECT_REQUIRED",
        "message": "The recipient is not enabled for managed delivery.",
        "retryable": False,
    }
    assert "leaked@example.test" not in str(result)
    assert effects.calls[0]["require_managed"] is True


@pytest.mark.asyncio
async def test_missing_managed_effect_path_is_rejected() -> None:
    result, _policy, _resolver, _effects = await _invoke(effects=None)

    assert result["status"] == "rejected"
    assert result["error"]["code"] == "MANAGED_EFFECT_REQUIRED"


@pytest.mark.asyncio
async def test_gateway_revalidates_input_before_managed_path_check() -> None:
    result, _policy, _resolver, _effects = await _invoke(
        input=_input(extra=True),
        effects=None,
    )

    assert result["error"]["code"] == "INVALID_SKILL_INPUT"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "effect_id",
    ["", "short", "a2a-effect-" + "f" * 40, "x" * 201],
)
async def test_effect_id_must_match_the_deterministic_request_identity(
    effect_id: str,
) -> None:
    result, policy, resolver, effects = await _invoke(effect_id=effect_id)

    assert result["status"] == "rejected"
    assert result["error"] == {
        "code": "INVALID_EFFECT_ID",
        "message": "The effect identity is invalid.",
        "retryable": False,
    }
    assert policy.events == []
    assert resolver.calls == []
    assert effects.calls == []
    ContractSchemas.load().validate_result(result)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("store", "expected"),
    [
        (_EffectStore(state="queued", provider_message_id=None), "accepted"),
        (_EffectStore(state="accepted", provider_message_id=None), "accepted"),
        (_EffectStore(state="sent", provider_message_id="provider-1"), "sent"),
        (
            _EffectStore(
                state="sent",
                provider_message_id="provider-1",
                delivery_status="read",
            ),
            "delivered",
        ),
    ],
)
async def test_business_status_uses_only_durable_effect_and_provider_evidence(
    store: _EffectStore, expected: str
) -> None:
    result, _policy, _resolver, _effects = await _invoke(effect_store=store)

    assert result["output"]["status"] == expected
    assert result["output"]["status"] != "read"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("state", "provider_message_id"),
    [
        ("planned", None),
        ("executing", None),
        ("sent", None),
        ("failed", "private-provider-id"),
        ("unknown", "private-provider-id"),
        ("unknown_nonrepeatable", "private-provider-id"),
        ("expired", "private-provider-id"),
        ("blocked", "private-provider-id"),
        ("cancelled", "private-provider-id"),
    ],
)
async def test_unproven_or_unsuccessful_effect_state_is_a_sanitized_failure(
    state: str,
    provider_message_id: str | None,
) -> None:
    result, *_ = await _invoke(
        effect_store=_EffectStore(
            state=state,
            provider_message_id=provider_message_id,
        )
    )

    assert result["status"] == "failed"
    assert result["error"] == {
        "code": "DELIVERY_FAILED",
        "message": "The delivery could not be completed.",
        "retryable": False,
    }
    assert "private-provider-id" not in str(result)
    ContractSchemas.load().validate_result(result)


@pytest.mark.asyncio
async def test_retry_keeps_the_caller_effect_identity() -> None:
    effects = _Effects()
    first, *_ = await _invoke(effects=effects)
    second, *_ = await _invoke(effects=effects)

    assert first == second
    assert [call["effect_id"] for call in effects.calls] == [
        _EFFECT_ID,
        _EFFECT_ID,
    ]


@pytest.mark.asyncio
async def test_real_socket_composes_with_a2a_invocation_validation_and_identity(
    tmp_path: Path,
) -> None:
    policy = _Policy()
    resolver = _Resolver()
    effects = _Effects()
    store = _EffectStore()

    async def handler(**kwargs: object) -> dict[str, object]:
        return await process_a2a_invocation(
            **kwargs,
            configured_peer="hermes",
            advertised_skills=frozenset({"whatsapp.send"}),
            policy_adapter=policy,
            recipient_resolver=resolver,
            effects=effects,
            effect_store=store,
            sender_account="default",
        )

    async def invoke(request: dict[str, object]) -> dict[str, object]:
        reader, writer = await asyncio.open_unix_connection(server.path)
        writer.write(json.dumps(request).encode() + b"\n")
        await writer.drain()
        response = json.loads(await reader.readline())
        writer.close()
        await writer.wait_closed()
        return response

    valid = {
        "cmd": "a2a_invoke",
        "args": {
            "peer": "hermes",
            "skill": "whatsapp.send",
            "input": _input(),
            "task_id": "task-1",
            "context_id": "context-1",
            "effect_id": _EFFECT_ID,
        },
    }
    server = GatewaySocket(
        path=tmp_path / "gateway.sock",
        a2a_invoke_handler=handler,
        rate_limit=10,
    )
    await server.start()
    try:
        first = await invoke(valid)
        second = await invoke(valid)
        invalid_effect = await invoke(
            {**valid, "args": {**valid["args"], "effect_id": "random"}}
        )
        invalid_input = await invoke(
            {
                **valid,
                "args": {
                    **valid["args"],
                    "input": {**valid["args"]["input"], "secret@lid": True},
                },
            }
        )
    finally:
        await server.stop()

    assert first["response"]["status"] == "completed"
    assert second == first
    assert [call["effect_id"] for call in effects.calls] == [_EFFECT_ID, _EFFECT_ID]
    assert invalid_effect["response"]["error"]["code"] == "INVALID_EFFECT_ID"
    assert invalid_input["response"]["error"]["code"] == "INVALID_SKILL_INPUT"
    assert "secret@lid" not in str(invalid_input)


class _AliasStore:
    def __init__(self) -> None:
        self.contact = SimpleNamespace(id="contact-1")

    def search_by_alias(self, query: str) -> list[object]:
        return [self.contact] if query.lower() in {"contact-alice", "alice"} else []

    def get_aliases(self, contact_id: str) -> list[object]:
        return [SimpleNamespace(alias="contact-alice")]

    def get_identifiers(self, contact_id: str) -> list[object]:
        return [
            SimpleNamespace(
                channel="whatsapp",
                identifier="private-contact@lid",
                kind="lid",
            )
        ]


class _Contacts:
    store = _AliasStore()


class _AliasPolicy:
    def policy_snapshot(self) -> object:
        group = SimpleNamespace(group_tags=["team-example"])
        whatsapp = SimpleNamespace(chats={"private-group@g.us": group})
        policy = SimpleNamespace(channels={"whatsapp": whatsapp})
        return SimpleNamespace(healthy=True, policy=policy)


@pytest.mark.parametrize(
    ("kind", "alias", "expected"),
    [
        ("group", "team-example", ("private-group@g.us", None)),
        ("contact", "contact-alice", ("private-contact@lid", None)),
        ("contact", "team-example", (None, "type_mismatch")),
        ("group", "contact-alice", (None, "type_mismatch")),
        ("group", "Team-Example", (None, "unknown")),
        ("contact", "alice", (None, "unknown")),
    ],
)
def test_recipient_resolution_uses_only_exact_typed_policy_aliases(
    kind: str,
    alias: str,
    expected: tuple[str | None, str | None],
) -> None:
    assert (
        a2a_invoke.resolve_whatsapp_recipient(
            kind,
            alias,
            policy_adapter=_AliasPolicy(),
            contacts_service=_Contacts(),
        )
        == expected
    )
