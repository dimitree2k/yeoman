"""Middleware pipeline — decomposed orchestrator stages.

Each module contains one middleware class extracted from the monolithic
``Orchestrator.handle()`` method.  See ``core/pipeline.py`` for the runner.
"""

from nanobot.pipeline.access import AccessControlMiddleware, NoReplyFilterMiddleware
from nanobot.pipeline.admin import AdminCommandMiddleware
from nanobot.pipeline.archive import ArchiveMiddleware
from nanobot.pipeline.dedup import DeduplicationMiddleware
from nanobot.pipeline.idea_capture import IdeaCaptureMiddleware
from nanobot.pipeline.new_chat import NewChatNotifyMiddleware
from nanobot.pipeline.normalize import NormalizationMiddleware
from nanobot.pipeline.outbound import OutboundMiddleware
from nanobot.pipeline.policy import PolicyMiddleware
from nanobot.pipeline.reply_context import ReplyContextMiddleware
from nanobot.pipeline.responder import ResponderMiddleware
from nanobot.pipeline.security_input import InputSecurityMiddleware

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
