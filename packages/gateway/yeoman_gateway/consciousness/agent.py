"""Phase 1 consciousness agent wrapper."""

from __future__ import annotations

import inspect
import json
import re
from collections.abc import Awaitable, Callable
from typing import Any

from loguru import logger

from yeoman_gateway.consciousness.tools import ConsciousnessTools

_CODE_FENCE_RE = re.compile(r"^\s*```(?:json|JSON)?\s*\n?(.*?)\n?\s*```\s*$", re.DOTALL)

Planner = Callable[[str], str | dict[str, Any] | Awaitable[str | dict[str, Any]]]


class ConsciousnessAgent:
    """Generate at most one proposal per run, or record a silent pass."""

    def __init__(self, *, tools: ConsciousnessTools, planner: Planner) -> None:
        self._tools = tools
        self._planner = planner

    async def run_once(
        self,
        *,
        trigger: str,
        target_channel: str | None = None,
        target_chat_id: str | None = None,
    ) -> dict[str, object]:
        self._tools.begin_run(trigger=trigger)
        logger.info(
            "consciousness agent run_once trigger={} target_channel={} target_chat={}",
            trigger,
            target_channel or "*",
            target_chat_id or "*",
        )
        eligible = await self._tools.read_eligible_chats()
        if target_channel:
            eligible = [
                chat
                for chat in eligible
                if str(chat.get("channel") or "") == target_channel
            ]
        if target_chat_id:
            eligible = [
                chat
                for chat in eligible
                if str(chat.get("chat_id") or "") == target_chat_id
            ]
        if not eligible:
            if target_channel:
                return {
                    "status": "silent_pass",
                    "reason": "target_channel_not_eligible",
                    "channel": target_channel,
                }
            if target_chat_id:
                return {
                    "status": "silent_pass",
                    "reason": "target_chat_not_eligible",
                    "chat_id": target_chat_id,
                }
            return {"status": "silent_pass", "reason": "no_eligible_chats"}

        chat = eligible[0]
        chat_id = str(chat["chat_id"])
        channel = str(chat["channel"])
        prompt = await self._build_prompt(channel=channel, chat_id=chat_id, eligible=eligible)
        raw = self._planner(prompt)
        if inspect.isawaitable(raw):
            raw = await raw
        decision = self._parse_decision(raw)
        if decision.get("silence") is True:
            return await self._tools.record_silent_pass(
                chat_id=chat_id,
                channel=channel,
                reason=str(decision.get("reason") or "planner_silence"),
                trigger=trigger,
            )

        message = str(decision.get("message") or "").strip()
        if not message:
            return await self._tools.record_silent_pass(
                chat_id=chat_id,
                channel=channel,
                reason="empty_planner_response",
                trigger=trigger,
            )
        decision_chat_id = str(decision.get("chat_id") or chat_id)
        if target_chat_id and decision_chat_id != target_chat_id:
            return await self._tools.record_silent_pass(
                chat_id=chat_id,
                channel=channel,
                reason="target_chat_mismatch",
                trigger=trigger,
            )
        decision_channel = str(decision.get("channel") or "").strip()
        if not decision_channel:
            matching_channels = [
                str(candidate.get("channel") or "")
                for candidate in eligible
                if str(candidate.get("chat_id") or "") == decision_chat_id
            ]
            unique_channels = sorted({candidate for candidate in matching_channels if candidate})
            if len(unique_channels) == 1:
                decision_channel = unique_channels[0]
            else:
                return await self._tools.record_silent_pass(
                    chat_id=chat_id,
                    channel=channel,
                    reason="ambiguous_chat_id",
                    trigger=trigger,
                )
        if target_channel and decision_channel != target_channel:
            return await self._tools.record_silent_pass(
                chat_id=chat_id,
                channel=channel,
                reason="target_channel_mismatch",
                trigger=trigger,
            )

        reply_to_raw = decision.get("reply_to_message_id")
        reply_to = str(reply_to_raw).strip() if reply_to_raw else None
        action_type = str(decision.get("action_type") or "observation")
        confidence = float(decision.get("confidence") or 0.0)
        logger.info(
            "consciousness agent decision trigger={} channel={} chat={} action={} "
            "confidence={:.2f} reply_to={}",
            trigger,
            decision_channel,
            decision_chat_id,
            action_type,
            confidence,
            reply_to or "-",
        )
        proposal = await self._tools.propose_speakup(
            channel=decision_channel,
            chat_id=decision_chat_id,
            message=message,
            action_type=action_type,
            confidence=confidence,
            reply_to_message_id=reply_to or None,
        )
        if proposal.get("status") != "proposed":
            return proposal
        return await self._tools.commit_speakup(str(proposal["proposal_id"]))

    async def _build_prompt(
        self,
        *,
        channel: str,
        chat_id: str,
        eligible: list[dict[str, object]],
    ) -> str:
        enriched_eligible = []
        for chat in eligible:
            enriched = dict(chat)
            usage = await self._tools.read_daily_usage(
                str(chat.get("chat_id") or ""),
                channel=str(chat.get("channel") or ""),
            )
            if usage.get("status") == "ok":
                enriched["daily_cap"] = usage["daily_cap"]
                enriched["base_daily_cap"] = usage["base_daily_cap"]
                enriched["max_daily_cap"] = usage["max_daily_cap"]
                enriched["sent_today"] = usage["sent_today"]
                enriched["daily_remaining"] = usage["daily_remaining"]
                enriched["budget_allowed"] = usage["budget_allowed"]
                enriched["budget_reason"] = usage["budget_reason"]
            enriched_eligible.append(enriched)
        window = await self._tools.read_chat_window(chat_id, n=20, channel=channel)
        memory_query = self._memory_query_from_window(window)
        memory = (
            await self._tools.search_memory(memory_query, chat_id, limit=5, channel=channel)
            if memory_query
            else {"status": "ok", "hits": []}
        )
        learned_taste = await self._tools.read_learned_chat_taste(
            chat_id,
            limit=5,
            channel=channel,
        )
        history = await self._tools.read_speakup_history(chat_id, n=10, channel=channel)
        persona_payload = await self._tools.read_persona_for_chat(chat_id, channel=channel)
        persona_text = (
            persona_payload.get("persona") if isinstance(persona_payload, dict) else None
        )
        trigger_rules: list[str] = []
        trigger = self._tools.current_trigger()
        if trigger == "burst":
            trigger_rules.append(
                "This is an active burst. React only to the current burst window. "
                "If there is no useful current-topic contribution, stay silent."
            )
        elif trigger == "lull":
            trigger_rules.append(
                "This is a lull after recent activity went quiet. You may start a "
                "standalone thought, callback, or fun fact, but do not pretend an old "
                "message is the current thread."
            )
        profile = str(chat.get("profile") or "").strip()
        profile_rules = self._profile_rules(profile)
        return json.dumps(
            {
                "instruction": (
                    "Return JSON only. Either {\"silence\": true, \"reason\": \"...\"} "
                    "or one proposal with chat_id, message, action_type, confidence, "
                    "and optionally reply_to_message_id. "
                    "Do not claim the daily cap is reached unless sent_today is greater "
                    "than or equal to daily_cap. Treat speakup_history status 'denied' "
                    "as owner feedback; 'rejected' and 'expired' are system outcomes."
                ),
                "trigger": trigger,
                "golden_rules": [
                    *trigger_rules,
                    *profile_rules,
                    "Do NOT echo, paraphrase, or restate any message in chat_window. "
                    "If your draft shares a 4-word run with any existing message, rewrite "
                    "it from a different angle or stay silent.",
                    "Add SPECIFIC value: a fact, a number, a named example, a contrarian "
                    "view, or a concrete observation. Generic affirmations are forbidden.",
                    "Do not ask a question that the chat is already actively discussing.",
                    "Match the persona block exactly. Speak as that character, not as a "
                    "neutral assistant.",
                    "Prefer silence over a weak post. A silent_pass with a clear reason is "
                    "always preferable to a generic, derivative, or echo-like message.",
                    "If your message reacts to ONE specific message in chat_window, "
                    "include its message_id as reply_to_message_id so the platform "
                    "renders it as a quoted reply. The chat may have moved on by the "
                    "time you post; the quote anchors your message to the right context. "
                    "If you have no specific anchor, omit reply_to_message_id.",
                    "For action_type correct_error, reply_to_message_id must identify "
                    "the message containing the claim being corrected. If that claim "
                    "is not in the fresh chat_window but a related current question is, "
                    "downgrade to answer_open_question and quote the current question "
                    "instead.",
                ],
                "anti_patterns": [
                    "Restating someone else's joke or quip as your own.",
                    "Vague openers like 'Was denkt ihr darüber?', 'Spannende Frage', "
                    "'Interessant', 'Guter Punkt'.",
                    "Posting feel-good filler without information density.",
                    "Mirroring the last speaker's phrasing with grammar polish.",
                ],
                "persona": persona_text,
                "eligible_chats": enriched_eligible,
                "chat_window": window,
                "memory": memory,
                "learned_taste": learned_taste,
                "speakup_history": history,
            },
            default=str,
        )

    @staticmethod
    def _profile_rules(profile: str) -> list[str]:
        if profile != "permissive":
            return []
        return [
            "Permissive profile: when the chat is lively, a short low-risk line may "
            "be useful even if it is not a data-heavy correction. Specific value can "
            "be social warmth, a compact joke, or a concrete opinion.",
            "For permissive chats, learned_taste is a caution, not a veto. Use it to "
            "avoid bad timing and repetition, but do not let it suppress every "
            "proactive social contribution.",
            "In positive high-noise banter, light_humor and contrarian are acceptable "
            "when they are brief, persona-matched, and non-echoing; the line does not "
            "need a sourced fact every time.",
        ]

    @staticmethod
    def _memory_query_from_window(window: dict[str, object]) -> str:
        messages = window.get("messages")
        if not isinstance(messages, list):
            return ""
        snippets: list[str] = []
        for row in messages[:5]:
            if not isinstance(row, dict):
                continue
            text = str(row.get("text") or "").strip()
            if text:
                snippets.append(text)
        return " ".join(snippets).strip()

    @staticmethod
    def _parse_decision(raw: str | dict[str, Any]) -> dict[str, Any]:
        if isinstance(raw, dict):
            return raw
        text = str(raw or "").strip()
        if not text:
            return {"silence": True, "reason": "empty_planner_response"}
        candidates = [text]
        fence_match = _CODE_FENCE_RE.match(text)
        if fence_match:
            candidates.append(fence_match.group(1).strip())
        first = text.find("{")
        last = text.rfind("}")
        if first != -1 and last > first:
            candidates.append(text[first : last + 1])
        for candidate in candidates:
            try:
                parsed = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                return parsed
        logger.warning(
            "consciousness planner returned non-JSON ({} chars): {!r}",
            len(text),
            text[:500],
        )
        return {"silence": True, "reason": "invalid_planner_json"}
