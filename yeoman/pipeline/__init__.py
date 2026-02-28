"""Middleware pipeline — decomposed orchestrator stages.

Each module contains one middleware class extracted from the monolithic
``Orchestrator.handle()`` method.  See ``core/pipeline.py`` for the runner.
"""

from yeoman.pipeline.access import AccessControlMiddleware, NoReplyFilterMiddleware
from yeoman.pipeline.admin import AdminCommandMiddleware
from yeoman.pipeline.archive import ArchiveMiddleware
from yeoman.pipeline.dedup import DeduplicationMiddleware
from yeoman.pipeline.idea_capture import IdeaCaptureMiddleware
from yeoman.pipeline.new_chat import NewChatNotifyMiddleware
from yeoman.pipeline.normalize import NormalizationMiddleware
from yeoman.pipeline.outbound import OutboundMiddleware
from yeoman.pipeline.policy import PolicyMiddleware
from yeoman.pipeline.reply_context import ReplyContextMiddleware
from yeoman.pipeline.responder import ResponderMiddleware
from yeoman.pipeline.security_input import InputSecurityMiddleware

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
