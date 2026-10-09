"""Explicit offline CLI for rebuilt history and historical audience proofs."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import typer

from yeoman_gateway.knowledge._history import HistoricalJournal
from yeoman_gateway.knowledge._history_audience import HistoryAudience
from yeoman_gateway.knowledge._history_reader import (
    HistoryReader,
    HistorySearchUnsupportedError,
)
from yeoman_gateway.knowledge._history_rebuild import rebuild_history, verify_history
from yeoman_gateway.knowledge.models import (
    KnowledgeError,
    TrustedAdminContext,
    TrustedReadContext,
)
from yeoman_gateway.knowledge.runtime import RuntimeKnowledgePolicy

from .knowledge_commands import knowledge_app

history_app = typer.Typer(help="Offline historical audience metadata and roster proofs")
knowledge_app.add_typer(history_app, name="history")


@history_app.command("build")
def history_build(
    collection: Path = typer.Option(..., "--collection", help="Explicit verified preservation collection"),
    target_home: Path = typer.Option(..., "--target-home", help="New empty isolated history target"),
    bridge_package_dir: Path | None = typer.Option(
        None, "--bridge-package-dir", help="Optional offline bridge decoder package"
    ),
    rosters: Path | None = typer.Option(
        None, "--rosters", help="Optional owner-supplied roster metadata (display only)"
    ),
) -> None:
    """Reconcile one explicit preservation collection into an isolated journal."""
    _isolated(collection, target_home, bridge_package_dir, rosters)
    report = rebuild_history(
        collection=collection,
        target_home=target_home,
        bridge_package_dir=bridge_package_dir,
        rosters=rosters,
    )
    typer.echo(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2))


@history_app.command("verify")
def history_verify(
    target_home: Path = typer.Option(..., "--target-home", help="Explicit rebuilt history target"),
    knowledge_snapshot: Path | None = typer.Option(
        None, "--knowledge-snapshot", help="Optional source-reference snapshot SQLite file"
    ),
) -> None:
    """Check the isolated journal and exact cited event/revision closure."""
    _isolated(target_home, knowledge_snapshot)
    report = verify_history(target_home=target_home, knowledge_snapshot=knowledge_snapshot)
    typer.echo(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2))


@history_app.command("coverage")
def history_coverage(
    records: Path = typer.Option(..., "--records", help="Metadata-only event JSON"),
    target_home: Path = typer.Option(..., "--target-home", help="Explicit isolated history target"),
) -> None:
    """Report event-time and audience coverage without rendering message bodies."""
    _isolated(target_home)
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
    _isolated(target_home)
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
    _isolated(target_home)
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
    _isolated(target_home)
    with HistoricalJournal(target_home, create=False) as journal:
        HistoryAudience(journal).revoke(proof_id, context=context, policy=policy_authority)
    typer.echo(json.dumps({"status": "revoked", "proof_id": proof_id}, sort_keys=True))


@history_app.command("search")
def history_search(
    target_home: Path = typer.Option(..., "--target-home", help="Explicit rebuilt history target"),
    query: str = typer.Option(..., "--query", help="Search terms"),
    channel: str = typer.Option(..., "--channel"),
    account: str = typer.Option(..., "--account"),
    chat_id: str = typer.Option(..., "--chat-id"),
    authorization: Path = typer.Option(..., "--authorization", help="Trusted read context JSON"),
    policy: Path = typer.Option(..., "--policy", help="Explicit current policy and membership snapshot"),
    since_ms: int | None = typer.Option(None, "--since-ms"),
    until_ms: int | None = typer.Option(None, "--until-ms"),
    native_id: str | None = typer.Option(None, "--native-id"),
    limit: int = typer.Option(30, "--limit", min=1, max=100),
) -> None:
    """Search only the owned FTS projection after current rights are checked."""
    context, authority = _read_authorization_context(
        authorization, policy, channel=channel, account=account, chat_id=chat_id
    )
    _isolated(target_home)
    with HistoricalJournal(target_home, create=False) as journal:
        reader = HistoryReader(journal, policy=authority)
        try:
            results = reader.search(
                query,
                context=context,
                channel=channel,
                account=account,
                chat_id=chat_id,
                since_ms=since_ms,
                until_ms=until_ms,
                native_id=native_id,
                limit=limit,
            )
        except HistorySearchUnsupportedError:
            _emit_history_receipts(
                {"status": "unsupported", "capability": "sqlite_fts5"}, ()
            )
            return
    _emit_history_receipts(
        {
            "status": "ok",
            "command": "search",
            "query": query,
            "scope": {"channel": channel, "account": account, "chat_id": chat_id},
            "count": len(results),
        },
        results,
    )


@history_app.command("recent")
def history_recent(
    target_home: Path = typer.Option(..., "--target-home", help="Explicit rebuilt history target"),
    channel: str = typer.Option(..., "--channel"),
    account: str = typer.Option(..., "--account"),
    chat_id: str = typer.Option(..., "--chat-id"),
    before_ms: int = typer.Option(..., "--before-ms"),
    authorization: Path = typer.Option(..., "--authorization", help="Trusted read context JSON"),
    policy: Path = typer.Option(..., "--policy", help="Explicit current policy and membership snapshot"),
    limit: int = typer.Option(8, "--limit", min=1, max=100),
) -> None:
    """Show the latest authorized canonical events for one explicit account scope."""
    context, authority = _read_authorization_context(
        authorization, policy, channel=channel, account=account, chat_id=chat_id
    )
    _isolated(target_home)
    with HistoricalJournal(target_home, create=False) as journal:
        results = HistoryReader(journal, policy=authority).recent(
            context=context,
            channel=channel,
            account=account,
            chat_id=chat_id,
            before_ms=before_ms,
            limit=limit,
        )
    _emit_history_receipts(
        {
            "status": "ok",
            "command": "recent",
            "scope": {"channel": channel, "account": account, "chat_id": chat_id},
            "count": len(results),
        },
        results,
    )


@history_app.command("excerpt")
def history_excerpt(
    event_id: str = typer.Option(..., "--event-id"),
    revision: int = typer.Option(..., "--revision", min=1),
    target_home: Path = typer.Option(..., "--target-home", help="Explicit rebuilt history target"),
    channel: str = typer.Option(..., "--channel"),
    account: str = typer.Option(..., "--account"),
    chat_id: str = typer.Option(..., "--chat-id"),
    authorization: Path = typer.Option(..., "--authorization", help="Trusted read context JSON"),
    policy: Path = typer.Option(..., "--policy", help="Explicit current policy and membership snapshot"),
) -> None:
    """Resolve one exact canonical/source alias and return its safe locator excerpt."""
    context, authority = _read_authorization_context(
        authorization, policy, channel=channel, account=account, chat_id=chat_id
    )
    _isolated(target_home)
    with HistoricalJournal(target_home, create=False) as journal:
        result = HistoryReader(journal, policy=authority).excerpt(
            event_id, revision, context=context
        )
    if result is not None and result.get("account") != account:
        result = None
    _emit_history_receipts(
        {
            "status": "ok",
            "command": "excerpt",
            "event_id": event_id,
            "revision": revision,
            "scope": {"channel": channel, "account": account, "chat_id": chat_id},
            "count": int(result is not None),
        },
        () if result is None else (result,),
    )


@history_app.command("reindex")
def history_reindex(
    target_home: Path = typer.Option(..., "--target-home", help="Explicit rebuilt history target"),
) -> None:
    """Rebuild the private FTS projection from canonical journal rows."""
    _isolated(target_home)
    with HistoricalJournal(target_home, create=False) as journal:
        report = HistoryReader(
            journal,
            policy=RuntimeKnowledgePolicy(engine=None),
        ).reindex()
    typer.echo(json.dumps({"metadata_receipt": report, "text_receipt": []}, sort_keys=True))


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


class _SnapshotRegistry:
    def __init__(self, *, channel: str, account: str, chat_id: str, members: list[str], revision: str) -> None:
        self.channel = channel
        self.account = account
        self.chat_id = chat_id
        self.members = members
        self.revision = revision

    def get_chat(self, channel: str, chat_id: str) -> dict[str, Any] | None:
        if (channel, chat_id) != (self.channel, self.chat_id):
            return None
        return {
            "metadata": {"participants": self.members},
            "last_sync_at": self.revision,
        }


def _read_authorization_context(
    authorization_path: Path,
    policy_path: Path,
    *,
    channel: str,
    account: str,
    chat_id: str,
) -> tuple[TrustedReadContext, RuntimeKnowledgePolicy]:
    """Build a read context only from matching explicit authorization snapshots."""
    authorization = _load_object(authorization_path, "authorization")
    snapshot = _load_object(policy_path, "policy snapshot")
    revision = snapshot.get("policy_revision", snapshot.get("revision"))
    if isinstance(revision, bool) or not isinstance(revision, int):
        raise KnowledgeError("unauthorized", "policy snapshot revision is invalid")
    if authorization.get("policy_revision") != revision:
        raise KnowledgeError("stale_revision", "authorization does not match policy snapshot")
    authorization_ref = authorization.get("authorization_ref")
    principal = authorization.get("principal_id")
    recipients = authorization.get("recipient_principals")
    if (
        not isinstance(authorization_ref, str)
        or not authorization_ref.strip()
        or not isinstance(principal, str)
        or not isinstance(recipients, list)
        or not recipients
        or any(not isinstance(item, str) or not item.strip() for item in recipients)
    ):
        raise KnowledgeError("unauthorized", "trusted read authorization is incomplete")
    records = snapshot.get("memberships")
    if not isinstance(records, list):
        raise KnowledgeError("unauthorized", "policy snapshot memberships are missing")
    matches = [
        item for item in records
        if isinstance(item, dict)
        and (item.get("channel"), item.get("account"), item.get("chat_id"))
        == (channel, account, chat_id)
    ]
    if len(matches) != 1:
        raise KnowledgeError("unauthorized", "policy snapshot must contain one exact chat membership")
    row = matches[0]
    members, membership_revision = row.get("members"), row.get("revision")
    if (
        not isinstance(members, list)
        or any(not isinstance(item, str) or not item.strip() for item in members)
        or not isinstance(membership_revision, str)
        or not membership_revision.strip()
        or authorization.get("membership_revision") != membership_revision
    ):
        raise KnowledgeError("stale_revision", "read authorization does not match current membership")
    if authorization.get("channel") != channel or authorization.get("chat_id") != chat_id:
        raise KnowledgeError("unauthorized", "read authorization scope does not match request")
    direct = authorization.get("is_direct")
    if not isinstance(direct, bool):
        raise KnowledgeError("unauthorized", "read authorization must specify direct-chat scope")
    try:
        context = TrustedReadContext(
            principal_id=principal,
            channel=channel,
            chat_id=chat_id,
            recipient_principals=frozenset(recipients),
            membership_revision=membership_revision,
            policy_revision=revision,
            purpose=str(authorization.get("purpose") or "reply"),
            now_ms=authorization.get("now_ms"),
            is_direct=direct,
            # A caller-supplied owner field is intentionally ignored.
            owner=False,
        )
    except (TypeError, ValueError, KnowledgeError) as exc:
        raise KnowledgeError("unauthorized", "trusted read context fields are invalid") from exc
    registry = _SnapshotRegistry(
        channel=channel,
        account=account,
        chat_id=chat_id,
        members=members,
        revision=membership_revision,
    )
    return context, RuntimeKnowledgePolicy(
        engine=None,
        chat_registry=registry,
        policy_revision=revision,
    )


def _emit_history_receipts(metadata: dict[str, Any], results: Any) -> None:
    typer.echo(
        json.dumps(
            {"metadata_receipt": metadata, "text_receipt": list(results)},
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        )
    )


def _isolated(*paths: Path | None) -> None:
    from yeoman_gateway.history.export import require_isolated_paths
    require_isolated_paths(*(path for path in paths if path is not None))
