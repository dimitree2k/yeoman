"""Participation configuration and effective-policy resolution (participation spec 6).

Synthetic identities only. These tests prove the activation matrix, the override
inheritance rules and the hard boundaries that autonomous opt-in must never widen.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError
from yeoman_gateway.policy.engine import PolicyEngine
from yeoman_gateway.policy.schema import (
    DEFAULT_PARTICIPATION_GUIDANCE,
    ParticipationPolicy,
    PolicyConfig,
)
from yeoman_shared.config.schema import (
    ProcessingConfig,
    ProcessingParticipationConfig,
    ProcessingParticipationMaintenanceConfig,
)

GROUP = "group@g.us"
OTHER_GROUP = "other@g.us"
OWNER = "owner@s.whatsapp.net"


def _processing(**participation: object) -> ProcessingConfig:
    return ProcessingConfig.model_validate({"participation": dict(participation)})


def _engine(*, chat_participation: dict[str, object] | None = None, extra: dict | None = None):
    chats: dict[str, object] = {}
    if chat_participation is not None:
        chats[GROUP] = {"participation": chat_participation}
    payload: dict[str, object] = {
        "owners": {"whatsapp": [OWNER]},
        "defaults": {"whenToReply": {"mode": "all"}},
        "channels": {"whatsapp": {"chats": chats}},
    }
    if extra:
        payload.update(extra)
    return PolicyEngine(PolicyConfig.model_validate(payload), workspace=Path("/tmp"))


# -- global configuration --------------------------------------------------------------


def test_processing_participation_defaults_are_disabled_and_shadow() -> None:
    cfg = ProcessingParticipationConfig()
    assert cfg.enabled is False
    assert cfg.shadow is True
    assert cfg.judge_route == ""
    assert cfg.context_window_minutes == 120
    assert cfg.context_max_messages == 40
    assert cfg.max_pending_chats == 64
    assert cfg.max_reevaluations == 1
    assert cfg.max_pending_source_refs == 64
    assert cfg.max_pending_source_bytes == 16384
    assert cfg.judge_timeout_seconds == 12
    assert cfg.judge_max_input_tokens == 4000
    assert cfg.judge_max_output_tokens == 256
    assert cfg.max_concurrent_decisions == 2
    assert cfg.opportunity_ttl_seconds == 120


@pytest.mark.parametrize(
    "payload",
    [
        {"contextWindowMinutes": 0},
        {"contextWindowMinutes": 1441},
        {"contextMaxMessages": 4},
        {"maxPendingChats": 0},
        {"maxPendingSourceRefs": 0},
        {"maxPendingSourceBytes": 512},
        {"maxConcurrentDecisions": 0},
        {"maxConcurrentDecisions": 9},
        {"maxReevaluations": 3},
        {"opportunityTtlSeconds": 9},
        {"judgeTimeoutSeconds": 0},
        {"judgeTimeoutSeconds": 61},
        {"judgeMaxInputTokens": 511},
        {"judgeMaxOutputTokens": 63},
        {"randomTimer": True},
        {"interests": {"cats": True}},
    ],
)
def test_processing_participation_rejects_out_of_bounds_and_unknown_keys(
    payload: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        ProcessingParticipationConfig.model_validate(payload)


def test_enabling_participation_requires_an_explicit_judge_route() -> None:
    with pytest.raises(ValidationError):
        ProcessingParticipationConfig.model_validate({"enabled": True})
    with pytest.raises(ValidationError):
        ProcessingParticipationConfig.model_validate({"enabled": True, "judgeRoute": "   "})
    cfg = ProcessingParticipationConfig.model_validate(
        {"enabled": True, "judgeRoute": "participation.judge"}
    )
    assert cfg.judge_route == "participation.judge"


def test_maintenance_defaults_are_independent_of_judging() -> None:
    cfg = ProcessingParticipationMaintenanceConfig()
    assert cfg.enabled is False
    assert cfg.interval_seconds == 900
    assert cfg.observation_window_minutes == 120
    assert cfg.batch_size == 20
    with pytest.raises(ValidationError):
        ProcessingParticipationMaintenanceConfig.model_validate({"batchSize": 101})
    with pytest.raises(ValidationError):
        ProcessingParticipationMaintenanceConfig.model_validate({"intervalSeconds": 0})


# -- per-chat policy -------------------------------------------------------------------


def test_participation_policy_defaults() -> None:
    policy = ParticipationPolicy()
    assert policy.enabled is False
    assert policy.guidance == DEFAULT_PARTICIPATION_GUIDANCE
    assert policy.allow_initiation is True
    assert policy.allow_continuation is True
    assert policy.allow_reactions is True
    assert policy.max_unaddressed_judge_calls_per_hour == 12
    assert policy.continuation_judge_reserve == 4
    assert policy.min_unaddressed_judge_gap_seconds == 30
    assert policy.max_unsolicited_comments_per_window == 3
    assert policy.comment_window_minutes == 30
    assert policy.max_reactions_per_window == 6


def test_participation_policy_rejects_unknown_keys_and_bad_bounds() -> None:
    with pytest.raises(ValidationError):
        PolicyConfig.model_validate(
            {
                "channels": {
                    "whatsapp": {"chats": {GROUP: {"participation": {"randomTimer": True}}}}
                }
            }
        )
    with pytest.raises(ValidationError):
        PolicyConfig.model_validate(
            {
                "channels": {
                    "whatsapp": {
                        "chats": {GROUP: {"participation": {"maxReactionsPerWindow": 61}}}
                    }
                }
            }
        )
    with pytest.raises(ValidationError):
        PolicyConfig.model_validate(
            {
                "channels": {
                    "whatsapp": {
                        "chats": {
                            GROUP: {
                                "participation": {
                                    "maxUnaddressedJudgeCallsPerHour": 2,
                                    "continuationJudgeReserve": 3,
                                }
                            }
                        }
                    }
                }
            }
        )
    with pytest.raises(ValidationError):
        PolicyConfig.model_validate(
            {
                "channels": {
                    "whatsapp": {
                        "chats": {GROUP: {"participation": {"guidance": "x" * 4001}}}
                    }
                }
            }
        )


def test_override_inheritance_is_specific_scalar_over_general() -> None:
    engine = _engine(
        chat_participation={
            "enabled": True,
            "maxUnsolicitedCommentsPerWindow": 2,
            "guidance": "Only join when asked about football.",
        }
    )
    resolved = engine.resolve_participation("whatsapp", GROUP)
    assert resolved.enabled is True
    assert resolved.max_unsolicited_comments_per_window == 2
    assert resolved.guidance == "Only join when asked about football."
    # Unspecified fields inherit the general layer.
    assert resolved.max_reactions_per_window == 6
    assert resolved.comment_window_minutes == 30
    sources = engine.participation_sources("whatsapp", GROUP)
    assert sources["enabled"] == f"channels.whatsapp.chats.{GROUP}.participation"
    assert sources["max_reactions_per_window"] == "defaults.participation"


def test_absent_chat_inherits_and_stays_disabled() -> None:
    engine = _engine(chat_participation={"enabled": True})
    other = engine.resolve_participation("whatsapp", OTHER_GROUP)
    assert other.enabled is False
    assert other.guidance == DEFAULT_PARTICIPATION_GUIDANCE


def test_channel_default_layer_is_used_before_defaults() -> None:
    engine = PolicyEngine(
        PolicyConfig.model_validate(
            {
                "channels": {
                    "whatsapp": {
                        "default": {"participation": {"enabled": False, "maxReactionsPerWindow": 1}},
                        "chats": {GROUP: {"participation": {"enabled": True}}},
                    }
                }
            }
        ),
        workspace=Path("/tmp"),
    )
    resolved = engine.resolve_participation("whatsapp", GROUP)
    assert resolved.enabled is True
    assert resolved.max_reactions_per_window == 1
    assert (
        engine.participation_sources("whatsapp", GROUP)["max_reactions_per_window"]
        == "channels.whatsapp.default.participation"
    )


# -- activation matrix (spec 6.1) ------------------------------------------------------


def _snapshot(engine: PolicyEngine, *, processing: ProcessingConfig, **kwargs: object):
    return engine.resolve_participation_snapshot(
        "whatsapp", GROUP, processing_config=processing, **kwargs
    )


def test_activation_matrix_opt_in_without_global_switch_stays_off() -> None:
    engine = _engine(chat_participation={"enabled": True})
    snapshot = _snapshot(engine, processing=ProcessingConfig())
    assert snapshot.opted_in is True
    assert snapshot.live is False
    assert snapshot.observing is False
    assert snapshot.invalid_reason == ""


def test_activation_matrix_global_on_chat_off_stays_off() -> None:
    engine = _engine()
    snapshot = _snapshot(
        engine, processing=_processing(enabled=True, shadow=False, judgeRoute="r")
    )
    assert snapshot.opted_in is False
    assert snapshot.live is False


def test_activation_matrix_shadow_lane_observes_but_never_acts() -> None:
    engine = _engine(chat_participation={"enabled": True})
    snapshot = _snapshot(
        engine, processing=_processing(enabled=True, shadow=True, judgeRoute="r")
    )
    assert snapshot.observing is True
    assert snapshot.live is False


def test_activation_matrix_live_requires_opt_in_and_no_shadow() -> None:
    engine = _engine(chat_participation={"enabled": True})
    snapshot = _snapshot(
        engine, processing=_processing(enabled=True, shadow=False, judgeRoute="r")
    )
    assert snapshot.live is True
    assert snapshot.invalid_reason == ""


def test_activation_matrix_rejects_unmanaged_target() -> None:
    engine = _engine(chat_participation={"enabled": True})
    snapshot = _snapshot(
        engine,
        processing=_processing(enabled=True, shadow=False, judgeRoute="r"),
        managed=False,
    )
    assert snapshot.invalid_reason == "target_not_managed"
    assert snapshot.live is False


def test_activation_matrix_rejects_live_under_processing_shadow() -> None:
    engine = _engine(chat_participation={"enabled": True})
    snapshot = _snapshot(
        engine,
        processing=_processing(enabled=True, shadow=False, judgeRoute="r"),
        processing_shadowed=True,
    )
    assert snapshot.invalid_reason == "processing_shadow_conflict"
    assert snapshot.live is False


def test_snapshot_reports_epoch_and_policy_version() -> None:
    engine = _engine(chat_participation={"enabled": True})
    snapshot = _snapshot(
        engine,
        processing=_processing(enabled=True, judgeRoute="r"),
        activation_epoch=11,
        policy_version="v7",
    )
    assert snapshot.activation_epoch == 11
    assert snapshot.policy_version == "v7"


def test_snapshot_limits_follow_policy_values() -> None:
    engine = _engine(
        chat_participation={
            "enabled": True,
            "maxUnsolicitedCommentsPerWindow": 2,
            "commentWindowMinutes": 45,
            "maxReactionsPerWindow": 3,
        }
    )
    snapshot = _snapshot(
        engine,
        processing=_processing(enabled=True, shadow=False, judgeRoute="r"),
    )
    assert snapshot.limits_for("initiation") == (2, 45 * 60_000, "rolling")
    assert snapshot.limits_for("comment") == (2, 45 * 60_000, "rolling")
    assert snapshot.limits_for("reaction") == (3, 45 * 60_000, "rolling")
    with pytest.raises(ValueError):
        snapshot.limits_for("initiation_bonus")


# -- hard boundaries that autonomy must not widen --------------------------------------


def test_autonomous_opt_in_does_not_widen_access_or_tools() -> None:
    engine = PolicyEngine(
        PolicyConfig.model_validate(
            {
                "owners": {"whatsapp": [OWNER]},
                "defaults": {
                    "whenToReply": {"mode": "mention_only"},
                    "whoCanTalk": {"mode": "allowlist", "senders": ["allowed@s.whatsapp.net"]},
                    "blockedSenders": {"senders": ["blocked@s.whatsapp.net"]},
                    "allowedTools": {"mode": "allowlist", "tools": ["search"]},
                    "toolAccess": {"exec": {"mode": "owner_only"}},
                },
                "channels": {"whatsapp": {"chats": {GROUP: {"participation": {"enabled": True}}}}},
            }
        ),
        workspace=Path("/tmp"),
    )
    resolved = engine.resolve_policy("whatsapp", GROUP)
    # The participation opt-in changes nothing about access or capability.
    assert resolved.who_can_talk_mode == "allowlist"
    assert "allowed@s.whatsapp.net" in resolved.who_can_talk_senders
    assert "blocked@s.whatsapp.net" in resolved.blocked_senders
    assert resolved.allowed_tools_mode == "allowlist"
    assert resolved.allowed_tools_tools == ["search"]
    assert resolved.tool_access["exec"]["mode"] == "owner_only"
    assert resolved.participation.enabled is True


def test_when_to_reply_off_remains_a_hard_stop_for_participation() -> None:
    engine = PolicyEngine(
        PolicyConfig.model_validate(
            {
                "channels": {
                    "whatsapp": {
                        "chats": {
                            GROUP: {
                                "whenToReply": {"mode": "off"},
                                "participation": {"enabled": True},
                            }
                        }
                    }
                }
            }
        ),
        workspace=Path("/tmp"),
    )
    resolved = engine.resolve_policy("whatsapp", GROUP)
    assert resolved.when_to_reply_mode == "off"
    assert resolved.participation.enabled is True  # opt-in does not override the veto


def test_explicit_empty_guidance_is_kept_as_written() -> None:
    engine = _engine(chat_participation={"enabled": True, "guidance": ""})
    assert engine.resolve_participation("whatsapp", GROUP).guidance == ""


def test_last_valid_policy_is_retained_on_invalid_reload(tmp_path: Path) -> None:
    """An invalid candidate never replaces the last valid policy (A19)."""
    from yeoman_gateway.policy.loader import load_policy

    path = tmp_path / "policy.json"
    path.write_text(
        """
        {
          "version": 2,
          "channels": {"whatsapp": {"chats": {"group@g.us": {
            "participation": {"enabled": true, "maxReactionsPerWindow": 5}}}}}
        }
        """
    )
    first = load_policy(path)
    assert first.channels["whatsapp"].chats[GROUP].participation.max_reactions_per_window == 5

    path.write_text(
        """
        {
          "version": 2,
          "channels": {"whatsapp": {"chats": {"group@g.us": {
            "participation": {"maxReactionsPerWindow": 5000}}}}}
        }
        """
    )
    with pytest.raises(Exception):
        load_policy(path)
    # The file is invalid, but the previously loaded policy object is untouched.
    assert first.channels["whatsapp"].chats[GROUP].participation.max_reactions_per_window == 5


# -- staged migration (02.3) -----------------------------------------------------------


def _migration_policy() -> dict:
    return {
        "version": 2,
        "owners": {"whatsapp": [OWNER]},
        "channels": {
            "whatsapp": {
                "chats": {
                    GROUP: {
                        "whoCanTalk": {"mode": "allowlist", "senders": ["ann@s.whatsapp.net"]},
                        "whenToReply": {"mode": "mention_only"},
                        "blockedSenders": {"senders": ["mallory@s.whatsapp.net"]},
                        "allowedTools": {"mode": "allowlist", "tools": ["web_search"]},
                        "replyBudget": {"enabled": False},
                        "spontaneity": {"enabled": True, "profile": "balanced", "dailyCap": 1},
                    },
                    OTHER_GROUP: {"whenToReply": {"mode": "all"}},
                }
            }
        },
    }


def test_migration_candidate_opts_in_without_broadening_access() -> None:
    from yeoman_gateway.policy.participation_migration import stage_autonomous_candidate

    source = _migration_policy()
    report = stage_autonomous_candidate(
        source, channel="whatsapp", chat_ids=[GROUP], base_daily_cap=1
    )
    candidate = report.candidate
    migrated = candidate["channels"]["whatsapp"]["chats"][GROUP]
    assert migrated["participation"]["enabled"] is True
    # Access and capability are byte-identical to the source.
    original = _migration_policy()["channels"]["whatsapp"]["chats"][GROUP]
    for key in ("whoCanTalk", "blockedSenders", "allowedTools", "replyBudget"):
        assert migrated[key] == original[key]
    assert migrated["whenToReply"] == original["whenToReply"]
    # The source mapping itself is untouched.
    assert "participation" not in source["channels"]["whatsapp"]["chats"][GROUP]
    # A non-migrated chat keeps its legacy behaviour.
    assert "participation" not in candidate["channels"]["whatsapp"]["chats"][OTHER_GROUP]
    assert "mention_only" in report.obsolete_for_autonomous[1]


def test_migration_candidate_is_valid_under_the_production_schema() -> None:
    from yeoman_gateway.policy.participation_migration import stage_autonomous_candidate

    report = stage_autonomous_candidate(
        _migration_policy(), channel="whatsapp", chat_ids=[GROUP], base_daily_cap=1
    )
    validated = PolicyConfig.model_validate(report.candidate)
    assert validated.channels["whatsapp"].chats[GROUP].participation.enabled is True
    engine = PolicyEngine(validated, workspace=Path("/tmp"))
    resolved = engine.resolve_policy("whatsapp", GROUP)
    # The legacy mode survives in the file but no longer decides participation.
    assert resolved.when_to_reply_mode == "mention_only"
    assert resolved.participation.enabled is True


def test_migration_reports_reduced_dynamic_allowance() -> None:
    from yeoman_gateway.policy.participation_migration import stage_autonomous_candidate

    report = stage_autonomous_candidate(
        _migration_policy(),
        channel="whatsapp",
        chat_ids=[GROUP],
        base_daily_cap=1,
        dynamic_cap=(True, 6),
    )
    assert report.reduced_allowance is True
    assert report.initiation_cap_reduction == ((GROUP, 6, 1),)


def test_migration_refuses_unconfigured_or_empty_targets() -> None:
    from yeoman_gateway.policy.participation_migration import (
        MigrationError,
        stage_autonomous_candidate,
    )

    with pytest.raises(MigrationError):
        stage_autonomous_candidate(_migration_policy(), channel="whatsapp", chat_ids=[])
    with pytest.raises(MigrationError):
        stage_autonomous_candidate(
            _migration_policy(), channel="whatsapp", chat_ids=["unknown@g.us"]
        )
    with pytest.raises(MigrationError):
        stage_autonomous_candidate(
            _migration_policy(), channel="telegram", chat_ids=["123"]
        )


def test_obsolete_knobs_are_reported_only_for_opted_in_chats() -> None:
    from yeoman_gateway.policy.participation_migration import (
        obsolete_knobs_for_autonomous_chat,
    )

    engine = _engine(chat_participation={"enabled": True})
    opted_in = engine.resolve_policy("whatsapp", GROUP)
    assert "whenToReply.mode=all" in obsolete_knobs_for_autonomous_chat(opted_in)
    not_opted_in = engine.resolve_policy("whatsapp", OTHER_GROUP)
    assert obsolete_knobs_for_autonomous_chat(not_opted_in) == ()


# -- owner controls stay authoritative (02.3) ------------------------------------------


def test_pause_veto_covers_migrated_and_non_migrated_chats(tmp_path: Path) -> None:
    """Owner pause is a hard stop for both chats, and stays reachable while paused."""
    from yeoman_gateway.adapters.policy_engine import EnginePolicyAdapter
    from yeoman_gateway.core.models import InboundEvent  # noqa: F401  (documented import path)
    from yeoman_gateway.policy.loader import save_policy

    path = tmp_path / "policy.json"
    policy = PolicyConfig.model_validate(
        {
            "owners": {"whatsapp": [OWNER]},
            "channels": {
                "whatsapp": {
                    "chats": {
                        GROUP: {"participation": {"enabled": True}},
                        OTHER_GROUP: {"whenToReply": {"mode": "all"}},
                    }
                }
            },
        }
    )
    save_policy(policy, path)
    engine = PolicyEngine(policy, workspace=tmp_path, apply_channels={"whatsapp"})
    adapter = EnginePolicyAdapter(
        engine=engine,
        known_tools=set(),
        policy_path=path,
        reload_on_change=False,
        workspace=tmp_path,
    )

    assert adapter.participation_pause_reason("whatsapp", GROUP) is None
    # Owner controls are reachable only from the owner DM; the global pause then
    # suppresses autonomous effects in every chat, migrated or not.
    applied = adapter.route_admin_command(_admin_event("/pause all"))
    assert applied is not None and applied.outcome == "applied"
    assert adapter.participation_pause_reason("whatsapp", GROUP) == "paused_global"
    assert adapter.participation_pause_reason("whatsapp", OTHER_GROUP) == "paused_global"

    resumed = adapter.route_admin_command(_admin_event("/start all"))
    assert resumed is not None and resumed.outcome == "applied"
    assert adapter.participation_pause_reason("whatsapp", GROUP) is None
    assert adapter.participation_pause_reason("whatsapp", OTHER_GROUP) is None


def test_when_to_reply_off_blocks_participation_snapshot_liveness() -> None:
    engine = PolicyEngine(
        PolicyConfig.model_validate(
            {
                "channels": {
                    "whatsapp": {
                        "chats": {
                            GROUP: {
                                "whenToReply": {"mode": "off"},
                                "participation": {"enabled": True},
                            }
                        }
                    }
                }
            }
        ),
        workspace=Path("/tmp"),
    )
    snapshot = engine.resolve_participation_snapshot(
        "whatsapp",
        GROUP,
        processing_config=_processing(enabled=True, shadow=False, judgeRoute="r"),
    )
    # The snapshot reports the opt-in, and the effect authorizer still sees the veto.
    assert snapshot.live is True
    assert engine.resolve_policy("whatsapp", GROUP).when_to_reply_mode == "off"


def _admin_event(command: str, *, chat_id: str = OWNER, is_group: bool = False):
    from yeoman_gateway.core.models import InboundEvent

    return InboundEvent(
        channel="whatsapp",
        chat_id=chat_id,
        sender_id=OWNER,
        content=command,
        message_id="m1",
        is_group=is_group,
    )
