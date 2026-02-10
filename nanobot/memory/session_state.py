"""Per-session WAL state files for memory durability."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from nanobot.utils.helpers import ensure_dir, safe_filename


class SessionStateStore:
    """Append-only markdown WAL for per-session state."""

    def __init__(self, workspace: Path, state_dir: str = "memory/session-state") -> None:
        relative = Path(state_dir)
        self._base = ensure_dir(workspace / relative)

    def _path_for_session(self, session_key: str) -> Path:
        safe_key = safe_filename(session_key.replace(":", "_"))
        return self._base / f"{safe_key}.md"

    def read(self, session_key: str) -> str:
        path = self._path_for_session(session_key)
        if not path.exists():
            return ""
        return path.read_text(encoding="utf-8")

    def pre_write(
        self,
        *,
        session_key: str,
        channel: str,
        chat_id: str,
        user_message: str,
        metadata: dict[str, object],
    ) -> Path:
        path = self._path_for_session(session_key)
        now_iso = datetime.now(UTC).isoformat()
        user_preview = " ".join(user_message.split())
        if len(user_preview) > 400:
            user_preview = user_preview[:400] + "..."

        if not path.exists():
            path.write_text(
                "# Session WAL State\n\n"
                f"- session_key: {session_key}\n"
                f"- channel: {channel}\n"
                f"- chat_id: {chat_id}\n"
                "\n"
                "## Turns\n\n",
                encoding="utf-8",
            )

        message_id = str(metadata.get("message_id") or "").strip()
        sender_id = str(metadata.get("sender_id") or metadata.get("sender") or "").strip()
        participant = str(metadata.get("participant") or "").strip()
        speaker = participant or sender_id
        reply_to = str(metadata.get("reply_to_message_id") or "").strip()
        reply_to_participant = str(metadata.get("reply_to_participant") or "").strip()
        media_kind = str(metadata.get("media_kind") or "").strip()
        media_type = str(metadata.get("media_type") or "").strip()

        with path.open("a", encoding="utf-8") as f:
            f.write(f"### {now_iso} PRE\n")
            if speaker:
                f.write(f"- speaker: {speaker}\n")
            if sender_id and sender_id != speaker:
                f.write(f"- sender_id: {sender_id}\n")
            if message_id:
                f.write(f"- message_id: {message_id}\n")
            if media_kind or media_type:
                media_label = " / ".join(part for part in [media_kind, media_type] if part)
                f.write(f"- media: {media_label}\n")
            f.write(f"- user: {user_preview}\n")
            if reply_to:
                f.write(f"- reply_to_message_id: {reply_to}\n")
            if reply_to_participant:
                f.write(f"- reply_to_participant: {reply_to_participant}\n")
            f.write("\n")
        return path

    def post_write(
        self,
        *,
        session_key: str,
        assistant_reply: str,
        pending_actions: list[str] | None = None,
    ) -> Path:
        path = self._path_for_session(session_key)
        now_iso = datetime.now(UTC).isoformat()
        reply_preview = " ".join((assistant_reply or "").split())
        if len(reply_preview) > 400:
            reply_preview = reply_preview[:400] + "..."

        if not path.exists():
            path.write_text("# Session WAL State\n\n## Turns\n\n", encoding="utf-8")

        with path.open("a", encoding="utf-8") as f:
            f.write(f"### {now_iso} POST\n")
            f.write(f"- assistant: {reply_preview or '(empty)'}\n")
            if pending_actions:
                f.write("- pending_actions:\n")
                for action in pending_actions[:10]:
                    f.write(f"  - {action}\n")
            f.write("\n")
        return path

    @property
    def state_dir(self) -> Path:
        return self._base
