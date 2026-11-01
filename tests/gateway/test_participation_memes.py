"""Autonomous memes and stickers: offered only on opt-in, capped, posted image-only.

Real temporary ledger and real meme rendering; the judge and the transport are fakes.
"""

from __future__ import annotations

import json
import time
from dataclasses import replace
from pathlib import Path

import pytest
from PIL import Image
from yeoman_gateway.media.document_cache import DocumentCache
from yeoman_gateway.media.memes.service import MemeService
from yeoman_gateway.policy.schema import ParticipationPolicy, ParticipationPolicyOverride
from yeoman_gateway.processing.participation import (
    ParticipationDecision,
    ParticipationDecisionError,
    ParticipationJudge,
)
from yeoman_gateway.processing.participation_memes import MemePlan, ParticipationMemes

from tests.gateway.test_participation_integration import (
    CHANNEL,
    CHAT,
    NOW_MS,
    _opportunity,
    _Reactor,
    _runtime,
)
from tests.gateway.test_participation_judge import _Client, _context
from tests.gateway.test_participation_judge import _opportunity as _judge_opportunity

WEEK_MS = 7 * 24 * 60 * 60 * 1000
DAY_MS = 24 * 60 * 60 * 1000

MEME = ParticipationDecision(
    action="meme",
    intent="initiate",
    reason="the plan collapsed exactly like the template",
    reading="friends joke that the trip plan fell apart",
    target_message_id="m1",
    meme=MemePlan(source="template", template="fine", captions=("trip plan", "this is fine")),
)
STICKER = ParticipationDecision(
    action="sticker",
    intent="initiate",
    reason="small gesture",
    target_message_id="m1",
    meme=MemePlan(source="template", template="doge", captions=("such plan", "wow")),
)


def _meme_snapshot(*, memes: int = 3, stickers: int = 2, spacing: int = 0) -> dict[str, object]:
    return {
        "allow_memes": True,
        "min_messages_between_memes": spacing,
        "meme_limits": (("meme", memes, WEEK_MS),) if memes else (),
        "sticker_limits": (("sticker", stickers, DAY_MS),) if stickers else (),
    }


class _CapturingJudge:
    def __init__(self, decision: ParticipationDecision) -> None:
        self.decision = decision
        self.contexts: list[dict[str, object]] = []

    async def decide(self, opportunity, context):
        del opportunity
        self.contexts.append(dict(context))
        return self.decision


class _FailingMemes(ParticipationMemes):
    async def render(self, plan, *, channel, chat_id, sticker):
        raise RuntimeError("render broke")


def _with_memes(tmp_path: Path, decision: ParticipationDecision, *, memes_cls=ParticipationMemes, **kwargs):
    runtime, _judge, context, log = _runtime(tmp_path, decision=decision, **kwargs)
    judge = _CapturingJudge(decision)
    sender = _Reactor()
    runtime._judge = judge
    runtime._memes = memes_cls(service=MemeService(outgoing_dir=tmp_path / "outgoing"))
    runtime._media_sender = sender
    return runtime, judge, sender, context, log


def _later(runtime, opportunity, *, hours: int):
    """Move the clock past the judge gap; the opportunity is created at that time."""
    now = NOW_MS + hours * 3_600_000
    runtime._clock_ms = lambda: now
    return replace(opportunity, created_at_ms=now)


async def _consumed(log, category: str, window_ms: int) -> int:
    return await log.consumed_slots(
        channel=CHANNEL, chat_id=CHAT, category=category, now_ms=NOW_MS, window_ms=window_ms
    )


@pytest.mark.asyncio
async def test_memes_are_not_offered_without_the_chat_flag(tmp_path: Path) -> None:
    runtime, judge, sender, context, log = _with_memes(tmp_path, MEME)
    await runtime.evaluate_participation(_opportunity())
    assert context.inputs is not None
    assert not {"meme", "sticker"} & set(context.inputs.allowed_actions)
    assert "meme_templates" not in judge.contexts[0]
    assert sender.calls == []
    log.close()


@pytest.mark.asyncio
async def test_meme_is_rendered_and_posted_as_one_captionless_image(tmp_path: Path) -> None:
    runtime, judge, sender, _context, log = _with_memes(
        tmp_path, MEME, snapshot_overrides=_meme_snapshot()
    )
    result = await runtime.evaluate_participation(_opportunity())
    assert result["status"] == "meme_submitted"
    seen = judge.contexts[0]
    assert {"meme", "sticker"} <= set(seen["allowed_actions"])
    assert "fine" in seen["meme_templates"]
    assert "Memes: 3 of 3 left" in str(seen["meme_budget_text"])
    (call,) = sender.calls
    path = Path(str(call["media_path"]))
    assert path.is_file() and path.suffix == ".jpg" and path.parent.name == "memes"
    assert "content" not in call and "caption" not in call
    assert call["admission"].action == "meme"
    assert await _consumed(log, "meme", WEEK_MS) == 1
    state = await log.disposition("opp-1")
    assert state is not None and state["disposition"] == "meme_submitted"
    log.close()


@pytest.mark.asyncio
async def test_sticker_is_rendered_as_webp_and_counts_as_a_sticker(tmp_path: Path) -> None:
    runtime, _judge, sender, _context, log = _with_memes(
        tmp_path, STICKER, snapshot_overrides=_meme_snapshot()
    )
    result = await runtime.evaluate_participation(_opportunity())
    assert result["status"] == "sticker_submitted"
    path = Path(str(sender.calls[0]["media_path"]))
    assert path.suffix == ".webp" and path.parent.name == "stickers"
    assert await _consumed(log, "sticker", DAY_MS) == 1
    assert await _consumed(log, "meme", WEEK_MS) == 0
    log.close()


@pytest.mark.asyncio
async def test_used_up_meme_cap_removes_only_the_meme_action(tmp_path: Path) -> None:
    runtime, judge, _sender, _context, log = _with_memes(
        tmp_path, MEME, snapshot_overrides=_meme_snapshot(memes=1)
    )
    assert (await runtime.evaluate_participation(_opportunity()))["status"] == "meme_submitted"
    second = _later(
        runtime, replace(_opportunity("m9"), opportunity_id="opp-2", observed_revision=40), hours=2
    )
    judge.decision = replace(STICKER, target_message_id="m9")
    result = await runtime.evaluate_participation(second)
    offered = set(judge.contexts[-1]["allowed_actions"])
    assert "meme" not in offered and "sticker" in offered
    assert "Memes: 0 of 1 left" in str(judge.contexts[-1]["meme_budget_text"])
    assert result["status"] == "sticker_submitted"
    log.close()


@pytest.mark.asyncio
async def test_message_spacing_blocks_both_image_actions(tmp_path: Path) -> None:
    runtime, judge, _sender, context, log = _with_memes(
        tmp_path, MEME, snapshot_overrides=_meme_snapshot(spacing=15)
    )
    assert (await runtime.evaluate_participation(_opportunity()))["status"] == "meme_submitted"
    soon = _later(
        runtime, replace(_opportunity("m4"), opportunity_id="opp-2", observed_revision=10), hours=2
    )
    await runtime.evaluate_participation(soon)
    assert context.inputs is not None
    assert not {"meme", "sticker"} & set(context.inputs.allowed_actions)
    later = _later(
        runtime, replace(_opportunity("m5"), opportunity_id="opp-3", observed_revision=18), hours=4
    )
    judge.decision = ParticipationDecision(action="silence", intent="initiate", reason="-")
    await runtime.evaluate_participation(later)
    assert {"meme", "sticker"} <= set(context.inputs.allowed_actions)
    log.close()


@pytest.mark.asyncio
async def test_render_failure_frees_the_slot_and_sends_nothing(tmp_path: Path) -> None:
    runtime, _judge, sender, _context, log = _with_memes(
        tmp_path, MEME, memes_cls=_FailingMemes, snapshot_overrides=_meme_snapshot()
    )
    result = await runtime.evaluate_participation(_opportunity())
    assert result["status"] == "meme_failed"
    assert sender.calls == []
    assert await _consumed(log, "meme", WEEK_MS) == 0
    state = await log.disposition("opp-1")
    assert state is not None and state["disposition"] == "meme_failed"
    log.close()


@pytest.mark.asyncio
async def test_failed_transport_releases_the_meme_slot(tmp_path: Path) -> None:
    runtime, _judge, sender, _context, log = _with_memes(
        tmp_path, MEME, snapshot_overrides=_meme_snapshot()
    )
    runtime._media_sender = _Reactor(state="failed")
    result = await runtime.evaluate_participation(_opportunity())
    assert result["status"] == "meme_failed"
    assert await _consumed(log, "meme", WEEK_MS) == 0
    log.close()


@pytest.mark.asyncio
async def test_shadow_lane_judges_a_meme_but_renders_nothing(tmp_path: Path) -> None:
    runtime, _judge, sender, _context, log = _with_memes(
        tmp_path, MEME, shadow=True, snapshot_overrides=_meme_snapshot()
    )
    result = await runtime.evaluate_participation(_opportunity(lane="shadow"))
    assert result["status"] == "shadow" and result["action"] == "meme"
    assert sender.calls == []
    assert not (tmp_path / "outgoing").exists()
    log.close()


# -- judge ----------------------------------------------------------------------------


def _meme_context(**overrides: object) -> dict[str, object]:
    base = _context(
        allowed_actions=["silence", "react", "comment", "meme", "sticker"],
        meme_templates=["drake", "fine"],
        meme_template_text="drake: Drakeposting (2 captions)\nfine: This is Fine (2 captions)",
        meme_photos=[{"id": "m1", "description": "a burnt pizza on a plate"}],
        meme_backgrounds=False,
        meme_budget_text="Memes: 2 of 3 left in the last 7 days.",
    )
    base.update(overrides)
    return base


def _answer(**fields: object) -> str:
    base: dict[str, object] = {
        "reading": "joke about the goalkeeper",
        "action": "meme",
        "intent": "initiate",
        "reason": "picture beats words",
        "evidence_ids": ["m2"],
        "anchor_message_id": None,
        "target_message_id": "m2",
        "contribution_type": None,
        "purpose": "",
        "emoji": None,
        "closes_exchange": False,
        "meme_source": "template",
        "meme_template": "drake",
        "meme_photo_id": None,
        "meme_captions": ["asking who plays", "nobody answers"],
        "meme_prompt": None,
    }
    base.update(fields)
    return json.dumps(base)


@pytest.mark.asyncio
async def test_judge_parses_a_template_meme_and_sees_the_offer() -> None:
    client = _Client(_answer())
    judge = ParticipationJudge(client=client)
    decision = await judge.decide(_judge_opportunity(), _meme_context())
    assert decision.action == "meme"
    assert decision.meme == MemePlan(
        source="template", template="drake", captions=("asking who plays", "nobody answers")
    )
    system, user = client.calls[0][0]["content"], client.calls[0][1]["content"]
    assert "Memes and stickers" in system
    assert "[Meme options]" in user and "drake: Drakeposting" in user
    assert 'id="m1": a burnt pizza on a plate' in user
    properties = client.response_formats[0]["json_schema"]["schema"]["properties"]
    assert properties["action"]["enum"] == [
        "silence",
        "react",
        "comment",
        "meme",
        "sticker",
    ]
    assert properties["intent"]["enum"] == ["initiate", "continue"]
    assert "meme_source" in properties
    assert client.max_tokens[0] >= 384


@pytest.mark.asyncio
async def test_judge_without_meme_offer_keeps_the_old_prompt_and_schema() -> None:
    client = _Client(_answer(action="silence"))
    judge = ParticipationJudge(client=client)
    await judge.decide(_judge_opportunity(), _context())
    assert "Memes and stickers" not in client.calls[0][0]["content"]
    assert "[Meme options]" not in client.calls[0][1]["content"]
    properties = client.response_formats[0]["json_schema"]["schema"]["properties"]
    assert properties["action"]["enum"] == ["silence", "react", "comment"]
    assert properties["intent"]["enum"] == ["initiate", "continue"]
    assert "meme_source" not in properties


@pytest.mark.asyncio
async def test_judge_rejects_a_meme_when_not_offered() -> None:
    judge = ParticipationJudge(client=_Client(_answer()))
    with pytest.raises(ParticipationDecisionError) as exc:
        await judge.decide(_judge_opportunity(), _context())
    assert exc.value.detail == "action_not_allowed"


@pytest.mark.parametrize(
    ("fields", "detail"),
    [
        ({"meme_template": "unknown"}, "meme_template_unknown"),
        ({"meme_captions": ["", " "]}, "meme_captions_empty"),
        ({"meme_source": "photo", "meme_photo_id": "m2"}, "meme_photo_not_offered"),
        ({"meme_source": "background", "meme_prompt": "a cat"}, "meme_background_unavailable"),
        ({"meme_source": "gif"}, "meme_source_invalid"),
    ],
)
@pytest.mark.asyncio
async def test_judge_rejects_meme_plans_outside_the_offer(fields, detail) -> None:
    judge = ParticipationJudge(client=_Client(_answer(**fields)))
    with pytest.raises(ParticipationDecisionError) as exc:
        await judge.decide(_judge_opportunity(), _meme_context())
    assert exc.value.detail == detail


@pytest.mark.asyncio
async def test_judge_accepts_an_offered_photo_and_a_sticker() -> None:
    judge = ParticipationJudge(
        client=_Client(_answer(action="sticker", meme_source="photo", meme_photo_id="m1"))
    )
    decision = await judge.decide(_judge_opportunity(), _meme_context())
    assert decision.action == "sticker"
    assert decision.meme is not None and decision.meme.photo_id == "m1"


# -- offer ----------------------------------------------------------------------------


def _cached_photo(tmp_path: Path, cache: DocumentCache, message_id: str) -> None:
    path = tmp_path / f"{message_id}.jpg"
    Image.new("RGB", (320, 240), "green").save(path)
    cache.record_media_item(
        channel=CHANNEL,
        chat_id=CHAT,
        message_id=message_id,
        sender_id="s",
        sender_name="Sam",
        kind="image",
        mime_type="image/jpeg",
        file_name=None,
        local_path=path,
        size_bytes=path.stat().st_size,
        timestamp=int(time.time()),
    )


@pytest.mark.asyncio
async def test_offer_lists_described_cached_photos_of_this_chat_only(tmp_path: Path) -> None:
    cache = DocumentCache(tmp_path / "cache.db")
    for message_id in ("p1", "p2", "p3"):
        _cached_photo(tmp_path, cache, message_id)
    memes = ParticipationMemes(
        service=MemeService(outgoing_dir=tmp_path / "outgoing"), media_cache=cache
    )
    messages = [
        {"event_id": "p1", "media_summary": "[Derived description]: two friends at a lake"},
        {"event_id": "p2", "media_summary": ""},
        {"event_id": "p3", "media_summary": "[image_description] a cat", "chat_id": "other@g.us"},
        {"event_id": "p4", "media_summary": "[Derived description]: not in the cache"},
    ]
    offer = memes.offer(channel=CHANNEL, chat_id=CHAT, messages=messages)
    assert offer.photos == (("p1", "two friends at a lake"),)
    assert len(offer.templates) >= 30 and offer.backgrounds is False
    path = await memes.render(
        MemePlan(source="photo", photo_id="p1", captions=("when the lake", "is cold")),
        channel=CHANNEL,
        chat_id=CHAT,
        sticker=False,
    )
    assert path.is_file()
    with pytest.raises(LookupError):
        await memes.render(
            MemePlan(source="photo", photo_id="p4", captions=("x",)),
            channel=CHANNEL,
            chat_id=CHAT,
            sticker=False,
        )


def test_policy_flag_is_off_by_default_and_tunable() -> None:
    default = ParticipationPolicy()
    assert default.auto_memes is False
    assert (default.max_memes_per_week, default.max_stickers_per_day) == (3, 2)
    tuned = ParticipationPolicyOverride.model_validate(
        {"autoMemes": True, "maxMemesPerWeek": 2, "maxStickersPerDay": 1,
         "minMessagesBetweenMemes": 30}
    )
    assert tuned.auto_memes is True and tuned.max_memes_per_week == 2
    assert tuned.min_messages_between_memes == 30


# -- transport ------------------------------------------------------------------------


class _ManagedRouter:
    def __init__(self) -> None:
        self.calls: list[tuple[object, dict[str, object]]] = []

    def manages(self, channel: str, chat_id: str) -> bool:
        del channel, chat_id
        return True

    async def submit_message(self, message: object, **kwargs: object) -> object:
        self.calls.append((message, kwargs))
        return "receipt"


class _Bus:
    async def publish_outbound(self, message: object) -> None:
        raise AssertionError("a managed meme must never take the legacy bus path")


@pytest.mark.asyncio
async def test_service_media_effect_is_captionless_and_needs_an_admission() -> None:
    from types import SimpleNamespace

    from yeoman_gateway.processing.dispatch import EffectNotDeliveredError, ServiceEffectProducer
    from yeoman_gateway.processing.models import MediaPayload

    router = _ManagedRouter()
    producer = ServiceEffectProducer(router=router, bus=_Bus())  # type: ignore[arg-type]
    with pytest.raises(EffectNotDeliveredError, match="admission"):
        await producer.send_media(
            source="speakup", operation_ref="m", channel=CHANNEL, chat_id=CHAT,
            media_path="/x/meme.jpg", effect_id="e1", admission=None,
        )
    assert router.calls == []
    await producer.send_media(
        source="speakup", operation_ref="m", channel=CHANNEL, chat_id=CHAT,
        media_path="/x/meme.jpg", effect_id="e1", admission=SimpleNamespace(admission_id="a1"),
    )
    ((message, kwargs),) = router.calls
    assert kwargs["capability"] == "send_media"
    assert kwargs["payload"] == MediaPayload(media=("/x/meme.jpg",))
    assert message.content == "" and message.media == ["/x/meme.jpg"]
