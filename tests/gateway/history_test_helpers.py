"""Shared synthetic setup for live History startup tests."""

import json
import sqlite3
from pathlib import Path


def prepare_empty_history_knowledge(tmp_path: Path, name: str = "knowledge") -> Path:
    """Publish an empty synthetic schema-3 copy for native capture fixtures."""
    from yeoman_gateway.knowledge._history_upgrade import upgrade_history_knowledge
    from yeoman_gateway.knowledge._store import KnowledgeStore

    directory = tmp_path / name
    directory.mkdir(parents=True, exist_ok=True)
    source = directory / "knowledge-v2.db"
    store = KnowledgeStore(source)
    store.set_meta("migration_complete", "1")
    store.close()
    target = directory / "knowledge-v3.db"
    upgrade_history_knowledge(source=source, target=target)
    return target


def prepare_history_retirement_knowledge(tmp_path: Path, name: str = "knowledge") -> Path:
    """Upgrade a real synthetic v2 store and persist its durable cutover handover."""
    from yeoman_gateway.knowledge._history_upgrade import upgrade_history_knowledge

    from tests.gateway.test_history_knowledge_upgrade import v2

    directory = tmp_path / name
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / "knowledge-v3.db"
    upgrade_history_knowledge(source=v2(directory / "knowledge-v2.db"), target=target)
    with sqlite3.connect(target) as db:
        db.execute(
            "INSERT INTO knowledge_history_capture_state VALUES ('handover',?,1)",
            (json.dumps({"version": 1, "generation": 1, "sources": [], "pending": [],
                         "processed": [], "legacy_boundary": [0, ""]}),),
        )
    return target

class ProjectedHistory:
    """Project native-shaped synthetic events; acquire the same reader as production."""

    def __init__(self, root: Path, *, messages=(), groups=(), contacts=()):
        self.root = root
        self.raw = root / "raw"
        self.path = root / "history.db"
        self.records = []
        self.attestations = []
        self.snapshots = []
        self.generation = 1
        self.dirty = True
        for spec in groups:
            self.add_group(**spec)
        for spec in contacts:
            self.add_contact(**spec)
        for spec in messages:
            self.add_message(**spec)

    def append(self, kind, *, chat_id, timestamp, direction="in", **payload):
        from tests.gateway.convhist.hist_fixtures import _raw
        ms = int(timestamp * 1000) if timestamp < 100_000_000_000 else int(timestamp)
        row = _raw(kind, kind, dict(chatJid=chat_id, timestamp=timestamp, **payload),
                   direction=direction, received=ms)
        row["chat_id"] = chat_id
        self.records.append(row)
        self.dirty = True

    def add_message(self, *, chat_id, message_id, text, timestamp, sender_id=None,
                    participant=None, sender_name=None, reply_to_message_id=None,
                    mentions=None, media=None, direction="in", channel="whatsapp"):
        self.append("message", chat_id=chat_id, timestamp=timestamp, direction=direction,
                    messageId=message_id, text=text, senderId=sender_id,
                    participantJid=participant, senderName=sender_name,
                    replyToMessageId=reply_to_message_id, mentionedJids=mentions, media=media,
                    fromAssistant=direction == "out")
        self.records[-1]["channel"] = channel

    def add_group(self, *, chat_id, participants, timestamp, name=None):
        self.append("membership_snapshot", chat_id=chat_id, timestamp=timestamp,
                    participants=list(participants), complete=True)
        if name is not None:
            from tests.gateway.convhist.hist_fixtures import _raw
            ms = int(timestamp * 1000) if timestamp < 100_000_000_000 else int(timestamp)
            self.records.append(_raw("group_subject", "group_subject",
                dict(chatJid=chat_id, value=name, snapshot=True, observedAtMs=ms), received=ms))

    def add_contact(self, *, identifiers, name, timestamp):
        from yeoman_gateway.history.attestations import make
        ms = int(timestamp * 1000) if timestamp < 100_000_000_000 else int(timestamp)
        self.attestations.extend([
            make("contact", ms, "synthetic test contact", identifiers=list(identifiers)),
            make("name", ms, "synthetic test name", anchor=identifiers[0], name=name),
        ])
        self.dirty = True

    def snapshot(self):
        from yeoman_gateway.history.live import HistoryBoundary
        from yeoman_gateway.history.project import project
        from yeoman_gateway.history.reader import HistoryReader

        from tests.gateway.convhist.hist_fixtures import write_jsonl
        if self.dirty:
            write_jsonl(self.raw / "whatsapp/events.jsonl", self.records)
            if self.attestations:
                write_jsonl(self.raw / "owner/attestations.jsonl", self.attestations)
            project([self.raw], self.path, publish_lineage_root=self.raw)
            self.dirty = False
        snapshot = HistoryReader(self.path).open_snapshot(HistoryBoundary(self.generation, ()))
        self.snapshots.append(snapshot)
        return snapshot

    async def read_turn(self):
        return self.snapshot()

    def health(self):
        return {"status": "ready", "generation": self.generation}

    def turn(self):
        from yeoman_gateway.history.context import history_turn
        return history_turn(self)

    def wire(self, consumer):
        consumer._history_projector = self
        consumer._history_knowledge = None
        return consumer

    def close(self):
        for snapshot in self.snapshots:
            snapshot.close()


def install_projected_responder_history(monkeypatch, request, tmp_path: Path) -> None:
    """Give ordinary WhatsApp responder tests a real projected History turn."""
    import time

    from yeoman_gateway.adapters.responder_llm import LLMResponder

    original = LLMResponder.generate_reply
    histories: dict[int, ProjectedHistory] = {}
    sequence = 0
    last_timestamp = 0

    async def generate_reply(responder, event, decision, **kwargs):
        nonlocal sequence, last_timestamp
        key = id(responder)
        history = histories.get(key)
        if (event.channel != "whatsapp" or (history is None and (
                responder._history_projector is not None))):
            return await original(responder, event, decision, **kwargs)

        if history is None:
            history = ProjectedHistory(tmp_path / f"responder-history-{key}")
            history.wire(responder)
            histories[key] = history
            responder._test_projected_history = history

        sequence += 1
        timestamp = max(int(time.time() * 1000), last_timestamp + 2)
        message_id = event.message_id or f"test-in-{sequence}"
        if event.message_id is None:
            from dataclasses import replace

            event = replace(event, message_id=message_id)
        history.add_message(
            chat_id=event.chat_id,
            message_id=message_id,
            text=event.content,
            timestamp=timestamp,
            sender_id=event.sender_id,
            sender_name=event.raw_metadata.get("sender_name"),
            direction="in",
        )
        async with history.turn() as snapshot:
            kwargs["history_snapshot"] = kwargs.get("history_snapshot") or snapshot
            result = await original(responder, event, decision, **kwargs)
        if result:
            sequence += 1
            last_timestamp = timestamp + 1
            history.add_message(
                chat_id=event.chat_id,
                message_id=f"test-out-{sequence}",
                text=result,
                timestamp=timestamp + 1,
                sender_id="yeoman",
                direction="out",
            )
        else:
            last_timestamp = timestamp
        return result

    original_direct = LLMResponder.process_direct

    async def process_direct(responder, content, **kwargs):
        channel = kwargs.get("channel", "cli")
        if channel not in {"whatsapp", "cli", "system"}:
            return await original_direct(responder, content, **kwargs)
        key = id(responder)
        history = histories.get(key)
        if history is None:
            if responder._history_projector is not None:
                return await original_direct(responder, content, **kwargs)
            history = ProjectedHistory(tmp_path / f"responder-history-{key}")
            history.wire(responder)
            histories[key] = history
        async with history.turn():
            return await original_direct(responder, content, **kwargs)

    monkeypatch.setattr(LLMResponder, "process_direct", process_direct)
    monkeypatch.setattr(LLMResponder, "generate_reply", generate_reply)
    request.addfinalizer(lambda: [history.close() for history in histories.values()])


def install_projected_thread_history(monkeypatch, request, tmp_path: Path) -> None:
    """Give actor test doubles the projected journal their generation consumes."""
    from yeoman_gateway.processing.responder import ThreadActorResponder

    original = ThreadActorResponder.generate_reply
    histories = {}
    projected = {}

    async def generate_reply(wrapper, event, decision):
        key = id(wrapper)
        history = histories.get(key)
        if history is None:
            if getattr(wrapper._inner, "_history_projector", None) is not None:
                return await original(wrapper, event, decision)
            history = ProjectedHistory(tmp_path / f"thread-history-{key}")
            history.wire(wrapper._inner)
            histories[key] = history
            projected[key] = set()
        for source in wrapper._store.events_after(after_ms=0):
            if source.event_id in projected[key] or source.channel != "whatsapp":
                continue
            history.add_message(chat_id=source.chat_id,
                message_id=source.source_message_id or source.event_id,
                text=str((source.payload or {}).get("text", "")),
                timestamp=source.occurred_ms, sender_id=source.principal,
                participant=source.principal)
            projected[key].add(source.event_id)
        return await original(wrapper, event, decision)

    monkeypatch.setattr(ThreadActorResponder, "generate_reply", generate_reply)
    request.addfinalizer(lambda: [history.close() for history in histories.values()])


def install_projected_effect_history(monkeypatch, request, tmp_path: Path) -> None:
    """Wire native history for effect tests that use the real bus executor."""
    from yeoman_gateway.processing.dispatch import BusEffectExecutor
    from yeoman_gateway.processing.effects import EffectGateway

    original = EffectGateway.__init__
    histories = []

    def initialize(gateway, *args, **kwargs):
        original(gateway, *args, **kwargs)
        if isinstance(gateway._executor, BusEffectExecutor):
            history = ProjectedHistory(tmp_path / f"effect-history-{id(gateway)}")
            gateway.set_history_scope(history, None)
            histories.append(history)

    monkeypatch.setattr(EffectGateway, "__init__", initialize)
    request.addfinalizer(lambda: [history.close() for history in histories])
