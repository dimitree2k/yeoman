"""Explicit offline CLI for historical audience coverage and owner attestations."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import typer

from yeoman_gateway.knowledge._history import HistoricalJournal
from yeoman_gateway.knowledge._history_audience import HistoryAudience
from yeoman_gateway.knowledge.models import KnowledgeError, TrustedAdminContext
from yeoman_gateway.knowledge.runtime import RuntimeKnowledgePolicy

from .knowledge_commands import knowledge_app

history_app = typer.Typer(help="Offline historical audience metadata and roster proofs")
knowledge_app.add_typer(history_app, name="history")


@history_app.command("coverage")
def history_coverage(
    records: Path = typer.Option(..., "--records", help="Metadata-only event JSON"),
    target_home: Path = typer.Option(..., "--target-home", help="Explicit isolated history target"),
) -> None:
    """Report event-time and audience coverage without rendering message bodies."""
    with HistoricalJournal(target_home, create=True) as journal:
        report = HistoryAudience(journal).coverage(_load_events(records))
    typer.echo(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2))


@history_app.command("roster-review")
def history_roster_review(
    records: Path = typer.Option(..., "--records", help="Metadata-only event JSON"),
    target_home: Path = typer.Option(..., "--target-home", help="Explicit isolated history target"),
    current_members: Path | None = typer.Option(
        None, "--current-members", help="Optional display-only current roster JSON"
    ),
) -> None:
    """Review historical evidence; current membership is display-only metadata."""
    current = _load_json(current_members) if current_members is not None else None
    if current is not None and not isinstance(current, dict):
        raise typer.BadParameter("current-members must contain a JSON object")
    with HistoricalJournal(target_home, create=True) as journal:
        report = HistoryAudience(journal).roster_review(
            _load_events(records), current_members=current
        )
    typer.echo(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2))


@history_app.command("roster-attest")
def history_roster_attest(
    target_home: Path = typer.Option(..., "--target-home", help="Explicit isolated history target"),
    roster: Path = typer.Option(..., "--roster", help="Explicit roster and exact confirmation JSON"),
    authorization: Path = typer.Option(..., "--authorization", help="Trusted admin context JSON"),
    policy: Path = typer.Option(..., "--policy", help="Explicit policy snapshot JSON"),
) -> None:
    """Store one exact owner-confirmed roster interval in the isolated target."""
    payload = _load_object(roster, "roster")
    context, policy_authority = _authorization_context(authorization, policy)
    with HistoricalJournal(target_home, create=True) as journal:
        proof_id = HistoryAudience(journal).attest(
            channel=payload.get("channel"),
            account=payload.get("account"),
            chat_id=payload.get("chat_id"),
            members=payload.get("members"),
            valid_from_ms=payload.get("valid_from_ms"),
            valid_until_ms=payload.get("valid_until_ms"),
            confirmation=payload.get("confirmation"),
            context=context,
            policy=policy_authority,
        )
    typer.echo(json.dumps({"status": "attested", "proof_id": proof_id}, sort_keys=True))


@history_app.command("roster-revoke")
def history_roster_revoke(
    target_home: Path = typer.Option(..., "--target-home", help="Explicit isolated history target"),
    proof_id: str = typer.Option(..., "--proof-id", help="Exact audience proof id"),
    authorization: Path = typer.Option(..., "--authorization", help="Trusted admin context JSON"),
    policy: Path = typer.Option(..., "--policy", help="Explicit policy snapshot JSON"),
) -> None:
    """Revoke one exact owner-attested proof after rechecking policy authority."""
    context, policy_authority = _authorization_context(authorization, policy)
    with HistoricalJournal(target_home, create=False) as journal:
        HistoryAudience(journal).revoke(proof_id, context=context, policy=policy_authority)
    typer.echo(json.dumps({"status": "revoked", "proof_id": proof_id}, sort_keys=True))


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise typer.BadParameter(f"cannot read JSON input: {path}") from exc


def _load_object(path: Path, name: str) -> dict[str, Any]:
    value = _load_json(path)
    if not isinstance(value, dict):
        raise typer.BadParameter(f"{name} must contain a JSON object")
    return value


def _load_events(path: Path) -> list[dict[str, Any]]:
    value = _load_json(path)
    if isinstance(value, dict):
        value = value.get("events")
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise typer.BadParameter("records must be a JSON array or an object with an events array")
    return value


def _authorization_context(
    authorization_path: Path, policy_path: Path
) -> tuple[TrustedAdminContext, RuntimeKnowledgePolicy]:
    authorization = _load_object(authorization_path, "authorization")
    snapshot = _load_object(policy_path, "policy snapshot")
    policy_data = snapshot.get("policy") if isinstance(snapshot.get("policy"), dict) else snapshot
    owners = policy_data.get("owners") if isinstance(policy_data, dict) else None
    revision = snapshot.get("policy_revision", snapshot.get("revision"))
    actor = authorization.get("actor_principal")
    authorization_ref = authorization.get("authorization_ref")
    authorization_revision = authorization.get("policy_revision")
    if (
        not isinstance(owners, dict)
        or isinstance(revision, bool)
        or not isinstance(revision, int)
        or isinstance(authorization_revision, bool)
        or not isinstance(authorization_revision, int)
    ):
        raise KnowledgeError("unauthorized", "policy snapshot or authorization is incomplete")
    owner_principals: set[str] = set()
    for channel, raw_owners in owners.items():
        if not isinstance(channel, str) or not isinstance(raw_owners, list):
            raise KnowledgeError("unauthorized", "policy snapshot owners are invalid")
        for raw_owner in raw_owners:
            if not isinstance(raw_owner, str) or not raw_owner:
                raise KnowledgeError("unauthorized", "policy snapshot owners are invalid")
            owner_principals.add(
                raw_owner if raw_owner.startswith(f"{channel}:") else f"{channel}:{raw_owner}"
            )
    if not isinstance(actor, str) or not isinstance(authorization_ref, str):
        raise KnowledgeError("unauthorized", "authorization identity or reference is missing")
    context = TrustedAdminContext(
        actor_principal=actor,
        policy_revision=authorization_revision,
        authorization_ref=authorization_ref,
        # This flag is derived from the explicit policy snapshot. A JSON owner flag is ignored.
        owner=actor in owner_principals,
    )
    authority = RuntimeKnowledgePolicy(
        engine=None,
        policy_revision=revision,
        admin_principals=frozenset(owner_principals),
    )
    authority.require_admin(context)
    return context, authority
