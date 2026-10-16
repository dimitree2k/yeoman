"""The sole allowlist for directory values entering prompts and tool results."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from .identifiers import is_valid_value
from .resolver import PeopleResolver, PeopleUnavailable, ReaderContext, _active

if TYPE_CHECKING:
    from yeoman_gateway.history.reader import HistorySnapshot

# E.164 country calling codes, including shared and international service prefixes.
_CALLING_CODES = frozenset(
    """1 7 20 27 30 31 32 33 34 36 39 40 41 43 44 45 46 47 48 49
51 52 53 54 55 56 57 58 60 61 62 63 64 65 66 81 82 84 86 90 91 92 93 94 95 98
211 212 213 216 218 220 221 222 223 224 225 226 227 228 229 230 231 232 233 234 235
236 237 238 239 240 241 242 243 244 245 246 247 248 249 250 251 252 253 254 255 256
257 258 260 261 262 263 264 265 266 267 268 269 290 291 297 298 299 350 351 352 353
354 355 356 357 358 359 370 371 372 373 374 375 376 377 378 380 381 382 383 385 386
387 389 420 421 423 500 501 502 503 504 505 506 507 508 509 590 591 592 593 594 595
596 597 598 599 670 672 673 674 675 676 677 678 679 680 681 682 683 685 686 687 688
689 690 691 692 800 808 850 852 853 855 856 870 878 880 881 882 883 886 888 960 961
962 963 964 965 966 967 968 970 971 972 973 974 975 976 977 979 992 993 994 995 996 998""".split()
)
_DETAILS = ("address", "home_city", "birthday", "relation", "notes")
_PUBLIC_IDS = frozenset(("phone", "wa_lid", "telegram_id", "telegram_username"))


@dataclass(frozen=True, slots=True)
class RenderedPerson:
    person_id: int
    display_name: str
    names: tuple[str, ...]
    phones: tuple[str, ...]
    identifiers: tuple[tuple[str, str], ...]
    shared_chat_names: tuple[str, ...]
    owner_details: tuple[tuple[str, str], ...]


def _owner_phone_code(directory: PeopleResolver, index, at_ms: int) -> str | None:
    owners = {index.terminal(p) for p, row in index.persons.items() if row["is_owner"]}
    if len(owners) != 1:
        return None
    owner = next(iter(owners))
    phones = {
        row["value"]
        for row in index.identifiers_by_terminal.get(owner, ())
        if row["kind"] == "phone"
        and is_valid_value("phone", row["value"])
        and index.terminal(row["person_id"]) == owner
        and _active(row, at_ms)
        and index.matches("phone", row["value"], at_ms) == {owner}
    }
    if len(phones) != 1:
        return None
    phone = next(iter(phones))
    cached = getattr(directory._local, "owner_codes", None)
    if cached is None or cached[0] is not index:
        cached = (index, {})
        directory._local.owner_codes = cached
    if phone not in cached[1]:
        cached[1][phone] = next(
            (phone[:n] for n in range(1, 4) if phone[:n] in _CALLING_CODES), None
        )
    return cached[1][phone]


def reader_context(
    directory: PeopleResolver,
    *,
    channel: str,
    chat_id: str,
    principal_id: str,
    is_owner: bool,
    at_ms: int,
    history: HistorySnapshot | None,
    national_calling_code: str | None = None,
) -> ReaderContext:
    """Inputs are authenticated server bindings; directory ownership supplies the country code."""
    from yeoman_gateway.history.live import HistoryPaused
    from yeoman_gateway.history.queries import HistoryQueries

    if channel == "whatsapp":
        if history is None:
            raise HistoryPaused("reader_unavailable")
        history.assert_current(history.generation)
    index = directory._index()
    kind = "other_dm"
    reader = ReaderContext(
        directory,
        channel,
        chat_id,
        principal_id,
        at_ms,
        kind,
        history,
        _owner_phone_code(directory, index, at_ms),
    )
    endpoint = directory._endpoint(chat_id, reader, index)
    principal = directory._endpoint(principal_id, reader, index)
    owners = {index.terminal(p) for p, row in index.persons.items() if row["is_owner"]}
    owner_endpoint = len(owners) == 1 and endpoint == principal == owners
    if channel == "whatsapp":
        from yeoman_gateway.history.ids import classify

        ident = classify(chat_id)
        if ident and ident.kind == "group":
            kind = "group"
        elif owner_endpoint and HistoryQueries(history).is_owner_direct_chat(
            principal_id, chat_id, at_ms, is_owner=is_owner, channel=channel
        ):
            kind = "owner_dm"
        history.assert_current(history.generation)
    elif (
        channel == "telegram"
        and chat_id.startswith("-")
        and chat_id[1:].isascii()
        and chat_id[1:].isdigit()
    ):
        kind = "group"
    elif is_owner and owner_endpoint:
        kind = "owner_dm"
    return replace(reader, kind=kind)


def render_person(person_id: int, reader: ReaderContext) -> RenderedPerson:
    directory = reader.directory
    index = directory._index()
    scope = directory._scope(reader, index)
    terminal = index.terminal(person_id)
    if terminal is None or (reader.kind != "owner_dm" and terminal not in scope.eligible_ids):
        raise PeopleUnavailable("people_out_of_scope")
    display = index.display_name(terminal)
    aliases = {name for name, _, _ in index.aliases_by_terminal.get(terminal, ())}
    names = (
        (display, *sorted(aliases - {display}, key=lambda name: (name.casefold(), name)))
        if aliases
        else ()
    )
    allow_phone = reader.kind == "owner_dm" or (
        reader.kind == "group" and terminal in scope.member_ids
    )
    identifiers = set()
    for row in index.identifiers_by_terminal.get(terminal, ()):
        kind, value = row["kind"], row["value"]
        if (
            index.terminal(row["person_id"]) != terminal
            or kind not in _PUBLIC_IDS
            or not _active(row, reader.at_ms)
            or row["hidden"]
            or not is_valid_value(kind, value)
        ):
            continue
        if kind == "phone" and not allow_phone:
            continue
        # Conflicting active ownership is not a safe public identifier.
        if index.matches(kind, value, reader.at_ms) == {terminal}:
            identifiers.add((kind, value))
    phones = tuple(sorted(value for kind, value in identifiers if kind == "phone"))
    details = tuple(
        (key, index.persons[terminal][key])
        for key in _DETAILS
        if reader.kind == "owner_dm" and index.persons[terminal][key] is not None
    )
    shared_names = directory._shared_chat_names(terminal, reader, index)
    return RenderedPerson(
        terminal,
        display,
        tuple(names),
        phones,
        tuple(sorted(identifiers)),
        shared_names,
        details,
    )
