"""Private semantic-memory implementation of the knowledge module.

Not a public import path: consumers use :mod:`yeoman_gateway.knowledge.api`.  The memory
store joins the knowledge store's single connection and never commits on its own.
"""

from yeoman_gateway.knowledge._memory.embeddings import MemoryEmbeddingService
from yeoman_gateway.knowledge._memory.extractor import ExtractedCandidate, MemoryExtractorService
from yeoman_gateway.knowledge._memory.models import (
    MemoryCaptureCandidate,
    MemoryCaptureResult,
    MemoryEntry,
    MemoryHit,
    MemoryScopeType,
    MemorySector,
)
from yeoman_gateway.knowledge._memory.service import MemoryService
from yeoman_gateway.knowledge._memory.store import MemoryStore

__all__ = [
    "ExtractedCandidate",
    "MemoryEmbeddingService",
    "MemoryExtractorService",
    "MemoryCaptureCandidate",
    "MemoryCaptureResult",
    "MemoryEntry",
    "MemoryHit",
    "MemorySector",
    "MemoryScopeType",
    "MemoryService",
    "MemoryStore",
]
