"""Person knowledge: people, statements, provenance and protected recall.

The public boundary is :mod:`yeoman_gateway.knowledge.api` plus
:mod:`yeoman_gateway.knowledge.models` and the trusted authority protocols in
:mod:`yeoman_gateway.knowledge.authority`.  Everything else is private implementation
owned by this module.
"""

from yeoman_gateway.knowledge.api import (
    KnowledgeService,
    KnowledgeStartupError,
    open_knowledge_store,
    workspace_id_for,
)

__all__ = [
    "KnowledgeService",
    "KnowledgeStartupError",
    "open_knowledge_store",
    "workspace_id_for",
]
