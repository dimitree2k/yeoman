"""Configuration schema using Pydantic."""

from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic_settings import BaseSettings

from yeoman_shared.config.defaults import (
    DEFAULT_MEMORY,
    DEFAULT_SECURITY,
    DEFAULT_WHATSAPP_MEDIA,
    DEFAULT_WHATSAPP_REPLY_CONTEXT,
    default_model_profiles,
    default_model_routes,
)
from yeoman_shared.reactions import DEFAULT_REACTION_EMOJIS, looks_like_emoji


def _default_model_profiles() -> dict[str, "ModelProfile"]:
    return {
        name: ModelProfile.model_validate(payload)
        for name, payload in default_model_profiles().items()
    }


class ModelProfile(BaseModel):
    """One model profile used for a specific capability route.

    Supports fallback chains: when the primary model fails (429/5xx),
    the router tries each fallback in order. Cooldown tracking prevents
    repeated failures from overwhelming degraded providers.
    """

    model_config = ConfigDict(extra="ignore")

    kind: Literal["chat", "vision", "asr", "ocr", "video", "embedding", "tts"]
    model: str | None = None
    provider: str | None = None
    max_tokens: int | None = None
    temperature: float | None = None
    timeout_ms: int | None = None
    # Fallback chain: list of profile names to try if this profile fails
    fallback: list[str] = Field(default_factory=list)
    # Cooldown seconds after a 429/5xx error before retrying this profile
    cooldown_seconds: int = 60
    # OpenRouter reasoning tokens config: {enabled, effort, max_tokens, exclude}
    reasoning: dict[str, Any] | None = None


class ModelRoutingConfig(BaseModel):
    """Capability-oriented model routing configuration."""

    model_config = ConfigDict(extra="ignore")

    profiles: dict[str, ModelProfile] = Field(default_factory=_default_model_profiles)
    routes: dict[str, str] = Field(default_factory=default_model_routes)

    @model_validator(mode="after")
    def _validate_routes(self) -> "ModelRoutingConfig":
        # Normalize route values (profile references) to snake_case so that
        # camelCase values in JSON match profile keys converted by convert_keys().
        from yeoman_shared.config.loader import camel_to_snake

        self.routes = {key: camel_to_snake(val) for key, val in self.routes.items()}
        missing = sorted({name for name in self.routes.values() if name not in self.profiles})
        if missing:
            raise ValueError("models.routes references unknown profiles: " + ", ".join(missing))
        return self


class WhatsAppMediaConfig(BaseModel):
    """WhatsApp media processing and retention settings."""

    model_config = ConfigDict(extra="ignore")

    enabled: bool = bool(DEFAULT_WHATSAPP_MEDIA["enabled"])
    incoming_dir: str = str(DEFAULT_WHATSAPP_MEDIA["incoming_dir"])
    outgoing_dir: str = str(DEFAULT_WHATSAPP_MEDIA["outgoing_dir"])
    retention_days: int = int(DEFAULT_WHATSAPP_MEDIA["retention_days"])
    describe_images: bool = bool(DEFAULT_WHATSAPP_MEDIA["describe_images"])
    pass_image_to_assistant: bool = bool(DEFAULT_WHATSAPP_MEDIA["pass_image_to_assistant"])
    max_image_bytes_mb: int = int(DEFAULT_WHATSAPP_MEDIA["max_image_bytes_mb"])
    persist_incoming_audio: bool = bool(DEFAULT_WHATSAPP_MEDIA["persist_incoming_audio"])
    transcribe_audio: bool = bool(DEFAULT_WHATSAPP_MEDIA["transcribe_audio"])
    max_audio_bytes_mb: int = int(DEFAULT_WHATSAPP_MEDIA["max_audio_bytes_mb"])
    delete_audio_after_transcription: bool = bool(
        DEFAULT_WHATSAPP_MEDIA["delete_audio_after_transcription"]
    )
    max_asr_concurrency: int = Field(
        default=int(DEFAULT_WHATSAPP_MEDIA["max_asr_concurrency"]), ge=1
    )
    max_tts_concurrency: int = Field(
        default=int(DEFAULT_WHATSAPP_MEDIA["max_tts_concurrency"]), ge=1
    )
    describe_videos: bool = bool(DEFAULT_WHATSAPP_MEDIA["describe_videos"])
    max_video_bytes_mb: int = int(DEFAULT_WHATSAPP_MEDIA["max_video_bytes_mb"])
    video_frame_count: int = int(DEFAULT_WHATSAPP_MEDIA["video_frame_count"])
    delete_video_after_description: bool = bool(
        DEFAULT_WHATSAPP_MEDIA["delete_video_after_description"]
    )
    describe_stickers: bool = bool(DEFAULT_WHATSAPP_MEDIA["describe_stickers"])
    delete_sticker_after_description: bool = bool(
        DEFAULT_WHATSAPP_MEDIA["delete_sticker_after_description"]
    )
    persist_incoming_documents: bool = bool(
        DEFAULT_WHATSAPP_MEDIA["persist_incoming_documents"]
    )
    max_document_bytes_mb: int = int(DEFAULT_WHATSAPP_MEDIA["max_document_bytes_mb"])
    max_document_text_pages: int = int(DEFAULT_WHATSAPP_MEDIA["max_document_text_pages"])
    max_document_prompt_chars: int = int(DEFAULT_WHATSAPP_MEDIA["max_document_prompt_chars"])
    ocr_images: bool = bool(DEFAULT_WHATSAPP_MEDIA["ocr_images"])

    @property
    def incoming_path(self) -> Path:
        return Path(self.incoming_dir).expanduser()

    @property
    def outgoing_path(self) -> Path:
        return Path(self.outgoing_dir).expanduser()


class WhatsAppConfig(BaseModel):
    """WhatsApp channel configuration."""

    model_config = ConfigDict(extra="ignore")

    enabled: bool = False
    bridge_url: str = "ws://localhost:3001"
    bridge_host: str = "127.0.0.1"
    bridge_port: int = 3001
    bridge_token: str = ""
    bridge_auto_repair: bool = True
    bridge_startup_timeout_ms: int = 15000
    auth_dir: str = "~/.yeoman/secrets/whatsapp-auth"
    debounce_ms: int = 0
    debounce_media_ms: int = 500
    read_receipts: bool = True
    accept_from_me: bool = False
    media_max_mb: int = 50
    max_dedupe_entries: int = 5000
    max_debounce_buckets: int = 2000
    reconnect_initial_ms: int = 1000
    reconnect_max_ms: int = 30000
    reconnect_factor: float = 2.0
    reconnect_jitter: float = 0.25
    reconnect_max_attempts: int = 0  # 0 means unlimited retries
    max_payload_bytes: int = 262144
    reply_context_window_limit: int = int(DEFAULT_WHATSAPP_REPLY_CONTEXT["window_limit"])
    reply_context_line_max_chars: int = int(DEFAULT_WHATSAPP_REPLY_CONTEXT["line_max_chars"])
    ambient_window_limit: int = int(DEFAULT_WHATSAPP_REPLY_CONTEXT["ambient_window_limit"])
    session_history_limit: int = int(DEFAULT_WHATSAPP_REPLY_CONTEXT["session_history_limit"])
    session_history_limit_group: int = int(DEFAULT_WHATSAPP_REPLY_CONTEXT["session_history_limit_group"])
    media: WhatsAppMediaConfig = Field(default_factory=WhatsAppMediaConfig)

    @property
    def resolved_bridge_port(self) -> int:
        if self.bridge_port:
            return self.bridge_port
        parsed = urlparse(self.bridge_url)
        if parsed.port is not None:
            return parsed.port
        if parsed.scheme == "wss":
            return 443
        if parsed.scheme == "ws":
            return 80
        return 3001

    @property
    def resolved_bridge_url(self) -> str:
        host = (self.bridge_host or "").strip()
        if not host:
            parsed = urlparse(self.bridge_url)
            host = parsed.hostname or "127.0.0.1"
        return f"ws://{host}:{self.resolved_bridge_port}"


class TelegramConfig(BaseModel):
    """Telegram channel configuration."""

    model_config = ConfigDict(extra="ignore")

    enabled: bool = False
    token: str = ""  # Bot token from @BotFather
    proxy: str | None = (
        None  # HTTP/SOCKS5 proxy URL, e.g. "http://127.0.0.1:7890" or "socks5://127.0.0.1:1080"
    )


class FeishuConfig(BaseModel):
    """Feishu/Lark channel configuration using WebSocket long connection."""

    model_config = ConfigDict(extra="ignore")

    enabled: bool = False
    app_id: str = ""  # App ID from Feishu Open Platform
    app_secret: str = ""  # App Secret from Feishu Open Platform
    encrypt_key: str = ""  # Encrypt Key for event subscription (optional)
    verification_token: str = ""  # Verification Token for event subscription (optional)


class DiscordConfig(BaseModel):
    """Discord channel configuration."""

    model_config = ConfigDict(extra="ignore")

    enabled: bool = False
    token: str = ""  # Bot token from Discord Developer Portal
    gateway_url: str = "wss://gateway.discord.gg/?v=10&encoding=json"
    intents: int = 37377  # GUILDS + GUILD_MESSAGES + DIRECT_MESSAGES + MESSAGE_CONTENT


class ChannelsConfig(BaseModel):
    """Configuration for chat channels."""

    whatsapp: WhatsAppConfig = Field(default_factory=WhatsAppConfig)
    telegram: TelegramConfig = Field(default_factory=TelegramConfig)
    discord: DiscordConfig = Field(default_factory=DiscordConfig)
    feishu: FeishuConfig = Field(default_factory=FeishuConfig)


class AgentDefaults(BaseModel):
    """Default agent configuration."""

    model_config = ConfigDict(
        extra="ignore", populate_by_name=True, env_prefix="YEOMAN_", env_nested_delimiter="__"
    )
    workspace: str = "~/.yeoman/workspace"
    model: str = "anthropic/claude-opus-4-5"
    max_tool_iterations: int = 20
    timing_logs_enabled: bool = False
    subagent_model: str | None = Field(default=None, alias="subagentModel")


class AgentsConfig(BaseModel):
    """Agent configuration."""

    defaults: AgentDefaults = Field(default_factory=AgentDefaults)


class ProviderConfig(BaseModel):
    """Provider credential configuration."""

    api_key: str = ""
    api_base: str | None = None
    extra_headers: dict[str, str] | None = None  # Custom headers (e.g. APP-Code for AiHubMix)


class ElevenLabsProviderConfig(ProviderConfig):
    """ElevenLabs provider config with optional TTS defaults."""

    model_config = ConfigDict(
        extra="ignore", populate_by_name=True, env_prefix="YEOMAN_", env_nested_delimiter="__"
    )

    voice_id: str | None = Field(default=None, alias="voiceId")
    model_id: str | None = Field(default=None, alias="modelId")


def _get_provider_names() -> list[str]:
    """Get provider names from registry."""
    from yeoman_gateway.providers.registry import PROVIDERS

    return [spec.name for spec in PROVIDERS]


class ProvidersConfig(BaseModel):
    """Configuration for provider credentials."""

    model_config = ConfigDict(extra="allow")
    elevenlabs: ElevenLabsProviderConfig = Field(default_factory=ElevenLabsProviderConfig)

    @model_validator(mode="after")
    def _inject_provider_defaults(self) -> "ProvidersConfig":
        """Auto-generate provider fields from registry."""
        from yeoman_gateway.providers.registry import PROVIDERS

        for spec in PROVIDERS:
            value = getattr(self, spec.name, None)
            if value is None:
                setattr(self, spec.name, ProviderConfig())
            elif isinstance(value, dict):
                setattr(self, spec.name, ProviderConfig.model_validate(value))
        return self


class GatewayConfig(BaseModel):
    """Gateway/server configuration."""

    host: str = "0.0.0.0"
    port: int = 18790


class WebSearchConfig(BaseModel):
    """Web search tool configuration."""

    tavily_api_key: str = ""  # Tavily API key (https://tavily.com)
    max_results: int = 5


class WebToolsConfig(BaseModel):
    """Web tools configuration."""

    search: WebSearchConfig = Field(default_factory=WebSearchConfig)
    max_fetch_bytes: int = 2_097_152  # 2 MB streaming cap
    blocked_domains: list[str] = Field(default_factory=list)
    allowed_domains: list[str] = Field(default_factory=list)  # empty = all allowed
    rate_limit_rpm: int = 20  # requests per minute across all web tools
    allowed_content_types: list[str] = Field(
        default_factory=lambda: [
            "text/",
            "application/json",
            "application/xml",
            "application/xhtml+xml",
        ]
    )


class MemoryCaptureConfig(BaseModel):
    """Capture configuration for semantic memory pipeline."""

    model_config = ConfigDict(extra="ignore")

    enabled: bool = bool(DEFAULT_MEMORY["capture"]["enabled"])
    channels: list[str] = Field(default_factory=lambda: list(DEFAULT_MEMORY["capture"]["channels"]))
    capture_assistant: bool = bool(DEFAULT_MEMORY["capture"]["capture_assistant"])
    queue_maxsize: int = int(DEFAULT_MEMORY["capture"]["queue_maxsize"])
    mode: Literal["heuristic", "llm", "hybrid"] = str(DEFAULT_MEMORY["capture"]["mode"])
    extract_route: str = str(DEFAULT_MEMORY["capture"]["extract_route"])
    max_candidates_per_message: int = int(DEFAULT_MEMORY["capture"]["max_candidates_per_message"])
    min_confidence: float = float(DEFAULT_MEMORY["capture"]["min_confidence"])
    min_salience: float = float(DEFAULT_MEMORY["capture"]["min_salience"])


class MemoryRecallConfig(BaseModel):
    """Recall configuration for semantic memory retrieval."""

    model_config = ConfigDict(extra="ignore")

    max_results: int = int(DEFAULT_MEMORY["recall"]["max_results"])
    max_prompt_chars: int = int(DEFAULT_MEMORY["recall"]["max_prompt_chars"])
    lexical_limit: int = int(DEFAULT_MEMORY["recall"]["lexical_limit"])
    vector_limit: int = int(DEFAULT_MEMORY["recall"]["vector_limit"])
    vector_candidate_limit: int = int(DEFAULT_MEMORY["recall"]["vector_candidate_limit"])
    include_trace: bool = bool(DEFAULT_MEMORY["recall"]["include_trace"])


class MemoryEmbeddingConfig(BaseModel):
    """Embedding configuration for semantic recall."""

    model_config = ConfigDict(extra="ignore")

    enabled: bool = bool(DEFAULT_MEMORY["embedding"]["enabled"])
    route: str = str(DEFAULT_MEMORY["embedding"]["route"])


class MemoryScoringConfig(BaseModel):
    """Scoring weights for composite ranking."""

    model_config = ConfigDict(extra="ignore")

    lexical_weight: float = float(DEFAULT_MEMORY["scoring"]["lexical_weight"])
    vector_weight: float = float(DEFAULT_MEMORY["scoring"]["vector_weight"])
    salience_weight: float = float(DEFAULT_MEMORY["scoring"]["salience_weight"])
    recency_weight: float = float(DEFAULT_MEMORY["scoring"]["recency_weight"])


class MemoryAclConfig(BaseModel):
    """ACL controls for capture."""

    model_config = ConfigDict(extra="ignore")

    owner_only_preference: bool = bool(DEFAULT_MEMORY["acl"]["owner_only_preference"])


class MemoryWalConfig(BaseModel):
    """Session-state WAL config."""

    model_config = ConfigDict(extra="ignore")

    enabled: bool = bool(DEFAULT_MEMORY["wal"]["enabled"])
    state_dir: str = str(DEFAULT_MEMORY["wal"]["state_dir"])


class MemorySharedConfig(BaseModel):
    """Shared chat facts (Plan 05). Every switch is off by default."""

    model_config = ConfigDict(extra="ignore")

    enabled: bool = bool(DEFAULT_MEMORY["shared"]["enabled"])
    extraction_enabled: bool = bool(DEFAULT_MEMORY["shared"]["extraction_enabled"])
    extractor_version: str = str(DEFAULT_MEMORY["shared"]["extractor_version"])
    max_jobs_waiting: int = Field(
        default=int(DEFAULT_MEMORY["shared"]["max_jobs_waiting"]), ge=1
    )
    require_known_membership: bool = bool(DEFAULT_MEMORY["shared"]["require_known_membership"])

    @model_validator(mode="after")
    def _validate_switches(self) -> "MemorySharedConfig":
        if self.extraction_enabled and not self.enabled:
            raise ValueError("memory.shared.extractionEnabled requires memory.shared.enabled")
        return self


class MemoryConfig(BaseModel):
    """Single active semantic memory system configuration."""

    model_config = ConfigDict(extra="ignore")

    enabled: bool = bool(DEFAULT_MEMORY["enabled"])
    mode: Literal["primary", "shadow"] = str(DEFAULT_MEMORY["mode"])
    db_path: str = str(DEFAULT_MEMORY["db_path"])
    shared: MemorySharedConfig = Field(default_factory=MemorySharedConfig)
    capture: MemoryCaptureConfig = Field(default_factory=MemoryCaptureConfig)
    recall: MemoryRecallConfig = Field(default_factory=MemoryRecallConfig)
    embedding: MemoryEmbeddingConfig = Field(default_factory=MemoryEmbeddingConfig)
    scoring: MemoryScoringConfig = Field(default_factory=MemoryScoringConfig)
    acl: MemoryAclConfig = Field(default_factory=MemoryAclConfig)
    wal: MemoryWalConfig = Field(default_factory=MemoryWalConfig)


class ExecIsolationConfig(BaseModel):
    """Container isolation configuration for exec tool."""

    enabled: bool = True
    backend: Literal["bubblewrap"] = "bubblewrap"
    fail_closed: bool = True
    batch_session_idle_seconds: int = 600
    max_containers: int = 5
    pressure_policy: Literal["preempt_oldest_active"] = "preempt_oldest_active"
    force_workspace_restriction: bool = True
    allowlist_path: str = "~/.config/yeoman/mount-allowlist.json"


class ExecToolConfig(BaseModel):
    """Shell exec tool configuration."""

    timeout: int = 60
    allow_host_execution: bool = False
    isolation: ExecIsolationConfig = Field(default_factory=ExecIsolationConfig)


class A2AWorkerConfig(BaseModel):
    """One named A2A worker reachable by Yeoman tools."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    url: str = Field(min_length=1)
    timeout_seconds: float = Field(default=120.0, gt=0, le=600)
    # Store only the environment-variable name; never persist a bearer token here.
    auth_token_env: str | None = Field(default=None, alias="authTokenEnv")
    # Remote peers require an explicit per-worker opt-in; local workers are the default.
    allow_remote: bool = Field(default=False, alias="allowRemote")


class A2AConfig(BaseModel):
    """Opt-in generic A2A worker registry configuration."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    enabled: bool = False
    workers: dict[str, A2AWorkerConfig] = Field(default_factory=dict)


class ToolsConfig(BaseModel):
    """Tools configuration."""

    web: WebToolsConfig = Field(default_factory=WebToolsConfig)
    exec: ExecToolConfig = Field(default_factory=ExecToolConfig)
    a2a: A2AConfig = Field(default_factory=A2AConfig)
    restrict_to_workspace: bool = False  # If true, restrict all tool access to workspace directory


class SecurityStagesConfig(BaseModel):
    """Enable/disable security checks by stage."""

    input: bool = bool(DEFAULT_SECURITY["stages"]["input"])
    tool: bool = bool(DEFAULT_SECURITY["stages"]["tool"])
    output: bool = bool(DEFAULT_SECURITY["stages"]["output"])


class SecurityConfig(BaseModel):
    """Security middleware configuration."""

    enabled: bool = bool(DEFAULT_SECURITY["enabled"])
    fail_mode: Literal["open", "closed", "mixed"] = str(DEFAULT_SECURITY["fail_mode"])
    stages: SecurityStagesConfig = Field(default_factory=SecurityStagesConfig)
    block_user_message: str = str(DEFAULT_SECURITY["block_user_message"])
    strict_profile: bool = bool(DEFAULT_SECURITY["strict_profile"])
    redact_placeholder: str = str(DEFAULT_SECURITY["redact_placeholder"])


class IpcConfig(BaseModel):
    """Inter-process communication configuration."""

    gateway_socket_path: str = "~/.yeoman/run/gateway.sock"
    overseer_socket_path: str = "~/.yeoman/run/overseer.sock"
    command_rate_limit: int = 10  # max commands per second


class WebhookSourceConfig(BaseModel):
    """Configuration for a single webhook source."""

    secret_env: str  # env var name holding HMAC secret
    deliver_to: dict[str, str]  # {"channel": "whatsapp", "chat_id": "..."}
    allowed_events: list[str] | None = None  # None = allow all, [] = block all
    rate_limit: int = 30  # requests per minute


class WebhooksConfig(BaseModel):
    """Webhook ingestion configuration."""

    enabled: bool = False
    sources: dict[str, WebhookSourceConfig] = Field(default_factory=dict)


class BusConfig(BaseModel):
    """Message bus configuration."""

    inbound_maxsize: int = 2000
    outbound_maxsize: int = 2000
    event_maxsize: int = 100


class ConsciousnessConfig(BaseModel):
    """Global controls for proactive consciousness service behavior."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    enabled: bool = False
    owner_dm_default_enabled: bool = Field(default=False, alias="ownerDmDefaultEnabled")
    cron_hour: int = Field(default=19, alias="cronHour", ge=0, le=23)
    cron_minute: int = Field(default=0, alias="cronMinute", ge=0, le=59)
    agent_max_iterations: int = Field(default=3, alias="agentMaxIterations", ge=1, le=8)
    agent_max_input_tokens: int = Field(default=10000, alias="agentMaxInputTokens", ge=1000)
    max_speakup_length_chars: int = Field(default=500, alias="maxSpeakupLengthChars", ge=1)
    default_daily_cap: int = Field(default=1, alias="defaultDailyCap", ge=0, le=10)
    dynamic_daily_cap_enabled: bool = Field(default=False, alias="dynamicDailyCapEnabled")
    dynamic_daily_cap_max: int = Field(default=6, alias="dynamicDailyCapMax", ge=0, le=20)
    dynamic_daily_cap_min_confidence: float = Field(
        default=0.9, alias="dynamicDailyCapMinConfidence", ge=0.0, le=1.0
    )
    dynamic_daily_cap_min_activity_messages: int = Field(
        default=8, alias="dynamicDailyCapMinActivityMessages", ge=1
    )
    min_speakup_gap_minutes: int = Field(default=0, alias="minSpeakupGapMinutes", ge=0)
    burst_max_per_window: int = Field(default=0, alias="burstMaxPerWindow", ge=0)
    reserved_daily_slots: int = Field(default=0, alias="reservedDailySlots", ge=0, le=10)
    reserve_daily_slots_until_hour: int = Field(
        default=16, alias="reserveDailySlotsUntilHour", ge=0, le=23
    )
    approval_timeout_seconds: int = Field(default=3600, alias="approvalTimeoutSeconds", ge=60)
    burst_enabled: bool = Field(default=False, alias="burstEnabled")
    burst_threshold_messages: int = Field(default=8, alias="burstThresholdMessages", ge=2)
    burst_window_minutes: int = Field(default=15, alias="burstWindowMinutes", ge=1)
    lull_enabled: bool = Field(default=False, alias="lullEnabled")
    lull_silence_minutes: int = Field(default=25, alias="lullSilenceMinutes", ge=1)
    lull_activity_window_minutes: int = Field(
        default=120, alias="lullActivityWindowMinutes", ge=1
    )
    lull_min_recent_activity: int = Field(default=4, alias="lullMinRecentActivity", ge=1)
    lull_check_interval_seconds: int = Field(
        default=60, alias="lullCheckIntervalSeconds", ge=10
    )


class PersonaEvolutionConfig(BaseModel):
    """Global guardrails for scheduled persona evolution."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    enabled: bool = True
    cron_expression: str = Field(default="0 3 * * *", alias="cronExpression")
    minimum_samples: int = Field(default=10, alias="minimumSamples", ge=0)
    mode: Literal["preview", "auto_apply"] = "preview"
    personas_allowlist: list[str] = Field(default_factory=list, alias="personasAllowlist")
    proposal_ttl_seconds: int = Field(default=86400, alias="proposalTtlSeconds", ge=60)


class ProcessingBudgetsConfig(BaseModel):
    """Transport budgets and queue limits (spec R08 start values)."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    thread_soft_units: int = Field(default=2, ge=1)
    thread_soft_window_seconds: int = Field(default=10, ge=1)
    chat_hard_units: int = Field(default=6, ge=1)
    chat_hard_window_seconds: int = Field(default=60, ge=1)
    outbox_waiting_per_chat: int = Field(default=20, ge=0)
    #: Enforce the soft thread limit as a refusal. Off until blocked effects can be
    #: re-queued when due - otherwise a chatty thread would lose a reply.
    thread_soft_enforce: bool = False


class ProcessingThreadsConfig(BaseModel):
    """Thread lifetime, follow-up and generation limits (spec R03, R04)."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    #: Automatic continuation window (spec: "Fortsetzungsfenster"). Owner-set: ten
    #: minutes, changeable in config.json without a code change.
    followup_window_seconds: int = Field(default=600, ge=0)
    idle_seconds: int = Field(default=1800, ge=1)
    reopen_window_seconds: int = Field(default=604800, ge=0)
    pending_inputs_per_thread: int = Field(default=32, ge=1)
    max_generations_global: int = Field(default=1, ge=1, le=2)
    max_generations_per_thread: int = Field(default=1, ge=1, le=2)


class ProcessingDeadlinesConfig(BaseModel):
    """Planning deadlines per effect class (spec section 4)."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    reactive_ms: int = Field(default=120_000, ge=1)
    semantic_reaction_ms: int = Field(default=30_000, ge=1)
    proactive_ms: int = Field(default=60_000, ge=1)


class ProcessingReconciliationConfig(BaseModel):
    """Unknown-effect reconciliation bounds (spec R07)."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    backoff_seconds: list[int] = Field(
        default_factory=lambda: [5, 15, 45, 120, 300, 600]
    )
    max_probes: int = Field(default=6, ge=1)
    deadline_seconds: int = Field(default=600, ge=1)
    claim_lease_seconds: int = Field(default=30, ge=1)
    probe_timeout_ms: int = Field(default=10_000, ge=1)
    #: Simultaneous probes per reconciliation tick.
    probe_concurrency: int = Field(default=2, ge=1, le=8)
    #: Provider lookups stay off by default: the reconciler works locally unless enabled.
    provider_lookup_enabled: bool = False
    #: Echo a client message id to the provider (off until the bridge proves idempotency).
    client_message_id: bool = False

    @model_validator(mode="after")
    def _validate_probe_within_lease(self) -> "ProcessingReconciliationConfig":
        if self.probe_timeout_ms > self.claim_lease_seconds * 1000:
            raise ValueError(
                "processing.reconciliation.probeTimeoutMs must not exceed claimLeaseSeconds"
            )
        if self.max_probes > len(self.backoff_seconds):
            raise ValueError(
                "processing.reconciliation.maxProbes exceeds the configured backoff schedule"
            )
        if any(step <= 0 for step in self.backoff_seconds):
            raise ValueError("processing.reconciliation.backoffSeconds must be positive")
        return self


class ProcessingExtractionConfig(BaseModel):
    """Async shared-memory extraction trigger (spec R09)."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    idle_seconds: int = Field(default=60, ge=1)
    max_delay_seconds: int = Field(default=300, ge=1)

    @model_validator(mode="after")
    def _validate_windows(self) -> "ProcessingExtractionConfig":
        if self.max_delay_seconds < self.idle_seconds:
            raise ValueError(
                "processing.extraction.maxDelaySeconds must not be below idleSeconds"
            )
        return self


class ProcessingRetentionConfig(BaseModel):
    """Retention windows for journal payloads and lineage metadata (spec R06, R10)."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    journal_payload_days: int = Field(default=7, ge=0)
    lineage_metadata_days: int = Field(default=30, ge=0)
    unresolved_days: int = Field(default=90, ge=0)
    shared_fact_days: int = Field(default=90, ge=0)

    @model_validator(mode="after")
    def _validate_windows(self) -> "ProcessingRetentionConfig":
        if self.journal_payload_days > self.lineage_metadata_days:
            raise ValueError(
                "processing.retention.journalPayloadDays must not exceed lineageMetadataDays"
            )
        if self.lineage_metadata_days > self.unresolved_days:
            raise ValueError(
                "processing.retention.lineageMetadataDays must not exceed unresolvedDays"
            )
        return self


class ProcessingConfig(BaseModel):
    """State-aware message processing (spec section 4).

    Disabled by default. Activation is a separate, explicitly ordered step per chat;
    enabling it here without a chat allowlist only wires the durable core.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    enabled: bool = False
    chats: list[str] = Field(default_factory=list)
    shadow_chats: list[str] = Field(default_factory=list)
    #: Chats where an ambient (unaddressed) message may still be answered, each with its
    #: own short-lived lineage. Empty everywhere else (routing spec, owner decision).
    ambient_chats: list[str] = Field(default_factory=list)
    #: Per-chat reply action: "answer" (default), "react" or "silence". A chat that must
    #: never be answered produces no turn, no effect and no typing indicator; "react"
    #: answers with a reaction taken from :attr:`reaction_emojis` (routing spec).
    reply_actions: dict[str, str] = Field(default_factory=dict)
    #: The complete vocabulary a model-chosen reaction may use, e.g. ["👍", "🤙", "🥱"].
    #: Anything else is dropped and logged - never replaced by a guessed face, never sent
    #: as text. An explicitly empty list means "no model-chosen reactions at all".
    #: Confirmations the gateway decides itself are not model choices and stay unaffected
    #: (see ``yeoman_shared.reactions``).
    reaction_emojis: list[str] = Field(default_factory=lambda: list(DEFAULT_REACTION_EMOJIS))
    db_path: str = "data/processing/processing.db"
    budgets: ProcessingBudgetsConfig = Field(default_factory=ProcessingBudgetsConfig)
    threads: ProcessingThreadsConfig = Field(default_factory=ProcessingThreadsConfig)
    deadlines: ProcessingDeadlinesConfig = Field(default_factory=ProcessingDeadlinesConfig)
    reconciliation: ProcessingReconciliationConfig = Field(
        default_factory=ProcessingReconciliationConfig
    )
    extraction: ProcessingExtractionConfig = Field(default_factory=ProcessingExtractionConfig)
    retention: ProcessingRetentionConfig = Field(default_factory=ProcessingRetentionConfig)

    @field_validator("reaction_emojis")
    @classmethod
    def _validate_reaction_emojis(cls, values: list[str]) -> list[str]:
        """Reject entries that are not emojis.

        A typo here would silently shrink Arvid's reactions ("thumbsup" instead of 👍), and
        the gateway would look broken rather than misconfigured. Naming the entry at
        startup is cheaper than debugging a missing face later.
        """
        approved: list[str] = []
        for value in values:
            entry = str(value).strip()
            if not looks_like_emoji(entry):
                raise ValueError(
                    "processing.reactionEmojis accepts single emojis only; "
                    f"{value!r} is not one"
                )
            approved.append(entry)
        return approved

    def is_chat_enabled(self, channel: str, chat_id: str) -> bool:
        """True when the new mode owns this exact chat (effects are allowed)."""
        if not self.enabled:
            return False
        scoped = {entry.strip() for entry in self.chats if entry.strip()}
        return bool(scoped) and f"{channel}:{chat_id}" in scoped

    def is_chat_shadowed(self, channel: str, chat_id: str) -> bool:
        """True when this chat is only observed.

        Shadow chats decide and journal, but must not produce effects or change what the
        user sees (spec section 5).
        """
        if not self.enabled:
            return False
        shadowed = {entry.strip() for entry in self.shadow_chats if entry.strip()}
        return bool(shadowed) and f"{channel}:{chat_id}" in shadowed


class Config(BaseSettings):
    """Root configuration for yeoman."""

    model_config = ConfigDict(
        extra="ignore", populate_by_name=True, env_prefix="YEOMAN_", env_nested_delimiter="__"
    )

    config_version: int = 2
    models: ModelRoutingConfig = Field(default_factory=ModelRoutingConfig)
    agents: AgentsConfig = Field(default_factory=AgentsConfig)
    channels: ChannelsConfig = Field(default_factory=ChannelsConfig)
    providers: ProvidersConfig = Field(default_factory=ProvidersConfig)
    gateway: GatewayConfig = Field(default_factory=GatewayConfig)
    tools: ToolsConfig = Field(default_factory=ToolsConfig)
    security: SecurityConfig = Field(default_factory=SecurityConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    bus: BusConfig = Field(default_factory=BusConfig)
    ipc: IpcConfig = Field(default_factory=IpcConfig)
    webhooks: WebhooksConfig = Field(default_factory=WebhooksConfig)
    consciousness: ConsciousnessConfig = Field(default_factory=ConsciousnessConfig)
    persona_evolution: PersonaEvolutionConfig = Field(
        default_factory=PersonaEvolutionConfig,
        alias="personaEvolution",
    )
    processing: ProcessingConfig = Field(default_factory=ProcessingConfig)

    @property
    def workspace_path(self) -> Path:
        """Get expanded workspace path."""
        from yeoman_shared.utils.helpers import get_data_path

        candidate = Path(self.agents.defaults.workspace).expanduser()
        return candidate if candidate.is_absolute() else get_data_path() / candidate

    def get_provider(
        self, model: str | None = None, *, provider_name: str | None = None
    ) -> ProviderConfig | None:
        """Get matched provider config (api_key, api_base, extra_headers). Falls back to first available.
        Env vars from ProviderSpec.env_key are used as fallback when config.api_key is empty.
        When *provider_name* is given (e.g. from a profile's ``provider`` field),
        that provider is returned directly — skipping keyword matching."""
        import os

        from yeoman_gateway.providers.registry import PROVIDERS, ProviderSpec, find_by_name

        def _has_api_key(provider_config: ProviderConfig, spec: ProviderSpec) -> bool:
            """Check if provider has api_key in config or env."""
            if provider_config.api_key:
                return True
            # Fallback to env var
            return bool(os.environ.get(spec.env_key))

        # Explicit provider override — skip keyword matching entirely.
        # When the caller names a provider, routing is strict: either that
        # provider resolves with credentials, or we return None. Falling
        # through to keyword/gateway fallback would silently misroute the
        # request (e.g. an openai-pinned embedding call ending up at
        # OpenRouter because OPENAI_API_KEY is missing).
        if provider_name:
            spec = find_by_name(provider_name)
            if spec:
                p = getattr(self.providers, spec.name, None)
                if p and _has_api_key(p, spec):
                    return p
            return None

        model_lower = (model or self.agents.defaults.model).lower()

        # Match by keyword (order follows PROVIDERS registry)
        for spec in PROVIDERS:
            p = getattr(self.providers, spec.name, None)
            if p and any(kw in model_lower for kw in spec.keywords) and _has_api_key(p, spec):
                return p

        # Fallback: gateways first, then others (follows registry order)
        for spec in PROVIDERS:
            p = getattr(self.providers, spec.name, None)
            if p and _has_api_key(p, spec):
                return p
        return None

    def get_api_key(self, model: str | None = None) -> str | None:
        """Get API key for the given model. Falls back to first available key.
        Checks config first, then env vars from ProviderSpec.env_key."""
        import os

        from yeoman_gateway.providers.registry import PROVIDERS

        # Check if provider in config has a key
        p = self.get_provider(model)
        if p and p.api_key:
            return p.api_key

        # Fallback to env vars (check in registry order)
        model_lower = (model or self.agents.defaults.model).lower()

        for spec in PROVIDERS:
            if any(kw in model_lower for kw in spec.keywords):
                env_key = os.environ.get(spec.env_key)
                if env_key:
                    return env_key

        # Fallback: any available key from env (gateways first)
        for spec in PROVIDERS:
            env_key = os.environ.get(spec.env_key)
            if env_key:
                return env_key

        return None
