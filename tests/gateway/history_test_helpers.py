"""Shared synthetic setup for live History startup tests."""

import json
import sqlite3
from pathlib import Path


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
        consumer._history_selected = True
        consumer._history_projector = self
        consumer._history_knowledge = None
        return consumer

    def close(self):
        for snapshot in self.snapshots:
            snapshot.close()
