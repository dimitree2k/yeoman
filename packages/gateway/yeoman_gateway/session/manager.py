"""Session management for conversation history."""

import json
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from loguru import logger
from yeoman_shared.raw_archive.paths import is_protected
from yeoman_shared.utils.helpers import get_operational_store_path, get_sessions_path, safe_filename

from yeoman_gateway.history.live import HistoryPaused
from yeoman_gateway.history.queries import HistoryQueries
from yeoman_gateway.history.reader import HistorySnapshot
from yeoman_gateway.history.writer_guard import (
    legacy_history_writers_disabled,
    require_legacy_history_writer,
)

if TYPE_CHECKING:
    from .operational import OperationalSessions

_LLM_HISTORY_METADATA_KEYS = frozenset(
    {
        "timestamp",
        "sender_id",
        "sender_name",
        "message_id",
        "reply_to_message_id",
        "reply_to_participant",
        "reply_to_text",
    }
)

LEGACY_CONTEXT_MARKER = "[legacy chat context - not thread-bound]"
LEGACY_CONTEXT_TURNS = 20
LEGACY_CONTEXT_MAX_CHARS = 6000


@dataclass
class Session:
    """
    A conversation session.

    Stores messages in JSONL format for easy reading and persistence.
    """

    key: str  # channel:chat_id
    messages: list[dict[str, Any]] = field(default_factory=list)
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)
    metadata: dict[str, Any] = field(default_factory=dict)
    operational_store: "OperationalSessions | None" = field(default=None, repr=False)
    channel: str = ""
    chat_id: str = ""
    thread_id: str | None = None
    history_snapshot: HistorySnapshot | None = field(default=None, repr=False)
    current_message_id: str | None = None
    turn_id: str = field(default_factory=lambda: uuid.uuid4().hex)

    def add_message(self, role: str, content: str, **kwargs: Any) -> None:
        """Add a message to the session."""
        msg = {
            "role": role,
            "content": content,
            "timestamp": datetime.now().isoformat(),
            **kwargs
        }
        self.messages.append(msg)
        self.updated_at = datetime.now()

    def add_tool_call(
        self,
        tool_name: str,
        tool_call_id: str,
        arguments: dict[str, Any],
        result: str,
    ) -> None:
        """Record a tool call trace (excluded from LLM context, kept for debugging)."""
        if self.operational_store is not None:
            self.operational_store.record_tool_trace(
                session_key=self.key, turn_id=self.turn_id, tool_call_id=tool_call_id,
                tool_name=tool_name, arguments=json.dumps(arguments, sort_keys=True),
                result=result, at_ms=int(datetime.now().timestamp() * 1000))
            return
        msg = {
            "role": "tool_trace",
            "tool_name": tool_name,
            "tool_call_id": tool_call_id,
            "arguments": arguments,
            "result": result[:2000],  # truncate large results
            "timestamp": datetime.now().isoformat(),
        }
        self.messages.append(msg)
        self.updated_at = datetime.now()

    def add_boundary(self) -> None:
        """Insert a session boundary marker. get_history() will not look past this."""
        if self.operational_store is not None:
            self.operational_store.set_boundary(channel=self.channel, chat_id=self.chat_id,
                                                at_ms=int(datetime.now().timestamp() * 1000))
            self.messages.clear()
            return
        self.messages.append({
            "role": "session_boundary",
            "timestamp": datetime.now().isoformat(),
        })
        self.updated_at = datetime.now()

    def get_history(self, max_messages: int = 50) -> list[dict[str, Any]]:
        """
        Get message history for LLM context.

        Scans backwards from the end and stops at the most recent
        ``session_boundary`` marker or at *max_messages*, whichever comes first.
        """
        # Find the most recent session boundary.
        boundary_idx = -1
        for i in range(len(self.messages) - 1, -1, -1):
            if self.messages[i].get("role") == "session_boundary":
                boundary_idx = i
                break

        start = boundary_idx + 1 if boundary_idx >= 0 else 0
        operational_boundary = (self.operational_store.boundary(channel=self.channel, chat_id=self.chat_id)
                                if self.operational_store is not None else None)
        candidates = [
            message
            for message in self.messages[start:]
            if message.get("hidden") is not True
        ]
        if operational_boundary is not None:
            dated = []
            for message in candidates:
                try:
                    at_ms = int(datetime.fromisoformat(message["timestamp"]).timestamp() * 1000)
                except (KeyError, TypeError, ValueError):
                    continue
                if at_ms > operational_boundary:
                    dated.append(message)
            candidates = dated

        # Apply max_messages limit.
        if len(candidates) > max_messages:
            candidates = candidates[-max_messages:]

        # Convert to LLM format, skipping internal or malformed rows.
        history: list[dict[str, Any]] = []
        allowed_roles = {"system", "user", "assistant"}
        for message in candidates:
            if message.get("hidden") is True:
                continue
            role = str(message.get("role") or "").strip()
            if role == "tool_trace" or role not in allowed_roles:
                continue
            if "content" not in message:
                continue
            content = message.get("content")
            if content is None:
                content = ""
            if not isinstance(content, (str, list, dict)):
                content = str(content)
            row = {"role": role, "content": content}
            for key in _LLM_HISTORY_METADATA_KEYS:
                if key in message and message[key] is not None:
                    row[key] = message[key]
            history.append(row)
        if self.operational_store is not None:
            if self.history_snapshot is None:
                raise HistoryPaused("session_snapshot_required")
            from yeoman_gateway.adapters.reply_archive_history import history_text
            queries = HistoryQueries(self.history_snapshot)
            rows = queries.recent(
                chat_id=self.chat_id,
                limit=min(max_messages, LEGACY_CONTEXT_TURNS) if self.thread_id and not self.chat_id.endswith("@g.us") else max_messages,
                after_ms=operational_boundary)
            projected = {("assistant" if row["direction"] == "out" else "user", row["native_message_id"])
                         for row in rows if row["native_message_id"]}
            rows = [row for row in rows if not (row["direction"] == "in" and self.current_message_id
                                                and row["native_message_id"] == self.current_message_id)]
            local = []
            seen = set(projected)
            for row in history:
                native_id = row.get("message_id")
                if row["role"] == "user" and self.current_message_id and native_id == self.current_message_id:
                    continue
                if native_id:
                    key = (row["role"], native_id)
                    if key in seen:
                        continue
                    message = queries.native_message(chat_id=self.chat_id, native_id=native_id)
                    if message is not None and row["role"] == ("assistant" if message["direction"] == "out" else "user"):
                        continue
                    seen.add(key)
                local.append(row)
            history = local
            preceding = [{"role": "assistant" if row["direction"] == "out" else "user",
                          "content": history_text(row), "message_id": row["native_message_id"],
                          "timestamp": row["sent_ms"], "sender_id": row["sender_identifier"]}
                         for row in rows]
            if self.thread_id and not self.chat_id.endswith("@g.us"):
                bounded = []
                total = 0
                for row in reversed(preceding):
                    row["content"] = " ".join(str(row["content"]).split())[:400]
                    size = len(f"{row['role']}: {row['content']}")
                    if total + size > LEGACY_CONTEXT_MAX_CHARS:
                        break
                    if row["content"]:
                        bounded.append(row)
                        total += size
                preceding = [{"role": "system", "content": LEGACY_CONTEXT_MARKER + "\n" +
                              "\n".join(f"{row['role']}: {row['content']}" for row in reversed(bounded))}] if bounded else []
            return (preceding + history)[-max_messages:]
        return history

    def get_full_history(self) -> list[dict[str, Any]]:
        """Return all messages including tool traces."""
        return list(self.messages)

    def clear(self) -> None:
        """Clear all messages in the session."""
        self.messages = []
        self.updated_at = datetime.now()


class SessionManager:
    """
    Manages conversation sessions.

    Sessions are stored as JSONL files in the sessions directory.
    """

    def __init__(self, workspace: Path, sessions_dir: Path | None = None, *,
                 operational_store: "OperationalSessions | None" = None,
                 history_selected: bool = False, legacy_history_disabled: bool = False):
        self.workspace = workspace
        self.operational_store = operational_store
        self.history_selected = history_selected is True
        self.legacy_history_disabled = legacy_history_writers_disabled(legacy_history_disabled)
        history_selected = self.history_selected
        legacy_history_disabled = self.legacy_history_disabled
        if (history_selected or legacy_history_disabled) and operational_store is None:
            from .operational import OperationalSessions
            self.operational_store = OperationalSessions(get_operational_store_path("session_metadata"))
        if history_selected or legacy_history_disabled:
            # Resolve legacy location without creating a retired inbound directory.
            self.sessions_dir = sessions_dir if sessions_dir is not None else get_sessions_path(create=False)
        else:
            self.sessions_dir = sessions_dir if sessions_dir is not None else get_sessions_path()
            self.sessions_dir.mkdir(parents=True, exist_ok=True)
        self._cache: dict[str, Session] = {}

    def uses_history(self, channel: str) -> bool:
        if channel != "whatsapp":
            return False
        if self.legacy_history_disabled and not self.history_selected:
            raise HistoryPaused("session_reader_unselected")
        return self.history_selected

    def recent_history(self, *, channel: str, chat_id: str, snapshot: HistorySnapshot,
                       limit: int) -> list[dict[str, Any]]:
        session = self.get_or_create(f"{channel}:{chat_id}", channel=channel, chat_id=chat_id,
                                     history_snapshot=snapshot)
        return session.get_history(max_messages=limit)

    def _get_session_path(self, key: str) -> Path:
        """Get the file path for a session."""
        safe_key = safe_filename(key.replace(":", "_"))
        return self.sessions_dir / f"{safe_key}.jsonl"

    def get_or_create(self, key: str, *, channel: str | None = None, chat_id: str | None = None,
                      thread_id: str | None = None, history_snapshot: HistorySnapshot | None = None,
                      turn_id: str | None = None, current_message_id: str | None = None) -> Session:
        """
        Get an existing session or create a new one.

        Args:
            key: Session key (usually channel:chat_id).

        Returns:
            The session.
        """
        if self.history_selected or self.legacy_history_disabled:
            if channel is None and key.startswith("whatsapp:"):
                raise ValueError("selected session routing requires explicit channel/chat")
            # Legacy non-WhatsApp keys retain their callers; never decode a new-store route.
            if channel is not None and self.uses_history(channel):
                if chat_id is None:
                    raise ValueError("selected session routing requires explicit channel/chat")
                assert self.operational_store is not None
                self.operational_store.set_route(session_key=key, channel=channel, chat_id=chat_id,
                                                 thread_id=thread_id)
                return Session(key=key, operational_store=self.operational_store,
                               channel=channel, chat_id=chat_id, thread_id=thread_id,
                               history_snapshot=history_snapshot, current_message_id=current_message_id,
                               turn_id=turn_id or uuid.uuid4().hex)
        require_legacy_history_writer(disabled=self.legacy_history_disabled, channel=channel or key.split(":", 1)[0])
        # Check cache
        if key in self._cache:
            return self._cache[key]

        # Try to load from disk
        session = self._load(key)
        if session is None:
            session = Session(key=key)

        self._cache[key] = session
        return session

    def _load(self, key: str) -> Session | None:
        """Load a session from disk."""
        require_legacy_history_writer(disabled=self.legacy_history_disabled, channel=key.split(":", 1)[0])
        path = self._get_session_path(key)

        if not path.exists():
            return None

        try:
            messages = []
            metadata = {}
            created_at = None

            with open(path) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue

                    data = json.loads(line)

                    if data.get("_type") == "metadata":
                        metadata = data.get("metadata", {})
                        created_at = datetime.fromisoformat(data["created_at"]) if data.get("created_at") else None
                    else:
                        messages.append(data)

            return Session(
                key=key,
                messages=messages,
                created_at=created_at or datetime.now(),
                metadata=metadata
            )
        except Exception as e:
            logger.warning(f"Failed to load session {key}: {e}")
            return None

    def save(self, session: Session) -> None:
        """Save a session to disk."""
        if session.operational_store is not None:
            return
        require_legacy_history_writer(disabled=self.legacy_history_disabled, channel=session.key.split(":", 1)[0])
        path = self._get_session_path(session.key)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")

        try:
            with open(temporary, "x") as f:
                metadata_line = {
                    "_type": "metadata",
                    "created_at": session.created_at.isoformat(),
                    "updated_at": session.updated_at.isoformat(),
                    "metadata": session.metadata
                }
                f.write(json.dumps(metadata_line) + "\n")
                for msg in session.messages:
                    f.write(json.dumps(msg) + "\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(temporary, path)
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if temporary.exists():
                temporary.unlink()

        self._cache[session.key] = session

    def delete(self, key: str) -> bool:
        """
        Delete a session.

        Args:
            key: Session key.

        Returns:
            True if deleted, False if not found.
        """
        require_legacy_history_writer(disabled=self.legacy_history_disabled, channel=key.split(":", 1)[0])
        # Remove from cache
        self._cache.pop(key, None)

        # Remove file
        path = self._get_session_path(key)
        if is_protected(path):
            logger.error("session delete refused: {} is inside the raw archive", path)
            return False
        if path.exists():
            path.unlink()
            return True
        return False

    def list_sessions(self) -> list[dict[str, Any]]:
        """
        List all sessions.

        Returns:
            List of session info dicts.
        """
        sessions = []

        for path in self.sessions_dir.glob("*.jsonl"):
            try:
                # Read just the metadata line
                with open(path) as f:
                    first_line = f.readline().strip()
                    if first_line:
                        data = json.loads(first_line)
                        if data.get("_type") == "metadata":
                            sessions.append({
                                "key": path.stem.replace("_", ":"),
                                "created_at": data.get("created_at"),
                                "updated_at": data.get("updated_at"),
                                "path": str(path)
                            })
            except Exception:
                continue

        return sorted(sessions, key=lambda x: x.get("updated_at", ""), reverse=True)
