"""Owner-editable person directory (data/people/people.db): the one place that answers "who is this?"."""

from .bridges import HistoryPerson, PersonIdentifier, history_person, knowledge_person_ids
from .disclosure import RenderedPerson, reader_context, render_person
from .history_source import ObservedPerson, read_observed_persons
from .identifiers import ObservedId
from .resolver import (
    Candidate,
    PeopleResolver,
    PeopleUnavailable,
    ReaderContext,
    ReaderScope,
    Resolution,
    name_tokens,
    resolve_people_in_text,
    resolve_person,
)
from .schema import open_people_db, people_db_path
from .store import PeopleIntegrityError, PeopleStore
from .sync import SyncReport, sync_from_history

__all__ = [
    "Candidate",
    "HistoryPerson",
    "ObservedId",
    "ObservedPerson",
    "PeopleIntegrityError",
    "PeopleResolver",
    "PeopleStore",
    "PeopleUnavailable",
    "PersonIdentifier",
    "ReaderContext",
    "ReaderScope",
    "RenderedPerson",
    "Resolution",
    "SyncReport",
    "history_person",
    "knowledge_person_ids",
    "name_tokens",
    "open_people_db",
    "people_db_path",
    "read_observed_persons",
    "reader_context",
    "render_person",
    "resolve_people_in_text",
    "resolve_person",
    "sync_from_history",
]
