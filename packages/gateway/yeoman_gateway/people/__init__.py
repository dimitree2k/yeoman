"""Owner-editable person directory (data/people/people.db): the one place that answers "who is this?"."""

from .history_source import ObservedPerson, read_observed_persons
from .identifiers import ObservedId
from .schema import open_people_db, people_db_path
from .store import PeopleIntegrityError, PeopleStore
from .sync import SyncReport, sync_from_history

__all__ = [
    "ObservedId",
    "ObservedPerson",
    "PeopleIntegrityError",
    "PeopleStore",
    "SyncReport",
    "open_people_db",
    "people_db_path",
    "read_observed_persons",
    "sync_from_history",
]
