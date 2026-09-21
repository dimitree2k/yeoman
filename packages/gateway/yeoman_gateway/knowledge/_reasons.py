"""The shared supersession vocabulary.

A separate module on purpose: the writers (``_statements``) and the readers
(``_retrieval``) both need these names, and a module-level import between them would be a
cycle.  Nothing here is behaviour - only the four reasons a statement may leave the
current view, and the honest default for a legacy row.
"""

from __future__ import annotations

from typing import Final

#: A proven replacement of an earlier state of the world (for example a move).
SUPERSESSION_STATE_CHANGE: Final[str] = "state_change"

#: The earlier claim was wrong and is retracted.  Never presented as "earlier true".
SUPERSESSION_CORRECTION: Final[str] = "correction"

#: The row failed a screen.  Excluded from every normal and historical read.
SUPERSESSION_QUALITY_REJECTED: Final[str] = "quality_rejected"

#: Legacy rows whose reason was never established.  Treated exactly like a quality
#: rejection until concrete evidence says otherwise - never guessed as a state change.
SUPERSESSION_UNKNOWN: Final[str] = "unknown"

__all__ = [
    "SUPERSESSION_CORRECTION",
    "SUPERSESSION_QUALITY_REJECTED",
    "SUPERSESSION_STATE_CHANGE",
    "SUPERSESSION_UNKNOWN",
]
