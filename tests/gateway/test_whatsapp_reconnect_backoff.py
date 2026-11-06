"""Bridge reconnect backoff stays bounded however many attempts have failed."""

from __future__ import annotations

from yeoman_gateway.bus.queue import MessageBus
from yeoman_gateway.channels.whatsapp import WhatsAppChannel
from yeoman_shared.config.schema import WhatsAppConfig


def _channel(**overrides: object) -> WhatsAppChannel:
    return WhatsAppChannel(WhatsAppConfig(reconnect_jitter=0.0, **overrides), MessageBus())


def test_backoff_grows_until_the_cap() -> None:
    channel = _channel()
    assert channel._compute_backoff_ms(1) == 1000
    assert channel._compute_backoff_ms(2) == 2000
    assert channel._compute_backoff_ms(10) == 30000


def test_backoff_does_not_overflow_after_many_attempts() -> None:
    # With the default factor 2.0 and unlimited attempts, attempt 1025 used to raise
    # OverflowError (34, 'Numerical result out of range') and stop the channel.
    channel = _channel()
    for attempt in (1025, 1067, 10_000, 10**9):
        assert channel._compute_backoff_ms(attempt) == 30000
