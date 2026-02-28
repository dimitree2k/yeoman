"""Middleware pipeline for inbound event processing.

Replaces the monolithic ``Orchestrator.handle()`` method with a composable
chain of independently testable middleware classes.  Uses the **pipeline
chain** pattern (same as Express.js, Django, FastAPI): each middleware calls
``next()`` to pass through, or sets ``ctx.halted = True`` to short-circuit.

Usage::

    pipeline = Pipeline([
        NormalizationMiddleware(),
        DeduplicationMiddleware(ttl_seconds=1200),
        PolicyMiddleware(policy=policy_port),
        ResponderMiddleware(responder=responder_port),
    ])
    intents = await pipeline.run(event)
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from yeoman.core.intents import OrchestratorIntent, RecordMetricIntent
from yeoman.core.models import InboundEvent, PolicyDecision

# Forward-compatible: will switch to Message once channels migrate.
type PipelineEvent = InboundEvent


@dataclass
class PipelineContext:
    """Mutable state flowing through the middleware chain.

    Attributes:
        event: The inbound event being processed.  Middleware may replace it
            with an enriched copy (e.g. reply context injection).
        decision: Set by the policy middleware; consumed by downstream stages.
        intents: Accumulated output intents.  Each middleware appends to this.
        reply: The LLM-generated reply text, set by the responder middleware
            and potentially modified by output security.
        halted: When ``True``, the pipeline stops executing further middleware.
    """

    event: PipelineEvent
    decision: PolicyDecision | None = None
    intents: list[OrchestratorIntent] = field(default_factory=list)
    reply: str | None = None
    halted: bool = False

    # ── Convenience helpers ──────────────────────────────────────────

    def metric(
        self,
        name: str,
        value: int = 1,
        labels: tuple[tuple[str, str], ...] = (),
    ) -> None:
        """Append a metric intent (shorthand used by most middleware)."""
        self.intents.append(RecordMetricIntent(name=name, value=value, labels=labels))

    def halt(self) -> None:
        """Signal the pipeline to stop after this middleware."""
        self.halted = True


NextFn = Callable[[PipelineContext], Awaitable[None]]
"""Signature for the ``next`` callback passed to each middleware."""


@runtime_checkable
class Middleware(Protocol):
    """Protocol for pipeline middleware.

    Implementations must be callable with ``(ctx, next)`` and may:

    1. Modify ``ctx`` and call ``await next(ctx)`` — **pass through**.
    2. Call ``ctx.halt()`` and append intents — **short-circuit**.
    3. Call ``await next(ctx)`` then inspect/modify the result — **post-process**.
    """

    async def __call__(self, ctx: PipelineContext, next: NextFn) -> None: ...


class Pipeline:
    """Ordered chain of middleware that processes an inbound event.

    The runner is intentionally tiny (~20 LOC).  All logic lives in the
    individual middleware classes.
    """

    __slots__ = ("_layers",)

    def __init__(self, layers: list[Middleware]) -> None:
        self._layers = list(layers)

    async def run(self, event: PipelineEvent) -> list[OrchestratorIntent]:
        """Process *event* through the full middleware chain and return intents."""
        ctx = PipelineContext(event=event)
        await self._execute(ctx, index=0)
        return ctx.intents

    async def _execute(self, ctx: PipelineContext, index: int) -> None:
        if ctx.halted or index >= len(self._layers):
            return
        layer = self._layers[index]
        await layer(ctx, lambda c: self._execute(c, index + 1))

    def __len__(self) -> int:
        return len(self._layers)

    def __repr__(self) -> str:
        names = [type(m).__name__ for m in self._layers]
        return f"Pipeline({' → '.join(names)})"
