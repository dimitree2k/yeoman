"""Inspect model capabilities and edit profiles without serializing credentials."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import typer
from pydantic import ValidationError
from yeoman_shared.config.loader import (
    _apply_env_overrides,
    _load_dotenv,
    convert_keys,
    get_config_path,
)
from yeoman_shared.config.schema import Config

from yeoman_gateway.model_catalog import (
    OPENROUTER_MODELS,
    ModelCatalog,
    direct_card,
    openrouter_card,
    validate_reasoning,
)

from .core import app

models_app = typer.Typer(help="Model cards, supported settings, profiles and chat assignments.")
app.add_typer(models_app, name="models")


def _read_config():
    path = get_config_path()
    original = path.read_bytes()
    raw = json.loads(original)
    _load_dotenv()
    try:
        config = _apply_env_overrides(Config.model_validate(convert_keys(raw)))
    except ValidationError as exc:
        raise typer.BadParameter("Invalid config.json; repair its schema before editing models") from exc
    return path, original, raw, config


def _catalog() -> ModelCatalog:
    path = get_config_path().with_name("model-catalog.json")
    return ModelCatalog.model_validate_json(path.read_bytes()) if path.exists() else ModelCatalog()


def _write(path: Path, data: dict, original: bytes | None) -> None:
    """Replace one JSON file, preserving its raw keys and rejecting intervening edits."""
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(data, stream, indent=2, ensure_ascii=False, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        if (path.read_bytes() if path.exists() else None) != original:
            raise ValueError(f"{path.name} changed concurrently; retry")
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def _provider(config, profile) -> str:
    if profile.provider:
        return profile.provider
    selected = config.get_provider(profile.model)
    return next((name for name, value in config.providers if value is selected), "unknown")


def _profile(raw, config, name):
    normalized = next(iter(convert_keys({name: None})))
    if normalized not in config.models.profiles:
        raise typer.BadParameter(f"Unknown profile: {name}")
    key = next(k for k in raw["models"]["profiles"] if next(iter(convert_keys({k: None}))) == normalized)
    return key, config.models.profiles[normalized]


@models_app.command("refresh")
def refresh() -> None:
    """Refresh cards for configured profiles. Never change profiles or policy."""
    import httpx

    _, _, _, config = _read_config()
    path = get_config_path().with_name("model-catalog.json")
    original = path.read_bytes() if path.exists() else None
    catalog = _catalog()
    pairs = {(_provider(config, p), p.model) for p in config.models.profiles.values() if p.model}
    remote = {}
    failed = False
    if any(provider == "openrouter" for provider, _ in pairs):
        try:
            response = httpx.get(OPENROUTER_MODELS, timeout=30)
            response.raise_for_status()
            remote = {item["id"]: item for item in response.json()["data"]}
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            typer.echo("OpenRouter refresh failed; keeping previously checked cards.", err=True)
            failed = True
    cards = []
    for provider, model in sorted(pairs):
        if provider == "openrouter":
            card = (catalog.find(provider, model) if failed else
                    openrouter_card(remote[model]) if model in remote else
                    direct_card(provider, model))
        else:
            card = direct_card(provider, model)
        cards.append(card)
    _write(path, ModelCatalog(cards=cards).model_dump(), original)
    typer.echo(f"Saved {len(cards)} cards to {path}; profiles unchanged.")
    if failed:
        raise typer.Exit(1)


@models_app.command("list")
def list_models() -> None:
    """List profiles with their provider-specific reasoning choices."""
    _, _, raw, config = _read_config()
    catalog = _catalog()
    for name in raw.get("models", {}).get("profiles", {}):
        _, profile = _profile(raw, config, name)
        provider = _provider(config, profile)
        card = catalog.find(provider, profile.model or "")
        typer.echo(f"{name}: {provider} / {profile.model} | {card.reasoning} | "
                   f"effort: {', '.join(card.efforts) or '—'}")


@models_app.command("show")
def show(profile: str) -> None:
    """Show a model card, provenance and the profile's chosen settings."""
    _, _, raw, config = _read_config()
    key, value = _profile(raw, config, profile)
    card = _catalog().find(_provider(config, value), value.model or "")
    typer.echo(json.dumps({"profile": key, "settings": raw["models"]["profiles"][key],
                           "capabilities": card.model_dump()}, indent=2, ensure_ascii=False))


def _validate_profile(card, profile) -> None:
    validate_reasoning(card, profile.reasoning)
    if profile.max_tokens is not None:
        if profile.max_tokens <= 0:
            raise ValueError("maxTokens must be positive")
        if card.max_output_tokens and profile.max_tokens > card.max_output_tokens:
            raise ValueError(f"maxTokens exceeds {card.max_output_tokens}")
    budget = (profile.reasoning or {}).get("max_tokens")
    if budget and profile.max_tokens and budget >= profile.max_tokens:
        raise ValueError("Reasoning budget must be smaller than maxTokens")


@models_app.command("check")
def check() -> None:
    """Audit existing profiles without changing or disabling them."""
    from datetime import UTC, datetime, timedelta

    _, _, raw, config = _read_config()
    catalog = _catalog()
    issues = 0
    for name in raw.get("models", {}).get("profiles", {}):
        _, profile = _profile(raw, config, name)
        if profile.kind not in {"chat", "vision"}:
            continue
        card = catalog.find(_provider(config, profile), profile.model or "")
        if card.reasoning == "unknown":
            typer.echo(f"{name}: capabilities unknown; run models refresh or consult provider documentation")
        elif card.checked_at and datetime.fromisoformat(card.checked_at) < datetime.now(UTC) - timedelta(days=7):
            typer.echo(f"{name}: capability source older than 7 days ({card.checked_at})")
        try:
            _validate_profile(card, profile)
        except ValueError as exc:
            issues += 1
            typer.echo(f"{name}: {exc}")
    typer.echo(f"{issues} invalid profile(s); no changes made.")
    if issues:
        raise typer.Exit(1)


@models_app.command("configure")
def configure(
    profile: str,
    reasoning: str | None = typer.Option(None, help="default, on or off; resets previous reasoning choices"),
    effort: str | None = typer.Option(None, help="Exact effort from models show"),
    reasoning_budget: int | None = None,
    model: str | None = None,
    provider: str | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    copy_to: str | None = typer.Option(None, help="Create a separate profile instead of changing the shared one"),
) -> None:
    """Set verified reasoning options; with no options, show the model card."""
    if all(v is None for v in (reasoning, effort, reasoning_budget, model, provider,
                                temperature, max_tokens, copy_to)):
        show(profile)
        return
    path, original, raw, config = _read_config()
    key, _ = _profile(raw, config, profile)
    settings = dict(raw["models"]["profiles"][key])
    if model is not None:
        settings["model"] = model
    if provider is not None:
        from yeoman_gateway.providers.registry import find_by_name

        if find_by_name(provider) is None:
            raise typer.BadParameter(f"Unknown chat provider: {provider}")
        settings["provider"] = provider
    if temperature is not None:
        settings["temperature"] = temperature
    if max_tokens is not None:
        settings.pop("max_tokens", None)
        settings["maxTokens"] = max_tokens
    if reasoning is not None:
        if reasoning not in {"default", "on", "off"}:
            raise typer.BadParameter("reasoning: choose default, on or off")
        if reasoning == "default" and (effort is not None or reasoning_budget is not None):
            raise typer.BadParameter("Provider default cannot include effort or budget")
        settings["reasoning"] = None if reasoning == "default" else {"enabled": reasoning == "on"}
    value = dict(settings.get("reasoning") or {})
    if effort is not None:
        value.pop("max_tokens", None)
        value.pop("maxTokens", None)
        value["effort"] = effort
    if reasoning_budget is not None:
        if effort is not None:
            raise typer.BadParameter("Choose effort or reasoning budget")
        value.pop("effort", None)
        value["max_tokens"] = reasoning_budget
    if effort is not None or reasoning_budget is not None:
        settings["reasoning"] = value
    target = copy_to or key
    normalized = next(iter(convert_keys({target: None})))
    if copy_to and (not target.strip() or normalized in config.models.profiles):
        raise typer.BadParameter("copy-to must be a new, nonempty profile name")
    raw["models"]["profiles"][target] = settings
    try:
        candidate = Config.model_validate(convert_keys(raw))
        selected = candidate.models.profiles[normalized]
        card = _catalog().find(_provider(config, selected), selected.model or "")
        _validate_profile(card, selected)
        if temperature is not None:
            import math

            enabled = (selected.reasoning or {}).get("enabled", card.default_enabled)
            if card.temperature is not True or (enabled and card.temperature_with_reasoning is False):
                raise ValueError("Custom temperature is unsupported or unverified in this mode")
            if not math.isfinite(temperature) or not 0 <= temperature <= 2:
                raise ValueError("temperature must be between 0 and 2")
        _write(path, raw, original)
    except ValidationError as exc:
        raise typer.BadParameter("Profile does not satisfy the configuration schema") from exc
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    typer.echo(f"Saved profile {target}. Restart Gateway/Overseer to activate profile changes.")


@models_app.command("assign")
def assign(
    profile: str,
    channel: str = typer.Option(...),
    chat: str | None = None,
    persona: str | None = typer.Option(None, help="Assign to existing chats explicitly using this personaFile"),
) -> None:
    """Assign a validated profile to an existing chat or a persona's existing chats."""
    from yeoman_gateway.policy.loader import get_policy_path
    from yeoman_gateway.policy.schema import PolicyConfig

    if (chat is None) == (persona is None):
        raise typer.BadParameter("Choose exactly one of --chat or --persona")
    _, _, raw, config = _read_config()
    key, selected = _profile(raw, config, profile)
    if selected.kind not in {"chat", "vision"} or not selected.model:
        raise typer.BadParameter("Chat assignments require a chat/vision profile with a model")
    try:
        _validate_profile(_catalog().find(_provider(config, selected), selected.model or ""), selected)
        path = get_policy_path()
        original = path.read_bytes()
        policy = json.loads(original)
        chats = policy.get("channels", {}).get(channel, {}).get("chats", {})
        targets = [jid for jid, value in chats.items()
                   if jid == chat or (persona is not None and value.get("personaFile") == persona)]
        if not targets:
            raise ValueError("No matching existing chat; use the exact chat ID or explicit personaFile")
        for jid in targets:
            chats[jid]["modelProfile"] = key
        parsed = PolicyConfig.model_validate(policy)
        _write(path, policy, original)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    reload_note = ("Policy reloads automatically." if parsed.runtime.reload_on_change
                   else "Policy auto-reload is disabled; restart Gateway to activate.")
    typer.echo(f"Assigned {key} to {len(targets)} existing chat(s). {reload_note}")
