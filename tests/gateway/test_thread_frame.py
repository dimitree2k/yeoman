"""The prompt keeps the current thread apart from the bot's replies in other threads."""

import sqlite3
from datetime import datetime

from yeoman_gateway.agent.context import ContextBuilder, _readable_time, build_thread_frame
from yeoman_gateway.history.reader import HistorySnapshot
from yeoman_gateway.history.schema import FTS_ROWS, create
from yeoman_gateway.session.manager import SessionManager
from yeoman_shared.utils.helpers import get_operational_store_path

GROUP = "123-456@g.us"
THREADS = {"bot-old": "th-old", "bot-cur": "th-cur", "q-cur": "th-cur"}


def _history():
    return [
        {"role": "user", "content": "Giving pledge?", "message_id": "q-old"},
        {"role": "assistant", "content": "Pledge is moral, no contract.",
         "message_id": "bot-old", "timestamp": 1_793_090_000_000},
        {"role": "user", "content": "Die Lichter zeigen an, dass das Auto automatisch faehrt",
         "message_id": "lights"},
    ]


def test_frame_separates_reply_from_other_thread():
    frame = build_thread_frame(history=_history(), thread_id="th-new", thread_of=THREADS.get)

    assert frame is not None
    assert frame.startswith("[Current Thread]")
    assert "does not reply to any earlier message" in frame
    assert _readable_time(1_793_090_000_000) in frame
    assert "other, finished threads" in frame


def test_frame_continues_when_current_message_replies_into_its_thread():
    history = [*_history(), {"role": "assistant", "content": "x", "message_id": "bot-cur"}]

    frame = build_thread_frame(history=history, thread_id="th-cur", thread_of=THREADS.get,
                               reply_to_message_id="q-cur")

    assert frame is not None
    assert "continue that thread" in frame


def test_no_frame_without_replies_from_other_threads():
    history = [{"role": "assistant", "content": "x", "message_id": "bot-cur"}]

    assert build_thread_frame(history=history, thread_id="th-cur", thread_of=THREADS.get) is None
    assert build_thread_frame(history=_history(), thread_id=None, thread_of=THREADS.get) is None
    assert build_thread_frame(history=_history(), thread_id="th-new", thread_of=None) is None


def test_frame_sits_right_before_current_message_and_times_are_readable(tmp_path):
    frame = build_thread_frame(history=_history(), thread_id="th-new", thread_of=THREADS.get)
    messages = ContextBuilder(tmp_path).build_messages(
        history=_history(),
        current_message="hast du mehr Infos dazu",
        current_metadata={"thread_frame": frame, "sender_id": "1@lid"},
        channel="whatsapp",
        chat_id=GROUP,
    )

    assert messages[-2] == {"role": "system", "content": frame}
    assert messages[-1]["role"] == "user"
    assert "hast du mehr Infos dazu" in messages[-1]["content"]


def test_readable_time_accepts_seconds_millis_and_text():
    expected = datetime.fromtimestamp(1_793_090_000).astimezone().strftime("%Y-%m-%d %H:%M")

    assert _readable_time(1_793_090_000) == expected
    assert _readable_time(1_793_090_000_000) == expected
    assert _readable_time("2026-10-27T11:00") == "2026-10-27T11:00"


def test_group_history_carries_sender_name_and_reply_link(tmp_path):
    from yeoman_gateway.session.operational import OperationalSessions

    db = sqlite3.connect(":memory:")
    create(db)
    db.execute("INSERT INTO contacts VALUES ('c-alex', 'person', NULL, 'Alex', 'confirmed',"
               " NULL, '[]')")
    db.execute(
        "INSERT INTO messages VALUES ('m1', 'whatsapp', ?, 'n1', 'c-alex', '1@lid',"
        " 'native_identifier', 'in', 10, 'native', 'Sieht geil aus', NULL, 'n0', NULL,"
        " 'native', '[]')",
        (GROUP,),
    )
    db.execute("INSERT INTO messages_fts(message_id,chat_id,text) " + FTS_ROWS)
    snapshot = HistorySnapshot(1, (), db)
    ops = OperationalSessions(get_operational_store_path("session_metadata", data_dir=tmp_path,
                                                         create=False))
    try:
        sessions = SessionManager(tmp_path, sessions_dir=tmp_path / "sessions",
                                  operational_store=ops)
        session = sessions.get_or_create("whatsapp:group", channel="whatsapp", chat_id=GROUP,
                                         thread_id="th-new", history_snapshot=snapshot)
        [row] = session.get_history()
    finally:
        snapshot.close()
        ops.close()

    assert row["sender_name"] == "Alex"
    assert row["reply_to_message_id"] == "n0"
