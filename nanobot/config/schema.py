"""Configuration schema using Pydantic."""

from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field
from pydantic_settings import BaseSettings


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
    agents: AgentsConfig = Field(default_factory=AgentsConfig)
    channels: ChannelsConfig = Field(default_factory=ChannelsConfig)
    providers: ProvidersConfig = Field(default_factory=ProvidersConfig)
    gateway: GatewayConfig = Field(default_factory=GatewayConfig)
    runtime: RuntimeConfig = Field(default_factory=RuntimeConfig)
    tools: ToolsConfig = Field(default_factory=ToolsConfig)
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
