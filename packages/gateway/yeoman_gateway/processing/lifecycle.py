"""Pure shadow verdicts; these never change durable thread state."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

from yeoman_gateway.processing.threads import ThreadPolicy


class LifecycleThread(Protocol):
    @property
    def thread_id(self) -> str: ...

    @property
    def last_activity_ms(self) -> int: ...

    @property
    def kind(self) -> str: ...


@dataclass(frozen=True, slots=True)
class LifecycleVerdict:
    state: str
    reason: str


def lifecycle_verdicts(
    threads: Sequence[LifecycleThread],
    other_messages: Mapping[str, int],
    *,
    now_ms: int,
    is_group: bool,
    policy: ThreadPolicy,
) -> dict[str, LifecycleVerdict]:
    threshold = policy.dormant_after_messages if is_group else policy.dormant_after_messages_dm
    verdicts = {}
    active = []
    for thread in sorted(threads, key=lambda t: (t.last_activity_ms, t.thread_id)):
        dormant = (
            now_ms - thread.last_activity_ms >= policy.dormant_after_seconds * 1000
            and other_messages.get(thread.thread_id, 0) >= threshold
        )
        verdicts[thread.thread_id] = LifecycleVerdict(
            "dormant" if dormant else "active", "idle_and_quiet" if dormant else "active"
        )
        if not dormant:
            active.append(thread.thread_id)
    for thread_id in active[: max(0, len(active) - policy.max_active_per_chat)]:
        verdicts[thread_id] = LifecycleVerdict("dormant", "over_cap")
    return verdicts
