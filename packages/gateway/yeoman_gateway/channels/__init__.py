"""Chat channels module with plugin architecture."""

from yeoman_gateway.channels.base import BaseChannel
from yeoman_gateway.channels.manager import ChannelManager

__all__ = ["BaseChannel", "ChannelManager"]
