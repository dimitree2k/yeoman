"""A2A v1.0 client primitives used by Yeoman worker tools."""

from __future__ import annotations

import ipaddress
import os
import re
import uuid
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

_A2A_VERSION = "1.0"
_AGENT_CARD_PATHS = ("/.well-known/agent-card.json", "/.well-known/agent.json")
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_LOOPBACK_NAMES = frozenset({"localhost", "ip6-localhost"})


class A2AError(RuntimeError):
    """Base class for A2A worker failures."""


class A2AWorkerConfigurationError(A2AError, ValueError):
    """A worker configuration is invalid or violates the local security default."""


class A2ATransportError(A2AError):
    """The worker could not be reached or returned invalid HTTP/JSON."""


class A2AProtocolError(A2AError):
    """The worker returned a JSON-RPC/A2A error response."""

    def __init__(self, message: str, *, code: int | None = None, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.data = data


@dataclass(frozen=True, slots=True)
class A2AWorker:
    """A named A2A peer.

    Workers are loopback-only by default. A remote URL is an explicit operator
    opt-in because this object is constructed from configuration and may cause
    the agent to send user-controlled task text to another host.
    """

    name: str
    url: str
    timeout_seconds: float = 120.0
    auth_token_env: str | None = None
    allow_remote: bool = False
    detach: bool = False

    def __post_init__(self) -> None:
        name = str(self.name or "").strip()
        if not name:
            raise A2AWorkerConfigurationError("A2A worker name cannot be empty")
        if any(ch.isspace() for ch in name):
            raise A2AWorkerConfigurationError("A2A worker name cannot contain whitespace")

        raw_url = str(self.url or "").strip()
        parsed = urlparse(raw_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise A2AWorkerConfigurationError(
                "A2A worker URL must use http or https and include a host"
            )
        if parsed.username or parsed.password or parsed.fragment:
            raise A2AWorkerConfigurationError(
                "A2A worker URL cannot contain credentials or a fragment"
            )
        try:
            is_loopback = _is_loopback_host(parsed.hostname)
        except ValueError as exc:
            raise A2AWorkerConfigurationError("A2A worker URL has an invalid host") from exc
        if not self.allow_remote and not is_loopback:
            raise A2AWorkerConfigurationError(
                "A2A worker URL must be loopback unless allow_remote is enabled"
            )
        if self.timeout_seconds <= 0:
            raise A2AWorkerConfigurationError("A2A worker timeout_seconds must be positive")
        if self.auth_token_env is not None:
            env_name = str(self.auth_token_env).strip()
            if not env_name or not _ENV_NAME_RE.fullmatch(env_name):
                raise A2AWorkerConfigurationError(
                    "A2A auth_token_env must be a valid environment variable name"
                )

        object.__setattr__(self, "name", name)
        object.__setattr__(self, "url", raw_url.rstrip("/"))
        if self.auth_token_env is not None:
            object.__setattr__(self, "auth_token_env", str(self.auth_token_env).strip())

    @classmethod
    def from_config(cls, name: str, config: Any) -> "A2AWorker":
        """Build a worker without copying a bearer token into the config object."""
        return cls(
            name=name,
            url=str(config.url),
            timeout_seconds=float(getattr(config, "timeout_seconds", 120.0)),
            auth_token_env=getattr(config, "auth_token_env", None),
            allow_remote=bool(getattr(config, "allow_remote", False)),
            detach=bool(getattr(config, "detach", False)),
        )

    @property
    def bearer_token(self) -> str | None:
        """Resolve the token at call time from the configured environment name."""
        if not self.auth_token_env:
            return None
        token = os.environ.get(self.auth_token_env, "").strip()
        return token or None


def _is_loopback_host(host: str) -> bool:
    normalized = host.strip().lower().rstrip(".")
    if normalized in _LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


@dataclass(frozen=True, slots=True)
class A2AWorkerResult:
    """Normalized result returned by an A2A task."""

    worker: str
    task_id: str
    context_id: str
    state: str
    text: str
    raw: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)


class A2AClient:
    """Minimal A2A v1.0 JSON-RPC client.

    The client intentionally implements only the synchronous ``SendMessage``
    path needed for a Yeoman tool. The registry can add streaming, task lookup,
    cancellation, and push notifications later without changing worker
    configuration or the tool boundary.
    """

    def __init__(
        self,
        worker: A2AWorker,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.worker = worker
        self._transport = transport

    async def discover(self) -> dict[str, Any]:
        """Fetch the canonical Agent Card, accepting the legacy path too."""
        headers = self._headers()
        async with self._client() as client:
            for path in _AGENT_CARD_PATHS:
                url = self._url(path)
                try:
                    response = await client.get(url, headers=headers)
                except httpx.HTTPError as exc:
                    raise A2ATransportError(
                        f"A2A worker '{self.worker.name}' card request failed: {exc}"
                    ) from exc
                if response.status_code == 404 and path != _AGENT_CARD_PATHS[-1]:
                    continue
                if response.status_code < 200 or response.status_code >= 300:
                    raise A2ATransportError(
                        f"A2A worker '{self.worker.name}' card returned HTTP {response.status_code}"
                    )
                try:
                    card = response.json()
                except ValueError as exc:
                    raise A2ATransportError(
                        f"A2A worker '{self.worker.name}' returned invalid Agent Card JSON"
                    ) from exc
                if not isinstance(card, dict):
                    raise A2ATransportError(
                        f"A2A worker '{self.worker.name}' Agent Card must be an object"
                    )
                return card
        raise A2ATransportError(f"A2A worker '{self.worker.name}' did not return an Agent Card")

    async def send_message(
        self,
        message: str,
        *,
        context_id: str | None = None,
    ) -> A2AWorkerResult:
        """Send one text task using A2A v1.0 ``SendMessage``."""
        text = str(message or "").strip()
        if not text:
            raise A2AProtocolError("A2A message cannot be empty")
        card = await self.discover()
        endpoint, tenant = self._select_interface(card)
        context = str(context_id or "").strip() or f"ctx-{uuid.uuid4().hex[:16]}"
        request_id = uuid.uuid4().hex
        payload: dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "SendMessage",
            "params": {
                "message": {
                    "role": "ROLE_USER",
                    "parts": [{"text": text, "mediaType": "text/plain"}],
                    "messageId": uuid.uuid4().hex,
                    "contextId": context,
                }
            },
        }
        if tenant:
            payload["params"]["tenant"] = tenant

        async with self._client() as client:
            try:
                response = await client.post(
                    endpoint,
                    headers={**self._headers(), "Content-Type": "application/json", "A2A-Version": _A2A_VERSION},
                    json=payload,
                )
            except httpx.HTTPError as exc:
                raise A2ATransportError(
                    f"A2A worker '{self.worker.name}' request failed: {exc}"
                ) from exc
        if response.status_code < 200 or response.status_code >= 300:
            raise A2ATransportError(
                f"A2A worker '{self.worker.name}' returned HTTP {response.status_code}"
            )
        try:
            body = response.json()
        except ValueError as exc:
            raise A2ATransportError(
                f"A2A worker '{self.worker.name}' returned invalid JSON-RPC JSON"
            ) from exc
        if not isinstance(body, dict):
            raise A2ATransportError("A2A JSON-RPC response must be an object")
        error = body.get("error")
        if isinstance(error, dict):
            code = error.get("code")
            message_text = str(error.get("message") or "A2A worker returned an error")
            raise A2AProtocolError(
                f"A2A worker '{self.worker.name}': {message_text}",
                code=code if isinstance(code, int) else None,
                data=error.get("data"),
            )
        if "result" not in body:
            raise A2AProtocolError(
                f"A2A worker '{self.worker.name}' returned neither result nor error"
            )
        return self._normalize_result(body["result"], context)

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=self._transport,
            timeout=httpx.Timeout(self.worker.timeout_seconds),
            follow_redirects=False,
        )

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if token := self.worker.bearer_token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    def _url(self, path: str) -> str:
        return urljoin(f"{self.worker.url}/", path.lstrip("/"))

    def _select_interface(self, card: dict[str, Any]) -> tuple[str, str | None]:
        interfaces = card.get("supportedInterfaces")
        if isinstance(interfaces, list):
            candidates = [item for item in interfaces if isinstance(item, dict)]
            candidates.sort(
                key=lambda item: 0
                if str(item.get("protocolVersion") or "") in {"1.0", "1.0.0"}
                else 1
            )
            for interface in candidates:
                binding = str(interface.get("protocolBinding") or "").upper()
                advertised = str(interface.get("url") or "").strip()
                if advertised and (not binding or binding == "JSONRPC"):
                    endpoint = self._resolve_endpoint(advertised)
                    return endpoint, str(interface.get("tenant") or "").strip() or None
        advertised = str(card.get("url") or "").strip()
        if advertised:
            return self._resolve_endpoint(advertised), None
        raise A2ATransportError("A2A Agent Card does not advertise a JSON-RPC interface URL")

    def _resolve_endpoint(self, advertised: str) -> str:
        parsed = urlparse(advertised)
        endpoint = advertised if parsed.scheme else self._url(advertised)
        parsed = urlparse(endpoint)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise A2ATransportError("A2A Agent Card advertised an invalid JSON-RPC endpoint")
        if parsed.username or parsed.password or parsed.fragment:
            raise A2ATransportError("A2A Agent Card advertised an unsafe JSON-RPC endpoint")
        if not self.worker.allow_remote and not _is_loopback_host(parsed.hostname):
            raise A2ATransportError(
                "A2A Agent Card endpoint is not loopback for a loopback-only worker"
            )
        return endpoint

    def _normalize_result(self, result: Any, fallback_context: str) -> A2AWorkerResult:
        if not isinstance(result, dict):
            raise A2AProtocolError("A2A result must be an object")
        payload: dict[str, Any]
        task = result.get("task")
        message = result.get("message")
        if isinstance(task, dict):
            payload = task
        elif isinstance(message, dict):
            payload = message
        else:
            payload = result

        task_id = str(payload.get("id") or payload.get("taskId") or "")
        context_id = str(payload.get("contextId") or fallback_context)
        status = payload.get("status") if isinstance(payload.get("status"), dict) else {}
        state = str(status.get("state") or "TASK_STATE_COMPLETED")
        text = _extract_result_text(payload)
        status_message = status.get("message")
        if not text and isinstance(status_message, dict):
            text = _extract_parts_text(status_message)
        return A2AWorkerResult(
            worker=self.worker.name,
            task_id=task_id,
            context_id=context_id,
            state=state,
            text=text,
            raw=payload,
        )


def _extract_parts_text(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    parts = value.get("parts")
    if not isinstance(parts, list):
        return ""
    chunks: list[str] = []
    for part in parts:
        if not isinstance(part, dict):
            continue
        text = part.get("text")
        if isinstance(text, str) and text:
            chunks.append(text)
    return "\n".join(chunks).strip()


def _extract_result_text(payload: dict[str, Any]) -> str:
    artifacts = payload.get("artifacts")
    if not isinstance(artifacts, list):
        return ""
    chunks = [_extract_parts_text(artifact) for artifact in artifacts]
    return "\n".join(chunk for chunk in chunks if chunk).strip()
