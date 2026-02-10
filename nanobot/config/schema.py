"""Configuration schema using Pydantic."""

from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic_settings import BaseSettings

from nanobot.config.defaults import (
    DEFAULT_MEMORY,
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

    kind: Literal["chat", "vision", "asr", "ocr", "video"]
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
            raise ValueError(
                "models.routes references unknown profiles: " + ", ".join(missing)
            )
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
    auth_dir: str = "~/.nanobot/whatsapp-auth"
    debounce_ms: int = 0
    read_receipts: bool = True
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
    proxy: str | None = None  # HTTP/SOCKS5 proxy URL, e.g. "http://127.0.0.1:7890" or "socks5://127.0.0.1:1080"


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
    workspace: str = "~/.nanobot/workspace"
    model: str = "anthropic/claude-opus-4-5"
    max_tokens: int = 8192
    temperature: float = 0.7
    max_tool_iterations: int = 20
    timing_logs_enabled: bool = False


class AgentsConfig(BaseModel):
    """Agent configuration."""
    defaults: AgentDefaults = Field(default_factory=AgentDefaults)


class ProviderConfig(BaseModel):
    """LLM provider configuration."""
    api_key: str = ""
    api_base: str | None = None
    extra_headers: dict[str, str] | None = None  # Custom headers (e.g. APP-Code for AiHubMix)


class ProvidersConfig(BaseModel):
    """Configuration for LLM providers."""
    anthropic: ProviderConfig = Field(default_factory=ProviderConfig)
    openai: ProviderConfig = Field(default_factory=ProviderConfig)
    openrouter: ProviderConfig = Field(default_factory=ProviderConfig)
    deepseek: ProviderConfig = Field(default_factory=ProviderConfig)
    groq: ProviderConfig = Field(default_factory=ProviderConfig)
    zhipu: ProviderConfig = Field(default_factory=ProviderConfig)
    dashscope: ProviderConfig = Field(default_factory=ProviderConfig)  # 阿里云通义千问
    vllm: ProviderConfig = Field(default_factory=ProviderConfig)
    gemini: ProviderConfig = Field(default_factory=ProviderConfig)
    moonshot: ProviderConfig = Field(default_factory=ProviderConfig)
    aihubmix: ProviderConfig = Field(default_factory=ProviderConfig)  # AiHubMix API gateway


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

    whatsapp_bridge: WhatsAppBridgeRuntimeConfig = Field(default_factory=WhatsAppBridgeRuntimeConfig)


class WebSearchConfig(BaseModel):
    """Web search tool configuration."""
    api_key: str = ""  # Brave Search API key
    max_results: int = 5


class WebToolsConfig(BaseModel):
    """Web tools configuration."""
    search: WebSearchConfig = Field(default_factory=WebSearchConfig)


class MemoryRecallConfig(BaseModel):
    """Recall configuration for long-term memory retrieval."""

    model_config = ConfigDict(extra="ignore")

    max_results: int = int(DEFAULT_MEMORY["recall"]["max_results"])
    max_prompt_chars: int = int(DEFAULT_MEMORY["recall"]["max_prompt_chars"])
    user_preference_layer_results: int = int(
        DEFAULT_MEMORY["recall"]["user_preference_layer_results"]
    )


class MemoryCaptureConfig(BaseModel):
    """Capture configuration for automatic memory extraction."""

    model_config = ConfigDict(extra="ignore")

    enabled: bool = bool(DEFAULT_MEMORY["capture"]["enabled"])
    mode: Literal["heuristic"] = str(DEFAULT_MEMORY["capture"]["mode"])
    min_confidence: float = float(DEFAULT_MEMORY["capture"]["min_confidence"])
    min_importance: float = float(DEFAULT_MEMORY["capture"]["min_importance"])
    channels: list[str] = Field(default_factory=lambda: list(DEFAULT_MEMORY["capture"]["channels"]))
    capture_assistant: bool = bool(DEFAULT_MEMORY["capture"]["capture_assistant"])
    max_entries_per_turn: int = int(DEFAULT_MEMORY["capture"]["max_entries_per_turn"])


class MemoryRetentionConfig(BaseModel):
    """Retention policy by memory kind."""

    model_config = ConfigDict(extra="ignore")

    episodic_days: int = int(DEFAULT_MEMORY["retention"]["episodic_days"])
    fact_days: int = int(DEFAULT_MEMORY["retention"]["fact_days"])
    preference_days: int = int(DEFAULT_MEMORY["retention"]["preference_days"])
    decision_days: int = int(DEFAULT_MEMORY["retention"]["decision_days"])


class MemoryWalConfig(BaseModel):
    """Write-ahead log config for session state durability."""

    model_config = ConfigDict(extra="ignore")

    enabled: bool = bool(DEFAULT_MEMORY["wal"]["enabled"])
    state_dir: str = str(DEFAULT_MEMORY["wal"]["state_dir"])


class MemoryEmbeddingConfig(BaseModel):
    """Reserved embedding config for future hybrid retrieval."""

    model_config = ConfigDict(extra="ignore")

    enabled: bool = bool(DEFAULT_MEMORY["embedding"]["enabled"])
    backend: Literal["reserved_hybrid", "sqlite_fts"] = str(DEFAULT_MEMORY["embedding"]["backend"])


class MemoryConfig(BaseModel):
    """Long-term memory system configuration."""

    model_config = ConfigDict(extra="ignore")

    enabled: bool = bool(DEFAULT_MEMORY["enabled"])
    db_path: str = str(DEFAULT_MEMORY["db_path"])
    backend: Literal["sqlite_fts", "reserved_hybrid"] = str(DEFAULT_MEMORY["backend"])
    recall: MemoryRecallConfig = Field(default_factory=MemoryRecallConfig)
    capture: MemoryCaptureConfig = Field(default_factory=MemoryCaptureConfig)
    retention: MemoryRetentionConfig = Field(default_factory=MemoryRetentionConfig)
    wal: MemoryWalConfig = Field(default_factory=MemoryWalConfig)
    embedding: MemoryEmbeddingConfig = Field(default_factory=MemoryEmbeddingConfig)


class ExecIsolationConfig(BaseModel):
    """Container isolation configuration for exec tool."""

    enabled: bool = False
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
    isolation: ExecIsolationConfig = Field(default_factory=ExecIsolationConfig)


class ToolsConfig(BaseModel):
    """Tools configuration."""
    web: WebToolsConfig = Field(default_factory=WebToolsConfig)
    exec: ExecToolConfig = Field(default_factory=ExecToolConfig)
    restrict_to_workspace: bool = False  # If true, restrict all tool access to workspace directory


class BusConfig(BaseModel):
    """Message bus configuration."""

    inbound_maxsize: int = 2000
    outbound_maxsize: int = 2000


class Config(BaseSettings):
    """Root configuration for nanobot."""
    config_version: int = 2
    models: ModelRoutingConfig = Field(default_factory=ModelRoutingConfig)
    agents: AgentsConfig = Field(default_factory=AgentsConfig)
    channels: ChannelsConfig = Field(default_factory=ChannelsConfig)
    providers: ProvidersConfig = Field(default_factory=ProvidersConfig)
    gateway: GatewayConfig = Field(default_factory=GatewayConfig)
    runtime: RuntimeConfig = Field(default_factory=RuntimeConfig)
    tools: ToolsConfig = Field(default_factory=ToolsConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    bus: BusConfig = Field(default_factory=BusConfig)

    @property
    def workspace_path(self) -> Path:
        """Get expanded workspace path."""
        base = Path.home() / ".nanobot"
        candidate = Path(self.agents.defaults.workspace).expanduser()
        return candidate if candidate.is_absolute() else base / candidate

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
            if spec.is_gateway and spec.default_api_base and p == getattr(self.providers, spec.name, None):
                return spec.default_api_base
        return None

    class Config:
        env_prefix = "NANOBOT_"
        env_nested_delimiter = "__"
