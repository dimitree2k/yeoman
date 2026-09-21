"""The bounded statement-capture worker: one thread, durable jobs, no new scheduler.

The worker mirrors the proven shared-fact queue shape (``run_due``, ``recover_stale``,
bounded waiting, counted refusal reasons) because that shape already fits the existing
``knowledge_jobs`` state machine.  What it adds is the owner requirement: promotion is a
*separate later decision* from observation.

Three properties matter more than throughput:

* **No provider call inside a transaction.**  Source verification and publication each run
  in their own short unit of work; the model call happens in between, with nothing open.
* **Revocation wins.**  Sources are re-verified after the model call and immediately
  before publication, so a delete or edit that lands during extraction stops the
  statement.  A cancelled job stays cancelled.
* **One idempotent job yields zero or more statements.**  Refusals are counted per reason;
  a job that publishes nothing is still a finished, observable outcome.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final

from loguru import logger

from yeoman_gateway.knowledge._capture import (
    CaptureReport,
    ObservedEvent,
    StatementCaptureProducer,
    observed_event,
    promoter_reason,
)
from yeoman_gateway.knowledge._memory.extraction_jobs import (
    REJECT_BASES,
    is_hedged,
    screen_capture_input,
    screen_statement_content,
)
from yeoman_gateway.knowledge.models import (
    ATTRIBUTE_KEYS,
    PERSON_ROLES,
    TIME_BASES,
    TIME_PRECISIONS,
    AttributeCandidate,
    AttributeValue,
    CaptureJobRecord,
    KnowledgeError,
    PersonLinkCandidate,
    SourceRef,
    StatementCandidate,
    TrustedCaptureContext,
    ValidationError,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from yeoman_shared.config.schema import Config, ModelProfile

#: Version stamped on jobs and statements.  A new version is a new job key by design.
STATEMENT_EXTRACTOR_VERSION = "statement-capture-v1"
#: Hard cap per job: a chatty batch must not multiply rows.
MAX_CANDIDATES_PER_JOB = 4
#: Bounded retry of a provider failure before the job is left visibly failed.
DEFAULT_MAX_ATTEMPTS = 3

STATEMENT_SYSTEM_PROMPT = """You extract statements from chat messages.

Return JSON only:
{"statements": [{"content": str, "source": int, "basis": str, "certainty": str, "valid_until": str|null}]}

Rules:
- "source" is the number of the message the statement rests on, exactly as given.
- "content": one short sentence in the language of the message. State what was said about
  the world, people, plans, preferences, dates or agreements.
- Never state who said it and never mention the assistant, the bot, this chat or a
  transcript. Phrases like "the author says" or "the message says" are forbidden.
- Never invent facts about someone who is not the author, and never merge two messages
  into one statement; use one entry per message.
- Never write about a delivery, a read receipt or a reaction.
- Never describe the conversation itself, not even impersonally: no "es wird gefragt",
  "es wurde gesagt", "wurde erwähnt", "die Frage wurde gestellt". State what was said
  about the world, or return nothing for that message.
- "basis": exactly one of "explicit_statement", "reported_statement", "opinion",
  "speculation", "inference", "delivery_claim". Use "explicit_statement" only when the
  author states the fact plainly; use "reported_statement" when the author reports what
  somebody else said.
- "certainty": "asserted" when the author states it plainly, "uncertain" when the message
  hedges, guesses or leaves it open. Keep the hedge wording in "content" - never turn a
  possibility into a fact.
- "valid_until": ISO date when the statement is explicitly time-bound, else null.
- Return an empty list when nothing qualifies. Never pad the list."""


@dataclass(frozen=True, slots=True)
class StatementDraft:
    """One untrusted statement proposal; provenance is attached by the runtime."""

    content: str
    source_index: int = 0
    basis: str = "explicit_statement"
    certainty: str = "asserted"
    valid_until_ms: int | None = None
    #: Local candidate tokens (``p0``, ``p1``, ...) the extractor used, never person ids.
    #: The runtime maps them to people it offered; an unknown token is discarded.
    people: tuple[tuple[str, str], ...] = ()
    attributes: tuple[tuple[str, str, str, str], ...] = ()
    time_basis: str = "unknown"
    time_precision: str = "unknown"
    unresolved_mentions: tuple[str, ...] = ()

    @property
    def uncertain(self) -> bool:
        return str(self.certainty).strip().lower() != "asserted"


class StatementExtractor:
    """Model-backed statement proposals over proven source text.

    This is deliberately not the shared-fact extractor: that one maps to
    ``SharedFactCandidate``, whose audience model and fact-shaped gate are a different
    contract.  What is shared, and reused here, is the provider plumbing and the existing
    ``models.routes["memory.capture.extract"]`` route - no second provider, no second
    architecture, and the model only ever proposes.
    """

    def __init__(
        self,
        *,
        config: "Config",
        route_key: str = "memory.capture.extract",
        max_candidates: int = MAX_CANDIDATES_PER_JOB,
        provider: Any | None = None,
    ) -> None:
        from yeoman_gateway.providers.litellm_provider import LiteLLMProvider

        self._config = config
        self._route_key = str(route_key)
        self._max_candidates = max(1, int(max_candidates))
        self._profile_name, profile = self._resolve_profile()
        self._model = str(profile.model or "").strip()
        self._max_tokens = int(profile.max_tokens or 700)
        self._temperature = float(profile.temperature if profile.temperature is not None else 0.0)
        if provider is not None:
            self._provider = provider
        else:
            provider_cfg = self._config.get_provider(self._model, provider_name=profile.provider)
            if provider_cfg is None:
                raise ValueError(
                    f"no provider with credentials for statement route '{self._route_key}'"
                )
            self._provider = LiteLLMProvider(
                api_key=provider_cfg.api_key if provider_cfg.api_key else None,
                api_base=provider_cfg.api_base,
                default_model=self._model,
                extra_headers=provider_cfg.extra_headers,
            )

    def _resolve_profile(self) -> tuple[str, "ModelProfile"]:
        route_name = self._config.models.routes.get(self._route_key)
        if not route_name:
            raise ValueError(f"models.routes missing '{self._route_key}'")
        profile = self._config.models.profiles.get(route_name)
        if profile is None:
            raise ValueError(
                f"models.routes['{self._route_key}'] points to missing profile '{route_name}'"
            )
        if profile.kind != "chat":
            raise ValueError(f"route '{self._route_key}' must target kind='chat'")
        if not profile.model:
            raise ValueError(f"profile '{route_name}' does not define a model")
        return route_name, profile

    def __call__(self, items: Sequence[ObservedEvent]) -> list[StatementDraft]:
        if not items:
            return []
        usable = [
            (original_index, item)
            for original_index, item in enumerate(items)
            if screen_capture_input(
                item.text, {"source_status": "revoked" if item.revoked else "active"}
            ).accepted
        ]
        if not usable:
            return []
        # The model sees the *original* message index, so a returned `source` maps back to
        # the exact proven revision it was shown - never to a shifted neighbour.
        listing = "\n".join(f"[{index}] {item.text}" for index, item in usable)
        rows = self._ask_model(listing)
        drafts: list[StatementDraft] = []
        for row in rows[: self._max_candidates]:
            draft = self._to_draft(row)
            if draft is not None:
                drafts.append(draft)
        return drafts

    def _ask_model(self, listing: str) -> list[Mapping[str, Any]]:
        from yeoman_gateway.knowledge._memory.fact_extractor import _extract_json

        messages = [
            {"role": "system", "content": STATEMENT_SYSTEM_PROMPT},
            {"role": "user", "content": f"Messages:\n{listing}"},
        ]
        response = asyncio.run(
            self._provider.chat(
                messages=messages,
                tools=None,
                model=self._model,
                max_tokens=self._max_tokens,
                temperature=self._temperature,
            )
        )
        content = str(getattr(response, "content", "") or "").strip()
        if not content:
            return []
        payload = _extract_json(content)
        if payload is None:
            logger.warning("statement extractor returned unparseable content")
            return []
        rows = payload.get("statements") if isinstance(payload, Mapping) else payload
        if not isinstance(rows, list):
            return []
        return [row for row in rows if isinstance(row, Mapping)]

    def _to_draft(self, row: Mapping[str, Any]) -> StatementDraft | None:
        content = str(row.get("content") or "").strip()
        if not content:
            return None
        try:
            source_index = max(0, int(row.get("source") or 0))
        except (TypeError, ValueError):
            source_index = 0
        basis = str(row.get("basis") or "").strip().lower() or "uncertain"
        certainty = str(row.get("certainty") or "").strip().lower() or "asserted"
        return StatementDraft(
            content=content,
            source_index=source_index,
            basis=basis,
            certainty=certainty,
            valid_until_ms=_parse_iso_ms(row.get("valid_until")),
        )


@dataclass(slots=True)
class WorkerReport(CaptureReport):
    """Promotion plus extraction counters of one worker pass."""

    jobs_run: int = 0
    published: int = 0
    refused: dict[str, int] = field(default_factory=dict)
    failed: int = 0

    def note_refusal(self, reason: str) -> None:
        if reason:
            self.refused[reason] = self.refused.get(reason, 0) + 1


class StatementCaptureWorker:
    """Drains due promotion jobs in one bounded background thread."""

    def __init__(
        self,
        *,
        knowledge: Any,
        processing: Any,
        extractor: Callable[[Sequence[ObservedEvent]], Iterable[StatementDraft]] | None = None,
        producer: StatementCaptureProducer | None = None,
        max_jobs: int = 20,
        poll_seconds: float = 5.0,
        stale_ms: int = 600_000,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        retry_delay_ms: int = 60_000,
        clock: Callable[[], int] | None = None,
    ) -> None:
        self._knowledge = knowledge
        self._processing = processing
        self._extractor = extractor
        self._producer = producer
        self._max_jobs = max(1, int(max_jobs))
        self._poll_seconds = float(poll_seconds)
        self._stale_ms = max(60_000, int(stale_ms))
        self._max_attempts = max(1, int(max_attempts))
        self._retry_delay_ms = max(1_000, int(retry_delay_ms))
        self._clock = clock or (lambda: int(time.time() * 1000))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.overflows = 0

    # -- lifecycle -------------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="statement-capture", daemon=True)
        self._thread.start()

    def stop(self, *, timeout: float = 5.0) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)
        self._thread = None

    def _loop(self) -> None:  # pragma: no cover - thread body
        while not self._stop.is_set():
            try:
                self.run_due(now_ms=self._clock())
            except Exception as exc:
                logger.warning("statement capture pass failed error_type={}", type(exc).__name__)
            self._stop.wait(self._poll_seconds)

    # -- one pass --------------------------------------------------------------

    def run_due(self, *, now_ms: int | None = None) -> WorkerReport:
        moment = int(now_ms if now_ms is not None else self._clock())
        report = WorkerReport()
        self.recover_stale(now_ms=moment)
        if self._producer is not None:
            promotion = self._producer.run_due(now_ms=moment)
            report.examined = promotion.examined
            report.promoted_sources = promotion.promoted_sources
            report.jobs = promotion.jobs
            report.refusals.update(promotion.refusals)
            self.overflows = self._producer.overflows
        if self._extractor is None:
            return report
        for receipt in self._knowledge.due_capture_jobs(now_ms=moment, limit=self._max_jobs):
            record = self._knowledge.capture_job_record(receipt.job_id)
            if str(record.state) != "queued":
                # Cancelled while queued: cancellation outranks this pass.
                continue
            self._run_job(record, report=report, now_ms=moment)
        return report

    def recover_stale(self, *, now_ms: int, stale_ms: int | None = None) -> int:
        """Re-queue jobs a crash left ``running``; a killed worker resumes its queue."""
        threshold = int(stale_ms if stale_ms is not None else self._stale_ms)
        recovered = 0
        for receipt in self._knowledge.stale_capture_jobs(
            updated_before_ms=int(now_ms) - threshold, limit=50
        ):
            self._knowledge.requeue_capture_job(
                receipt.job_id,
                due_ms=int(now_ms),
                reason="recovered_after_crash",
                ts_ms=int(now_ms),
            )
            recovered += 1
        return recovered

    # -- job execution ---------------------------------------------------------

    def _run_job(self, record: CaptureJobRecord, *, report: WorkerReport, now_ms: int) -> None:
        report.jobs_run += 1
        items = self._load_sources(record, report=report, ts_ms=now_ms)
        if items is None:
            return
        # Revocation outranks every other refusal: a revoked source is cancelled, not
        # skipped, and its derived statements stop being readable.
        revoked = [item for item in items if item.revoked]
        if revoked:
            self._cancel_for_revocation(record, revoked, now_ms=now_ms, report=report)
            return
        for item in items:
            reason = promoter_reason(item)
            if reason:
                self._finish(record, state="skipped", reason=reason, report=report, ts_ms=now_ms)
                return

        self._knowledge.mark_capture_job(
            record.job_id, "running", reason="extracting", ts_ms=now_ms
        )
        try:
            drafts = list(self._extractor(items))  # provider call: no transaction open
        except Exception as exc:
            self._provider_failure(record, exc, now_ms=now_ms, report=report)
            return

        # Revocation wins every race: re-verify after the model call and before publishing.
        current = self._knowledge.capture_job_record(record.job_id)
        if str(current.state) == "cancelled":
            report.note_refusal("cancelled_during_extraction")
            return
        refreshed = self._load_sources(record, report=report, ts_ms=now_ms, quiet=True)
        if refreshed is None:
            # The source disappeared while the model was working (retention or a lost
            # revision).  Fail closed and finish the job instead of leaving it running.
            self._finish(
                record, state="skipped", reason="unknown_source", report=report, ts_ms=now_ms
            )
            return
        revoked = [item for item in refreshed if item.revoked]
        if revoked:
            self._cancel_for_revocation(record, revoked, now_ms=now_ms, report=report)
            return

        published = 0
        refused: dict[str, int] = {}
        for draft in drafts:
            # Screen twice: once on the raw proposal, once on the candidate the runtime
            # built from it.  The second screen is cheap insurance against a mapping bug.
            reason = screen_draft(draft)
            if reason:
                refused[reason] = refused.get(reason, 0) + 1
                continue
            candidate = self._candidate(draft, refreshed, record)
            if candidate is None:
                refused["invalid_candidate"] = refused.get("invalid_candidate", 0) + 1
                continue
            reason = _screen(candidate)
            if reason:
                refused[reason] = refused.get(reason, 0) + 1
                continue
            try:
                result = self._knowledge.capture(
                    candidate, context=self._context(record, refreshed)
                )
            except KnowledgeError as exc:
                refused[str(exc.code)] = refused.get(str(exc.code), 0) + 1
            except ValidationError as exc:  # pragma: no cover - defensive
                refused[str(exc.code)] = refused.get(str(exc.code), 0) + 1
            else:
                published += len(result.statement_ids) if result.ok else 0
        for reason, count in refused.items():
            for _ in range(count):
                report.note_refusal(reason)
        report.published += published
        if published:
            reason = f"published={published}"
        elif refused:
            reason = f"refused={max(sorted(refused), key=lambda key: refused[key])}"
        else:
            reason = "no_candidates"
        self._finish(record, state="done", reason=reason, report=report, ts_ms=now_ms)

    def _load_sources(
        self,
        record: CaptureJobRecord,
        *,
        report: WorkerReport,
        ts_ms: int,
        quiet: bool = False,
    ) -> list[ObservedEvent] | None:
        if record.unresolved:
            self._finish(
                record, state="skipped", reason="unknown_source", report=report, ts_ms=ts_ms
            )
            return None
        if not record.sources:
            self._finish(record, state="skipped", reason="no_sources", report=report, ts_ms=ts_ms)
            return None
        items: list[ObservedEvent] = []
        for source in record.sources:
            event = self._event(source)
            if event is None:
                if not quiet:
                    self._finish(
                        record,
                        state="skipped",
                        reason="unknown_source",
                        report=report,
                        ts_ms=ts_ms,
                    )
                return None
            items.append(observed_event(event, self._authority(source)))
        return items

    def _event(self, source: SourceRef) -> Any:
        getter = getattr(self._processing, "get_event", None)
        if not callable(getter):
            return None
        try:
            return getter(source.event_id)
        except Exception:  # pragma: no cover - defensive
            return None

    def _authority(self, source: SourceRef) -> Mapping[str, Any] | None:
        getter = getattr(self._processing, "get_event_source_authority", None)
        if not callable(getter):
            return None
        try:
            return getter(source.event_id, int(source.revision))
        except Exception:  # pragma: no cover - defensive
            return None

    def _context(
        self, record: CaptureJobRecord, items: Sequence[ObservedEvent]
    ) -> TrustedCaptureContext:
        revision = getattr(self._knowledge, "policy_revision", 1)
        return TrustedCaptureContext(
            request_id=f"job:{record.job_id}",
            policy_revision=int(revision if isinstance(revision, int) else 1),
            capture_basis="observed_source_batch",
            authorized_sources=tuple(item.source for item in items),
        )

    def _candidate(
        self,
        draft: StatementDraft,
        items: Sequence[ObservedEvent],
        record: CaptureJobRecord,
    ) -> StatementCandidate | None:
        if not items:
            return None
        index = int(draft.source_index)
        if index < 0 or index >= len(items):
            return None
        source = items[index]
        if not source.principal:
            return None
        uncertain = draft.uncertain or is_hedged(draft.content)
        # Only people the runtime offered for *this* source may be referenced, and the
        # transport speaker is set here - never by the model.
        eligible = self._eligible_people(items, source)
        links = self._links(draft, source, eligible)
        attributes = self._attributes(draft, eligible)
        try:
            return StatementCandidate(
                content=draft.content,
                sources=(source.source,),
                people=links,
                attributes=attributes,
                extractor_version=str(record.extractor_version or STATEMENT_EXTRACTOR_VERSION),
                confidence=0.5 if uncertain else 0.75,
                valid_until_ms=draft.valid_until_ms,
                unresolved_mentions=tuple(draft.unresolved_mentions),
                time_basis=(
                    draft.time_basis if draft.time_basis in TIME_BASES else "unknown"
                ),
                time_precision=(
                    draft.time_precision
                    if draft.time_precision in TIME_PRECISIONS
                    else "unknown"
                ),
                kind="uncertain" if uncertain else "fact",
                sector="semantic",
            )
        except ValidationError:
            return None

    def _eligible_people(
        self, items: Sequence[ObservedEvent], source: ObservedEvent
    ) -> tuple[str, ...]:
        """People this source may reference: its own author, plus proven chat members.

        Never the global contact list: a statement may only name somebody the runtime can
        prove was in this conversation.
        """
        offered: list[str] = []
        mapping = getattr(self._knowledge, "person_for_principal", None)
        if callable(mapping):
            person_id = mapping(source.principal)
            if person_id:
                offered.append(str(person_id))
        return tuple(dict.fromkeys(offered))

    def _links(
        self,
        draft: StatementDraft,
        source: ObservedEvent,
        eligible: tuple[str, ...],
    ) -> tuple[PersonLinkCandidate, ...]:
        """Turn the extractor's local candidate tokens into typed role edges.

        The transport speaker is added by the runtime from the source principal, so a
        name in the text can never replace the authenticated sender.  A token the runtime
        did not offer, an unknown role and a missing attribution are all discarded.
        """
        allowed = set(eligible)
        links: list[PersonLinkCandidate] = []
        mapping = getattr(self._knowledge, "person_for_principal", None)
        speaker = str(mapping(source.principal) or "") if callable(mapping) else ""
        if speaker:
            links.append(
                PersonLinkCandidate(
                    person_id=speaker,
                    role="speaker",
                    source=source.source,
                    attribution="transport",
                )
            )
        for token, role in draft.people:
            if role not in PERSON_ROLES or role == "speaker":
                continue
            person_id = str(token)
            if person_id not in allowed:
                # Either a token the runtime never offered or a free-form identifier from
                # the model: discarded, never resolved by a name lookup.
                continue
            links.append(
                PersonLinkCandidate(
                    person_id=str(person_id),
                    role=str(role),
                    source=source.source,
                    attribution="extracted",
                )
            )
        seen: set[tuple[str, str, str, int]] = set()
        unique: list[PersonLinkCandidate] = []
        for link in links:
            key = (link.person_id, link.role, link.source.event_id, link.source.revision)
            if key in seen:
                continue
            seen.add(key)
            unique.append(link)
        return tuple(unique)

    def _attributes(
        self, draft: StatementDraft, eligible: tuple[str, ...]
    ) -> tuple[AttributeCandidate, ...]:
        """Build validated facets for people the extractor was allowed to talk about."""
        if not draft.attributes:
            return ()
        allowed = set(eligible)
        out: list[AttributeCandidate] = []
        for person_token, key, value, polarity in draft.attributes:
            person_id = str(person_token)
            if person_id not in allowed:
                continue
            if key not in ATTRIBUTE_KEYS:
                continue
            try:
                out.append(
                    AttributeCandidate(
                        person_id=person_id,
                        attribute_key=str(key),
                        value=AttributeValue(text=str(value), precision="unknown"),
                        polarity=str(polarity) if polarity else "positive",
                    )
                )
            except ValidationError:
                continue
        return tuple(out)

    def _finish(
        self,
        record: CaptureJobRecord,
        *,
        state: str,
        reason: str,
        report: WorkerReport,
        ts_ms: int,
    ) -> None:
        current = self._knowledge.capture_job_record(record.job_id)
        if str(current.state) == "cancelled" and state != "cancelled":
            # Cancellation is terminal: a late pass may not overwrite it with ``done``.
            report.note_refusal("cancelled_during_extraction")
            return
        self._knowledge.mark_capture_job(record.job_id, state, reason=reason, ts_ms=int(ts_ms))
        if state == "failed":
            report.failed += 1

    def _provider_failure(
        self, record: CaptureJobRecord, exc: Exception, *, now_ms: int, report: WorkerReport
    ) -> None:
        logger.warning("statement extraction failed error_type={}", type(exc).__name__)
        if int(record.attempts) + 1 < self._max_attempts:
            self._knowledge.requeue_capture_job(
                record.job_id,
                due_ms=int(now_ms) + self._retry_delay_ms,
                reason="provider_error",
            )
            report.note_refusal("provider_error")
            return
        self._finish(record, state="failed", reason="provider_error", report=report, ts_ms=now_ms)

    def _cancel_for_revocation(
        self,
        record: CaptureJobRecord,
        revoked: Sequence[ObservedEvent],
        *,
        now_ms: int,
        report: WorkerReport,
    ) -> None:
        """A revoked source never publishes, and its statements stop being readable."""
        for item in revoked:
            self._invalidate(item)
        report.note_refusal("source_revoked")
        self._knowledge.mark_capture_job(record.job_id, "cancelled", reason="source_revoked")

    def _invalidate(self, item: ObservedEvent) -> None:
        invalidate = getattr(self._knowledge, "invalidate_source", None)
        if not callable(invalidate):
            return
        revision = getattr(self._knowledge, "policy_revision", 1)
        try:
            invalidate(
                item.source,
                context=TrustedCaptureContext(
                    request_id=f"revoked:{item.event_id}",
                    policy_revision=int(revision if isinstance(revision, int) else 1),
                    capture_basis="source_revocation",
                    authorized_sources=(item.source,),
                ),
            )
        except Exception as exc:  # pragma: no cover - defensive; revocation is idempotent
            logger.warning("statement revocation failed error_type={}", type(exc).__name__)


#: Upper bound of one section handed to the extractor.  A section is a *reviewable* unit:
#: the cursor only advances to the end that was actually read, so a bounded prompt can
#: never mark unread original text as processed.
MAX_SOURCE_SECTION_CHARS: Final[int] = 4000


def split_source_sections(text: str, *, max_chars: int = MAX_SOURCE_SECTION_CHARS) -> list[str]:
    """Split a long source at sentence and line boundaries, losing nothing.

    Deliberately not a character crop: ``text[:8000]`` silently discards the rest while
    the caller records the whole message as processed.  Here every character ends up in
    exactly one section, a single oversized sentence is kept whole rather than cut, and
    the caller can advance its cursor by the concatenated length.
    """
    if max_chars <= 0:
        raise ValueError("max_chars must be positive")
    if not text:
        return []
    sections: list[str] = []
    remaining = text
    while len(remaining) > max_chars:
        window = remaining[:max_chars]
        cut = _section_boundary(window)
        sections.append(remaining[:cut])
        remaining = remaining[cut:]
    if remaining:
        sections.append(remaining)
    return sections


def _section_boundary(window: str) -> int:
    """The last natural boundary inside ``window``, or its full length."""
    for separator in ("\n\n", "\n", ". ", "! ", "? ", "; ", ", "):
        index = window.rfind(separator)
        if index > 0:
            return index + len(separator)
    return len(window)


def _screen(candidate: StatementCandidate) -> str:
    """Deterministic second screen, after the model proposed the statement.

    A hedge is not refused here: the statement keeps its wording and is stored as an
    assertion (never as confirmed), which is what "uncertainty stays visible" means.
    An unsupported basis is refused, because a guess may not become durable knowledge.
    """
    verdict = screen_statement_content(candidate.content)
    if verdict.rejected:
        return verdict.reason
    return ""


def screen_draft(draft: StatementDraft) -> str:
    """Cheap rejection of a proposal before it is turned into a candidate."""
    basis = str(draft.basis or "").strip().lower()
    if basis in REJECT_BASES:
        return basis
    if basis not in ("explicit_statement", "reported_statement"):
        return "unsupported_basis"
    verdict = screen_statement_content(draft.content)
    if verdict.rejected:
        return verdict.reason
    return ""


def _parse_iso_ms(raw: object) -> int | None:
    from datetime import UTC, datetime

    if not raw or not isinstance(raw, str):
        return None
    try:
        parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return int(parsed.timestamp() * 1000)


__all__ = [
    "MAX_CANDIDATES_PER_JOB",
    "STATEMENT_EXTRACTOR_VERSION",
    "STATEMENT_SYSTEM_PROMPT",
    "StatementCaptureWorker",
    "StatementDraft",
    "StatementExtractor",
    "WorkerReport",
    "MAX_SOURCE_SECTION_CHARS",
    "screen_draft",
    "split_source_sections",
]
