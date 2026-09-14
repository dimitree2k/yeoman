"""Strict structured Hermes A2A client."""

from __future__ import annotations

import asyncio
import ipaddress
import os
import re
import uuid
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

from yeoman_gateway.a2a.contracts import (
    CONTRACT_RELEASE,
    PROFILE_URI,
    A2AContractValidationError,
    ContractSchemas,
)

_A2A_VERSION = "1.0"
_AGENT_CARD_PATHS = ("/.well-known/agent-card.json", "/.well-known/agent.json")
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_LOOPBACK_NAMES = frozenset({"localhost", "ip6-localhost"})
_V1_STATES = frozenset({"TASK_STATE_SUBMITTED", "TASK_STATE_WORKING", "TASK_STATE_INPUT_REQUIRED", "TASK_STATE_COMPLETED", "TASK_STATE_FAILED", "TASK_STATE_CANCELED", "TASK_STATE_REJECTED"})
_TERMINAL_STATES = frozenset({"TASK_STATE_COMPLETED", "TASK_STATE_FAILED", "TASK_STATE_CANCELED", "TASK_STATE_REJECTED"})


class A2AError(RuntimeError):
    """Base class for A2A worker failures."""


class A2AWorkerConfigurationError(A2AError, ValueError):
    """A worker configuration is invalid or violates the local security default."""


class A2ATransportError(A2AError):
    """The worker could not be reached or returned invalid HTTP/JSON."""


class A2AProtocolError(A2AError):
    """The worker violated JSON-RPC, A2A, or the Hermes profile."""

    def __init__(self, message: str, *, code: int | None = None, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class A2APollTimeoutError(A2AError):
    """The local bounded poll window elapsed before Hermes finished the task."""


@dataclass(frozen=True, slots=True)
class A2AWorker:
    name: str
    url: str
    timeout_seconds: float = 120.0
    auth_token_env: str | None = None
    allow_remote: bool = False
    detach: bool = False

    def __post_init__(self) -> None:
        name = str(self.name or "").strip()
        if not name or any(char.isspace() for char in name):
            raise A2AWorkerConfigurationError("A2A worker name is invalid")
        raw_url = str(self.url or "").strip()
        parsed = urlparse(raw_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise A2AWorkerConfigurationError("A2A worker URL must use http or https and include a host")
        if parsed.username or parsed.password or parsed.fragment:
            raise A2AWorkerConfigurationError("A2A worker URL cannot contain credentials or a fragment")
        if not self.allow_remote and not _is_loopback_host(parsed.hostname):
            raise A2AWorkerConfigurationError("A2A worker URL must be loopback unless allow_remote is enabled")
        if self.timeout_seconds <= 0:
            raise A2AWorkerConfigurationError("A2A worker timeout_seconds must be positive")
        if self.auth_token_env is not None and not _ENV_NAME_RE.fullmatch(str(self.auth_token_env).strip()):
            raise A2AWorkerConfigurationError("A2A auth_token_env must be a valid environment variable name")
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "url", raw_url.rstrip("/"))
        if self.auth_token_env is not None:
            object.__setattr__(self, "auth_token_env", str(self.auth_token_env).strip())

    @classmethod
    def from_config(cls, name: str, config: Any) -> A2AWorker:
        return cls(name=name, url=str(config.url), timeout_seconds=float(getattr(config, "timeout_seconds", 120.0)), auth_token_env=getattr(config, "auth_token_env", None), allow_remote=bool(getattr(config, "allow_remote", False)), detach=bool(getattr(config, "detach", False)))

    @property
    def bearer_token(self) -> str | None:
        return os.environ.get(self.auth_token_env, "").strip() or None if self.auth_token_env else None


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
    worker: str
    task_id: str
    context_id: str
    state: str
    skill: str
    output: dict[str, Any] | None = None
    reference_task_ids: tuple[str, ...] = ()
    error_code: str | None = None
    retryable: bool | None = None
    raw: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)


class A2AClient:
    """A non-streaming Hermes profile client. Discovery is intentionally per call."""

    def __init__(self, worker: A2AWorker, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.worker = worker
        self._transport = transport
        self._schemas = ContractSchemas.load()

    async def discover(self) -> dict[str, Any]:
        async with self._client() as client:
            for path in _AGENT_CARD_PATHS:
                try:
                    response = await client.get(self._url(path), headers=self._headers())
                except httpx.HTTPError as exc:
                    raise A2ATransportError(f"A2A worker '{self.worker.name}' card request failed") from exc
                if response.status_code == 404 and path != _AGENT_CARD_PATHS[-1]:
                    continue
                if not 200 <= response.status_code < 300:
                    raise A2ATransportError(f"A2A worker '{self.worker.name}' card returned HTTP {response.status_code}")
                try:
                    card = response.json()
                except ValueError as exc:
                    raise A2ATransportError("A2A worker returned invalid Agent Card JSON") from exc
                if not isinstance(card, dict):
                    raise A2AProtocolError("A2A Agent Card must be an object")
                self._select_interface(card)
                self._advertised_skills(card)
                return card
        raise A2ATransportError(f"A2A worker '{self.worker.name}' did not return an Agent Card")

    async def invoke_skill(self, skill: str, input: dict[str, Any], *, context_id: str | None = None, reference_task_ids: tuple[str, ...] | list[str] = ()) -> A2AWorkerResult:
        invocation = {"skill": skill, "input": input}
        self._validate_invocation(invocation)
        card = await self.discover()
        if skill not in self._advertised_skills(card):
            raise A2AProtocolError(f"A2A Agent Card does not advertise skill '{skill}'")
        endpoint, tenant = self._select_interface(card)
        context = str(context_id or "").strip() or f"ctx-{uuid.uuid4().hex[:16]}"
        references = self._references(reference_task_ids)
        message: dict[str, Any] = {"role": "ROLE_USER", "messageId": uuid.uuid4().hex, "contextId": context, "parts": [{"data": invocation, "mediaType": "application/json"}]}
        if references:
            message["referenceTaskIds"] = list(references)
        params: dict[str, Any] = {"message": message}
        if tenant:
            params["tenant"] = tenant
        return self._task_result(await self._rpc(endpoint, "message/send", params), skill, context, references)

    async def get_task(self, task_id: str, *, skill: str, context_id: str, reference_task_ids: tuple[str, ...] | list[str] = ()) -> A2AWorkerResult:
        task = str(task_id or "").strip()
        context = str(context_id or "").strip()
        if not task or not context:
            raise A2AProtocolError("A2A task and context ids are required")
        self._validate_invocation({"skill": skill, "input": {}}, validate_request=False)
        card = await self.discover()
        if skill not in self._advertised_skills(card):
            raise A2AProtocolError(f"A2A Agent Card does not advertise skill '{skill}'")
        endpoint, tenant = self._select_interface(card)
        params: dict[str, Any] = {"id": task}
        if tenant:
            params["tenant"] = tenant
        return self._task_result(
            await self._rpc(endpoint, "tasks/get", params),
            skill,
            context,
            self._references(reference_task_ids),
            expected_task_id=task,
        )

    async def poll_task(self, task_id: str, *, skill: str, context_id: str, reference_task_ids: tuple[str, ...] | list[str] = (), deadline_seconds: float = 1800, interval_seconds: float = 5) -> A2AWorkerResult:
        if deadline_seconds <= 0 or deadline_seconds > 1800 or interval_seconds <= 0 or interval_seconds > 60:
            raise A2AProtocolError("A2A polling bounds are invalid")
        result: A2AWorkerResult | None = None
        deadline = asyncio.get_running_loop().time() + deadline_seconds
        while asyncio.get_running_loop().time() < deadline:
            remaining = deadline - asyncio.get_running_loop().time()
            try:
                result = await asyncio.wait_for(
                    self.get_task(task_id, skill=skill, context_id=context_id, reference_task_ids=reference_task_ids),
                    timeout=remaining,
                )
            except TimeoutError as exc:
                raise A2APollTimeoutError("A2A task polling timed out") from exc
            if result.state in _TERMINAL_STATES:
                return result
            await asyncio.sleep(min(interval_seconds, max(0, deadline - asyncio.get_running_loop().time())))
        raise A2APollTimeoutError("A2A task polling timed out")

    def _validate_invocation(self, invocation: dict[str, Any], *, validate_request: bool = True) -> None:
        try:
            self._schemas.validate_invocation(invocation)
            if validate_request:
                self._schemas.validate_request(str(invocation["skill"]), invocation["input"])
        except A2AContractValidationError as exc:
            # The caller can fix this, so the message names the offending fields. Retryable
            # because a corrected invocation is expected to succeed and nothing was sent yet.
            raise A2AProtocolError(
                f"A2A invocation rejected locally: {exc}", retryable=True
            ) from exc

    def _task_result(
        self,
        result: Any,
        skill: str,
        context: str,
        references: tuple[str, ...],
        *,
        expected_task_id: str | None = None,
    ) -> A2AWorkerResult:
        task = result.get("task") if isinstance(result, dict) and isinstance(result.get("task"), dict) else result
        if not isinstance(task, dict):
            raise A2AProtocolError("A2A result must contain a task object")
        task_id, task_context, status = task.get("id"), task.get("contextId"), task.get("status")
        if not isinstance(task_id, str) or not task_id.strip():
            raise A2AProtocolError("A2A task id is required")
        if expected_task_id is not None and task_id != expected_task_id:
            raise A2AProtocolError("A2A task id does not match request")
        if not isinstance(task_context, str) or task_context != context:
            raise A2AProtocolError("A2A task context does not match invocation")
        if not isinstance(status, dict) or status.get("state") not in _V1_STATES:
            raise A2AProtocolError("A2A task state is invalid")
        state = status["state"]
        output, error_code, retryable = self._structured_output(task, skill, task_id, context, references) if state in _TERMINAL_STATES else (None, None, None)
        return A2AWorkerResult(worker=self.worker.name, task_id=task_id, context_id=context, state=state, skill=skill, output=output, reference_task_ids=references, error_code=error_code, retryable=retryable, raw=task)

    def _structured_output(self, task: dict[str, Any], skill: str, task_id: str, context: str, references: tuple[str, ...]) -> tuple[dict[str, Any] | None, str | None, bool | None]:
        artifacts = task.get("artifacts")
        if not isinstance(artifacts, list) or len(artifacts) != 1:
            raise A2AProtocolError("A2A final task requires one structured artifact")
        parts = artifacts[0].get("parts") if isinstance(artifacts[0], dict) else None
        if not isinstance(parts, list) or len(parts) != 1:
            raise A2AProtocolError("A2A final artifact requires one structured part")
        part = parts[0]
        if not isinstance(part, dict) or part.get("mediaType") != "application/json" or not isinstance(part.get("data"), dict):
            raise A2AProtocolError("A2A final artifact must contain JSON data")
        structured = part["data"]
        try:
            self._schemas.validate_result(structured)
        except A2AContractValidationError as exc:
            raise A2AProtocolError("A2A structured result is invalid") from exc
        if structured.get("skill") != skill:
            raise A2AProtocolError("A2A result skill does not match invocation")
        correlation = structured.get("correlation")
        if not isinstance(correlation, dict) or correlation.get("task_id") != task_id or correlation.get("context_id") != context:
            raise A2AProtocolError("A2A result correlation does not match task")
        if tuple(correlation.get("reference_task_ids", ())) != references:
            raise A2AProtocolError("A2A result references do not match invocation")
        output = structured.get("output")
        expected_status = {
            "TASK_STATE_COMPLETED": "completed",
            "TASK_STATE_REJECTED": "rejected",
            "TASK_STATE_FAILED": "failed",
            "TASK_STATE_CANCELED": "failed",
        }.get(task.get("status", {}).get("state"))
        if expected_status is not None and structured.get("status") != expected_status:
            raise A2AProtocolError("A2A task state and profile result status disagree")
        if structured["status"] == "completed":
            if not isinstance(output, dict):
                raise A2AProtocolError("A2A completed result requires output")
            try:
                self._schemas.validate_response(skill, output)
            except A2AContractValidationError as exc:
                raise A2AProtocolError("A2A structured output is invalid") from exc
            return output, None, None
        if output is not None:
            raise A2AProtocolError("A2A non-completed result cannot contain output")
        error = structured.get("error")
        if not isinstance(error, dict) or not isinstance(error.get("code"), str) or not isinstance(error.get("retryable"), bool):
            raise A2AProtocolError("A2A terminal failure requires structured error")
        return None, error["code"], error["retryable"]

    async def _rpc(self, endpoint: str, method: str, params: dict[str, Any]) -> Any:
        request_id = uuid.uuid4().hex
        payload = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        async with self._client() as client:
            try:
                response = await client.post(endpoint, headers={**self._headers(), "Content-Type": "application/json", "A2A-Version": _A2A_VERSION}, json=payload)
            except httpx.HTTPError as exc:
                raise A2ATransportError(f"A2A worker '{self.worker.name}' request failed") from exc
        if not 200 <= response.status_code < 300:
            raise A2ATransportError(f"A2A worker '{self.worker.name}' returned HTTP {response.status_code}")
        try:
            body = response.json()
        except ValueError as exc:
            raise A2ATransportError("A2A worker returned invalid JSON-RPC JSON") from exc
        if not isinstance(body, dict):
            raise A2AProtocolError("A2A JSON-RPC response must be an object")
        if body.get("jsonrpc") != "2.0" or body.get("id") != request_id:
            raise A2AProtocolError("A2A JSON-RPC response id is invalid")
        error = body.get("error")
        if isinstance(error, dict):
            if set(body) != {"jsonrpc", "id", "error"} or isinstance(error.get("code"), bool) or not isinstance(error.get("code"), int) or not isinstance(error.get("message"), str):
                raise A2AProtocolError("A2A JSON-RPC error envelope is invalid")
            raise A2AProtocolError("A2A worker returned an error", code=error["code"], retryable=False)
        if set(body) != {"jsonrpc", "id", "result"}:
            raise A2AProtocolError("A2A JSON-RPC response must contain a result")
        return body["result"]

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=self._transport, timeout=httpx.Timeout(self.worker.timeout_seconds), follow_redirects=False)

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if token := self.worker.bearer_token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    def _url(self, path: str) -> str:
        return urljoin(f"{self.worker.url}/", path.lstrip("/"))

    def _select_interface(self, card: dict[str, Any]) -> tuple[str, str | None]:
        interfaces = card.get("supportedInterfaces")
        if not isinstance(interfaces, list):
            raise A2AProtocolError("A2A Agent Card must advertise a v1 JSON-RPC interface")
        for interface in interfaces:
            if not isinstance(interface, dict):
                raise A2AProtocolError("A2A Agent Card interface is malformed")
            if interface.get("protocolBinding") == "JSONRPC" and interface.get("protocolVersion") in {"1.0", "1.0.0"}:
                advertised = interface.get("url")
                if isinstance(advertised, str) and advertised.strip():
                    return self._resolve_endpoint(advertised), str(interface.get("tenant") or "").strip() or None
        raise A2AProtocolError("A2A Agent Card must advertise a v1 JSON-RPC interface")

    def _advertised_skills(self, card: dict[str, Any]) -> frozenset[str]:
        if card.get("version") != CONTRACT_RELEASE:
            raise A2AProtocolError("A2A Agent Card version is incompatible")
        capabilities = card.get("capabilities")
        extensions = capabilities.get("extensions") if isinstance(capabilities, dict) else None
        if not isinstance(extensions, list) or not any(
            isinstance(item, dict)
            and item.get("uri") == PROFILE_URI
            and ("required" not in item or isinstance(item["required"], bool))
            for item in extensions
        ):
            raise A2AProtocolError("A2A Agent Card does not carry the Hermes profile")
        skills = card.get("skills")
        if not isinstance(skills, list):
            raise A2AProtocolError("A2A Agent Card skills are malformed")
        advertised: set[str] = set()
        for item in skills:
            if not isinstance(item, dict) or not isinstance(item.get("id"), str):
                raise A2AProtocolError("A2A Agent Card skill is malformed")
            input_modes, output_modes = item.get("inputModes"), item.get("outputModes")
            if not isinstance(input_modes, list) or not isinstance(output_modes, list) or any(not isinstance(mode, str) for mode in input_modes + output_modes):
                raise A2AProtocolError("A2A Agent Card skill modes are malformed")
            if "application/json" in input_modes and "application/json" in output_modes:
                advertised.add(item["id"])
        return frozenset(advertised)

    def _resolve_endpoint(self, advertised: str) -> str:
        parsed = urlparse(advertised)
        endpoint = advertised if parsed.scheme else self._url(advertised)
        parsed = urlparse(endpoint)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
            raise A2AProtocolError("A2A Agent Card advertised an unsafe JSON-RPC endpoint")
        if not self.worker.allow_remote and not _is_loopback_host(parsed.hostname):
            raise A2AProtocolError("A2A Agent Card endpoint is not loopback for a loopback-only worker")
        configured = urlparse(self.worker.url)
        if (parsed.scheme, parsed.hostname, parsed.port) != (configured.scheme, configured.hostname, configured.port):
            raise A2AProtocolError("A2A Agent Card endpoint is outside the trusted origin")
        return endpoint

    @staticmethod
    def _references(values: tuple[str, ...] | list[str]) -> tuple[str, ...]:
        references = tuple(values)
        if len(references) > 20 or any(not isinstance(value, str) or not value.strip() for value in references) or len(set(references)) != len(references):
            raise A2AProtocolError("A2A reference task ids are invalid")
        return references
