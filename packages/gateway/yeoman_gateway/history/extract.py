"""Turn Layer 1 lines into message copies, event copies and identity observations."""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from .attestations import Attestation, parse
from .convert.common import clean_text, media_kind
from .ids import Ident, classify
from .layer1 import Layer1Line
from .resolve import REF, ContactRecord, IdentityInput

SOURCE_RANKS = {"journal": 1, "bridge_refs": 2, "reply_context": 3, "inbound_archive": 3,
                "session_jsonl": 4, "memory": 5, "knowledge_memory": 5}
_IDENTITY_KINDS = frozenset({"contact_record", "identifier_record", "name_record", "pair_record"})
_USABLE_STATUS = frozenset({"", "active", "verified", "candidate", "observed", "confirmed"})
_SEND_TYPES: dict[str, str | None] = {
    "send_text": None, "send_media": "unknown", "send_image": "image", "send_video": "video",
    "send_audio": "audio", "send_voice": "audio", "send_document": "document"}
_MEMBER_ACTIONS = {"add": "member_add", "remove": "member_remove", "promote": "member_promote",
                   "demote": "member_demote"}


def rank_of(ref: str) -> int:
    rel = ref.split("#", 1)[0]
    sub, _, name = rel.partition("/")
    if sub == "whatsapp":
        return 0
    stem = name.removesuffix(".jsonl")
    if stem.startswith("memory_pre_rebackfill"):
        return 6
    return SOURCE_RANKS.get(stem, 7)


@dataclass
class MessageCopy:
    ref: str
    rank: int
    channel: str
    chat_id: str
    native_id: str | None
    direction: str
    sender: Ident | None
    sender_raw: str | None
    sender_name: str | None
    from_assistant: bool
    occurred_ms: int | None
    time_certainty: str
    text: str | None
    media: dict[str, Any] | None
    description: str | None
    reply_to: str | None
    mentions: list[str] | None
    provenance: str
    inferred_sender: bool = False
    extra_refs: tuple[str, ...] = ()
    batch_key: str | None = None


@dataclass
class EventCopy:
    ref: str
    rank: int
    kind: str
    channel: str
    chat_id: str
    target_native_id: str | None
    actor: Ident | None
    actor_raw: str | None
    from_assistant: bool
    occurred_ms: int | None
    time_certainty: str
    payload: dict[str, Any]
    provenance: str
    native_event_id: str | None = None


@dataclass(frozen=True)
class Description:
    ref: str
    channel: str
    chat_id: str
    native_id: str
    mode: str
    text: str
    generator: str | None
    generated_ms: int | None


@dataclass(frozen=True)
class MediaRecord:
    ref: str
    channel: str
    chat_id: str
    native_id: str
    media: dict[str, Any]


@dataclass
class Extracted:
    messages: list[MessageCopy] = field(default_factory=list)
    events: list[EventCopy] = field(default_factory=list)
    descriptions: list[Description] = field(default_factory=list)
    media_records: list[MediaRecord] = field(default_factory=list)
    identity: IdentityInput = field(default_factory=IdentityInput)
    attestations: list[Attestation] = field(default_factory=list)
    outcomes: Counter[tuple[str, str]] = field(default_factory=Counter)

    def count(self, ref: str, outcome: str) -> None:
        self.outcomes[(ref.split("#", 1)[0], outcome)] += 1


def extract(lines: Iterable[Layer1Line]) -> Extracted:
    out = Extracted()
    pending: dict[str, tuple[Layer1Line, dict[str, Any], str]] = {}
    for line in lines:
        if line.record is None:
            out.count(line.ref, "invalid_json")
            continue
        sub = line.ref.split("/", 1)[0]
        if sub == "owner":
            _owner(line, out)
        elif sub == "derived":
            _derived(line, out)
        elif sub == "whatsapp":
            _raw(line, out, pending)
        else:
            _backfill(line, out)
    for request_line, _, _ in pending.values():
        out.count(request_line.ref, "skipped:outbound_without_result")
    out.identity.attestations.extend(out.attestations)
    return out


def _sender(payload: dict[str, Any], *, allow_group: bool = True) -> tuple[Ident | None, str | None, list[Ident]]:
    found = [(str(payload[k]), i) for k in ("participantJid", "senderPhoneJid", "senderId")
             if (i := classify(payload.get(k))) is not None]
    idents = [i for _, i in found]
    for raw, ident in found:
        if ident.strong:
            return ident, raw, idents
    for raw, ident in found:
        if ident.kind in ("numeric", "assistant") or (allow_group and ident.kind == "group"):
            return ident, raw, idents
    return None, None, idents


def _observe(out: Extracted, idents: list[Ident], sender: Ident | None, name: Any, ms: int | None,
             ref: str, link: bool, chat: str) -> None:
    inp = out.identity
    inp.see(classify(chat), None, ref)
    for ident in idents:
        inp.see(ident, ms, ref)
    if link:
        for lid in (i for i in idents if i.kind == "lid"):
            for pn in (i for i in idents if i.kind == "pn_jid"):
                inp.link(lid, pn, "native_pair", ref)
    if name and sender is not None and sender.kind in ("lid", "pn_jid", "newsletter", "numeric"):
        inp.name(sender.value, name, ms, ref)


def _message(out: Extracted, ref: str, p: dict[str, Any], *, channel: str, chat: str,
             native_id: Any, direction: str, ms: int | None, certainty: str, provenance: str,
             from_assistant: bool | None = None, media: Any = None, description: str | None = None,
             mentions: Any = None, extra_refs: tuple[str, ...] = (), batch_key: str | None = None) -> None:
    assistant = bool(p.get("fromAssistant")) if from_assistant is None else from_assistant
    sender, sender_raw, idents = (None, None, []) if assistant else _sender(p, allow_group=False)
    name = None if assistant else p.get("senderName")
    cleaned = clean_text(p.get("text"))
    if not (isinstance(media, dict) and media) and cleaned.placeholder:
        media = {"kind": media_kind(cleaned.placeholder)}
    out.messages.append(MessageCopy(
        ref=ref, rank=rank_of(ref), channel=channel, chat_id=chat,
        native_id=str(native_id) if native_id else None,
        direction="out" if assistant else direction, sender=sender, sender_raw=sender_raw,
        sender_name=name, from_assistant=assistant, occurred_ms=ms, time_certainty=certainty,
        text=cleaned.text, media=media if isinstance(media, dict) and media else None,
        description=description or cleaned.description, reply_to=p.get("replyToMessageId") or None,
        mentions=mentions if isinstance(mentions, list) and mentions else None, provenance=provenance,
        inferred_sender=bool(p.get("senderInferredFromChat")), extra_refs=extra_refs,
        batch_key=batch_key,
    ))
    if not assistant:
        _observe(out, idents, sender, name, ms, ref,
                 provenance == "native" and not p.get("lidConflict"), chat)


def _event(out: Extracted, ref: str, kind: str, p: dict[str, Any], *, channel: str, chat: str,
           ms: int | None, certainty: str, provenance: str, from_assistant: bool = False) -> None:
    assistant = from_assistant or bool(p.get("fromAssistant"))
    actor, actor_raw, idents = (None, None, []) if assistant else _sender(p)
    if kind == "reaction":
        payload: dict[str, Any] = {"emoji": p.get("emoji") or None, "removed": bool(p.get("removed"))}
    elif kind == "edit":
        payload = {"text": p.get("text")}
    else:
        payload = {}
    target = p.get("targetMessageId") or (p.get("messageId") if kind in ("edit", "delete") else None)
    native_event_id = p.get("nativeEventId")
    out.events.append(EventCopy(ref, rank_of(ref), kind, channel, chat, target or None, actor, actor_raw,
                                assistant, ms, certainty, payload, provenance,
                                str(native_event_id) if native_event_id else None))
    if not assistant:
        _observe(out, idents, actor, None, ms, ref, False, chat)


def _membership(out: Extracted, ref: str, type_: str, p: dict[str, Any], *, channel: str, chat: str,
                ms: int | None, certainty: str, provenance: str) -> str:
    if type_ == "membership_snapshot":
        kind = "member_snapshot"
    else:
        kind = _MEMBER_ACTIONS.get(str(p.get("action")), "")
        if not kind:
            return f"skipped:membership_action:{p.get('action')}"
    members: set[tuple[str, ...]] = set()
    for item in p.get("participants") or []:
        if isinstance(item, dict):
            lid = classify(item.get("lid"))
            pn = classify(item.get("phoneJid") or item.get("phone_jid") or item.get("pn") or item.get("jid"))
        else:
            ident = classify(item)
            lid, pn = (ident, None) if ident is not None and ident.kind == "lid" else (None, ident)
        for ident in (lid, pn):
            out.identity.see(ident, ms, ref)
        if provenance == "native" and lid is not None and pn is not None:
            out.identity.link(lid, pn, "native_pair", ref)
        members.add(tuple(sorted(i.value for i in (lid, pn) if i is not None)))
    payload: dict[str, Any] = {"participants": [list(member) for member in sorted(members)]}
    if kind == "member_snapshot":
        payload["complete"] = bool(p.get("complete"))
    raw_actor = p.get("actor")
    if isinstance(raw_actor, dict):
        actor_lid = classify(raw_actor.get("lid"))
        actor_phone = classify(raw_actor.get("phoneJid") or raw_actor.get("phone_jid")
                                or raw_actor.get("pn") or raw_actor.get("jid"))
        actor = actor_lid or actor_phone
        actor_raw = (raw_actor.get("lid") if actor_lid is not None else
                     raw_actor.get("phoneJid") or raw_actor.get("phone_jid")
                     or raw_actor.get("pn") or raw_actor.get("jid"))
        for ident in (actor_lid, actor_phone):
            out.identity.see(ident, ms, ref)
        if provenance == "native" and actor_lid is not None and actor_phone is not None:
            out.identity.link(actor_lid, actor_phone, "native_pair", ref)
    else:
        actor = classify(raw_actor)
        actor_raw = str(raw_actor) if raw_actor else None
        out.identity.see(actor, ms, ref)
    out.events.append(EventCopy(ref, rank_of(ref), kind, channel, chat, None, actor,
                                str(actor_raw) if actor_raw else None, False, ms, certainty,
                                payload, provenance))
    return "event"


def _raw_time(p: dict[str, Any], record: dict[str, Any]) -> tuple[int | None, str]:
    if p.get("timestamp") is not None:
        number = int(float(p["timestamp"]))
        return (number * 1000 if number < 100_000_000_000 else number), "provider_timestamp"
    if p.get("providerTimestampMs"):
        return int(p["providerTimestampMs"]), "provider_timestamp"
    if record.get("received_ms"):
        return int(record["received_ms"]), "capture_time_approx"
    return None, "unknown"


def _raw(line: Layer1Line, out: Extracted,
         pending: dict[str, tuple[Layer1Line, dict[str, Any], str]]) -> None:
    record = line.record or {}
    native = record.get("native") or {}
    type_, kind = native.get("type"), record.get("kind")
    p = native.get("payload") or {}
    channel = record.get("channel") or "whatsapp"
    if channel != "whatsapp":
        out.count(line.ref, "out_of_scope_channel")
        return
    chat = p.get("chatJid") or record.get("chat_id") or ""
    ms, certainty = _raw_time(p, record)
    if kind == "message" and type_ == "message":
        _message(out, line.ref, p, channel=channel, chat=chat, native_id=p.get("messageId"),
                 direction=record.get("direction") or "in", ms=ms, certainty=certainty,
                 provenance="native", media=record.get("media") or p.get("media"),
                 mentions=p.get("mentionedJids"))
        out.count(line.ref, "message")
    elif kind in ("reaction", "edit", "delete") and type_ == kind:
        _event(out, line.ref, kind, p, channel=channel, chat=chat, ms=ms, certainty=certainty,
               provenance="native")
        out.count(line.ref, "event")
    elif type_ in ("membership_snapshot", "membership_change"):
        out.count(line.ref, _membership(out, line.ref, type_, p, channel=channel, chat=chat, ms=ms,
                                        certainty=certainty, provenance="native"))
    elif kind == "outbound_request" and type_ in _SEND_TYPES:
        correlation = str(record.get("correlation_id") or line.ref)
        previous = pending.get(correlation)
        if previous is not None:
            out.count(previous[0].ref, "skipped:duplicate_outbound_correlation")
        pending[correlation] = (line, p, type_)
    elif kind == "outbound_result" and type_ in _SEND_TYPES:
        request = pending.pop(str(record.get("correlation_id") or ""), None)
        sent = (native.get("result") or {}).get("sent") or {}
        native_id = sent.get("messageId") or sent.get("providerMessageId")
        if request is None or not native_id:
            if request is not None:
                out.count(request[0].ref, "skipped:outbound_not_sent")
            out.count(line.ref, "skipped:outbound_not_sent")
            return
        request_line, request_p, request_type = request
        kind_of_media = _SEND_TYPES[request_type]
        _message(out, request_line.ref,
                 {"text": request_p.get("text") or request_p.get("caption"),
                  "replyToMessageId": request_p.get("replyToMessageId")},
                 channel=channel, chat=record.get("chat_id") or request_p.get("to") or chat,
                 native_id=native_id, direction="out", ms=record.get("received_ms"),
                 certainty="capture_time_approx" if record.get("received_ms") else "unknown",
                 provenance="native", from_assistant=True,
                 media={"kind": kind_of_media} if kind_of_media else None, extra_refs=(line.ref,))
        out.count(request_line.ref, "message")
        out.count(line.ref, "skipped:outbound_result_paired")
    elif kind == "outbound_request" and type_ == "react":
        emoji = p.get("emoji") or ""
        received = record.get("received_ms")
        out.events.append(EventCopy(line.ref, 0, "reaction", channel, chat, p.get("messageId"), None, None,
                                    True, received, "capture_time_approx" if received else "unknown",
                                    {"emoji": emoji or None, "removed": emoji == ""}, "native"))
        out.count(line.ref, "event")
    elif kind == "outbound_result":
        out.count(line.ref, "skipped:outbound_result_paired")
    elif type_ == "receipt" or kind == "receipt":
        out.count(line.ref, "skipped:receipt")
    else:
        out.count(line.ref, f"skipped:unhandled:{kind}:{type_}")


def _identity(ref: str, kind: str, record: dict[str, Any], p: dict[str, Any], out: Extracted) -> str:
    inp = out.identity
    if kind == "contact_record":
        if not p.get("contactRef"):
            return "skipped:contact_without_id"
        inp.contact_record(ContactRecord(str(p["contactRef"]), p.get("createdMs"), p.get("displayName"),
                                         p.get("preferredName"), ref))
        return "identity"
    if kind == "name_record":
        if p.get("mappingRetracted") or str(p.get("status") or "").lower() in ("retracted", "rejected"):
            return "skipped:name_retracted"
        if not p.get("contactRef") or not p.get("name"):
            return "skipped:name_incomplete"
        inp.name(REF + str(p["contactRef"]), p["name"], None, ref)
        return "identity"
    if (record.get("channel") or "whatsapp") != "whatsapp":
        return "out_of_scope_channel"
    if kind == "pair_record":
        lid, pn = classify(p.get("lid")), classify(p.get("pnJid"))
        if lid is None or pn is None or lid.kind != "lid" or pn.kind != "pn_jid":
            return "skipped:pair_incomplete"
        inp.link(lid, pn, "native_pair", ref)
        return "identity"
    status = str(p.get("status") or "").lower()
    if status not in _USABLE_STATUS:
        return f"skipped:binding_status:{status}"
    ident = classify(p.get("identifier"))
    if ident is None or not ident.strong or not p.get("contactRef"):
        return "skipped:identifier_not_full"
    inp.bind(str(p["contactRef"]), ident, ref)
    return "identity"


def _backfill(line: Layer1Line, out: Extracted) -> None:
    record = line.record or {}
    kind = record.get("kind")
    p = record.get("payload") or {}
    if record.get("skip_reason"):
        out.count(line.ref, f"skipped:{record['skip_reason']}")
        return
    if kind in _IDENTITY_KINDS:
        out.count(line.ref, _identity(line.ref, kind, record, p, out))
        return
    channel = record.get("channel") or ""
    if channel != "whatsapp":
        out.count(line.ref, "out_of_scope_channel")
        return
    chat = record.get("chat_id") or p.get("chatJid") or ""
    ms, certainty = record.get("occurred_ms"), record.get("time_certainty") or "unknown"
    provenance = record.get("provenance") or "derived_only"
    if kind == "message":
        segments = p.get("segments")
        if isinstance(segments, list):
            last_id = p.get("messageId")
            parent_media = p.get("media") if isinstance(p.get("media"), dict) else (
                {"kind": p["mediaKind"]} if p.get("mediaKind") else None)
            for index, segment in enumerate(segments):
                if not isinstance(segment, dict):
                    continue
                segment_payload = {**p, **segment}
                segment_payload.pop("segments", None)
                segment_payload["senderId"] = segment.get("senderId", p.get("senderId"))
                segment_id = segment.get("messageId")
                position = len(segments) - index - 1
                batch_key = (json.dumps([chat, str(last_id), position], separators=(",", ":"))
                             if position > 0 and last_id else
                             json.dumps(["source_ref", f"{line.ref}/{index}"], separators=(",", ":"))
                             if not last_id else None)
                segment_media = ({**(parent_media or {}), "kind": segment["mediaKind"]}
                                 if segment.get("mediaKind") else parent_media)
                _message(out, f"{line.ref}/{index}", segment_payload, channel=channel, chat=chat,
                         native_id=segment_id, direction=record.get("direction") or "in", ms=ms,
                         certainty=record.get("time_certainty") or "unknown",
                         provenance=segment.get("provenance") or provenance,
                         media=segment_media, description=segment.get("description"),
                         batch_key=batch_key)
            out.count(line.ref, "message")
            return
        media = p.get("media") if isinstance(p.get("media"), dict) else (
            {"kind": p["mediaKind"]} if p.get("mediaKind") else None)
        _message(out, line.ref, p, channel=channel, chat=chat, native_id=p.get("messageId"),
                 direction=record.get("direction") or "in", ms=ms, certainty=certainty,
                 provenance=provenance, media=media, description=p.get("generatedDescription"))
        out.count(line.ref, "message")
    elif kind in ("reaction", "edit", "delete"):
        _event(out, line.ref, kind, p, channel=channel, chat=chat, ms=ms, certainty=certainty,
               provenance=provenance)
        out.count(line.ref, "event")
    elif kind in ("membership_snapshot", "membership_change"):
        out.count(line.ref, _membership(out, line.ref, kind, p, channel=channel, chat=chat, ms=ms,
                                        certainty=certainty, provenance=provenance))
    elif kind == "media_record":
        if p.get("messageId") and isinstance(p.get("media"), dict):
            out.media_records.append(MediaRecord(line.ref, channel, chat, str(p["messageId"]), p["media"]))
            out.count(line.ref, "media_record")
        else:
            out.count(line.ref, "skipped:media_without_message_id")
    else:
        out.count(line.ref, f"skipped:unhandled:{kind}")


def _owner(line: Layer1Line, out: Extracted) -> None:
    try:
        out.attestations.append(parse(line))
    except ValueError:
        out.count(line.ref, "invalid_attestation")
        return
    out.count(line.ref, "attestation")


def _derived(line: Layer1Line, out: Extracted) -> None:
    record = line.record or {}
    if record.get("kind") != "media_description" or not record.get("native_message_id") \
            or not record.get("text"):
        out.count(line.ref, "skipped:derived_incomplete")
        return
    if (record.get("channel") or "whatsapp") != "whatsapp":
        out.count(line.ref, "out_of_scope_channel")
        return
    out.descriptions.append(Description(line.ref, "whatsapp", record.get("chat_id") or "",
                                        str(record["native_message_id"]), record.get("mode") or "description",
                                        record["text"], record.get("generator"), record.get("generated_ms")))
    out.count(line.ref, "description")
