"""Middleware pipeline — decomposed orchestrator stages.

Each module contains one middleware class extracted from the monolithic
``Orchestrator.handle()`` method.  See ``core/pipeline.py`` for the runner.
"""

__all__ = [
    "AccessControlMiddleware",
    "AdminCommandMiddleware",
    "ArchiveMiddleware",
    "DeduplicationMiddleware",
    "IdeaCaptureMiddleware",
    "InputSecurityMiddleware",
    "NewChatNotifyMiddleware",
    "NormalizationMiddleware",
    "NoReplyFilterMiddleware",
    "OutboundMiddleware",
    "PolicyMiddleware",
    "ReplyContextMiddleware",
    "ResponderMiddleware",
]
