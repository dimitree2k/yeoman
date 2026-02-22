"""Centralized opinionated defaults for generated/migrated config files."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

DEFAULT_ASSISTANT_MODEL = "anthropic/claude-opus-4-5"
DEFAULT_SUBAGENT_MODEL = "openai/gpt-4o-mini"
DEFAULT_VISION_MODEL = "google/gemini-2.5-flash-lite"
DEFAULT_ASR_MODEL = "whisper-large-v3"
DEFAULT_TTS_MODEL = "tts-1"

DEFAULT_MODEL_PROFILES: dict[str, dict[str, Any]] = {
    "assistant_default": {
        "kind": "chat",
        "model": DEFAULT_ASSISTANT_MODEL,
        "max_tokens": 8192,
        "temperature": 0.7,
    },
    "subagent_default": {
        "kind": "chat",
        "model": DEFAULT_SUBAGENT_MODEL,
        "max_tokens": 4096,
        "temperature": 0.3,
        "timeout_ms": 60000,
    },
    "vision_whatsapp_cheap": {
        "kind": "vision",
        "model": DEFAULT_VISION_MODEL,
        "max_tokens": 160,
        "temperature": 0.1,
        "timeout_ms": 12000,
    },
    "asr_default": {
        "kind": "asr",
        "provider": "groq_whisper",
        "model": DEFAULT_ASR_MODEL,
        "timeout_ms": 60000,
    },
    "tts_default": {
        "kind": "tts",
        "provider": "openai_tts",
        "model": DEFAULT_TTS_MODEL,
        "timeout_ms": 30000,
    },
    "memory_embed_fast": {
        "kind": "embedding",
        "model": "openai/text-embedding-3-small",
        "timeout_ms": 12000,
    },
    "memory_capture_fast": {
        "kind": "chat",
        "model": "openai/gpt-4o-mini",
        "max_tokens": 700,
        "temperature": 0.0,
        "timeout_ms": 15000,
    },
}

DEFAULT_MODEL_ROUTES: dict[str, str] = {
    "assistant.reply": "assistant_default",
    "vision.describe_image": "vision_whatsapp_cheap",
    "asr.transcribe_audio": "asr_default",
    "tts.speak": "tts_default",
    "whatsapp.vision.describe_image": "vision_whatsapp_cheap",
    "whatsapp.asr.transcribe_audio": "asr_default",
    "whatsapp.tts.speak": "tts_default",
    "memory.embed": "memory_embed_fast",
    "memory.capture.extract": "memory_capture_fast",
}

DEFAULT_WHATSAPP_MEDIA: dict[str, Any] = {
    "enabled": True,
    "incoming_dir": "~/.nanobot/var/media/incoming/whatsapp",
    "outgoing_dir": "~/.nanobot/var/media/outgoing/whatsapp",
    "retention_days": 30,
    "describe_images": True,
    "pass_image_to_assistant": False,
    "max_image_bytes_mb": 8,
    "persist_incoming_audio": False,
    "transcribe_audio": True,
    "max_audio_bytes_mb": 25,
    "delete_audio_after_transcription": True,
    "max_asr_concurrency": 2,
    "max_tts_concurrency": 2,
}

DEFAULT_WHATSAPP_REPLY_CONTEXT: dict[str, Any] = {
    "window_limit": 6,
    "line_max_chars": 256,
    "ambient_window_limit": 8,
}

DEFAULT_MEMORY: dict[str, Any] = {
    "enabled": True,
    "mode": "primary",
    "db_path": "~/.nanobot/data/memory/memory.db",
    "capture": {
        "enabled": True,
        "channels": ["cli", "telegram", "whatsapp", "discord", "feishu"],
        "capture_assistant": False,
        "queue_maxsize": 1000,
        "mode": "hybrid",
        "extract_route": "memory.capture.extract",
        "max_candidates_per_message": 4,
        "min_confidence": 0.6,
        "min_salience": 0.45,
    },
    "recall": {
        "max_results": 8,
        "max_prompt_chars": 2400,
        "lexical_limit": 24,
        "vector_limit": 24,
        "vector_candidate_limit": 256,
        "include_trace": True,
    },
    "embedding": {
        "enabled": True,
        "route": "memory.embed",
    },
    "scoring": {
        "lexical_weight": 0.45,
        "vector_weight": 0.35,
        "salience_weight": 0.1,
        "recency_weight": 0.1,
    },
    "acl": {
        "owner_only_preference": True,
    },
    "wal": {
        "enabled": True,
        "state_dir": "memory/session-state",
    },
}

DEFAULT_SECURITY: dict[str, Any] = {
    "enabled": True,
    "fail_mode": "closed",
    "stages": {
        "input": True,
        "tool": True,
        "output": True,
    },
    "block_user_message": "😂",
    "strict_profile": True,
    "redact_placeholder": "[REDACTED]",
}


def default_model_profiles() -> dict[str, dict[str, Any]]:
    """Return a deep-copied models.profiles payload."""
    return deepcopy(DEFAULT_MODEL_PROFILES)


def default_model_routes() -> dict[str, str]:
    """Return a copied models.routes payload."""
    return dict(DEFAULT_MODEL_ROUTES)


def default_whatsapp_media() -> dict[str, Any]:
    """Return a deep-copied channels.whatsapp.media payload."""
    return deepcopy(DEFAULT_WHATSAPP_MEDIA)


def default_whatsapp_reply_context() -> dict[str, Any]:
    """Return a copied channels.whatsapp reply-context payload."""
    return dict(DEFAULT_WHATSAPP_REPLY_CONTEXT)


def default_memory() -> dict[str, Any]:
    """Return a deep-copied memory payload."""
    return deepcopy(DEFAULT_MEMORY)


def default_security() -> dict[str, Any]:
    """Return a deep-copied security payload."""
    return deepcopy(DEFAULT_SECURITY)


def apply_missing_defaults(snake_config: dict[str, Any]) -> None:
    """Inject missing config defaults without overriding existing user values."""
    if not isinstance(snake_config, dict):
        return

    assistant_model = _resolve_assistant_model(snake_config)

    models = snake_config.setdefault("models", {})
    if isinstance(models, dict):
        profiles = models.setdefault("profiles", {})
        if isinstance(profiles, dict):
            for name, payload in default_model_profiles().items():
                seeded = deepcopy(payload)
                if name == "assistant_default":
                    seeded["model"] = assistant_model
                current = profiles.get(name)
                if not isinstance(current, dict):
                    profiles[name] = seeded
                else:
                    for k, v in seeded.items():
                        current.setdefault(k, v)
                    if name == "assistant_default":
                        current["model"] = str(current.get("model") or assistant_model)

        routes = models.setdefault("routes", {})
        if isinstance(routes, dict):
            for route, profile_name in default_model_routes().items():
                routes.setdefault(route, profile_name)

    channels = snake_config.setdefault("channels", {})
    if isinstance(channels, dict):
        whatsapp = channels.setdefault("whatsapp", {})
        if isinstance(whatsapp, dict):
            whatsapp.setdefault("accept_from_me", False)
            media = whatsapp.setdefault("media", {})
            if isinstance(media, dict):
                for k, v in default_whatsapp_media().items():
                    media.setdefault(k, v)
            for k, v in default_whatsapp_reply_context().items():
                whatsapp.setdefault(k, v)

    memory = snake_config.setdefault("memory", {})
    if isinstance(memory, dict):
        seeded_memory = default_memory()
        for k, v in seeded_memory.items():
            if isinstance(v, dict):
                nested = memory.setdefault(k, {})
                if isinstance(nested, dict):
                    for nk, nv in v.items():
                        nested.setdefault(nk, nv)
                else:
                    memory[k] = deepcopy(v)
            elif isinstance(v, list):
                if not isinstance(memory.get(k), list):
                    memory[k] = list(v)
            else:
                memory.setdefault(k, v)

    security = snake_config.setdefault("security", {})
    if isinstance(security, dict):
        seeded_security = default_security()
        for k, v in seeded_security.items():
            if isinstance(v, dict):
                nested = security.setdefault(k, {})
                if isinstance(nested, dict):
                    for nk, nv in v.items():
                        nested.setdefault(nk, nv)
                else:
                    security[k] = deepcopy(v)
            else:
                security.setdefault(k, v)


def _resolve_assistant_model(snake_config: dict[str, Any]) -> str:
    agents = snake_config.get("agents")
    if not isinstance(agents, dict):
        return DEFAULT_ASSISTANT_MODEL
    defaults = agents.get("defaults")
    if not isinstance(defaults, dict):
        return DEFAULT_ASSISTANT_MODEL
    model = defaults.get("model")
    if isinstance(model, str) and model.strip():
        return model.strip()
    return DEFAULT_ASSISTANT_MODEL
