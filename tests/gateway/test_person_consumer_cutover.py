"""Native-history-backed consumer lookups refuse unresolved or unreleased identities.

Consumer reads use a prepared Knowledge store and native projected identity facts.
Legacy contacts are poisoned wherever the consumer has a Knowledge facade, so no
fallback can silently answer a lookup. All identifiers are synthetic.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from yeoman_gateway.agent.tools.resolve_contact import (
    ResolveContactTool,
)
from yeoman_gateway.core.intents import SendOutboundIntent
from yeoman_gateway.core.models import InboundEvent
from yeoman_gateway.core.pipeline import PipelineContext
from yeoman_gateway.ipc.a2a_invoke import resolve_whatsapp_recipient
from yeoman_gateway.knowledge.models import Identifier
from yeoman_gateway.pipeline.outbound import OutboundMiddleware
from yeoman_gateway.pipeline.reply_context import ReplyContextMiddleware

from tests.gateway.history_test_helpers import ProjectedHistory

CHAT = "finance@g.us"
FRANK_PHONE = "4917632625469@s.whatsapp.net"
OWNER = "whatsapp:4910000000001"


class _PoisonedLegacyContacts:
    """A legacy cache whose every use fails the test that installed it."""

    def __getattr__(self, name: str) -> object:
        raise AssertionError(
            f"the legacy contacts cache was consulted for {name!r} although knowledge is wired"
        )


@pytest.fixture
def knowledge(tmp_path: Path):
    """Prepared-v3 Knowledge joined to a real projected History lease."""
    import json
    import sqlite3

    from yeoman_gateway.history.context import history_knowledge_scope
    from yeoman_gateway.history.queries import HistoryQueries
    from yeoman_gateway.knowledge._history_upgrade import upgrade_history_knowledge
    from yeoman_gateway.knowledge._store import KnowledgeStore
    from yeoman_gateway.knowledge.api import open_knowledge_store
    from yeoman_gateway.knowledge.authority import FakePolicyAuthority, PolicyMembership
    from yeoman_gateway.knowledge.models import Identifier
    from yeoman_gateway.knowledge.runtime import RuntimeKnowledgeSources

    history = ProjectedHistory(tmp_path / "native-consumer-history")
    history.add_contact(
        identifiers=["10009@s.whatsapp.net"], name="Synthetic", timestamp=1_700_000_000
    )
    initial = history.snapshot()
    frozen_person = HistoryQueries(initial).resolve_identifier(
        "10009@s.whatsapp.net", at_ms=1_700_000_000_000, time_basis="native"
    )
    initial.close()
    assert frozen_person

    source_path = tmp_path / "knowledge-v2.db"
    store = KnowledgeStore(source_path)
    store.set_meta("migration_complete", "1")
    store.execute(
        "INSERT INTO contacts(id,display_name,created_at,updated_at) VALUES (?,?,'date','date')",
        (frozen_person, "Synthetic"),
    )
    store.execute(
        "INSERT INTO knowledge_identifier_bindings(binding_id,channel,kind,namespace,value,person_id,"
        "valid_from_ms,evidence_ref,mapping_verified,created_ms,updated_ms)"
        " VALUES ('frozen-binding','whatsapp','phone_jid','whatsapp','10009@s.whatsapp.net',?,1,'frozen-proof',1,1,1)",
        (frozen_person,),
    )
    # A stale compatibility projection is retained in this pre-cutover database.
    # Native-history consumers must ignore it after schema-3 publication.
    store.execute(
        "INSERT INTO contact_identifiers(channel,identifier,contact_id,kind) "
        "VALUES ('whatsapp','4917000000000@s.whatsapp.net',?,'phone_jid')",
        (frozen_person,),
    )
    store.execute(
        "INSERT INTO contact_aliases(contact_id,alias,source,first_seen,last_seen,status,address_allowed,normalized_alias)"
        " VALUES (?,'Synthetic','owner_confirmed','date','date','confirmed',1,'synthetic')",
        (frozen_person,),
    )
    store.commit_if_idle()
    store.close()
    target_path = tmp_path / "knowledge-v3.db"
    upgrade_history_knowledge(source=source_path, target=target_path)
    with sqlite3.connect(target_path) as db:
        db.execute(
            "INSERT INTO knowledge_history_capture_state VALUES ('handover',?,1)",
            (json.dumps({"version": 1, "generation": 1, "sources": [], "pending": [],
                         "processed": [], "legacy_boundary": [0, ""]}),),
        )

    class _ConsumerPolicy(FakePolicyAuthority):
        def admin_actor(self) -> str:
            return OWNER

        def membership(self, context):
            return PolicyMembership(
                members=frozenset({context.principal_id}), revision="consumer-cutover"
            )

    service = open_knowledge_store(
        target_path,
        workspace_id="consumer-cutover-tests",
        source_authority=RuntimeKnowledgeSources(),
        policy_authority=_ConsumerPolicy(admins={OWNER}, capture_actors={OWNER}),
    )

    class _ScopedKnowledge:
        def __getattr__(self, name):
            value = getattr(service, name)
            if not callable(value) or name == "close":
                return value

            def invoke(*args, **kwargs):
                snapshot = history.snapshot()
                try:
                    with history_knowledge_scope(snapshot, service):
                        return value(*args, **kwargs)
                finally:
                    snapshot.close()

            return invoke

    scoped = _ScopedKnowledge()

    def issue_person(
        value: str,
        *,
        kind: str = "phone_jid",
        name: str | None = None,
        extra: tuple[Identifier, ...] = (),
        channel: str = "whatsapp",
    ) -> str:
        if channel != "whatsapp" or kind != "phone_jid":
            raise AssertionError("fixture accepts only native WhatsApp phone contacts")
        history.add_contact(
            identifiers=[value, *(item.value for item in extra)],
            name=name or value,
            timestamp=1_700_000_001,
        )
        resolved = scoped.resolve_identifier(
            Identifier("whatsapp", "phone_jid", value, "whatsapp")
        )
        assert resolved.person_id, resolved.reason
        return resolved.person_id

    scoped.issue_person = issue_person
    try:
        yield scoped
    finally:
        service.close()
        history.close()


def _event(**overrides: object) -> InboundEvent:
    defaults: dict[str, object] = {
        "channel": "whatsapp",
        "chat_id": CHAT,
        "sender_id": FRANK_PHONE,
        "content": "hello",
        "message_id": "msg-001",
        "is_group": True,
    }
    defaults.update(overrides)
    return InboundEvent(**defaults)




# ── resolve_contact: the responder's delivery resolver ─────────────────────────


# The former bare Knowledge-only WhatsApp resolver unit cases are retired: group
# resolution now requires a projected current roster. The live projected resolver
# behavior, including member and nonmember authorization, is covered in
# test_contacts_tool.py.


@pytest.mark.asyncio
async def test_the_tool_reports_an_unresolved_person_instead_of_a_legacy_match(
    knowledge, tmp_path: Path
) -> None:
    tool = ResolveContactTool(

        knowledge=knowledge,
    )
    tool.set_context("whatsapp", CHAT)

    from yeoman_gateway.processing.tool_context import (
        ToolInvocationContext,
        reset_tool_context,
        set_tool_context,
    )

    history = ProjectedHistory(tmp_path / "unresolved-contact-history")
    async with history.turn() as snapshot:
        token = set_tool_context(ToolInvocationContext(
            channel="whatsapp", chat_id=CHAT, history_snapshot=snapshot,
        ))
        try:
            payload = await tool.execute(query="Frank")
        finally:
            reset_tool_context(token)
    history.close()

    assert '"ok": false' in payload
    assert "contact_not_resolved" in payload






# ── outbound mentions ──────────────────────────────────────────────────────────


async def _run_outbound(middleware: OutboundMiddleware, reply: str) -> PipelineContext:
    ctx = PipelineContext(event=_event())
    ctx.reply = reply
    ctx.decision = type(
        "D", (), {"when_to_reply_mode": "all", "voice_output_mode": "text"}
    )()

    async def next_fn(c: PipelineContext) -> None:
        pass

    await middleware(ctx, next_fn)
    return ctx


def _mention_candidates(ctx: PipelineContext) -> list[str]:
    intents = [item for item in ctx.intents if isinstance(item, SendOutboundIntent)]
    assert len(intents) == 1
    return list(intents[0].event.metadata.get("mention_candidates", []))


@pytest.mark.asyncio
async def test_frozen_native_release_produces_one_phone_mention_candidate(knowledge) -> None:
    person = knowledge.issue_person("10009@s.whatsapp.net", name="Synthetic")
    assert person

    ctx = await _run_outbound(OutboundMiddleware(knowledge=knowledge), "Hi @Synthetic")

    assert _mention_candidates(ctx) == ["10009@s.whatsapp.net"]


@pytest.mark.asyncio
async def test_a_mention_of_an_unproven_name_adds_no_candidate(knowledge) -> None:
    middleware = OutboundMiddleware(knowledge=knowledge)

    ctx = await _run_outbound(middleware, "Hey @Frank, alles gut?")

    assert _mention_candidates(ctx) == []


# ── reply context ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_reply_context_speakers_come_from_proven_bindings(
    knowledge, tmp_path: Path
) -> None:
    archive = ProjectedHistory(tmp_path / "reply-history")
    knowledge.issue_person(FRANK_PHONE, name="Frank Taeger")
    archive.add_contact(identifiers=[FRANK_PHONE], name="Frank Taeger", timestamp=1)
    base = int(datetime.now(UTC).timestamp()) - 600
    archive.add_message(
        channel="whatsapp",
        chat_id=CHAT,
        message_id="m-1",
        participant=None,
        sender_id=FRANK_PHONE,
        text="hey there",
        timestamp=base,
        sender_name="push-name-must-not-win",
    )
    archive.add_message(
        channel="whatsapp",
        chat_id=CHAT,
        message_id="msg-002",
        participant=None,
        sender_id="4917000000000@s.whatsapp.net",
        text="und noch was",
        timestamp=base + 60,
        sender_name="someone else",
    )
    middleware = ReplyContextMiddleware()
    ctx = PipelineContext(event=_event(message_id="msg-002"))
    try:
        async with archive.turn():
            await middleware(ctx, _noop)
    finally:
        archive.close()

    ambient = ctx.event.raw_metadata.get("ambient_context_window", [])
    assert ambient == ["[Frank Taeger] hey there"]


async def _noop(ctx: PipelineContext) -> None:
    return None


# ── A2A recipient resolution ───────────────────────────────────────────────────


class _AliasPolicy:
    """The policy side of an A2A recipient lookup: group tags stay policy-owned."""

    def policy_snapshot(self) -> object:
        from types import SimpleNamespace

        group = SimpleNamespace(group_tags=["team-example"])
        whatsapp = SimpleNamespace(chats={"private-group@g.us": group})
        policy = SimpleNamespace(channels={"whatsapp": whatsapp})
        return SimpleNamespace(healthy=True, policy=policy)


@pytest.mark.parametrize(
    ("kind", "alias", "expected"),
    [
        ("group", "team-example", ("private-group@g.us", None)),
        ("group", "Team-Example", (None, "unknown")),
    ],
)
def test_the_a2a_policy_path_is_unchanged(knowledge, kind, alias, expected) -> None:
    assert (
        resolve_whatsapp_recipient(
            kind,
            alias,
            policy_adapter=_AliasPolicy(),
            knowledge=knowledge,
            contacts_service=_PoisonedLegacyContacts(),
        )
        == expected
    )


def test_frozen_native_release_resolves_a2a_to_its_phone_address(knowledge) -> None:
    person = knowledge.issue_person("10009@s.whatsapp.net", name="Synthetic")
    assert person

    assert resolve_whatsapp_recipient(
        "contact",
        "Synthetic",
        policy_adapter=_AliasPolicy(),
        knowledge=knowledge,
    ) == ("10009@s.whatsapp.net", None)


def test_a2a_delivery_does_not_release_a_native_display_name(knowledge) -> None:
    """Native recognition is not permission to send an unsolicited address."""
    knowledge.issue_person(FRANK_PHONE, name="Alice")

    assert resolve_whatsapp_recipient(
        "contact",
        "Alice",
        policy_adapter=_AliasPolicy(),
        knowledge=knowledge,
    ) == (None, "unknown")


def test_a2a_delivery_refuses_ambiguous_native_names(knowledge) -> None:
    first = knowledge.issue_person(FRANK_PHONE, name="Alice")
    second = knowledge.issue_person("4917632625470@s.whatsapp.net", name="Alice")
    assert first != second
    assert resolve_whatsapp_recipient(
        "contact",
        "Alice",
        policy_adapter=_AliasPolicy(),
        knowledge=knowledge,
        contacts_service=_PoisonedLegacyContacts(),
    ) == (None, "unknown")


def test_a2a_delivery_refuses_unreleased_native_name(knowledge) -> None:
    """A projected display name alone is not an address release."""
    knowledge.issue_person(FRANK_PHONE, name="Alice")
    assert resolve_whatsapp_recipient(
        "contact",
        "contact-alice",
        policy_adapter=_AliasPolicy(),
        knowledge=knowledge,
    ) == (None, "unknown")


# ── a knowledge outage degrades, it never falls back ───────────────────────────


class _BrokenKnowledge:
    """A facade that is present but unavailable: every call fails."""

    def __getattr__(self, name: str) -> object:
        raise RuntimeError(f"knowledge unavailable for {name!r}")


def test_a_knowledge_outage_resolves_nothing_and_raises_nothing(knowledge) -> None:
    assert resolve_whatsapp_recipient(
        "contact",
        "contact-alice",
        policy_adapter=_AliasPolicy(),
        knowledge=_BrokenKnowledge(),
        contacts_service=_PoisonedLegacyContacts(),
    ) == (None, "unknown")


# ── the composition root is part of the cutover ────────────────────────────────


def test_the_knowledge_composition_does_not_hand_the_legacy_cache_to_consumers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the facade open, the composition passes knowledge and not the old cache.

    The selected composition passes the native Knowledge facade to its consumers.
    Every path in this
    test is synthetic and temporary; no runtime data outside ``tmp_path`` is opened.
    """
    from yeoman_gateway.app.bootstrap import build_gateway_runtime
    from yeoman_gateway.bus.queue import MessageBus
    from yeoman_gateway.policy.engine import PolicyEngine
    from yeoman_gateway.policy.schema import PolicyConfig
    from yeoman_shared.config.schema import Config

    class _NeverProvider:
        async def chat(self, *_args: object, **_kwargs: object) -> None:
            raise AssertionError("building the runtime must not call a model")

        def get_default_model(self) -> str:
            return "test/provider"

    monkeypatch.setenv("YEOMAN_HOME", str(tmp_path))
    from tests.gateway.history_test_helpers import prepare_history_retirement_knowledge

    knowledge_path = prepare_history_retirement_knowledge(tmp_path)
    config = Config.model_validate(
        {
            "knowledge": {
                "enabled": True,
                "db_path": str(knowledge_path),
                "legacy_contacts_paths": [str(tmp_path / "legacy-contacts.db")],
                "legacy_memory_paths": [str(tmp_path / "legacy-memory.db")],
                "capture_enabled": False,
            },
            "processing": {"enabled": False, "participation": {"enabled": False}},
            "security": {"enabled": False},
        }
    )
    # Hermeticity first: an unchecked field name would silently fall back to the real
    # default paths, and this test must never go near a live store.
    assert config.knowledge.enabled is True
    assert Path(config.knowledge.db_path).expanduser() == knowledge_path
    assert [Path(item).expanduser() for item in config.knowledge.legacy_contacts_paths] == [
        tmp_path / "legacy-contacts.db"
    ]
    assert [Path(item).expanduser() for item in config.knowledge.legacy_memory_paths] == [
        tmp_path / "legacy-memory.db"
    ]

    policy = PolicyEngine(PolicyConfig(), workspace=tmp_path)
    runtime = build_gateway_runtime(
        config=config,
        provider=_NeverProvider(),  # type: ignore[arg-type]
        policy_engine=policy,
        policy_path=None,
        workspace=tmp_path / "workspace",
        bus=MessageBus(),
    )
    try:
        assert runtime.responder.knowledge is not None, "the facade must be the authority"
        assert not hasattr(runtime.responder, "contacts_service"), "resolver has no cache fallback field"

        # The pipeline layers are built from the same value: neither the reply-context nor
        # the outbound middleware receives the legacy cache in this composition.
        pipeline = runtime.orchestrator._orchestrator._pipeline  # noqa: SLF001 - wiring check
        layers = pipeline._layers  # noqa: SLF001 - wiring check
        people_middleware = [
            layer
            for layer in layers
            if isinstance(layer, (ReplyContextMiddleware, OutboundMiddleware))
        ]
        assert len(people_middleware) == 2
        for layer in people_middleware:
            if isinstance(layer, OutboundMiddleware):
                assert layer._knowledge is runtime.responder.knowledge
            else:
                assert not hasattr(layer, "_knowledge")
            assert not hasattr(layer, "_contacts")
    finally:
        pass  # retired runtime fields are not lifecycle resources


@pytest.mark.asyncio
async def test_an_outage_costs_the_mention_but_never_the_reply(knowledge) -> None:
    middleware = OutboundMiddleware(knowledge=_BrokenKnowledge())

    ctx = await _run_outbound(middleware, "Hey @Frank, alles gut?")

    # The reply is still assembled and sent; only the unresolved mention is dropped.
    assert _mention_candidates(ctx) == []
    intents = [item for item in ctx.intents if isinstance(item, SendOutboundIntent)]
    assert intents[0].event.content == "Hey @Frank, alles gut?"


@pytest.mark.asyncio
async def test_an_outage_still_renders_the_archive_name_in_reply_context(
    knowledge, tmp_path: Path
) -> None:
    archive = ProjectedHistory(tmp_path / "outage-history")
    base = int(datetime.now(UTC).timestamp()) - 600
    archive.add_message(
        channel="whatsapp",
        chat_id=CHAT,
        message_id="m-1",
        participant=None,
        sender_id=FRANK_PHONE,
        text="hey there",
        timestamp=base,
        sender_name="archive-name",
    )
    archive.add_message(
        channel="whatsapp",
        chat_id=CHAT,
        message_id="msg-002",
        participant=None,
        sender_id="4917000000000@s.whatsapp.net",
        text="und noch was",
        timestamp=base + 60,
        sender_name="someone else",
    )
    middleware = ReplyContextMiddleware()
    ctx = PipelineContext(event=_event(message_id="msg-002"))
    try:
        async with archive.turn():
            await middleware(ctx, _noop)
    finally:
        archive.close()

    ambient = ctx.event.raw_metadata.get("ambient_context_window", [])
    assert ambient == ["[archive-name] hey there"]


def test_an_unproven_legacy_projection_is_not_a_known_delivery_identifier(
    knowledge,
) -> None:
    knowledge.issue_person(FRANK_PHONE, name="Frank Taeger")
    known = knowledge.known_identifier_values()

    assert FRANK_PHONE in known
    assert "4917000000000@s.whatsapp.net" not in known


