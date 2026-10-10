"""Authenticated owner-control acknowledgements.

The owner's response controls (``/stop``, ``/start``) are executed by the
deterministic admin command dispatcher.  Their acknowledgement is the only
outbound message that may still leave the process while the owner's global
response pause is active - otherwise the owner could pause responses and never
see that the pause was applied.

A ``AdminCommandResult`` is authenticated in-process: it exists only because the
admin command router accepted an owner command.  That fact is not expressible in
the outbound message itself, so the middleware that turns a handled result into a
:class:`~yeoman_gateway.core.intents.SendOutboundIntent` registers the exact
acknowledgement here and attaches the resulting single-use token to the outbound
metadata.  Transports claim the token; a message whose token is missing, stale,
already used or bound to a different channel/chat/content is treated exactly like
any other outbound message.

Nothing model-supplied can create an exemption: the token is 256 random bits
generated inside the gateway process, it is never logged and it is consumed by
the first transport that claims it.  The content is never itself a key, so a
model that reproduces the acknowledgement text gains nothing.
"""

from __future__ import annotations

import secrets
from collections import OrderedDict
from collections.abc import Mapping
from typing import Any

#: Outbound metadata key carrying the single-use acknowledgement token.
CONTROL_ACK_KEY = "owner_control_ack"

#: Admin outcomes whose acknowledgement proves an applied owner control.
APPLIED_CONTROL_OUTCOME = "applied"

#: Upper bound on pending acknowledgements; older entries are evicted first.
DEFAULT_CAPACITY = 64


class OwnerControlAcknowledgements:
    """Process-local registry of pending, single-use control acknowledgements."""

    def __init__(self, *, capacity: int = DEFAULT_CAPACITY) -> None:
        self._capacity = max(1, int(capacity))
        self._pending: OrderedDict[str, tuple[str, str, str]] = OrderedDict()

    def register(self, *, channel: str, chat_id: str, content: str) -> str:
        """Record one exact acknowledgement and return its single-use token."""
        token = secrets.token_hex(32)
        self._pending[token] = (str(channel), str(chat_id), str(content))
        while len(self._pending) > self._capacity:
            self._pending.popitem(last=False)
        return token

    def _entry(self, token: object, *, channel: str, chat_id: str, content: str) -> str | None:
        if not isinstance(token, str) or not token:
            return None
        entry = self._pending.get(token)
        if entry is None or entry != (str(channel), str(chat_id), str(content)):
            return None
        return token

    def matches(
        self, token: object, *, channel: str, chat_id: str, content: str
    ) -> bool:
        """True when *token* is a pending acknowledgement for this exact message."""
        return self._entry(token, channel=channel, chat_id=chat_id, content=content) is not None

    def matches_metadata(
        self,
        metadata: Mapping[str, Any] | None,
        *,
        channel: str,
        chat_id: str,
        content: str,
    ) -> bool:
        """Convenience wrapper for an outbound metadata mapping."""
        if not isinstance(metadata, Mapping):
            return False
        return self.matches(
            metadata.get(CONTROL_ACK_KEY), channel=channel, chat_id=chat_id, content=content
        )

    def claim(
        self, token: object, *, channel: str, chat_id: str, content: str
    ) -> bool:
        """Consume a pending acknowledgement; the second claim of a token fails."""
        claimed = self._entry(token, channel=channel, chat_id=chat_id, content=content)
        if claimed is None:
            return False
        self._pending.pop(claimed, None)
        return True
