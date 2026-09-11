"""The ``react`` reply action: one emoji instead of an answer.

A chat configured with ``replyActions: "react"`` does not want text. Running a full answer
turn only to receive a face would cost a persona prompt, history, memory recall, tools and
a typing indicator - exactly the expense this action exists to avoid. So the emoji is
chosen by one deliberately small model call: the message in, one emoji out, nothing else.
No session history, no tools, no typing indicator, no answer turn.

The vocabulary is the owner's (``processing.reactionEmojis``) and the answer is validated
against it, so the model can pick but never invent. An unapproved or empty answer means
silence, not a guessed face.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from loguru import logger
from yeoman_shared.reactions import allowed_reaction

from yeoman_gateway.core.intents import SendReactionIntent

#: Deliberately terse: the whole call is one decision about one message.
REACTION_SYSTEM_PROMPT = (
    "Du wählst höchstens eine Emoji-Reaktion für eine Chat-Nachricht.\n"
    "Antworte ausschließlich mit genau einem Emoji aus der erlaubten Liste, "
    "oder mit dem Wort none, wenn keine Reaktion passt.\n"
    "Keine Erklärung, kein Text, keine Satzzeichen."
)

#: The message a reaction is chosen for is truncated: a face does not need the whole essay.
MAX_MESSAGE_CHARS = 1200


class ReactionChooser:
    """One cheap model call that turns a message into an approved emoji."""

    def __init__(
        self,
        *,
        config: Any,
        route_key: str = "memory.capture.extract",
        timeout_seconds: float = 12.0,
    ) -> None:
        from yeoman_gateway.providers.litellm_provider import LiteLLMProvider

        self._config = config
        self._route_key = str(route_key)
        self._timeout_seconds = max(1.0, float(timeout_seconds))
        route_name = config.models.routes.get(self._route_key)
        if not route_name:
            raise ValueError(f"models.routes missing '{self._route_key}'")
        profile = config.models.profiles.get(route_name)
        if profile is None:
            raise ValueError(
                f"models.routes['{self._route_key}'] points to missing profile '{route_name}'"
            )
        model = str(profile.model or "").strip()
        if not model:
            raise ValueError(f"profile '{route_name}' does not define a model")
        provider_cfg = config.get_provider(model, provider_name=profile.provider)
        if provider_cfg is None:
            raise ValueError(f"no provider with credentials for reaction route '{self._route_key}'")
        self._model = model
        self._provider = LiteLLMProvider(
            api_key=provider_cfg.api_key if provider_cfg.api_key else None,
            api_base=provider_cfg.api_base,
            default_model=model,
            extra_headers=provider_cfg.extra_headers,
        )

    async def choose(self, text: str, *, allowed: Sequence[str]) -> str | None:
        """The approved emoji for this message, or ``None`` when nothing fits."""
        import asyncio

        content = " ".join(str(text or "").split())[:MAX_MESSAGE_CHARS]
        vocabulary = [str(item).strip() for item in allowed if str(item).strip()]
        if not content or not vocabulary:
            return None
        messages = [
            {
                "role": "system",
                "content": f"{REACTION_SYSTEM_PROMPT}\nErlaubt: {' '.join(vocabulary)}",
            },
            {"role": "user", "content": content},
        ]
        try:
            response = await asyncio.wait_for(
                self._provider.chat(
                    messages=messages,
                    tools=None,
                    model=self._model,
                    max_tokens=8,
                    temperature=0.0,
                ),
                timeout=self._timeout_seconds,
            )
        except Exception as exc:
            logger.warning(
                "reaction_choice_failed route={} error_type={}", self._route_key, type(exc).__name__
            )
            return None
        chosen = allowed_reaction(getattr(response, "content", "") or "", vocabulary)
        if chosen is None:
            logger.debug("reaction_choice_empty route={}", self._route_key)
        return chosen


class ReactionAction:
    """Turns the routing decision ``react`` into exactly one reaction effect.

    Nothing else happens: no turn, no mailbox entry, no typing, no text. The effect carries
    the source message as its own lineage (routing spec, criterion 8).
    """

    def __init__(
        self,
        *,
        chooser: ReactionChooser,
        router: Any,
        allowed_emojis: Sequence[str],
        clock: Callable[[], int] | None = None,
    ) -> None:
        self._chooser = chooser
        self._router = router
        self._allowed_emojis = tuple(str(item) for item in allowed_emojis)
        self._clock = clock

    async def __call__(
        self,
        *,
        channel: str,
        chat_id: str,
        message_id: str,
        text: str,
        principal: str,
    ) -> str | None:
        """Choose and send one reaction. Returns the emoji, or ``None`` for silence."""
        emoji = await self._chooser.choose(text, allowed=self._allowed_emojis)
        if emoji is None:
            return None
        return await self.send(
            emoji=emoji,
            channel=channel,
            chat_id=chat_id,
            message_id=message_id,
            principal=principal,
        )

    async def send(
        self,
        *,
        emoji: str,
        channel: str,
        chat_id: str,
        message_id: str,
        principal: str,
    ) -> str | None:
        """Send one already-chosen reaction. Returns the emoji, or ``None`` if refused.

        The emoji is validated here as well: a caller may hand over a verdict from a model
        call (the ambient judge), and that must pass the owner's vocabulary like any other
        model-chosen face.
        """
        if not message_id:
            logger.debug("reaction_action_skipped chat={} reason=no_message_id", chat_id)
            return None
        chosen = allowed_reaction(emoji, self._allowed_emojis)
        if chosen is None:
            logger.debug("reaction_action_emoji_rejected chat={}", chat_id)
            return None
        delivered = await self._router.submit_reaction(
            SendReactionIntent(
                channel=channel,
                chat_id=chat_id,
                message_id=message_id,
                emoji=chosen,
            ),
            principal=principal,
        )
        if not delivered:
            logger.debug(
                "reaction_action_not_delivered chat={} message_id={}", chat_id, message_id
            )
            return None
        return chosen
