"""Task 7, second half: the remaining person consumers answer from knowledge alone.

Reply context, outbound mentions, the responder's contact tools, history summarization
and A2A recipient resolution all resolve a person through the public knowledge facade as
soon as one is wired.  The legacy contacts cache stays as the transitional answer for a
composition *without* knowledge and is never a second opinion: these tests wire a legacy
double that fails the test on any attribute access, so a regression is loud instead of
silent.  All identifiers here are synthetic.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from yeoman_gateway.agent.tools.resolve_contact import (
    ResolveContactTool,
    resolve_contact_reference,
)
from yeoman_gateway.adapters.reply_archive_sqlite import SqliteReplyArchiveAdapter
from yeoman_gateway.agent.tools.summarize_history import SummarizeHistoryTool
from yeoman_gateway.core.intents import SendOutboundIntent
from yeoman_gateway.core.models import InboundEvent
from yeoman_gateway.core.pipeline import PipelineContext
from yeoman_gateway.ipc.a2a_invoke import resolve_whatsapp_recipient
from yeoman_gateway.knowledge.models import Identifier
from yeoman_gateway.pipeline.outbound import OutboundMiddleware
from yeoman_gateway.pipeline.reply_context import ReplyContextMiddleware
from yeoman_gateway.storage.chat_registry import ChatRegistry
from yeoman_gateway.storage.inbound_archive import InboundArchive

OWNER = "whatsapp:4910000000001"
FRANK_PHONE = "4917632625469@s.whatsapp.net"
FRANK_LID = "46918273106072@lid"
CHAT = "finance@g.us"


class _PoisonedLegacyContacts:
    """A legacy cache whose every use fails the test that installed it."""

    def __getattr__(self, name: str) -> object:
        raise AssertionError(
            f"the legacy contacts cache was consulted for {name!r} although knowledge is wired"
        )


@pytest.fixture
def knowledge(tmp_path: Path):
    """A public knowledge facade over a synthetic store, plus an evidence issuer."""
    from yeoman_gateway.knowledge.api import open_knowledge_store
    from yeoman_gateway.knowledge.authority import (
        FakePolicyAuthority,
        FakeSourceAuthority,
        PolicyMembership,
    )
    from yeoman_gateway.knowledge.models import Identifier, TrustedIdentityObservation

    authority = FakeSourceAuthority()

    class _ConsumerPolicy(FakePolicyAuthority):
        def admin_actor(self) -> str:
            return OWNER

        def membership(self, context):
            return PolicyMembership(
                members=frozenset({context.principal_id}), revision="consumer-cutover"
            )

    service = open_knowledge_store(
        tmp_path / "knowledge.db",
        workspace_id="consumer-cutover-tests",
        source_authority=authority,
        policy_authority=_ConsumerPolicy(admins={OWNER}, capture_actors={OWNER}),
    )
    counter = {"n": 0}

    def issue(
        value: str,
        *,
        kind: str = "phone_jid",
        name: str | None = None,
        extra: tuple[object, ...] = (),
        mapping: bool = False,
        channel: str = "whatsapp",
    ) -> str:
        """Create the person a *verified* platform observation points at."""
        counter["n"] += 1
        observation = TrustedIdentityObservation(
            identifiers=(Identifier(channel, kind, value, "account-tests"), *extra),
            evidence_ref=f"cutover-observation-{counter['n']}",
            observed_name=name,
            observed_at_ms=service._now(),  # noqa: SLF001 - synthetic clock seam
            mapping_verified=mapping,
        )
        authority.issue_observation(observation)
        resolved = service.resolve_observation(observation)
        assert resolved.person_id, resolved.reason
        return resolved.person_id

    def allow_address(person_id: str, name: str, *, scope: str = "global") -> None:
        service.observe_alias(
            person_id=person_id,
            name=name,
            alias_kind="short_name",
            scope_key=scope,
            evidence_ref=f"cutover-evidence-{name}",
            status="confirmed",
            address_allowed=True,
        )

    service.issue_person = issue  # type: ignore[attr-defined]
    service.allow_address = allow_address  # type: ignore[attr-defined]
    try:
        yield service
    finally:
        service.close()


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


def _registry(tmp_path: Path) -> ChatRegistry:
    registry = ChatRegistry(db_path=tmp_path / "chat_registry.db")
    registry.register_chat(
        channel="whatsapp",
        chat_id=CHAT,
        chat_type="group",
        readable_name="Finanzgruppe",
        metadata={"participants": [{"id": FRANK_LID, "phoneNumber": FRANK_PHONE}]},
    )
    return registry


# ── resolve_contact: the responder's delivery resolver ─────────────────────────


def test_a_proven_person_resolves_to_one_delivery_address(knowledge) -> None:
    knowledge.issue_person(FRANK_PHONE, name="Frank Taeger")

    result = resolve_contact_reference(
        reference="Frank",
        channel="whatsapp",
        chat_id=CHAT,
        contacts=_PoisonedLegacyContacts(),
        knowledge=knowledge,
    )

    assert result is not None
    assert result.jid == FRANK_PHONE
    assert result.display_name == "Frank Taeger"


def test_two_people_with_one_name_are_not_a_delivery_address(knowledge) -> None:
    knowledge.issue_person(FRANK_PHONE, name="Frank Taeger")
    knowledge.issue_person("4917632625470@s.whatsapp.net", name="Frank Taeger")

    assert (
        resolve_contact_reference(
            reference="Frank",
            channel="whatsapp",
            chat_id=CHAT,
            contacts=_PoisonedLegacyContacts(),
            knowledge=knowledge,
        )
        is None
    )


def test_an_identifier_without_a_proven_binding_is_not_a_delivery_address(knowledge) -> None:
    assert (
        resolve_contact_reference(
            reference="@46918273106072",
            channel="whatsapp",
            chat_id=CHAT,
            contacts=_PoisonedLegacyContacts(),
            knowledge=knowledge,
        )
        is None
    )


@pytest.mark.asyncio
async def test_the_tool_reports_an_unresolved_person_instead_of_a_legacy_match(
    knowledge, tmp_path: Path
) -> None:
    tool = ResolveContactTool(
        contacts=_PoisonedLegacyContacts(),
        knowledge=knowledge,
        chat_registry=_registry(tmp_path),
    )
    tool.set_context("whatsapp", CHAT)

    payload = await tool.execute(query="Frank")

    assert '"ok": false' in payload
    assert "contact_not_resolved" in payload


# ── summarize_history ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_history_names_come_from_proven_bindings(knowledge, tmp_path: Path) -> None:
    archive = InboundArchive(db_path=tmp_path / "archive.db")
    knowledge.issue_person(FRANK_PHONE, name="Frank Taeger")
    archive.record_inbound(
        channel="whatsapp",
        chat_id=CHAT,
        message_id="m-1",
        participant=None,
        sender_id=FRANK_PHONE,
        text=f"hey @{FRANK_PHONE.split('@')[0]} check this",
        timestamp=int(datetime.now(UTC).timestamp()) - 600,
        sender_name="push-name-must-not-win",
    )
    tool = SummarizeHistoryTool(
        archive, _PoisonedLegacyContacts(), knowledge=knowledge
    )
    tool.set_context("whatsapp", CHAT)
    try:
        result = await tool.execute(hours_back=48)
    finally:
        archive.close()

    assert "Frank Taeger" in result
    assert "push-name-must-not-win" not in result
    assert "@Frank" in result


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
async def test_a_mention_uses_one_proven_phone_address(knowledge) -> None:
    """Two proven kinds are not a choice: the phone JID is the WhatsApp address."""
    person = knowledge.issue_person(
        FRANK_PHONE, name="Frank Taeger", extra=(Identifier("whatsapp", "lid", FRANK_LID, "account-tests"),), mapping=True
    )
    knowledge.allow_address(person, "Frank")
    middleware = OutboundMiddleware(
        contacts=_PoisonedLegacyContacts(), knowledge=knowledge
    )

    ctx = await _run_outbound(middleware, "Hey @Frank, alles gut?")

    candidates = _mention_candidates(ctx)
    assert FRANK_PHONE in candidates
    assert FRANK_LID not in candidates


@pytest.mark.asyncio
async def test_a_mention_of_an_unproven_name_adds_no_candidate(knowledge) -> None:
    middleware = OutboundMiddleware(
        contacts=_PoisonedLegacyContacts(), knowledge=knowledge
    )

    ctx = await _run_outbound(middleware, "Hey @Frank, alles gut?")

    assert _mention_candidates(ctx) == []


# ── reply context ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_reply_context_speakers_come_from_proven_bindings(
    knowledge, tmp_path: Path
) -> None:
    archive = InboundArchive(db_path=tmp_path / "reply_archive.db")
    knowledge.issue_person(FRANK_PHONE, name="Frank Taeger")
    base = int(datetime.now(UTC).timestamp()) - 600
    archive.record_inbound(
        channel="whatsapp",
        chat_id=CHAT,
        message_id="m-1",
        participant=None,
        sender_id=FRANK_PHONE,
        text="hey there",
        timestamp=base,
        sender_name="push-name-must-not-win",
    )
    archive.record_inbound(
        channel="whatsapp",
        chat_id=CHAT,
        message_id="msg-002",
        participant=None,
        sender_id="4917000000000@s.whatsapp.net",
        text="und noch was",
        timestamp=base + 60,
        sender_name="someone else",
    )
    middleware = ReplyContextMiddleware(
        archive=SqliteReplyArchiveAdapter(archive),
        contacts=_PoisonedLegacyContacts(),
        knowledge=knowledge,
    )
    ctx = PipelineContext(event=_event(message_id="msg-002"))
    try:
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
        ("contact", "contact-alice", (FRANK_PHONE, None)),
        ("group", "contact-alice", (None, "type_mismatch")),
    ],
)
def test_the_a2a_policy_path_is_unchanged(knowledge, kind, alias, expected) -> None:
    person = knowledge.issue_person(FRANK_PHONE, name="Alice")
    knowledge.allow_address(person, "contact-alice")

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


def test_a2a_delivery_needs_an_explicitly_released_address(knowledge) -> None:
    """T04: recognition is not permission - a peer gets no address for a push name."""
    person = knowledge.issue_person(FRANK_PHONE, name="Alice")
    knowledge.observe_alias(
        person_id=person,
        name="contact-alice",
        alias_kind="short_name",
        scope_key="global",
        evidence_ref="cutover-evidence-alice",
        status="confirmed",
    )

    assert resolve_whatsapp_recipient(
        "contact",
        "contact-alice",
        policy_adapter=_AliasPolicy(),
        knowledge=knowledge,
    ) == (None, "unknown")

    knowledge.allow_address(person, "contact-alice")
    assert resolve_whatsapp_recipient(
        "contact",
        "contact-alice",
        policy_adapter=_AliasPolicy(),
        knowledge=knowledge,
    ) == (FRANK_PHONE, None)


def test_a2a_delivery_refuses_two_people_with_one_released_alias(knowledge) -> None:
    first = knowledge.issue_person(FRANK_PHONE, name="Alice")
    second = knowledge.issue_person("4917632625470@s.whatsapp.net", name="Alice Two")
    knowledge.allow_address(first, "contact-alice")
    knowledge.allow_address(second, "contact-alice")

    assert resolve_whatsapp_recipient(
        "contact",
        "contact-alice",
        policy_adapter=_AliasPolicy(),
        knowledge=knowledge,
        contacts_service=_PoisonedLegacyContacts(),
    ) == (None, "unknown")


def test_a2a_delivery_ignores_a_chat_scoped_alias(knowledge) -> None:
    """A name released inside one conversation is not a global address."""
    person = knowledge.issue_person(FRANK_PHONE, name="Alice")
    knowledge.allow_address(person, "contact-alice", scope="channel:whatsapp:chat:finance@g.us")

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
    knowledge.issue_person(FRANK_PHONE, name="Frank Taeger")

    assert (
        resolve_contact_reference(
            reference="Frank",
            channel="whatsapp",
            chat_id=CHAT,
            knowledge=_BrokenKnowledge(),
            contacts=_PoisonedLegacyContacts(),
        )
        is None
    )
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

    The legacy service stays constructed - it still owns the memory scoping and the
    shutdown path - but no cut-over consumer receives it any more.  Every path in this
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
    config = Config.model_validate(
        {
            "knowledge": {
                "enabled": True,
                "db_path": str(tmp_path / "knowledge.db"),
                "legacy_contacts_paths": [str(tmp_path / "legacy-contacts.db")],
                "legacy_memory_paths": [str(tmp_path / "legacy-memory.db")],
                "capture_enabled": False,
            },
            "personaEvolution": {"enabled": False},
            "processing": {"enabled": False, "participation": {"enabled": False}},
            "security": {"enabled": False},
        }
    )
    # Hermeticity first: an unchecked field name would silently fall back to the real
    # default paths, and this test must never go near a live store.
    assert config.knowledge.enabled is True
    assert Path(config.knowledge.db_path).expanduser() == tmp_path / "knowledge.db"
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
        assert runtime.responder.contacts_service is None, "no second, unproven cache"
        assert runtime.contacts is not None, "...but the service still owns its lifecycle"

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
            assert getattr(layer, "_knowledge", None) is not None
            assert getattr(layer, "_contacts", None) is None
    finally:
        runtime.inbound_archive.close()
        runtime.chat_registry.close()
        runtime.contacts.close()
        runtime.memory.close()


@pytest.mark.asyncio
async def test_an_outage_costs_the_mention_but_never_the_reply(knowledge) -> None:
    middleware = OutboundMiddleware(
        contacts=_PoisonedLegacyContacts(), knowledge=_BrokenKnowledge()
    )

    ctx = await _run_outbound(middleware, "Hey @Frank, alles gut?")

    # The reply is still assembled and sent; only the unresolved mention is dropped.
    assert _mention_candidates(ctx) == []
    intents = [item for item in ctx.intents if isinstance(item, SendOutboundIntent)]
    assert intents[0].event.content == "Hey @Frank, alles gut?"


@pytest.mark.asyncio
async def test_an_outage_still_renders_the_archive_name_in_reply_context(
    knowledge, tmp_path: Path
) -> None:
    archive = InboundArchive(db_path=tmp_path / "outage_archive.db")
    base = int(datetime.now(UTC).timestamp()) - 600
    archive.record_inbound(
        channel="whatsapp",
        chat_id=CHAT,
        message_id="m-1",
        participant=None,
        sender_id=FRANK_PHONE,
        text="hey there",
        timestamp=base,
        sender_name="archive-name",
    )
    archive.record_inbound(
        channel="whatsapp",
        chat_id=CHAT,
        message_id="msg-002",
        participant=None,
        sender_id="4917000000000@s.whatsapp.net",
        text="und noch was",
        timestamp=base + 60,
        sender_name="someone else",
    )
    middleware = ReplyContextMiddleware(
        archive=SqliteReplyArchiveAdapter(archive),
        contacts=_PoisonedLegacyContacts(),
        knowledge=_BrokenKnowledge(),
    )
    ctx = PipelineContext(event=_event(message_id="msg-002"))
    try:
        await middleware(ctx, _noop)
    finally:
        archive.close()

    ambient = ctx.event.raw_metadata.get("ambient_context_window", [])
    assert ambient == ["[archive-name] hey there"]


# ── review findings of the first cutover round ─────────────────────────────────


def _retract(knowledge, person_id: str, name: str) -> None:
    """Retract a name as a mapping: "that was never me" (spec 7.3)."""
    row = knowledge._store.query_one(  # noqa: SLF001 - locating the alias under test
        "SELECT id FROM contact_aliases WHERE contact_id = ? AND alias = ?",
        (person_id, name),
    )
    assert row is not None, f"no alias {name!r} for {person_id}"
    knowledge.retire_alias(
        alias_id=int(row["id"]),
        context=knowledge.admin_context_for(reason="cutover-test"),
        correct_mapping=True,
    )


def test_two_proven_phone_addresses_are_ambiguous_not_a_first_match(knowledge) -> None:
    """Spec 7.2: several equal targets are ambiguous."""
    knowledge.issue_person(
        FRANK_PHONE,
        name="Frank Taeger",
        extra=(
            Identifier("whatsapp", "phone_jid", "4917632625470@s.whatsapp.net", "account-tests"),
        ),
        mapping=True,
    )

    assert (
        resolve_contact_reference(
            reference="Frank",
            channel="whatsapp",
            chat_id=CHAT,
            knowledge=knowledge,
            contacts=_PoisonedLegacyContacts(),
        )
        is None
    )


def test_a_conversation_address_is_preferred_over_an_unknown_one(knowledge) -> None:
    person = knowledge.issue_person(
        "4917632625469@s.whatsapp.net",
        name="Frank Taeger",
        extra=(Identifier("whatsapp", "phone_jid", "4917632625470@s.whatsapp.net", "account-tests"),),
        mapping=True,
    )
    in_conversation = "4917632625470@s.whatsapp.net"

    resolved = knowledge.identifier_for_name(
        "Frank Taeger",
        channel="whatsapp",
        prefer=(in_conversation,),
        prefer_kind="phone_jid",
    )

    assert resolved is not None
    assert resolved.value == in_conversation
    assert knowledge.person_identifiers(person)


def test_a_retracted_name_is_not_a_delivery_target_any_more(knowledge) -> None:
    person = knowledge.issue_person(FRANK_PHONE, name="Wimsekt")
    knowledge.allow_address(person, "Wim")

    assert (
        resolve_contact_reference(
            reference="Wim",
            channel="whatsapp",
            chat_id=CHAT,
            knowledge=knowledge,
            contacts=_PoisonedLegacyContacts(),
        )
        is not None
    )

    _retract(knowledge, person, "Wim")

    assert "Wim" not in knowledge.alias_names(person)
    assert knowledge.person_display_name(person) != "Wim"
    assert (
        resolve_contact_reference(
            reference="Wim",
            channel="whatsapp",
            chat_id=CHAT,
            knowledge=knowledge,
            contacts=_PoisonedLegacyContacts(),
        )
        is None
    )


def test_a_mention_cannot_synthesise_a_whatsapp_address_from_another_channel(
    knowledge,
) -> None:
    """A proven Telegram identifier is not a WhatsApp delivery target (L04/T13)."""
    knowledge.issue_person("1234567890", kind="telegram_id", name="Telegram Person", channel="telegram")

    assert (
        resolve_contact_reference(
            reference="@1234567890",
            channel="whatsapp",
            chat_id=CHAT,
            knowledge=knowledge,
            contacts=_PoisonedLegacyContacts(),
        )
        is None
    )
    # ...and it is still a proven person on its own channel.
    assert knowledge.owners_of_identifier_value("1234567890", channel="telegram")


def test_a_merge_keeps_the_released_address_of_the_merged_member(knowledge) -> None:
    released = knowledge.issue_person(FRANK_PHONE, name="Frank Taeger")
    knowledge.allow_address(released, "Frank")
    other = knowledge.issue_person(
        "46918273106072@lid", kind="lid", name="Frank Zwei", mapping=True
    )
    knowledge.merge_people_with_policy(released, other, reason="cutover-test")

    delivered = {
        item.value
        for item in knowledge.delivery_identifiers_for_alias("Frank", channel="whatsapp")
    }

    # A merge does not hide the merged member's proven address...
    assert FRANK_PHONE in delivered
    # ...and the A2A path refuses two addresses instead of picking one.
    assert resolve_whatsapp_recipient(
        "contact",
        "Frank",
        policy_adapter=_AliasPolicy(),
        knowledge=knowledge,
    ) == (None, "unknown")


def test_an_unproven_legacy_projection_is_not_a_known_delivery_identifier(
    knowledge,
) -> None:
    person = knowledge.issue_person(FRANK_PHONE, name="Frank Taeger")
    knowledge._store.execute(  # noqa: SLF001 - planting the unproven legacy projection
        "INSERT INTO contact_identifiers (channel, identifier, contact_id, kind)"
        " VALUES ('whatsapp', '4917000000000@s.whatsapp.net', ?, 'phone_jid')",
        (person,),
    )

    known = knowledge.known_identifier_values()

    assert FRANK_PHONE in known
    assert "4917000000000@s.whatsapp.net" not in known


@pytest.mark.asyncio
async def test_a_history_outage_keeps_the_archive_names(knowledge, tmp_path: Path) -> None:
    archive = InboundArchive(db_path=tmp_path / "outage_history.db")
    archive.record_inbound(
        channel="whatsapp",
        chat_id=CHAT,
        message_id="m-1",
        participant=None,
        sender_id=FRANK_PHONE,
        text="hey there",
        timestamp=int(datetime.now(UTC).timestamp()) - 600,
        sender_name="archive-name",
    )
    tool = SummarizeHistoryTool(archive, None, knowledge=_BrokenKnowledge())
    tool.set_context("whatsapp", CHAT)
    try:
        result = await tool.execute(hours_back=48)
    finally:
        archive.close()

    assert "archive-name" in result
    assert "hey there" in result
