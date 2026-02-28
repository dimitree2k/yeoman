"""Async message queue for decoupled channel-agent communication."""

import asyncio
from typing import Awaitable, Callable

from loguru import logger

from nanobot.bus.events import InboundMessage, OutboundMessage, ReactionMessage


class MessageBus:
    """
    Async message bus that decouples chat channels from the agent core.

    Channels push messages to the inbound queue, and the agent processes
    them and pushes responses to the outbound queue.
    """

    def __init__(
        self, *, inbound_maxsize: int = 0, outbound_maxsize: int = 0, reaction_maxsize: int = 0
    ):
        self.inbound: asyncio.Queue[InboundMessage] = asyncio.Queue(maxsize=max(0, inbound_maxsize))
        self.outbound: asyncio.Queue[OutboundMessage] = asyncio.Queue(
            maxsize=max(0, outbound_maxsize)
        )
        self.reaction: asyncio.Queue[ReactionMessage] = asyncio.Queue(
            maxsize=max(0, reaction_maxsize)
        )
        self._outbound_subscribers: dict[
            str, list[Callable[[OutboundMessage], Awaitable[None]]]
        ] = {}
        self._reaction_subscribers: dict[
            str, list[Callable[[ReactionMessage], Awaitable[None]]]
        ] = {}
        self._running = False
        self._inbound_dropped = 0
        self._outbound_dropped = 0
        self._reaction_dropped = 0

    async def _put_bounded(self, queue: asyncio.Queue, msg: object, channel: str) -> None:
        if queue.maxsize > 0 and queue.full():
            try:
                queue.get_nowait()
                if channel == "inbound":
                    self._inbound_dropped += 1
                    dropped = self._inbound_dropped
                else:
                    self._outbound_dropped += 1
                    dropped = self._outbound_dropped
                if dropped == 1 or dropped % 100 == 0:
                    logger.warning(f"MessageBus {channel} queue overflow: dropped={dropped}")
            except asyncio.QueueEmpty:
                pass
        await queue.put(msg)

    async def publish_inbound(self, msg: InboundMessage) -> None:
        """Publish a message from a channel to the agent."""
        await self._put_bounded(self.inbound, msg, "inbound")

    async def consume_inbound(self) -> InboundMessage:
        """Consume the next inbound message (blocks until available)."""
        return await self.inbound.get()

    async def publish_outbound(self, msg: OutboundMessage) -> None:
        """Publish a response from the agent to channels."""
        await self._put_bounded(self.outbound, msg, "outbound")

    async def consume_outbound(self) -> OutboundMessage:
        """Consume the next outbound message (blocks until available)."""
        return await self.outbound.get()

    async def publish_reaction(self, msg: ReactionMessage) -> None:
        """Publish a reaction from the agent to channels."""
        await self._put_bounded(self.reaction, msg, "reaction")

    async def consume_reaction(self) -> ReactionMessage:
        """Consume the next reaction message (blocks until available)."""
        return await self.reaction.get()

    def subscribe_reaction(
        self, channel: str, callback: Callable[[ReactionMessage], Awaitable[None]]
    ) -> None:
        """Subscribe to reaction messages for a specific channel."""
        if channel not in self._reaction_subscribers:
            self._reaction_subscribers[channel] = []
        self._reaction_subscribers[channel].append(callback)

    def subscribe_outbound(
        self, channel: str, callback: Callable[[OutboundMessage], Awaitable[None]]
    ) -> None:
        """Subscribe to outbound messages for a specific channel."""
        if channel not in self._outbound_subscribers:
            self._outbound_subscribers[channel] = []
        self._outbound_subscribers[channel].append(callback)

    async def dispatch_outbound(self) -> None:
        """
        Dispatch outbound messages to subscribed channels.
        Run this as a background task.
        """
        self._running = True
        while self._running:
            try:
                msg = await asyncio.wait_for(self.outbound.get(), timeout=1.0)
                subscribers = self._outbound_subscribers.get(msg.channel, [])
                for callback in subscribers:
                    try:
                        await callback(msg)
                    except Exception as e:
                        logger.error(f"Error dispatching to {msg.channel}: {e}")
            except asyncio.TimeoutError:
                continue

    async def dispatch_reactions(self) -> None:
        """Dispatch reaction messages to subscribed channels."""
        self._running = True
        while self._running:
            try:
                msg = await asyncio.wait_for(self.reaction.get(), timeout=1.0)
                subscribers = self._reaction_subscribers.get(msg.channel, [])
                for callback in subscribers:
                    try:
                        await callback(msg)
                    except Exception as e:
                        logger.error(f"Error dispatching reaction to {msg.channel}: {e}")
            except asyncio.TimeoutError:
                continue

    def stop(self) -> None:
        """Stop the dispatcher loop."""
        self._running = False

    @property
    def inbound_size(self) -> int:
        """Number of pending inbound messages."""
        return self.inbound.qsize()

    @property
    def outbound_size(self) -> int:
        """Number of pending outbound messages."""
        return self.outbound.qsize()

    @property
    def reaction_size(self) -> int:
        """Number of pending reaction messages."""
        return self.reaction.qsize()

    @property
    def inbound_dropped(self) -> int:
        """Number of dropped inbound messages due to queue overflow."""
        return self._inbound_dropped

    @property
    def outbound_dropped(self) -> int:
        """Number of dropped outbound messages due to queue overflow."""
        return self._outbound_dropped

    @property
    def reaction_dropped(self) -> int:
        """Number of dropped reaction messages due to queue overflow."""
        return self._reaction_dropped
