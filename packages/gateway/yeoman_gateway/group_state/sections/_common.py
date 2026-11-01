"""Shared, bounded helpers for the deterministic sections."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from yeoman_gateway.agent.untrusted import neutralize_markers

from ..contract import BuildContext
from ..models import Layer, Section, Trust

MAX_NAME_CHARS = 40
#: A section never claims more source ids than this; the store and readers share the cap.
MAX_SECTION_SOURCES = 500


def safe_name(value: object) -> str:
    """Neutralize user-controlled display names and cap them for every renderer."""
    name = neutralize_markers(" ".join(str(value or "").split()))
    return name[:MAX_NAME_CHARS]


def names_for(queries: Any, contact_ids: Iterable[str]) -> dict[str, str]:
    """Terminal contact id to a safe display name; missing names become empty strings."""
    wanted = tuple(dict.fromkeys(str(item) for item in contact_ids if str(item)))
    raw = queries.contact_names(wanted) if wanted else {}
    return {contact_id: safe_name(raw.get(contact_id, "")) for contact_id in wanted}


def row_message_id(row: Mapping[str, Any]) -> str:
    return str(row.get("message_id") or "")


#: History reads return at most this many rows per call.
_PAGE = 500
#: A window read stops here; a busy week of one group stays well below it.
MAX_WINDOW_ROWS = 5_000
#: Ids per reaction read; each call costs about the same, so calls stay few.
_REACTION_IDS_PER_CALL = 500


def rows_in_window(
    queries: Any,
    *,
    chat_id: str,
    after_ms: int,
    until_ms: int,
    max_rows: int | None = None,
) -> tuple[list[Mapping[str, Any]], bool]:
    """Every timed row in ``(after_ms, until_ms]``, oldest first, paging past the read cap.

    The flag is True when ``max_rows`` (default ``MAX_WINDOW_ROWS``) cut the window short, so a caller can say so
    instead of presenting a partial count as the whole window.
    """
    limit = MAX_WINDOW_ROWS if max_rows is None else int(max_rows)
    pages: list[list[Mapping[str, Any]]] = []
    before_id: str | None = None
    total = 0
    truncated = False
    while True:
        page = queries.recent(
            chat_id=chat_id,
            limit=_PAGE,
            before_id=before_id,
            after_ms=after_ms,
            until_ms=until_ms,
        )
        if not page:
            break
        pages.append(page)
        total += len(page)
        if len(page) < _PAGE:
            break
        if total >= limit:
            truncated = True
            break
        before_id = row_message_id(page[0])
        if not before_id:
            truncated = True
            break
    return [row for page in reversed(pages) for row in page], truncated


def reactions_for(
    queries: Any, message_ids: Iterable[str], *, until_ms: int
) -> tuple[list[Mapping[str, Any]], bool]:
    """Reaction events on these messages, in few bounded calls.

    A call whose events fill a whole page is split in half and read again, so a busy
    chunk is never cut short; the flag is True only when one message alone fills a page.
    """
    wanted = tuple(dict.fromkeys(item for item in message_ids if item))
    events: list[Mapping[str, Any]] = []
    truncated = False
    pending = [
        wanted[start : start + _REACTION_IDS_PER_CALL]
        for start in range(0, len(wanted), _REACTION_IDS_PER_CALL)
    ]
    while pending:
        chunk = pending.pop()
        batch = queries.reactions(message_ids=chunk, until_ms=until_ms, limit=_PAGE)
        if len(batch) >= _PAGE and len(chunk) > 1:
            middle = len(chunk) // 2
            pending.extend((chunk[:middle], chunk[middle:]))
            continue
        events.extend(batch)
        truncated = truncated or len(batch) >= _PAGE
    return events, truncated


def rows_sources(rows: Iterable[Mapping[str, Any]]) -> tuple[str, ...]:
    ids = [row_message_id(row) for row in rows]
    return tuple(dict.fromkeys(item for item in ids if item))[:MAX_SECTION_SOURCES]


def newest_cursor(rows: Iterable[Mapping[str, Any]]) -> str:
    newest: Mapping[str, Any] | None = None
    for row in rows:
        if row.get("sent_ms") is None:
            continue
        if newest is None or int(row["sent_ms"]) >= int(newest["sent_ms"]):
            newest = row
    return row_message_id(newest) if newest is not None else ""


def make_section(
    ctx: BuildContext,
    *,
    key: str,
    layer: Layer,
    trust: Trust = "runtime",
    ttl_ms: int,
    sources: Iterable[str] = (),
    source_cursor: str = "",
    period_start_ms: int | None = None,
    period_end_ms: int | None = None,
    data: Mapping[str, Any],
) -> Section:
    unique = tuple(dict.fromkeys(str(item) for item in sources if str(item)))
    return Section(
        key=key,
        schema_version=1,
        trust=trust,
        layer=layer,
        produced_at_ms=ctx.now_ms,
        ttl_ms=int(ttl_ms),
        source_cursor=str(source_cursor or ""),
        period_start_ms=period_start_ms,
        period_end_ms=period_end_ms,
        sources=unique,
        data=data,
    )
