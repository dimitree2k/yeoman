"""Plan 01 / spec section 4: central processing defaults and their validation."""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from yeoman_shared.config.schema import Config, ProcessingConfig


def test_processing_is_disabled_by_default() -> None:
    cfg = Config()
    assert isinstance(cfg.processing, ProcessingConfig)
    assert cfg.processing.enabled is False
    assert cfg.processing.chats == []
    assert cfg.processing.db_path == "data/processing/processing.db"


def test_processing_carries_the_v1_start_values() -> None:
    processing = Config().processing
    assert processing.threads.followup_window_seconds == 15
    assert processing.threads.idle_seconds == 1800
    assert processing.threads.reopen_window_seconds == 7 * 86400
    assert processing.threads.pending_inputs_per_thread == 32
    assert processing.threads.max_generations_global == 1
    assert processing.budgets.thread_soft_units == 2
    assert processing.budgets.thread_soft_window_seconds == 10
    assert processing.budgets.chat_hard_units == 6
    assert processing.budgets.chat_hard_window_seconds == 60
    assert processing.budgets.outbox_waiting_per_chat == 20
    assert processing.deadlines.reactive_ms == 120_000
    assert processing.deadlines.semantic_reaction_ms == 30_000
    assert processing.deadlines.proactive_ms == 60_000
    assert processing.reconciliation.backoff_seconds == [5, 15, 45, 120, 300, 600]
    assert processing.reconciliation.claim_lease_seconds == 30
    assert processing.reconciliation.probe_timeout_ms == 10_000
    assert processing.extraction.idle_seconds == 60
    assert processing.extraction.max_delay_seconds == 300
    assert processing.retention.journal_payload_days == 7
    assert processing.retention.lineage_metadata_days == 30
    assert processing.retention.unresolved_days == 90
    assert processing.retention.shared_fact_days == 90


def test_negative_limits_are_rejected() -> None:
    with pytest.raises(ValidationError):
        ProcessingConfig.model_validate({"threads": {"pending_inputs_per_thread": 0}})
    with pytest.raises(ValidationError):
        ProcessingConfig.model_validate({"budgets": {"outbox_waiting_per_chat": -1}})
    with pytest.raises(ValidationError):
        ProcessingConfig.model_validate({"deadlines": {"reactive_ms": -5}})
    with pytest.raises(ValidationError):
        ProcessingConfig.model_validate({"retention": {"journal_payload_days": -1}})


def test_unknown_processing_values_are_rejected() -> None:
    with pytest.raises(ValidationError):
        ProcessingConfig.model_validate({"threads": {"idle_seconds_typo": 60}})
    with pytest.raises(ValidationError):
        ProcessingConfig.model_validate({"enabledd": True})


def test_camel_case_config_file_keys_are_accepted() -> None:
    """config.json is camelCase; the loader converts before validation."""
    from yeoman_shared.config.loader import convert_keys

    cfg = ProcessingConfig.model_validate(
        convert_keys(
            {
                "enabled": True,
                "chats": ["whatsapp:chat1"],
                "dbPath": "data/processing/other.db",
                "reconciliation": {"claimLeaseSeconds": 30, "probeTimeoutMs": 9000},
                "retention": {"journalPayloadDays": 5, "lineageMetadataDays": 20},
            }
        )
    )
    assert cfg.db_path == "data/processing/other.db"
    assert cfg.reconciliation.probe_timeout_ms == 9000
    assert cfg.retention.journal_payload_days == 5


def test_probe_timeout_must_fit_inside_the_claim_lease() -> None:
    with pytest.raises(ValidationError):
        ProcessingConfig.model_validate(
            {"reconciliation": {"claim_lease_seconds": 5, "probe_timeout_ms": 10_000}}
        )
    cfg = ProcessingConfig.model_validate(
        {"reconciliation": {"claim_lease_seconds": 30, "probe_timeout_ms": 10_000}}
    )
    assert cfg.reconciliation.probe_timeout_ms == 10_000


def test_retention_windows_must_be_ordered() -> None:
    with pytest.raises(ValidationError):
        ProcessingConfig.model_validate(
            {"retention": {"journal_payload_days": 40, "lineage_metadata_days": 30}}
        )


def test_chat_activation_is_explicit_and_scoped() -> None:
    disabled = ProcessingConfig()
    assert disabled.is_chat_enabled("whatsapp", "chat1") is False

    enabled_without_allowlist = ProcessingConfig.model_validate({"enabled": True})
    assert enabled_without_allowlist.is_chat_enabled("whatsapp", "chat1") is False

    scoped = ProcessingConfig.model_validate(
        {"enabled": True, "chats": ["whatsapp:chat1"]}
    )
    assert scoped.is_chat_enabled("whatsapp", "chat1") is True
    assert scoped.is_chat_enabled("whatsapp", "chat2") is False
    assert scoped.is_chat_enabled("telegram", "chat1") is False


def test_shadow_chats_are_observed_only() -> None:
    """Spec section 5: shadow decides and journals, but never owns the chat."""
    config = ProcessingConfig.model_validate(
        {"enabled": True, "chats": ["whatsapp:live"], "shadow_chats": ["whatsapp:observed"]}
    )
    assert config.is_chat_enabled("whatsapp", "live") is True
    assert config.is_chat_shadowed("whatsapp", "live") is False
    assert config.is_chat_enabled("whatsapp", "observed") is False
    assert config.is_chat_shadowed("whatsapp", "observed") is True

    disabled = ProcessingConfig.model_validate({"shadow_chats": ["whatsapp:observed"]})
    assert disabled.is_chat_shadowed("whatsapp", "observed") is False


def test_two_global_generations_are_allowed_but_not_default() -> None:
    """Spec section 4: one generation by default, two only after isolation testing."""
    assert Config().processing.threads.max_generations_global == 1
    assert Config().processing.threads.max_generations_per_thread == 1

    two = ProcessingConfig.model_validate(
        {"threads": {"max_generations_global": 2, "max_generations_per_thread": 2}}
    )
    assert two.threads.max_generations_global == 2
    assert two.threads.max_generations_per_thread == 2

    with pytest.raises(ValidationError):
        ProcessingConfig.model_validate({"threads": {"max_generations_global": 3}})


def test_reconciliation_probe_settings() -> None:
    """Plan 04 defaults: two probes at a time, no provider lookup, no client id echo."""
    cfg = Config().processing.reconciliation
    assert cfg.probe_concurrency == 2
    assert cfg.provider_lookup_enabled is False
    assert cfg.client_message_id is False

    wider = ProcessingConfig.model_validate(
        {"reconciliation": {"probe_concurrency": 8, "provider_lookup_enabled": True}}
    )
    assert wider.reconciliation.probe_concurrency == 8
    assert wider.reconciliation.provider_lookup_enabled is True

    with pytest.raises(ValidationError):
        ProcessingConfig.model_validate({"reconciliation": {"probe_concurrency": 0}})
    with pytest.raises(ValidationError):
        ProcessingConfig.model_validate({"reconciliation": {"probe_concurrency": 9}})
