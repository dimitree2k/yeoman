"""Message bus module for decoupled channel-agent communication."""

from yeoman_gateway.bus.events import InboundMessage, OutboundMessage
from yeoman_gateway.bus.queue import MessageBus

__all__ = ["MessageBus", "InboundMessage", "OutboundMessage"]
