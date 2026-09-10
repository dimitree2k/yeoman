"""Phase timings and drop/deferral counters for the processing pipeline (Plan 06, R08/R10).

Two constraints shape this module:

* Labels are **phase names only**. Message text, chat ids, principals and message ids are
  never recorded, so a timing sample cannot leak content into a log or a metric label.
* Sampling is **bounded**: a fixed-size ring per phase, so a busy gateway cannot grow
  memory through observability.
"""

from __future__ import annotations

import math
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Callable, Iterator, Mapping, Sequence

#: The phases the pipeline reports. Keys are fixed so typos cannot invent new series.
PHASES: tuple[str, ...] = (
    "ingest",
    "journal_commit",
    "queue_in",
    "queue_out",
    "enrichment",
    "security",
    "model",
    "tools",
    "effect_queue",
    "transport_confirm",
)

DEFAULT_CAPACITY = 512


def percentile(values: Sequence[float], quantile: float) -> float:
    """Nearest-rank percentile of an unsorted sample. Empty input yields ``0.0``."""
    if not values:
        return 0.0
    ordered = sorted(float(value) for value in values)
    if quantile <= 0:
        return ordered[0]
    if quantile >= 1:
        return ordered[-1]
    rank = max(1, min(len(ordered), math.ceil(quantile * len(ordered))))
    return ordered[rank - 1]


def median(values: Sequence[float]) -> float:
    return percentile(values, 0.5)


@dataclass(frozen=True, slots=True)
class TimingsReport:
    """Per-phase latency summary plus the pipeline's drop/deferral counters."""

    medians: Mapping[str, float] = field(default_factory=dict)
    p95: Mapping[str, float] = field(default_factory=dict)
    samples: Mapping[str, int] = field(default_factory=dict)
    dropped: Mapping[str, int] = field(default_factory=dict)
    deferred: Mapping[str, int] = field(default_factory=dict)

    @property
    def phases(self) -> tuple[str, ...]:
        return tuple(self.medians)

    def as_lines(self) -> list[str]:
        """Stable, label-safe text form for a status command or a log line."""
        lines = [
            f"{phase}: median={self.medians[phase]:.1f}ms p95={self.p95[phase]:.1f}ms"
            f" n={self.samples[phase]}"
            for phase in sorted(self.medians)
        ]
        for reason, count in sorted(self.dropped.items()):
            lines.append(f"dropped[{reason}]={count}")
        for reason, count in sorted(self.deferred.items()):
            lines.append(f"deferred[{reason}]={count}")
        return lines


class PhaseTimings:
    """Bounded duration samples per phase, plus counters for drops and deferrals."""

    def __init__(
        self,
        *,
        capacity: int = DEFAULT_CAPACITY,
        clock: Callable[[], float] | None = None,
    ) -> None:
        if capacity < 1:
            raise ValueError("capacity must be at least one sample")
        self._capacity = int(capacity)
        self._clock = clock or time.monotonic
        self._samples: dict[str, list[float]] = {phase: [] for phase in PHASES}
        self._dropped: dict[str, int] = {}
        self._deferred: dict[str, int] = {}
        self.rejected_labels = 0

    # -- sampling ---------------------------------------------------------------

    def observe(self, phase: str, duration_ms: float) -> None:
        """Record one duration. An unknown phase name is refused, not invented."""
        if phase not in self._samples:
            self.rejected_labels += 1
            return
        bucket = self._samples[phase]
        bucket.append(max(0.0, float(duration_ms)))
        if len(bucket) > self._capacity:
            del bucket[: len(bucket) - self._capacity]

    @contextmanager
    def phase(self, name: str) -> Iterator[None]:
        """Time a block, e.g. ``with timings.phase("journal_commit"): ...``."""
        started = self._clock()
        try:
            yield
        finally:
            self.observe(name, (self._clock() - started) * 1000.0)

    # -- counters ---------------------------------------------------------------

    def note_drop(self, reason: str) -> None:
        self._dropped[str(reason)] = self._dropped.get(str(reason), 0) + 1

    def note_deferral(self, reason: str) -> None:
        self._deferred[str(reason)] = self._deferred.get(str(reason), 0) + 1

    def counters(self) -> tuple[dict[str, int], dict[str, int]]:
        return dict(self._dropped), dict(self._deferred)

    # -- reporting --------------------------------------------------------------

    def sample_count(self, phase: str) -> int:
        return len(self._samples.get(phase, ()))

    def report(self) -> TimingsReport:
        return TimingsReport(
            medians={phase: median(values) for phase, values in self._samples.items() if values},
            p95={phase: percentile(values, 0.95) for phase, values in self._samples.items() if values},
            samples={phase: len(values) for phase, values in self._samples.items() if values},
            dropped=dict(self._dropped),
            deferred=dict(self._deferred),
        )

    def reset(self) -> None:
        for bucket in self._samples.values():
            bucket.clear()
        self._dropped.clear()
        self._deferred.clear()


def compare_reports(
    legacy: TimingsReport, managed: TimingsReport, *, phases: Sequence[str] = PHASES
) -> list[str]:
    """Side-by-side latency lines for a legacy-versus-managed load run.

    Only reported for phases that both runs actually sampled, so an unmeasured phase is
    visibly absent rather than silently zero.
    """
    lines: list[str] = []
    for phase in phases:
        if phase not in legacy.medians or phase not in managed.medians:
            continue
        lines.append(
            f"{phase}: legacy median={legacy.medians[phase]:.1f}ms p95={legacy.p95[phase]:.1f}ms"
            f" | managed median={managed.medians[phase]:.1f}ms p95={managed.p95[phase]:.1f}ms"
        )
    for reason in sorted(set(legacy.deferred) | set(managed.deferred)):
        lines.append(
            f"deferred[{reason}]: legacy={legacy.deferred.get(reason, 0)}"
            f" managed={managed.deferred.get(reason, 0)}"
        )
    for reason in sorted(set(legacy.dropped) | set(managed.dropped)):
        lines.append(
            f"dropped[{reason}]: legacy={legacy.dropped.get(reason, 0)}"
            f" managed={managed.dropped.get(reason, 0)}"
        )
    return lines
