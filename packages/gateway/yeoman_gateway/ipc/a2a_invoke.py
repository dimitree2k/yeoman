"""Structured A2A invocations accepted by the Gateway IPC boundary."""

from __future__ import annotations

import hashlib
import re
import time
from collections.abc import Callable, Collection, Mapping
from pathlib import Path
from typing import Any, Protocol

from yeoman_gateway.a2a.contracts import A2AContractValidationError, ContractSchemas
from yeoman_gateway.core.models import InboundEvent
from yeoman_gateway.processing.dispatch import SERVICE_PRINCIPALS, EffectNotDeliveredError
from yeoman_gateway.processing.models import DELIVERED_STATUSES_TUPLE, EffectReceipt

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")
_SKILL = re.compile(r"^(conversation|[a-z][a-z0-9]*(\.[a-z][a-z0-9]*)+)$")
_TEXT_TOOL = "message"
_VOICE_TOOL = "send_voice"
_MEDIA_TOOL = "send_media"
_MEDIA_MIME_TYPES = {
    "image": frozenset({"image/png", "image/jpeg", "image/gif", "image/webp"}),
    "file": frozenset({"application/pdf", "text/plain"}),
}


class _PolicyAdapter(Protocol):
    def evaluate(self, event: InboundEvent) -> Any: ...


class _Effects(Protocol):
    async def send(self, **kwargs: Any) -> EffectReceipt | None: ...


class _VoiceGenerator(Protocol):
    async def generate(
        self, effect_id: str, request: Mapping[str, Any]
    ) -> dict[str, object]: ...


_MediaSender = Callable[..., Any]


class _EffectStore(Protocol):
    def get_effect(self, effect_id: str) -> Any: ...

    def effect_transport_receipt(self, effect_id: str) -> Any: ...

    def delivery_signals(self, *, chat_id: str, message_id: str) -> tuple[Any, ...]: ...


_RecipientResolver = Callable[[str, str], tuple[str | None, str | None]]


def resolve_whatsapp_recipient(
    kind: str,
    alias: str,
    *,
    policy_adapter: Any,
    contacts_service: Any,
) -> tuple[str | None, str | None]:
    """Resolve an exact typed alias without accepting identifiers or fuzzy matches."""
    refresh = getattr(policy_adapter, "owner_recipients", None)
    if callable(refresh):
        refresh("whatsapp")
    snapshot = policy_adapter.policy_snapshot()
    if not snapshot.healthy or snapshot.policy is None:
        return None, "unknown"

    group_hits: list[str] = []
    whatsapp = snapshot.policy.channels.get("whatsapp")
    for chat_id, override in getattr(whatsapp, "chats", {}).items():
        if chat_id.endswith("@g.us") and alias in list(override.group_tags or []):
            group_hits.append(chat_id)

    contact_hits: list[str] = []
    for contact in contacts_service.store.search_by_alias(alias):
        if not any(
            candidate.alias == alias
            for candidate in contacts_service.store.get_aliases(contact.id)
        ):
            continue
        contact_hits.extend(
            identifier.identifier
            for identifier in contacts_service.store.get_identifiers(contact.id)
            if identifier.channel == "whatsapp"
            and not identifier.identifier.endswith("@g.us")
        )

    hits = group_hits if kind == "group" else contact_hits
    other_hits = contact_hits if kind == "group" else group_hits
    unique = list(dict.fromkeys(hits))
    if len(unique) == 1:
        return unique[0], None
    if not unique and other_hits:
        return None, "type_mismatch"
    return None, "unknown"


def _correlation(
    *, task_id: str, context_id: str, idempotency_key: object
) -> dict[str, object]:
    correlation: dict[str, object] = {}
    if isinstance(task_id, str) and task_id:
        correlation["task_id"] = task_id
    if isinstance(context_id, str) and context_id:
        correlation["context_id"] = context_id
    if isinstance(idempotency_key, str) and _IDENTIFIER.fullmatch(idempotency_key):
        correlation["idempotency_key"] = idempotency_key
    return correlation


def _failure(
    *,
    skill: str,
    code: str,
    message: str,
    correlation: Mapping[str, object],
    status: str = "rejected",
) -> dict[str, object]:
    result: dict[str, object] = {
        "skill": skill if isinstance(skill, str) and _SKILL.fullmatch(skill) else "whatsapp.send",
        "status": status,
        "error": {"code": code, "message": message, "retryable": False},
    }
    if correlation:
        result["correlation"] = dict(correlation)
    return result


def _business_status(store: _EffectStore, effect_id: str) -> tuple[str, str | None]:
    stored = store.get_effect(effect_id)
    if stored is None:
        raise RuntimeError("effect was not durably recorded")
    state = str(getattr(stored, "state", ""))
    if state in {"queued", "accepted"}:
        return "accepted", None
    if state != "sent":
        raise RuntimeError("effect outcome is not successful")

    receipt = store.effect_transport_receipt(effect_id)
    provider_id = str(getattr(receipt, "provider_message_id", "") or "") or None
    if receipt is None or provider_id is None:
        raise RuntimeError("sent effect has no durable transport receipt")
    signals = store.delivery_signals(chat_id=receipt.chat_id, message_id=provider_id)
    delivered = any(
        str((getattr(signal, "payload", None) or {}).get("status", "")).lower()
        in DELIVERED_STATUSES_TUPLE
        for signal in signals
    )
    return ("delivered" if delivered else "sent"), provider_id


async def process_a2a_invocation(
    *,
    peer: str,
    skill: str,
    input: dict[str, Any],
    task_id: str,
    context_id: str,
    effect_id: str,
    configured_peer: str,
    advertised_skills: Collection[str],
    policy_adapter: _PolicyAdapter,
    recipient_resolver: _RecipientResolver,
    effects: _Effects | None,
    effect_store: _EffectStore,
    sender_account: str,
    enabled_content_types: Collection[str] = frozenset({"text", "voice"}),
    voice_generator: _VoiceGenerator | None = None,
    resolved_artifacts: Collection[Mapping[str, Any]] = (),
    artifact_root: Path | None = None,
    media_sender: _MediaSender | None = None,
    clock: Callable[[], float] | None = None,
    schemas: ContractSchemas | None = None,
) -> dict[str, object]:
    """Validate, authorize, and execute one advertised structured request."""
    schemas = schemas or ContractSchemas.load()
    correlation = _correlation(
        task_id=task_id,
        context_id=context_id,
        idempotency_key=input.get("idempotency_key") if isinstance(input, dict) else None,
    )
    invocation = {"skill": skill, "input": input}
    try:
        schemas.validate_invocation(invocation)
    except A2AContractValidationError:
        return _failure(
            skill=skill,
            code="INVALID_INVOCATION",
            message="The invocation is invalid.",
            correlation=correlation,
        )
    if peer != configured_peer or not configured_peer:
        return _failure(
            skill=skill,
            code="PEER_UNAUTHORIZED",
            message="The peer is not authorized.",
            correlation=correlation,
        )
    if skill not in advertised_skills:
        return _failure(
            skill=skill,
            code="SKILL_NOT_ADVERTISED",
            message="The skill is not available.",
            correlation=correlation,
        )
    try:
        schemas.validate_request(skill, input)
    except A2AContractValidationError:
        return _failure(
            skill=skill,
            code="INVALID_SKILL_INPUT",
            message="The skill input is invalid.",
            correlation=correlation,
        )
    expected_effect_id = "a2a-effect-" + hashlib.sha256(
        f"{peer}\0{skill}\0{input['idempotency_key']}".encode()
    ).hexdigest()[:40]
    if not isinstance(effect_id, str) or effect_id != expected_effect_id:
        return _failure(
            skill=skill,
            code="INVALID_EFFECT_ID",
            message="The effect identity is invalid.",
            correlation=correlation,
        )
    if skill == "media.voice.generate":
        if voice_generator is None:
            return _failure(
                skill=skill,
                code="SKILL_NOT_ADVERTISED",
                message="The skill is not available.",
                correlation=correlation,
            )
        try:
            artifact = await voice_generator.generate(effect_id, input)
        except Exception:
            return _failure(
                skill=skill,
                code="VOICE_GENERATION_FAILED",
                message="Voice generation failed.",
                correlation=correlation,
                status="failed",
            )
        return {
            "skill": skill,
            "status": "completed",
            "output": {"internal_artifact": artifact},
            "correlation": correlation,
        }
    if skill != "whatsapp.send":
        return _failure(
            skill=skill,
            code="SKILL_NOT_ADVERTISED",
            message="The skill is not available.",
            correlation=correlation,
        )
    if "sender" in input:
        return _failure(
            skill=skill,
            code="SENDER_NOT_SELECTABLE",
            message="The WhatsApp sender is selected locally.",
            correlation=correlation,
        )

    content = input["content"]
    content_types = {part["type"] for part in content}
    if not content_types <= set(enabled_content_types):
        return _failure(
            skill=skill,
            code="CONTENT_TYPE_DENIED",
            message="The requested content is not enabled.",
            correlation=correlation,
        )
    media_parts = [part for part in content if part["type"] in {"image", "file"}]
    text_parts = [part for part in content if part["type"] == "text"]
    voice_parts = [part for part in content if part["type"] == "voice"]
    invalid_content = (
        not content_types <= {"text", "voice", "image", "file"}
        or bool(voice_parts) and len(content) != 1
        or len(media_parts) > 1
        or bool(media_parts) and (len(text_parts) > 1 or bool(voice_parts))
        or not media_parts and not voice_parts and not text_parts
    )
    if invalid_content:
        return _failure(
            skill=skill,
            code="CONTENT_TYPE_DENIED",
            message="The requested content is not enabled.",
            correlation=correlation,
        )
    delivery = input.get("delivery") or {}
    if "reply_to_message_id" in delivery:
        return _failure(
            skill=skill,
            code="CONTENT_TYPE_DENIED",
            message="Replies are not enabled for this skill.",
            correlation=correlation,
        )
    if effects is None:
        return _failure(
            skill=skill,
            code="MANAGED_EFFECT_REQUIRED",
            message="The recipient is not enabled for managed delivery.",
            correlation=correlation,
        )

    recipient = input["recipient"]
    chat_id, resolution = recipient_resolver(recipient["type"], recipient["alias"])
    if chat_id is None:
        code = (
            "RECIPIENT_TYPE_MISMATCH"
            if resolution == "type_mismatch"
            else "RECIPIENT_UNKNOWN"
        )
        return _failure(
            skill=skill,
            code=code,
            message="The recipient alias is unavailable.",
            correlation=correlation,
        )

    media_path: str | None = None
    media_kind = voice_parts[0]["type"] if voice_parts else (
        media_parts[0]["type"] if media_parts else None
    )
    if media_kind is not None:
        if artifact_root is None or media_sender is None:
            return _failure(
                skill=skill,
                code="CONTENT_TYPE_DENIED",
                message="Media content is not enabled.",
                correlation=correlation,
            )
        part = voice_parts[0] if voice_parts else media_parts[0]
        media_path = _validated_media_path(
            peer=peer,
            part=part,
            resolved_artifacts=resolved_artifacts,
            artifact_root=artifact_root,
            now=(clock or time.time)(),
        )
        if media_path is None:
            return _failure(
                skill=skill,
                code="ARTIFACT_DENIED",
                message="The media artifact is unavailable.",
                correlation=correlation,
            )
    text = "\n".join(part["text"] for part in text_parts)
    if media_parts:
        inline_caption = str(media_parts[0].get("caption") or "")
        if inline_caption and text:
            return _failure(
                skill=skill,
                code="CONTENT_TYPE_DENIED",
                message="Only one media caption is accepted.",
                correlation=correlation,
            )
        text = inline_caption or text
    event = InboundEvent(
        channel="whatsapp",
        chat_id=chat_id,
        sender_id=SERVICE_PRINCIPALS["a2a"],
        content=text,
        is_group=recipient["type"] == "group",
        mentioned_bot=True,
        reply_to_bot=True,
        raw_metadata={"source": "hermes_a2a", "a2a_peer": peer},
    )
    decision = policy_adapter.evaluate(event)
    if (
        not decision.accept_message
        or not decision.should_respond
        or (
            _VOICE_TOOL
            if media_kind == "voice"
            else _MEDIA_TOOL if media_kind in {"image", "file"} else _TEXT_TOOL
        )
        not in decision.allowed_tools
    ):
        return _failure(
            skill=skill,
            code="TARGET_POLICY_DENIED",
            message="The recipient policy denied delivery.",
            correlation=correlation,
        )

    try:
        if media_path is not None:
            send_media = media_sender
            if send_media is None:  # narrowed above; keep the trust boundary explicit
                raise RuntimeError("media sender unavailable")
            send_args = {
                "operation_ref": effect_id,
                "chat_id": chat_id,
                "path": media_path,
                "effect_id": effect_id,
            }
            if media_kind in {"image", "file"}:
                send_args["caption"] = text
            await send_media(**send_args)
        else:
            await effects.send(
                source="a2a",
                operation_ref=effect_id,
                channel="whatsapp",
                chat_id=chat_id,
                content=text,
                effect_id=effect_id,
                require_managed=True,
            )
        status, provider_id = _business_status(effect_store, effect_id)
    except EffectNotDeliveredError:
        return _failure(
            skill=skill,
            code="MANAGED_EFFECT_REQUIRED",
            message="The recipient is not enabled for managed delivery.",
            correlation=correlation,
        )
    except Exception:
        return _failure(
            skill=skill,
            code="DELIVERY_FAILED",
            message="The delivery could not be completed.",
            correlation=correlation,
            status="failed",
        )

    output: dict[str, object] = {
        "delivery_id": effect_id,
        "status": status,
        "recipient": dict(recipient),
        "sender_account": sender_account,
    }
    if provider_id is not None:
        output["provider_message_id"] = provider_id
    try:
        schemas.validate_response(skill, output)
    except A2AContractValidationError:
        return _failure(
            skill=skill,
            code="DELIVERY_FAILED",
            message="The delivery result was invalid.",
            correlation=correlation,
            status="failed",
        )
    result: dict[str, object] = {
        "skill": skill,
        "status": "completed",
        "output": output,
        "correlation": correlation,
    }
    schemas.validate_result(result)
    return result


def _validated_media_path(
    *,
    peer: str,
    part: Mapping[str, Any],
    resolved_artifacts: Collection[Mapping[str, Any]],
    artifact_root: Path | None,
    now: float,
) -> str | None:
    if artifact_root is None or len(resolved_artifacts) != 1:
        return None
    artifact = next(iter(resolved_artifacts))
    kind = str(part.get("type") or "")
    mime_type = str(part.get("mime_type") or "")
    if (
        artifact.get("peer") != peer
        or artifact.get("uri") != part.get("uri")
        or artifact.get("mime_type") != mime_type
        or (
            kind in _MEDIA_MIME_TYPES
            and mime_type not in _MEDIA_MIME_TYPES[kind]
        )
    ):
        return None
    try:
        root = artifact_root.expanduser().resolve(strict=True)
        path = Path(str(artifact["path"]))
        resolved = path.resolve(strict=True)
        if (
            path.is_symlink()
            or not resolved.is_relative_to(root)
            or not resolved.is_file()
            or (
                "filename" in artifact
                and str(artifact["filename"]) != resolved.name
            )
            or float(artifact["expires_at"]) <= now
            or resolved.stat().st_size != int(artifact["size_bytes"])
            or hashlib.sha256(resolved.read_bytes()).hexdigest() != artifact["sha256"]
        ):
            return None
    except (KeyError, OSError, TypeError, ValueError):
        return None
    return str(resolved)


__all__ = ["process_a2a_invocation", "resolve_whatsapp_recipient"]
