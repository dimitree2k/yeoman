"""Configuration schema using Pydantic."""

from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic_settings import BaseSettings

from nanobot.config.defaults import (
    DEFAULT_MEMORY,
    DEFAULT_SECURITY,
    DEFAULT_WHATSAPP_MEDIA,
    DEFAULT_WHATSAPP_REPLY_CONTEXT,
    default_model_profiles,
    default_model_routes,
)


def _default_model_profiles() -> dict[str, "ModelProfile"]:
    return {
        name: ModelProfile.model_validate(payload)
        for name, payload in default_model_profiles().items()
    }


class ModelProfile(BaseModel):
    """One model profile used for a specific capability route."""

    model_config = ConfigDict(extra="ignore")

    kind: Literal["chat", "vision", "asr", "ocr", "video", "embedding", "tts"]
    model: str | None = None
    provider: str | None = None
    max_tokens: int | None = None
    temperature: float | None = None
    timeout_ms: int | None = None


class ModelRoutingConfig(BaseModel):
    """Capability-oriented model routing configuration."""

    model_config = ConfigDict(extra="ignore")

    profiles: dict[str, ModelProfile] = Field(default_factory=_default_model_profiles)
    routes: dict[str, str] = Field(default_factory=default_model_routes)

    @model_validator(mode="after")
    def _validate_routes(self) -> "ModelRoutingConfig":
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
    delete_audio_after_transcription: bool = bool(DEFAULT_WHATSAPP_MEDIA["delete_audio_after_transcription"])
    max_asr_concurrency: int = Field(default=int(DEFAULT_WHATSAPP_MEDIA["max_asr_concurrency"]), ge=1)
    max_tts_concurrency: int = Field(default=int(DEFAULT_WHATSAPP_MEDIA["max_tts_concurrency"]), ge=1)

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
    auth_dir: str = "~/.nanobot/secrets/whatsapp-auth"
    debounce_ms: int = 0
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

    model_config = ConfigDict(extra="ignore", populate_by_name=True, env_prefix="NANOBOT_", env_nested_delimiter="__")
    workspace: str = "~/.nanobot/workspace"
    model: str = "anthropic/claude-opus-4-5"
    max_tokens: int = 8192
    temperature: float = 0.7
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

    model_config = ConfigDict(extra="ignore", populate_by_name=True, env_prefix="NANOBOT_", env_nested_delimiter="__")

    voice_id: str | None = Field(default=None, alias="voiceId")
    model_id: str | None = Field(default=None, alias="modelId")


def _get_provider_names() -> list[str]:
    """Get provider names from registry."""
    from nanobot.providers.registry import PROVIDERS

    return [spec.name for spec in PROVIDERS]


class ProvidersConfig(BaseModel):
    """Configuration for provider credentials."""

    model_config = ConfigDict(extra="allow")
    elevenlabs: ElevenLabsProviderConfig = Field(default_factory=ElevenLabsProviderConfig)

    @model_validator(mode="after")
    def _inject_provider_defaults(self) -> "ProvidersConfig":
        """Auto-generate provider fields from registry."""
        from nanobot.providers.registry import PROVIDERS

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


class WhatsAppBridgeRuntimeConfig(BaseModel):
    """Runtime supervision config for the WhatsApp bridge process."""

    model_config = ConfigDict(extra="ignore")

    host: str = "127.0.0.1"
    port: int = 3001
    token: str = ""
    auto_repair: bool = True
    startup_timeout_ms: int = 15000
    max_payload_bytes: int = 262144


class RuntimeConfig(BaseModel):
    """Out-of-process runtime subsystem configuration."""

    model_config = ConfigDict(extra="ignore")

    whatsapp_bridge: WhatsAppBridgeRuntimeConfig = Field(
        default_factory=WhatsAppBridgeRuntimeConfig
    )


class WebSearchConfig(BaseModel):
    """Web search tool configuration."""

    tavily_api_key: str = ""  # Tavily API key (https://tavily.com)
    max_results: int = 5


class WebToolsConfig(BaseModel):
    """Web tools configuration."""

    search: WebSearchConfig = Field(default_factory=WebSearchConfig)


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


class MemoryConfig(BaseModel):
    """Single active semantic memory system configuration."""

    model_config = ConfigDict(extra="ignore")

    enabled: bool = bool(DEFAULT_MEMORY["enabled"])
    mode: Literal["primary", "shadow"] = str(DEFAULT_MEMORY["mode"])
    db_path: str = str(DEFAULT_MEMORY["db_path"])
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
    allowlist_path: str = "~/.config/nanobot/mount-allowlist.json"


class ExecToolConfig(BaseModel):
    """Shell exec tool configuration."""

    timeout: int = 60
    allow_host_execution: bool = False
    isolation: ExecIsolationConfig = Field(default_factory=ExecIsolationConfig)


class ToolsConfig(BaseModel):
    """Tools configuration."""

    web: WebToolsConfig = Field(default_factory=WebToolsConfig)
    exec: ExecToolConfig = Field(default_factory=ExecToolConfig)
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


class BusConfig(BaseModel):
    """Message bus configuration."""

    inbound_maxsize: int = 2000
    outbound_maxsize: int = 2000


class Config(BaseSettings):
    """Root configuration for nanobot."""
    model_config = ConfigDict(extra="ignore", populate_by_name=True, env_prefix="NANOBOT_", env_nested_delimiter="__")

    config_version: int = 2
    models: ModelRoutingConfig = Field(default_factory=ModelRoutingConfig)
    agents: AgentsConfig = Field(default_factory=AgentsConfig)
    channels: ChannelsConfig = Field(default_factory=ChannelsConfig)
    providers: ProvidersConfig = Field(default_factory=ProvidersConfig)
    gateway: GatewayConfig = Field(default_factory=GatewayConfig)
    runtime: RuntimeConfig = Field(default_factory=RuntimeConfig)
    tools: ToolsConfig = Field(default_factory=ToolsConfig)
    security: SecurityConfig = Field(default_factory=SecurityConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    bus: BusConfig = Field(default_factory=BusConfig)

    @property
    def workspace_path(self) -> Path:
        """Get expanded workspace path."""
        from nanobot.utils.helpers import get_data_path
        candidate = Path(self.agents.defaults.workspace).expanduser()
        return candidate if candidate.is_absolute() else get_data_path() / candidate

    def get_provider(self, model: str | None = None) -> ProviderConfig | None:
        """Get matched provider config (api_key, api_base, extra_headers). Falls back to first available."""
        from nanobot.providers.registry import PROVIDERS

        model_lower = (model or self.agents.defaults.model).lower()

        # Match by keyword (order follows PROVIDERS registry)
        for spec in PROVIDERS:
            p = getattr(self.providers, spec.name, None)
            if p and any(kw in model_lower for kw in spec.keywords) and p.api_key:
                return p

        # Fallback: gateways first, then others (follows registry order)
        for spec in PROVIDERS:
            p = getattr(self.providers, spec.name, None)
            if p and p.api_key:
                return p
        return None

    def get_api_key(self, model: str | None = None) -> str | None:
        """Get API key for the given model. Falls back to first available key."""
        p = self.get_provider(model)
        return p.api_key if p else None

    def get_api_base(self, model: str | None = None) -> str | None:
        """Get API base URL for the given model. Applies default URLs for known gateways."""
        from nanobot.providers.registry import PROVIDERS

        p = self.get_provider(model)
        if p and p.api_base:
            return p.api_base
        # Only gateways get a default URL here. Standard providers (like Moonshot)
        # handle their base URL via env vars in _setup_env, NOT via api_base —
        # otherwise find_gateway() would misdetect them as local/vLLM.
        for spec in PROVIDERS:
            if (
                spec.is_gateway
                and spec.default_api_base
                and p == getattr(self.providers, spec.name, None)
            ):
                return spec.default_api_base
        return None

