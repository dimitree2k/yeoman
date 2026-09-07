"""Named A2A worker registry for Yeoman tools."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from typing import Any

from yeoman_gateway.a2a.client import A2AClient, A2AWorker, A2AWorkerResult


class A2AWorkerRegistry:
    """Register and dispatch to named A2A peers.

    The registry is deliberately independent from chat channels. A future
    worker can be added here and exposed through a separate Yeoman tool without
    changing WhatsApp handling or the protocol client.
    """

    def __init__(
        self,
        workers: Iterable[A2AWorker] | Mapping[str, A2AWorker] = (),
        *,
        client_factory: Callable[[A2AWorker], A2AClient] | None = None,
    ) -> None:
        self._workers: dict[str, A2AWorker] = {}
        self._client_factory = client_factory or (lambda worker: A2AClient(worker))
        values = workers.values() if isinstance(workers, Mapping) else workers
        for worker in values:
            self.register(worker)

    @classmethod
    def from_config(cls, config: Any) -> "A2AWorkerRegistry | None":
        """Build a registry only when the opt-in config gate is enabled."""
        if config is None or not bool(getattr(config, "enabled", False)):
            return None
        raw_workers = getattr(config, "workers", {}) or {}
        workers = [A2AWorker.from_config(name, worker) for name, worker in raw_workers.items()]
        if not workers:
            return None
        return cls(workers)

    def register(self, worker: A2AWorker) -> None:
        if not isinstance(worker, A2AWorker):
            raise TypeError("A2AWorkerRegistry accepts A2AWorker instances")
        if worker.name in self._workers:
            raise ValueError(f"A2A worker '{worker.name}' is already registered")
        self._workers[worker.name] = worker

    def unregister(self, name: str) -> None:
        self._workers.pop(str(name).strip(), None)

    def get(self, name: str) -> A2AWorker | None:
        return self._workers.get(str(name).strip())

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(self._workers)

    async def call(
        self,
        worker_name: str,
        message: str,
        *,
        context_id: str | None = None,
    ) -> A2AWorkerResult:
        name = str(worker_name or "").strip()
        worker = self._workers.get(name)
        if worker is None:
            raise KeyError(f"unknown A2A worker '{name}'")
        client = self._client_factory(worker)
        return await client.send_message(message, context_id=context_id)
