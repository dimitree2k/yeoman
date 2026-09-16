"""Structured A2A delegation tool."""

from __future__ import annotations

import asyncio
import contextvars
import hashlib
import json
import re
import time
from typing import Any

from loguru import logger

from yeoman_gateway.a2a.client import A2APollTimeoutError, A2AProtocolError, A2ATransportError
from yeoman_gateway.a2a.contracts import A2AContractValidationError
from yeoman_gateway.a2a.registry import A2AWorkerRegistry
from yeoman_gateway.agent.tools.base import Tool
from yeoman_gateway.channels.whatsapp import _markdown_to_whatsapp as _to_chat_markup
from yeoman_gateway.observability import private_log_identifier, safe_log_token
from yeoman_gateway.processing.models import (
    EffectConflictError,
    EffectTarget,
    ExternalActionPayload,
)
from yeoman_gateway.processing.tool_context import current_tool_context

from .a2a_research import A2AResearchStore, PendingResearch, sibling_path

DELEGATION_WINDOW_MS = 600_000
DELEGATION_WORKER_ID = "a2a_delegate"
DELEGATION_LEASE_MS = 900_000

#: One ``poll_task`` call already waits the client's maximum window (1800s). Deep
#: research can legitimately outlive a single window, so a poll timeout extends the
#: wait by another round instead of reporting POLL_TIMEOUT for a task that is still
#: making progress. The budget resets on restart, where durable pending entries are
#: resumed by ``resume_pending_research``.
RESEARCH_POLL_EXTENSIONS = 3
ASYNC_RESEARCH_SKILLS = frozenset({"research.deep", "trading.analyze"})
POLL_TIMEOUT_CONTENT = "error=POLL_TIMEOUT retryable=True"

#: The worker wraps its answer in a reasoning preamble and returns it as JSON. Neither belongs
#: in a chat message, so the delivery path renders the structured payload into chat text.
_REASONING_FENCE_RE = re.compile(r"^\s*💭?\s*\*\*Reasoning:\*\*\s*```[\s\S]*?```\s*", re.MULTILINE)
_REASONING_BLOCK_RE = re.compile(r"^\s*💭?\s*\*\*Reasoning:\*\*[\s\S]*?(?=\n\s*\n|$)", re.MULTILINE)
_SERVER_PATH_LINE_RE = re.compile(
    r"^\s*(?:[-•*]\s*)?(?:Der\s+)?(?:vollständige[rn]?\s+)?(?:Report|Vollreport|Reportdatei)"
    r"[^\n]*?(?:/[\w./-]+\.md|/\w+/\.hermes/\S*)[^\n]*$",
    re.IGNORECASE | re.MULTILINE,
)
_SERVER_PATH_RE = re.compile(r"(?:^|\s)(?:/home/|/srv/|/var/|/opt/)\S*")
_SOURCE_LINE_LIMIT = 5
#: A chat answer stays short by default: the long report belongs in the file, not in the message.
_CARD_DECISION_HEADINGS = (
    "entscheidung",
    "kurzfazit",
    "fazit",
    "ergebnis",
    "decision",
    "summary",
    "take",
)
_CARD_HEADING_RE = re.compile(r"^#{2,4}\s*(.+?)\s*$")
_CARD_DECISION_RE = re.compile(r"\b(buy|sell|hold|underweight|overweight|neutral|reduzieren|kaufen|verkaufen|halten)\b", re.IGNORECASE)
_CARD_BULLET_LIMIT = 4
_CARD_FALLBACK_CHARS = 700
_CARD_NOTE = "Langfassung auf Abruf."
_TICKER_RE = re.compile(r"\(([A-Z]{1,5})\)|\$([A-Z]{1,5})\b")


def _ticker(text: str) -> str:
    match = _TICKER_RE.search(text)
    return next((group for group in match.groups() if group), "") if match else ""


def _trading_signal(report: str) -> str:
    """Cache only a verdict from a labelled decision section, never a title or risk quote."""
    lines = report.splitlines()
    headings = [
        (index, match.group(1).lower())
        for index, line in enumerate(lines)
        if (match := _CARD_HEADING_RE.match(line.strip()))
    ]
    for wanted in ("entscheidung|decision|signal|empfehlung", "kurzfazit|fazit"):
        for position, (start, heading) in enumerate(headings):
            if not re.search(wanted, heading):
                continue
            end = headings[position + 1][0] if position + 1 < len(headings) else len(lines)
            for line in lines[start + 1 : end]:
                match = re.search(r"\*{1,2}(BUY|HOLD|SELL|KAUFEN|HALTEN|VERKAUFEN)\*{1,2}", line, re.IGNORECASE)
                if match:
                    return match.group(1).upper()
    return ""


def _strip_reasoning(text: str) -> str:
    """Drop the model's planning preamble: a chat reader wants the answer, not the plan."""
    stripped = _REASONING_FENCE_RE.sub("", text, count=1)
    if stripped == text:
        stripped = _REASONING_BLOCK_RE.sub("", text, count=1)
    return stripped.strip()


def _strip_markdown_line_breaks(text: str) -> str:
    """Two trailing spaces are markdown's hard line break; in a chat they are stray whitespace."""
    return re.sub(r"[ \t]+$", "", text, flags=re.MULTILINE)


def _strip_server_paths(text: str) -> str:
    """Remove lines naming the worker's report file: that path is not reachable from the chat."""
    without_lines = _SERVER_PATH_LINE_RE.sub("", text)
    return _SERVER_PATH_RE.sub(" ", without_lines).strip()


def _source_lines(sources: Any) -> str:
    if not isinstance(sources, list):
        return ""
    lines: list[str] = []
    for entry in sources[:_SOURCE_LINE_LIMIT]:
        if isinstance(entry, dict):
            title = str(entry.get("title") or "").strip()
            url = str(entry.get("url") or "").strip()
        else:
            title, url = "", str(entry).strip()
        if not url and not title:
            continue
        lines.append(f"• {title}: {url}" if title and url else f"• {title or url}")
    if not lines:
        return ""
    return "Quellen:\n" + "\n".join(lines)


def _card_body(text: str) -> str:
    """Condense a long report into lead, decision and the strongest reasons.

    The full report stays in the file; a chat reader wants the verdict and why, not the whole
    document. Falls back to a bounded excerpt when no decision section is recognisable.
    """
    lines = text.splitlines()
    heading_rows = [
        (index, match.group(1))
        for index, line in enumerate(lines)
        if (match := _CARD_HEADING_RE.match(line.strip()))
    ]
    title = next(
        (line.strip()[2:].strip() for line in lines if line.strip().startswith("# ")),
        "",
    )
    if not title and heading_rows:
        title = heading_rows[0][1]
    lead = " ".join(title.split())

    decision_row = next(
        ((index, title) for index, title in heading_rows if "entscheidung" in title.lower() or "decision" in title.lower()),
        None,
    ) or next(
        ((index, title) for index, title in heading_rows if any(token in title.lower() for token in _CARD_DECISION_HEADINGS)),
        None,
    )
    decision = ""
    bullets: list[str] = []
    if decision_row is not None:
        for line in lines[decision_row[0] + 1 :]:
            stripped = line.strip()
            if not stripped:
                continue
            heading = _CARD_HEADING_RE.match(stripped)
            if heading:
                if bullets:
                    # The reasons usually sit in the section after the verdict; stop there.
                    break
                continue
            if stripped.startswith(("- ", "* ", "• ")):
                if len(bullets) < _CARD_BULLET_LIMIT:
                    bullets.append("• " + stripped[2:].strip())
                elif bullets:
                    break
                continue
            if not decision:
                decision = stripped
    signal = next(
        (line.strip() for line in lines if _CARD_DECISION_RE.search(line) and not line.lstrip().startswith("#")),
        "",
    )
    if signal and _CARD_DECISION_RE.search(decision):
        signal = ""
    if not decision and not bullets and not signal:
        return " ".join(text.split())[:_CARD_FALLBACK_CHARS]

    parts = [part for part in (lead, decision, signal, *bullets) if part]
    return "\n\n".join(parts)


def render_research_output(payload: Any, *, mode: str = "card", offer_full: bool = True) -> str:
    """Render a research result as chat text.

    The structured payload is ``{"report": ..., "sources": [...]}`` where the report carries a
    reasoning preamble and server-side file paths. For a chat message all three are noise, so
    the body is cleaned and the sources are listed explicitly instead of being buried in prose.

    ``mode="card"`` (the default) condenses the report to the verdict plus its strongest reasons
    and points at the full version on request; ``mode="full"`` keeps the whole cleaned report for
    callers that need the detail. Plain strings (errors, summaries) pass through unchanged.
    """
    if isinstance(payload, dict):
        report = payload.get("report")
        text = str(report) if isinstance(report, str) else ""
        sources = _source_lines(payload.get("sources"))
        if not text and not sources:
            return ""
        # Condense while the markdown structure is still intact: the chat conversion below
        # flattens headings into plain lines, which would hide the section boundaries.
        cleaned = _strip_markdown_line_breaks(_strip_server_paths(_strip_reasoning(text))).strip()
        body = _card_body(cleaned) if mode == "card" else cleaned
        body = _to_chat_markup(body).strip()
        if mode == "card" and offer_full and body and body != cleaned:
            body = f"{body}\n\n{_CARD_NOTE}"
        if sources:
            return f"{body}\n\n{sources}" if body else sources
        return body
    if payload is None:
        return ""
    return str(payload)


class A2ADelegateTool(Tool):
    """Invoke an advertised Hermes profile skill; never sends a text conversation."""

    def __init__(
        self,
        registry: A2AWorkerRegistry,
        *,
        store: Any | None = None,
        delivery: Any | None = None,
        pending_store: A2AResearchStore | None = None,
        research_poll_extensions: int = RESEARCH_POLL_EXTENSIONS,
        quota_governance: Any | None = None,
    ) -> None:
        self._registry = registry
        self._store = store
        self._delivery = delivery
        self._research_poll_extensions = max(0, int(research_poll_extensions))
        self._quota_governance = quota_governance
        self._research_store: A2AResearchStore | None
        if pending_store is not None:
            self._research_store = pending_store
        else:
            path = sibling_path(store)
            self._research_store = A2AResearchStore(path) if path else None
        self._background: set[asyncio.Task[None]] = set()
        self._background_task_ids: set[str] = set()
        self._channel = ""
        self._chat_id = ""
        self._session_key = ""
        self.resume_pending_research()

    def set_context(self, channel: str, chat_id: str, *, session_key: str = "") -> None:
        self._channel, self._chat_id, self._session_key = (
            str(channel or ""),
            str(chat_id or ""),
            str(session_key or ""),
        )

    @property
    def name(self) -> str:
        return "a2a_delegate"

    @property
    def description(self) -> str:
        return f"Invoke one structured skill on: {', '.join(self._registry.names) or 'configured workers'}."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "worker": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Registered A2A worker name.",
                },
                "skill": {
                    "type": "string",
                    "enum": ["search.web", "research.deep", "trading.analyze"],
                    "description": "Supported delegated skill.",
                },
                "input": {"type": "object", "description": "Skill-specific structured input."},
            },
            "required": ["worker", "skill", "input"],
            "additionalProperties": False,
        }

    def _context_id(self) -> str | None:
        return self._session_key or None

    def _claim(self, worker: str, skill: str, input: dict[str, Any]) -> tuple[bool, str, str]:
        if self._store is None:
            return True, "no-outbox", ""
        canonical = json.dumps(
            {"skill": skill, "input": input}, sort_keys=True, separators=(",", ":")
        )
        digest = hashlib.sha256(canonical.encode()).hexdigest()
        context = current_tool_context()
        channel = context.channel if context else self._channel
        chat_id = context.chat_id if context else self._chat_id
        turn = (
            context.reply_to_message_id
            if context and context.reply_to_message_id
            else f"window:{int(time.time() * 1000) // DELEGATION_WINDOW_MS}"
        )
        operation_key = f"a2a:{worker}:{channel}:{chat_id}:{turn}"
        # The window makes an identical retry collapse onto one effect; the payload digest keeps
        # two *different* delegations in the same window distinct. Without it, a corrected retry
        # after a locally rejected invocation conflicts with the rejected attempt's effect and
        # never reaches the worker (production 2026-09-14).
        effect_key = f"{operation_key}:{digest}"
        effect_id = "a2a-" + hashlib.sha256(effect_key.encode()).hexdigest()[:32]
        try:
            stored = self._store.enqueue_effect(
                effect_id=effect_id,
                operation_key=effect_key,
                payload=ExternalActionPayload(
                    action="a2a_delegate",
                    arguments={"worker": worker, "skill": skill, "input_sha256": digest},
                ),
                now_ms=int(time.time() * 1000),
                trace_id=operation_key,
                capability="a2a_delegate",
                target=EffectTarget(channel="a2a", chat_id=worker),
                state="queued",
            )
            if str(stored) != effect_id:
                # The store collapsed this call onto an existing effect; only an identical
                # payload is a retry. Log it, because the caller sees just the note.
                logger.warning(
                    "A2A delegation collapsed onto an existing effect worker={} note=duplicate",
                    safe_log_token(worker),
                )
                return False, "duplicate", effect_id
            if not self._store.claim_effect(
                effect_id, DELEGATION_WORKER_ID, int(time.time() * 1000), DELEGATION_LEASE_MS
            ):
                # Same payload, same window, already settled (for example by a failed attempt):
                # the store never re-executes an unproven effect on its own.
                logger.warning(
                    "A2A delegation already settled in this window worker={} state={} note=duplicate",
                    safe_log_token(worker),
                    self._effect_state(effect_id),
                )
                return False, "duplicate", effect_id
        except EffectConflictError:
            logger.warning(
                "A2A delegation conflicts with an earlier effect in this window worker={} note=conflict",
                safe_log_token(worker),
            )
            return False, "conflict", effect_id
        except Exception as exc:
            logger.warning(
                "A2A delegation could not be claimed worker={} error_type={}",
                safe_log_token(worker),
                type(exc).__name__,
            )
            return False, "claim-failed", ""
        return True, "claimed", effect_id

    def _effect_state(self, effect_id: str) -> str:
        """Best-effort state of a stored effect, for logs only."""
        getter = getattr(self._store, "effect_state", None)
        if not callable(getter):
            return "unknown"
        try:
            return str(getter(effect_id) or "unknown")
        except Exception:
            return "unknown"

    @staticmethod
    def _is_local_rejection(exc: BaseException) -> bool:
        """True when the invocation was rejected before any network transmission.

        ``A2AClient`` validates the invocation locally and reports the schema violation as the
        cause; a worker-side or transport failure never carries that cause.
        """
        return isinstance(exc, A2AProtocolError) and isinstance(
            exc.__cause__, A2AContractValidationError
        )

    @staticmethod
    def _question_text(input: dict[str, Any]) -> str:
        """The owner-facing request text, bounded: it is echoed in the follow-up header."""
        raw = input.get("question")
        if not isinstance(raw, str):
            return ""
        return " ".join(raw.split())[:300]

    @staticmethod
    def _elapsed_text(created_ms: int, now_ms: int) -> str:
        minutes = max(0, int((now_ms - created_ms) / 60000)) if created_ms else 0
        if minutes < 1:
            return "unter einer Minute"
        if minutes < 60:
            return f"{minutes} Minuten"
        hours = minutes // 60
        rest = minutes % 60
        return f"{hours} h {rest} min" if rest else f"{hours} h"

    def _follow_up_content(self, content: str, question: str, created_ms: int) -> str:
        """Label a detached result as the answer to a specific, earlier request.

        A long run can finish while the chat has moved on to another topic, so the delivered
        message states which request it answers and how long it took. Nothing is suppressed:
        the answer still arrives, it is just not mistaken for a reply to the current question.
        """
        head = "Nachtrag zur Trading-Recherche"
        if question:
            head += f" zu deiner Frage „{question}“"
        head += f" (angefragt vor {self._elapsed_text(created_ms, int(time.time() * 1000))}):"
        if not content:
            return head
        return f"{head}\n\n{content}"

    def _settle(self, effect_id: str, state: str) -> None:
        if self._store is None or not effect_id:
            return
        try:
            self._store.transition(
                effect_id,
                expected="executing",
                target=state,
                now_ms=int(time.time() * 1000),
                worker_id=DELEGATION_WORKER_ID,
            )
        except Exception as exc:
            logger.warning(
                "A2A delegation outcome not recorded state={} error_type={}",
                state,
                type(exc).__name__,
            )

    @staticmethod
    def _detached_task(coro: Any) -> asyncio.Task[None]:
        """Start service work with a fresh context, never a closed turn context."""

        return asyncio.create_task(coro, context=contextvars.Context())

    def resume_pending_research(self) -> None:
        """Resume durable polling when the tool is built during gateway startup."""

        if self._research_store is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # A synchronous construction is supported by tests and tooling. The first
            # async invocation will retry the resume scan.
            return
        for pending in self._research_store.pending():
            self._schedule_research_poll(pending, loop=loop)

    def _schedule_research_poll(
        self,
        pending: PendingResearch,
        *,
        loop: asyncio.AbstractEventLoop | None = None,
    ) -> None:
        if pending.task_id in self._background_task_ids:
            return
        loop = loop or asyncio.get_running_loop()
        task = self._detached_task(
            self._poll_research(
                pending.worker,
                pending.task_id,
                pending.skill,
                pending.context_id,
                pending.reference_task_ids,
                pending.effect_id,
                pending.channel,
                pending.chat_id,
                pending.question,
                pending.created_ms,
                pending.canonical_user_id,
                pending.symbol,
            )
        )
        self._background.add(task)
        self._background_task_ids.add(pending.task_id)

        def _finished(done: asyncio.Task[None]) -> None:
            self._background.discard(done)
            self._background_task_ids.discard(pending.task_id)
            if not done.cancelled():
                try:
                    done.exception()
                except Exception as exc:  # pragma: no cover - defensive callback
                    logger.warning(
                        "A2A research worker ended unexpectedly error_type={}", type(exc).__name__
                    )

        task.add_done_callback(_finished)

    async def execute(self, **kwargs: Any) -> str:
        # A tool can be created before the event loop exists; make restart recovery
        # deterministic at the first real call as well as at normal async startup.
        self.resume_pending_research()
        worker, skill, input = (
            str(kwargs.get("worker") or ""),
            str(kwargs.get("skill") or ""),
            kwargs.get("input"),
        )
        if not worker or not skill or not isinstance(input, dict):
            raise ValueError("worker, skill, and structured input are required")
        if skill not in {"search.web", "research.deep", "trading.analyze"}:
            raise ValueError("a2a_delegate supports only search.web, research.deep and trading.analyze")
        if worker not in self._registry.names:
            raise ValueError(f"unknown A2A worker '{worker}'")
        context = current_tool_context()
        question = self._question_text(input)
        ticker = _ticker(question) or (_ticker(context.request_text) if context is not None else "")
        if (
            worker == "hermes"
            and skill == "research.deep"
            and context is not None
            and re.search(r"(?i)\btrading[\s-]?(?:guru|agents)\b", context.request_text)
        ):
            skill = "trading.analyze"
        if skill == "trading.analyze" and worker != "hermes":
            return f"[{worker} | not-sent | worker-binding]"
        if skill == "trading.analyze":
            input = {**input, "output_format": "markdown"}
        if skill == "trading.analyze" and self._research_store is not None and context is not None:
            cached = self._research_store.cached_card(context.canonical_user_id, ticker)
            if cached:
                return f"[hermes | trading.analyze | CACHED | {ticker}]\n{cached}\n\nBereits vorhandene Analyse; kein neuer A2A-Auftrag."
            if self._research_store.has_recent_report(context.canonical_user_id, ticker):
                return f"[hermes | trading.analyze | not-sent | signal_unavailable] Analyse für {ticker} liegt vor, aber ein eindeutiges Signal fehlt. Kein neuer A2A-Auftrag."
        allowed, note, effect_id = self._claim(worker, skill, input)
        if not allowed:
            return f"[{worker} | not-sent | {note}]"
        quota = None
        if self._quota_governance is not None:
            quota = self._quota_governance.claim(
                self.name, {"skill": skill, "input": input}
            )
            if not quota.allowed:
                self._settle(effect_id, "failed")
                retry = (
                    f" retry_at_ms={quota.retry_at_ms}"
                    if quota.reason == "quota_exhausted"
                    else ""
                )
                return f"[{worker} | not-sent | {quota.reason}{retry}]"
        logger.info(
            "A2A delegation started channel={} chat={} worker={} skill={}",
            safe_log_token(self._channel, max_length=40),
            private_log_identifier(self._chat_id),
            safe_log_token(worker),
            safe_log_token(skill),
        )
        try:
            result = await self._registry.invoke_skill(
                worker, skill, input, context_id=self._context_id()
            )
        except Exception as exc:
            reason = str(exc)
            if self._is_local_rejection(exc):
                # Nothing left this host: the invocation was rejected before transmission, so
                # the effect is proven not-executed. Settle it as a failure instead of an
                # unproven outcome (which the store never re-executes) and hand the caller the
                # reason, so a corrected retry is possible and the model can act on it.
                self._settle(effect_id, "failed")
                if quota is not None:
                    self._quota_governance.release(quota)
                logger.warning(
                    "A2A delegation rejected locally channel={} chat={} worker={} skill={} reason={}",
                    safe_log_token(self._channel, max_length=40),
                    private_log_identifier(self._chat_id),
                    safe_log_token(worker),
                    safe_log_token(skill),
                    safe_log_token(reason, max_length=200),
                )
                return f"[{worker} | not-sent | rejected] {reason}"
            self._settle(effect_id, "unknown")
            logger.warning(
                "A2A delegation failed channel={} chat={} worker={} skill={} error_type={}",
                safe_log_token(self._channel, max_length=40),
                private_log_identifier(self._chat_id),
                safe_log_token(worker),
                safe_log_token(skill),
                type(exc).__name__,
            )
            raise
        self._settle(effect_id, "sent")
        if skill in ASYNC_RESEARCH_SKILLS and result.state in {
            "TASK_STATE_SUBMITTED",
            "TASK_STATE_WORKING",
        }:
            turn = current_tool_context()
            channel = turn.channel if turn is not None else self._channel
            chat_id = turn.chat_id if turn is not None else self._chat_id
            pending = PendingResearch(
                task_id=result.task_id,
                worker=worker,
                skill=result.skill,
                context_id=result.context_id,
                reference_task_ids=tuple(result.reference_task_ids),
                channel=channel,
                chat_id=chat_id,
                effect_id=effect_id,
                question=self._question_text(input),
                created_ms=int(time.time() * 1000),
                canonical_user_id=turn.canonical_user_id if turn is not None else "",
                symbol=ticker if skill == "trading.analyze" else "",
            )
            if self._research_store is not None:
                self._research_store.put(pending)
            self._schedule_research_poll(pending)
        logger.info(
            "A2A delegation completed channel={} chat={} worker={} skill={} task_id={} context_id={} state={}",
            safe_log_token(self._channel, max_length=40),
            private_log_identifier(self._chat_id),
            safe_log_token(result.worker),
            safe_log_token(result.skill),
            safe_log_token(result.task_id),
            safe_log_token(result.context_id),
            safe_log_token(result.state, max_length=80),
        )
        output = (
            json.dumps(result.output, ensure_ascii=False, sort_keys=True)
            if result.output is not None
            else (
                f"error={result.error_code} retryable={result.retryable}"
                if result.error_code
                else ""
            )
        )
        return f"[{result.worker} | {result.skill} | {result.state} | {result.task_id}]\n{output}".rstrip()

    async def _poll_research(
        self,
        worker: str,
        task_id: str,
        skill: str,
        context_id: str,
        reference_task_ids: tuple[str, ...],
        effect_id: str,
        channel: str,
        chat_id: str,
        question: str = "",
        created_ms: int = 0,
        canonical_user_id: str = "",
        symbol: str = "",
    ) -> None:
        final_content: str
        extensions = 0
        while True:
            try:
                result = await self._registry.poll_task(
                    worker,
                    task_id,
                    skill=skill,
                    context_id=context_id,
                    reference_task_ids=reference_task_ids,
                )
            except A2APollTimeoutError:
                # A single poll already covers the client's maximum window. Extend it a
                # bounded number of times before reporting a timeout, so long research
                # is not failed while it is still progressing.
                if extensions < self._research_poll_extensions:
                    extensions += 1
                    logger.info(
                        "A2A research poll extended worker={} skill={} extension={}",
                        safe_log_token(worker),
                        safe_log_token(skill),
                        extensions,
                    )
                    continue
                final_content = POLL_TIMEOUT_CONTENT
            except A2ATransportError:
                final_content = "error=TRANSPORT_FAILURE retryable=True"
            except A2AProtocolError:
                final_content = "error=PROTOCOL_FAILURE retryable=False"
            except Exception as exc:
                logger.warning(
                    "A2A research polling failed worker={} error_type={}",
                    safe_log_token(worker),
                    type(exc).__name__,
                )
                final_content = "error=POLL_FAILURE retryable=False"
            else:
                report_saved = False
                card = render_research_output(result.output, offer_full=False)
                if (
                    self._research_store is not None
                    and channel
                    and chat_id
                    and isinstance(result.output, dict)
                    and isinstance(result.output.get("report"), str)
                    and result.output["report"].strip()
                    and result.state == "TASK_STATE_COMPLETED"
                ):
                    try:
                        self._research_store.save_report(
                            effect_id,
                            channel=channel,
                            chat_id=chat_id,
                            content=render_research_output(result.output, mode="full"),
                            canonical_user_id=canonical_user_id if skill == "trading.analyze" else "",
                            symbol=symbol if skill == "trading.analyze" else "",
                            card=card,
                            signal=_trading_signal(result.output["report"]) if skill == "trading.analyze" else "",
                        )
                    except Exception as exc:
                        logger.warning("A2A full report was not stored error_type={}", type(exc).__name__)
                    else:
                        report_saved = True
                final_content = render_research_output(result.output, offer_full=report_saved) or (
                    f"error={result.error_code} retryable={result.retryable}"
                )
            break
        if self._delivery is not None and channel and chat_id:
            try:
                receipt = await self._delivery.send(
                    source="a2a",
                    operation_ref=f"a2a-result:{effect_id}",
                    channel=channel,
                    chat_id=chat_id,
                    content=self._follow_up_content(final_content, question, created_ms),
                )
            except Exception as exc:
                logger.warning(
                    "A2A research result delivery failed worker={} error_type={}",
                    safe_log_token(worker),
                    type(exc).__name__,
                )
            else:
                state = str(getattr(receipt, "state", "") or "")
                if receipt is not None and state not in {"sent", "delivered"}:
                    logger.warning(
                        "A2A research result remains pending worker={} state={}",
                        safe_log_token(worker),
                        safe_log_token(state or "unknown"),
                    )
                    return
                if self._research_store is not None:
                    self._research_store.delete(task_id, effect_id=effect_id)
