"""Message bus module for decoupled channel-agent communication."""

from yeoman.bus.events import InboundMessage, OutboundMessage
from yeoman.bus.queue import MessageBus

__all__ = ["MessageBus", "InboundMessage", "OutboundMessage"]
