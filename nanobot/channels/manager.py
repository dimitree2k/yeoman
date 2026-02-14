"""Channel manager for coordinating chat channels."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from loguru import logger

from nanobot.bus.queue import MessageBus
from nanobot.channels.base import BaseChannel
from nanobot.config.schema import Config
from nanobot.providers.openai_compatible import resolve_openai_compatible_credentials

if TYPE_CHECKING:
    from nanobot.media.router import ModelRouter
    from nanobot.media.storage import MediaStorage
    from nanobot.providers.factory import ProviderFactory
    from nanobot.session.manager import SessionManager
    from nanobot.storage.inbound_archive import InboundArchive


class ChannelManager:
    """
    Manages chat channels and coordinates message routing.

    Responsibilities:
    - Initialize enabled channels (Telegram, WhatsApp, etc.)
    - Start/stop channels
    - Route outbound messages
    """

    def __init__(
        self,
        config: Config,
        bus: MessageBus,
        session_manager: "SessionManager | None" = None,
        inbound_archive: "InboundArchive | None" = None,
        model_router: "ModelRouter | None" = None,
        media_storage: "MediaStorage | None" = None,
        provider_factory: "ProviderFactory | None" = None,
    ):
        self.config = config
        self.bus = bus
        self.session_manager = session_manager
        self.inbound_archive = inbound_archive
        self.model_router = model_router
        self.media_storage = media_storage
        self.provider_factory = provider_factory
        self.channels: dict[str, BaseChannel] = {}
        self._dispatch_task: asyncio.Task | None = None

        self._init_channels()

    def _init_channels(self) -> None:
        """Initialize channels based on config."""

        # Telegram channel
        if self.config.channels.telegram.enabled:
            try:
                from nanobot.channels.telegram import TelegramChannel
                self.channels["telegram"] = TelegramChannel(
                    self.config.channels.telegram,
                    self.bus,
                    groq_api_key=self.config.providers.groq.api_key,
                    session_manager=self.session_manager,
                )
                logger.info("Telegram channel enabled")
            except ImportError as e:
                logger.warning(f"Telegram channel not available: {e}")

        # WhatsApp channel
        if self.config.channels.whatsapp.enabled:
            try:
                from nanobot.channels.whatsapp import WhatsAppChannel
                openai_compat = resolve_openai_compatible_credentials(self.config)
                self.channels["whatsapp"] = WhatsAppChannel(
                    self.config.channels.whatsapp,
                    self.bus,
                    inbound_archive=self.inbound_archive,
                    model_router=self.model_router,
                    media_storage=self.media_storage,
                    provider_factory=self.provider_factory,
                    groq_api_key=self.config.providers.groq.api_key or None,
                    openai_api_key=openai_compat.api_key if openai_compat else None,
                    openai_api_base=openai_compat.api_base if openai_compat else None,
                    openai_extra_headers=openai_compat.extra_headers if openai_compat else None,
                )
                logger.info("WhatsApp channel enabled")
            except ImportError as e:
                logger.warning(f"WhatsApp channel not available: {e}")

        # Discord channel
        if self.config.channels.discord.enabled:
            try:
                from nanobot.channels.discord import DiscordChannel
                self.channels["discord"] = DiscordChannel(
                    self.config.channels.discord, self.bus
                )
                logger.info("Discord channel enabled")
            except ImportError as e:
                logger.warning(f"Discord channel not available: {e}")

        # Feishu channel
        if self.config.channels.feishu.enabled:
            try:
                from nanobot.channels.feishu import FeishuChannel
                self.channels["feishu"] = FeishuChannel(
                    self.config.channels.feishu, self.bus
                )
                logger.info("Feishu channel enabled")
            except ImportError as e:
                logger.warning(f"Feishu channel not available: {e}")

    async def _start_channel(self, name: str, channel: BaseChannel) -> None:
        """Start a channel and log any exceptions."""
        try:
            await channel.start()
        except Exception as e:
            logger.error(f"Failed to start channel {name}: {e}")

    async def start_all(self) -> None:
        """Start all channels and the outbound dispatcher."""
        if not self.channels:
            logger.warning("No channels enabled")
            return

        # Start outbound dispatcher
        self._dispatch_task = asyncio.create_task(self._dispatch_outbound())

        # Start channels
        tasks = []
        for name, channel in self.channels.items():
            logger.info(f"Starting {name} channel...")
            tasks.append(asyncio.create_task(self._start_channel(name, channel)))

        # Wait for all to complete (they should run forever)
        await asyncio.gather(*tasks, return_exceptions=True)

    async def stop_all(self) -> None:
        """Stop all channels and the dispatcher."""
        logger.info("Stopping all channels...")

        # Stop dispatcher
        if self._dispatch_task:
            self._dispatch_task.cancel()
            try:
                await self._dispatch_task
            except asyncio.CancelledError:
                pass

        # Stop all channels
        for name, channel in self.channels.items():
            try:
                await channel.stop()
                logger.info(f"Stopped {name} channel")
            except Exception as e:
                logger.error(f"Error stopping {name}: {e}")

    async def _dispatch_outbound(self) -> None:
        """Dispatch outbound messages to the appropriate channel."""
        logger.info("Outbound dispatcher started")

        while True:
            try:
                msg = await asyncio.wait_for(
                    self.bus.consume_outbound(),
                    timeout=1.0
                )

                channel = self.channels.get(msg.channel)
                if channel:
                    try:
                        logger.debug(
                            "Outbound dispatch start channel={} chat={} reply_to={} media_count={} content_len={}",
                            msg.channel,
                            msg.chat_id,
                            bool(msg.reply_to),
                            len(msg.media or []),
                            len(msg.content or ""),
                        )
                        await channel.send(msg)
                        logger.debug(
                            "Outbound dispatch success channel={} chat={}",
                            msg.channel,
                            msg.chat_id,
                        )
                    except Exception as e:
                        logger.error(f"Error sending to {msg.channel}: {e}")
                else:
                    logger.warning(f"Unknown channel: {msg.channel}")

            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break

    async def set_typing(self, channel_name: str, chat_id: str, enabled: bool) -> None:
        """Best-effort typing indicator dispatch to a specific channel."""
        channel = self.channels.get(channel_name)
        if channel is None:
            return

        method_name = "start_typing" if enabled else "stop_typing"
        method = getattr(channel, method_name, None)
        if not callable(method):
            return

        try:
            await method(chat_id)
        except Exception as e:
            logger.debug(
                "Failed to toggle typing channel={} chat={} enabled={}: {}",
                channel_name,
                chat_id,
                enabled,
                e,
            )

    def get_channel(self, name: str) -> BaseChannel | None:
        """Get a channel by name."""
        return self.channels.get(name)

    def get_status(self) -> dict[str, Any]:
        """Get status of all channels."""
        return {
            name: {
                "enabled": True,
                "running": channel.is_running
            }
            for name, channel in self.channels.items()
        }

    @property
    def enabled_channels(self) -> list[str]:
        """Get list of enabled channel names."""
        return list(self.channels.keys())
