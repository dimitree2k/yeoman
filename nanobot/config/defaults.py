"""Centralized opinionated defaults for generated/migrated config files."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

DEFAULT_ASSISTANT_MODEL = "anthropic/claude-opus-4-5"
DEFAULT_VISION_MODEL = "google/gemini-2.5-flash-lite"
DEFAULT_ASR_MODEL = "whisper-large-v3"

DEFAULT_MODEL_PROFILES: dict[str, dict[str, Any]] = {
    "assistant_default": {
        "kind": "chat",
        "model": DEFAULT_ASSISTANT_MODEL,
        "max_tokens": 8192,
        "temperature": 0.7,
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
}

DEFAULT_MODEL_ROUTES: dict[str, str] = {
    "assistant.reply": "assistant_default",
    "vision.describe_image": "vision_whatsapp_cheap",
    "asr.transcribe_audio": "asr_default",
    "whatsapp.vision.describe_image": "vision_whatsapp_cheap",
    "whatsapp.asr.transcribe_audio": "asr_default",
}

DEFAULT_WHATSAPP_MEDIA: dict[str, Any] = {
    "enabled": True,
    "incoming_dir": "~/.nanobot/media/incoming/whatsapp",
    "outgoing_dir": "~/.nanobot/media/outgoing/whatsapp",
    "retention_days": 30,
    "describe_images": True,
    "pass_image_to_assistant": False,
    "max_image_bytes_mb": 8,
}

DEFAULT_WHATSAPP_REPLY_CONTEXT: dict[str, Any] = {
    "window_limit": 6,
    "line_max_chars": 256,
}

DEFAULT_MEMORY: dict[str, Any] = {
    "enabled": True,
    "db_path": "~/.nanobot/memory/longterm.db",
    "backend": "sqlite_fts",
    "recall": {
        "max_results": 8,
        "max_prompt_chars": 2400,
        "user_preference_layer_results": 2,
    },
    "capture": {
        "enabled": True,
        "mode": "heuristic",
        "min_confidence": 0.78,
        "min_importance": 0.60,
        "channels": ["cli", "telegram", "whatsapp", "discord", "feishu"],
        "capture_assistant": False,
        "max_entries_per_turn": 4,
    },
    "retention": {
        "episodic_days": 90,
        "fact_days": 3650,
        "preference_days": 3650,
        "decision_days": 3650,
    },
    "wal": {
        "enabled": True,
        "state_dir": "memory/session-state",
    },
    "embedding": {
        "enabled": False,
        "backend": "reserved_hybrid",
    },
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
