"""Phase 1 consciousness agent wrapper."""

from __future__ import annotations

import inspect
import json
from collections.abc import Awaitable, Callable
from typing import Any

from yeoman_gateway.consciousness.tools import ConsciousnessTools

Planner = Callable[[str], str | dict[str, Any] | Awaitable[str | dict[str, Any]]]


class ConsciousnessAgent:
    """Generate at most one proposal per run, or record a silent pass."""

    def __init__(self, *, tools: ConsciousnessTools, planner: Planner) -> None:
        self._tools = tools
        self._planner = planner

    async def run_once(self, *, trigger: str) -> dict[str, object]:
        self._tools.begin_run(trigger=trigger)
        eligible = await self._tools.read_eligible_chats()
        if not eligible:
            return {"status": "silent_pass", "reason": "no_eligible_chats"}

        chat_id = str(eligible[0]["chat_id"])
        prompt = await self._build_prompt(chat_id=chat_id, eligible=eligible)
        raw = self._planner(prompt)
        if inspect.isawaitable(raw):
            raw = await raw
        decision = self._parse_decision(raw)
        if decision.get("silence") is True:
            return await self._tools.record_silent_pass(
                chat_id=chat_id,
                reason=str(decision.get("reason") or "planner_silence"),
                trigger=trigger,
            )

        message = str(decision.get("message") or "").strip()
        if not message:
            return await self._tools.record_silent_pass(
                chat_id=chat_id,
                reason="empty_planner_response",
                trigger=trigger,
            )

        proposal = await self._tools.propose_speakup(
            chat_id=str(decision.get("chat_id") or chat_id),
            message=message,
            action_type=str(decision.get("action_type") or "observation"),
            confidence=float(decision.get("confidence") or 0.0),
        )
        if proposal.get("status") != "proposed":
            return proposal
        return await self._tools.commit_speakup(str(proposal["proposal_id"]))

    async def _build_prompt(
        self,
        *,
        chat_id: str,
        eligible: list[dict[str, object]],
    ) -> str:
        enriched_eligible = []
        for chat in eligible:
            enriched = dict(chat)
            usage = await self._tools.read_daily_usage(str(chat.get("chat_id") or ""))
            if usage.get("status") == "ok":
                enriched["sent_today"] = usage["sent_today"]
                enriched["daily_remaining"] = usage["daily_remaining"]
            enriched_eligible.append(enriched)
        window = await self._tools.read_chat_window(chat_id, n=20)
        memory = await self._tools.search_memory("", chat_id, limit=5)
        history = await self._tools.read_speakup_history(chat_id, n=10)
        return json.dumps(
            {
                "instruction": (
                    "Return JSON only. Either {\"silence\": true, \"reason\": \"...\"} "
                    "or one proposal with chat_id, message, action_type, confidence. "
                    "Do not claim the daily cap is reached unless sent_today is greater "
                    "than or equal to daily_cap. Treat speakup_history status 'denied' "
                    "as owner feedback; 'rejected' and 'expired' are system outcomes."
                ),
                "eligible_chats": enriched_eligible,
                "chat_window": window,
                "memory": memory,
                "speakup_history": history,
            },
            default=str,
        )

    @staticmethod
    def _parse_decision(raw: str | dict[str, Any]) -> dict[str, Any]:
        if isinstance(raw, dict):
            return raw
        text = str(raw or "").strip()
        if not text:
            return {"silence": True, "reason": "empty_planner_response"}
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return {"silence": True, "reason": "invalid_planner_json"}
        return parsed if isinstance(parsed, dict) else {"silence": True, "reason": "invalid_planner_json"}
