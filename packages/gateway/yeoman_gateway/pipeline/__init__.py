"""Middleware pipeline — decomposed orchestrator stages.

Each module contains one middleware class extracted from the monolithic
``Orchestrator.handle()`` method.  See ``core/pipeline.py`` for the runner.
"""

from yeoman_gateway.pipeline.access import AccessControlMiddleware, NoReplyFilterMiddleware
from yeoman_gateway.pipeline.admin import AdminCommandMiddleware
from yeoman_gateway.pipeline.archive import ArchiveMiddleware
from yeoman_gateway.pipeline.dedup import DeduplicationMiddleware
from yeoman_gateway.pipeline.idea_capture import IdeaCaptureMiddleware
from yeoman_gateway.pipeline.implicit_address import ImplicitBotAddressMiddleware
from yeoman_gateway.pipeline.new_chat import NewChatNotifyMiddleware
from yeoman_gateway.pipeline.normalize import NormalizationMiddleware
from yeoman_gateway.pipeline.outbound import OutboundMiddleware
from yeoman_gateway.pipeline.policy import PolicyMiddleware
from yeoman_gateway.pipeline.reply_context import ReplyContextMiddleware
from yeoman_gateway.pipeline.responder import ResponderMiddleware
from yeoman_gateway.pipeline.security_input import InputSecurityMiddleware
from yeoman_gateway.pipeline.speakup_approval import SpeakupApprovalMiddleware

__all__ = [
    "AccessControlMiddleware",
    "AdminCommandMiddleware",
    "ArchiveMiddleware",
    "DeduplicationMiddleware",
    "IdeaCaptureMiddleware",
    "ImplicitBotAddressMiddleware",
    "InputSecurityMiddleware",
    "NewChatNotifyMiddleware",
    "NormalizationMiddleware",
    "NoReplyFilterMiddleware",
    "OutboundMiddleware",
    "PolicyMiddleware",
    "ReplyContextMiddleware",
    "ResponderMiddleware",
    "SpeakupApprovalMiddleware",
]
