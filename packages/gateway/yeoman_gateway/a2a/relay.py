"""Standalone authenticated A2A relay for Yeoman-owned inbound skills."""

from __future__ import annotations

import hashlib
import hmac
import http.server
import ipaddress
import json
import logging
import os
import re
import socket
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from yeoman_gateway.a2a.contracts import (
    CONTRACT_RELEASE,
    PROFILE_URI,
    A2AContractValidationError,
    ContractSchemas,
)

LOG = logging.getLogger("yeoman.a2a.relay")
_PRIVATE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")
_SKILL_ID = re.compile(r"^(conversation|[a-z][a-z0-9]*(\.[a-z][a-z0-9]*)+)$")
_ALLOWED_CONTENT_TYPES = frozenset({"text", "image", "file", "voice"})
_CGNAT = ipaddress.ip_network("100.64.0.0/10")
_TERMINAL_STATES = frozenset({"TASK_STATE_COMPLETED", "TASK_STATE_REJECTED", "TASK_STATE_FAILED"})
_SKILL_CARDS: dict[str, tuple[str, str, list[str]]] = {
    "whatsapp.send": (
        "WhatsApp delivery",
        "Deliver policy-approved content to a configured recipient alias.",
        ["whatsapp", "delivery"],
    ),
    "media.voice.generate": (
        "Voice generation",
        "Generate a deliverable voice artifact using the configured runtime.",
        ["voice", "audio"],
    ),
}


class RelayConfigurationError(ValueError):
    """The relay environment is unsafe or incomplete."""


class _RequestError(ValueError):
    def __init__(self, rpc_code: int, message: str) -> None:
        self.rpc_code = rpc_code
        self.message = message
        super().__init__(message)


class _ProfileRejectionError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


class _IPCError(RuntimeError):
    pass


def _csv(value: str) -> frozenset[str]:
    return frozenset(item.strip() for item in value.split(",") if item.strip())


def _integer(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError as exc:
        raise RelayConfigurationError(f"{name} must be an integer") from exc


def _number(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except ValueError as exc:
        raise RelayConfigurationError(f"{name} must be a number") from exc


def _boolean(name: str, default: bool) -> bool:
    value = os.environ.get(name, "true" if default else "false").strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise RelayConfigurationError(f"{name} must be a boolean")


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RelayConfigurationError(f"{name} is required")
    return value


@dataclass(frozen=True)
class RelayConfig:
    """All relay-owned runtime settings, loaded without private defaults."""

    bind_host: str
    port: int
    allowed_peer_ips: frozenset[str]
    peer_id: str
    bearer_secret: str = field(repr=False)
    socket_path: Path
    state_path: Path
    public_url: str
    whatsapp_enabled: bool
    voice_enabled: bool
    content_types: frozenset[str]
    timeout_seconds: float = 30.0
    max_body_bytes: int = 65_536
    max_response_bytes: int = 524_288
    rate_limit_per_minute: int = 60

    @classmethod
    def from_env(cls) -> RelayConfig:
        return cls(
            bind_host=_required("YEOMAN_A2A_BIND_HOST"),
            port=_integer("YEOMAN_A2A_PORT", 9900),
            allowed_peer_ips=_csv(_required("YEOMAN_A2A_ALLOWED_PEER_IPS")),
            peer_id=_required("YEOMAN_A2A_PEER_ID"),
            bearer_secret=_required("YEOMAN_A2A_BEARER_SECRET"),
            socket_path=Path(_required("YEOMAN_A2A_SOCKET_PATH")).expanduser(),
            state_path=Path(_required("YEOMAN_A2A_STATE_PATH")).expanduser(),
            public_url=_required("YEOMAN_A2A_PUBLIC_URL").rstrip("/"),
            whatsapp_enabled=_boolean("YEOMAN_A2A_WHATSAPP_ENABLED", False),
            voice_enabled=_boolean("YEOMAN_A2A_VOICE_ENABLED", False),
            content_types=_csv(os.environ.get("YEOMAN_A2A_CONTENT_TYPES", "text")),
            timeout_seconds=_number("YEOMAN_A2A_TIMEOUT_SECONDS", 30.0),
            max_body_bytes=_integer("YEOMAN_A2A_MAX_BODY_BYTES", 65_536),
            max_response_bytes=_integer("YEOMAN_A2A_MAX_RESPONSE_BYTES", 524_288),
            rate_limit_per_minute=_integer("YEOMAN_A2A_RATE_LIMIT_PER_MINUTE", 60),
        )

    def validate(self) -> None:
        try:
            bind = ipaddress.ip_address(self.bind_host)
        except ValueError as exc:
            raise RelayConfigurationError(
                "bind host must be an explicit private IP address"
            ) from exc
        if bind.is_unspecified or not (bind.is_private or bind.is_loopback or bind in _CGNAT):
            raise RelayConfigurationError("bind host must be an explicit private IP address")
        if not 1 <= self.port <= 65_535:
            raise RelayConfigurationError("port must be between 1 and 65535")
        if not self.allowed_peer_ips:
            raise RelayConfigurationError("at least one peer IP is required")
        try:
            for value in self.allowed_peer_ips:
                ipaddress.ip_address(value.split("%", 1)[0])
        except ValueError as exc:
            raise RelayConfigurationError("allowed peer IPs must be IP addresses") from exc
        if not _PRIVATE_ID.fullmatch(self.peer_id):
            raise RelayConfigurationError("peer ID is malformed")
        if not self.bearer_secret:
            raise RelayConfigurationError("bearer secret is required")
        if not self.socket_path.is_absolute() or not self.state_path.is_absolute():
            raise RelayConfigurationError("socket and state paths must be absolute")
        parsed = urlparse(self.public_url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise RelayConfigurationError("public URL must be an HTTPS URL without credentials")
        if parsed.hostname.lower().rstrip(".") in {"localhost", "ip6-localhost"}:
            raise RelayConfigurationError("public URL cannot advertise an internal host")
        if parsed.query or parsed.fragment:
            raise RelayConfigurationError("public URL cannot contain a query or fragment")
        try:
            advertised_ip = ipaddress.ip_address(parsed.hostname)
        except ValueError:
            advertised_ip = None
        if advertised_ip is not None and not advertised_ip.is_global:
            raise RelayConfigurationError("public URL cannot advertise an internal IP address")
        if not self.content_types or not self.content_types <= _ALLOWED_CONTENT_TYPES:
            raise RelayConfigurationError("content types contain an unsupported value")
        if (
            self.timeout_seconds <= 0
            or self.max_body_bytes <= 0
            or self.max_response_bytes <= 0
            or self.rate_limit_per_minute <= 0
        ):
            raise RelayConfigurationError("time, size, and rate limits must be positive")


@dataclass(frozen=True)
class _Claim:
    task_id: str
    effect_id: str
    request_hash: str
    context_id: str
    reference_task_ids: list[str]
    task: dict[str, Any]
    conflict: bool = False


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _audit() -> dict[str, str]:
    return {"profile": PROFILE_URI, "contractRelease": CONTRACT_RELEASE}


def _opaque(value: Any) -> bool:
    return isinstance(value, str) and 0 < len(value) <= 200


def _correlation(
    task_id: str,
    context_id: str,
    reference_task_ids: list[str],
    idempotency_key: str | None,
) -> dict[str, Any]:
    value: dict[str, Any] = {"task_id": task_id}
    if context_id:
        value["context_id"] = context_id
    if reference_task_ids:
        value["reference_task_ids"] = reference_task_ids
    if idempotency_key is not None:
        value["idempotency_key"] = idempotency_key
    return value


def _profile_failure(
    skill: str,
    status: str,
    code: str,
    message: str,
    correlation: dict[str, Any],
    *,
    retryable: bool = False,
) -> dict[str, Any]:
    return {
        "skill": skill,
        "status": status,
        "error": {"code": code, "message": message, "retryable": retryable},
        "correlation": correlation,
    }


def _task(
    task_id: str,
    context_id: str,
    state: str,
    result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    task: dict[str, Any] = {
        "id": task_id,
        "contextId": context_id,
        "status": {"state": state, "timestamp": _now()},
        "metadata": _audit(),
    }
    if result is not None:
        task["artifacts"] = [
            {
                "artifactId": f"result-{task_id}",
                "parts": [{"data": result, "mediaType": "application/json"}],
            }
        ]
    return task


def canonical_request_hash(invocation: dict[str, Any]) -> str:
    return hashlib.sha256(_json(invocation).encode()).hexdigest()


def _effect_id(peer: str, skill: str, key: str) -> str:
    digest = hashlib.sha256(f"{peer}\0{skill}\0{key}".encode()).hexdigest()
    return f"a2a-effect-{digest[:40]}"


class _RelayStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    task_id TEXT PRIMARY KEY,
                    peer TEXT NOT NULL,
                    context_id TEXT NOT NULL,
                    reference_task_ids TEXT NOT NULL,
                    task_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS tasks_peer_id ON tasks(peer, task_id);
                CREATE TABLE IF NOT EXISTS idempotency (
                    peer TEXT NOT NULL,
                    skill TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    request_hash TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    effect_id TEXT NOT NULL,
                    lease_until REAL NOT NULL DEFAULT 0,
                    lease_token TEXT,
                    PRIMARY KEY(peer, skill, idempotency_key),
                    FOREIGN KEY(task_id) REFERENCES tasks(task_id)
                );
                """
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def claim(
        self,
        *,
        peer: str,
        invocation: dict[str, Any],
        context_id: str,
        reference_task_ids: list[str],
    ) -> _Claim:
        skill = invocation["skill"]
        key = invocation["input"]["idempotency_key"]
        request_hash = canonical_request_hash(invocation)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """SELECT request_hash, task_id, effect_id
                   FROM idempotency WHERE peer=? AND skill=? AND idempotency_key=?""",
                (peer, skill, key),
            ).fetchone()
            if row is not None:
                old_hash, task_id, effect_id = row
                if old_hash != request_hash:
                    return self._conflict(
                        connection,
                        peer,
                        skill,
                        key,
                        request_hash,
                        context_id,
                        reference_task_ids,
                    )
                task_row = connection.execute(
                    """SELECT context_id, reference_task_ids, task_json
                       FROM tasks WHERE task_id=? AND peer=?""",
                    (task_id, peer),
                ).fetchone()
                if task_row is None:
                    raise RuntimeError("idempotency row has no task")
                stored_context, stored_references, task_json = task_row
                return _Claim(
                    task_id,
                    effect_id,
                    request_hash,
                    stored_context,
                    json.loads(stored_references),
                    json.loads(task_json),
                )

            task_id = f"task-{uuid.uuid4().hex}"
            context = context_id or f"context-{uuid.uuid4().hex}"
            effect_id = _effect_id(peer, skill, key)
            task = _task(task_id, context, "TASK_STATE_SUBMITTED")
            connection.execute(
                """INSERT INTO tasks
                   (task_id, peer, context_id, reference_task_ids, task_json)
                   VALUES (?, ?, ?, ?, ?)""",
                (task_id, peer, context, _json(reference_task_ids), _json(task)),
            )
            connection.execute(
                """INSERT INTO idempotency
                   (peer, skill, idempotency_key, request_hash, task_id, effect_id)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (peer, skill, key, request_hash, task_id, effect_id),
            )
            return _Claim(task_id, effect_id, request_hash, context, reference_task_ids, task)

    def _conflict(
        self,
        connection: sqlite3.Connection,
        peer: str,
        skill: str,
        key: str,
        request_hash: str,
        context_id: str,
        reference_task_ids: list[str],
    ) -> _Claim:
        digest = hashlib.sha256(f"{peer}\0{skill}\0{key}\0{request_hash}".encode()).hexdigest()
        task_id = f"task-conflict-{digest[:32]}"
        existing = connection.execute(
            """SELECT context_id, reference_task_ids, task_json
               FROM tasks WHERE task_id=? AND peer=?""",
            (task_id, peer),
        ).fetchone()
        if existing is not None:
            stored_context, stored_references, task_json = existing
            task = json.loads(task_json)
            references = json.loads(stored_references)
        else:
            stored_context = context_id or f"context-{uuid.uuid4().hex}"
            references = reference_task_ids
            correlation = _correlation(task_id, stored_context, references, key)
            result = _profile_failure(
                skill,
                "rejected",
                "IDEMPOTENCY_CONFLICT",
                "The idempotency key was already used for a different request.",
                correlation,
            )
            task = _task(task_id, stored_context, "TASK_STATE_REJECTED", result)
            connection.execute(
                """INSERT INTO tasks
                   (task_id, peer, context_id, reference_task_ids, task_json)
                   VALUES (?, ?, ?, ?, ?)""",
                (task_id, peer, stored_context, _json(references), _json(task)),
            )
        return _Claim(
            task_id,
            "",
            request_hash,
            stored_context,
            references,
            task,
            conflict=True,
        )

    def acquire(
        self, claim: _Claim, peer: str, lease_seconds: float
    ) -> tuple[str | None, dict[str, Any]]:
        token = uuid.uuid4().hex
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """SELECT i.lease_until, t.task_json
                   FROM idempotency AS i JOIN tasks AS t ON t.task_id=i.task_id
                   WHERE i.peer=? AND i.task_id=? AND i.request_hash=?""",
                (peer, claim.task_id, claim.request_hash),
            ).fetchone()
            if row is None:
                raise RuntimeError("claim disappeared")
            lease_until, task_json = row
            task = json.loads(task_json)
            if task["status"]["state"] in _TERMINAL_STATES or lease_until > time.time():
                return None, task
            connection.execute(
                """UPDATE idempotency SET lease_until=?, lease_token=?
                   WHERE peer=? AND task_id=? AND request_hash=?""",
                (time.time() + lease_seconds, token, peer, claim.task_id, claim.request_hash),
            )
            return token, task

    def finish(
        self,
        *,
        peer: str,
        claim: _Claim,
        lease_token: str,
        task: dict[str, Any],
    ) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """SELECT i.lease_token, t.task_json
                   FROM idempotency AS i JOIN tasks AS t ON t.task_id=i.task_id
                   WHERE i.peer=? AND i.task_id=? AND i.request_hash=?""",
                (peer, claim.task_id, claim.request_hash),
            ).fetchone()
            if row is None:
                raise RuntimeError("claim disappeared")
            current_token, task_json = row
            if current_token != lease_token:
                return json.loads(task_json)
            connection.execute(
                "UPDATE tasks SET task_json=? WHERE task_id=? AND peer=?",
                (_json(task), claim.task_id, peer),
            )
            connection.execute(
                """UPDATE idempotency SET lease_until=0, lease_token=NULL
                   WHERE peer=? AND task_id=? AND request_hash=?""",
                (peer, claim.task_id, claim.request_hash),
            )
            return task

    def remember(
        self,
        peer: str,
        task: dict[str, Any],
        context_id: str,
        reference_task_ids: list[str],
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO tasks
                   (task_id, peer, context_id, reference_task_ids, task_json)
                   VALUES (?, ?, ?, ?, ?)""",
                (task["id"], peer, context_id, _json(reference_task_ids), _json(task)),
            )

    def get(self, peer: str, task_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT task_json FROM tasks WHERE peer=? AND task_id=?", (peer, task_id)
            ).fetchone()
        return json.loads(row[0]) if row else None


class RelayService:
    """Protocol logic shared by the thin HTTP handler."""

    def __init__(self, config: RelayConfig, *, schemas: ContractSchemas | None = None) -> None:
        self.config = config
        self.store = _RelayStore(config.state_path)
        self.schemas = schemas or ContractSchemas.load()
        self._rate_lock = threading.Lock()
        self._requests: dict[str, list[float]] = {}

    def _configured_skills(self) -> tuple[str, ...]:
        skills: list[str] = []
        if self.config.whatsapp_enabled:
            skills.append("whatsapp.send")
        if self.config.voice_enabled:
            skills.append("media.voice.generate")
        return tuple(skills)

    def _downstream_ready(self) -> bool:
        try:
            envelope = self._socket_call({"cmd": "ping"})
        except _IPCError:
            return False
        return envelope == {"status": "ok", "response": "pong"}

    def agent_card(self) -> dict[str, Any]:
        skills = []
        if self._downstream_ready():
            for skill_id in self._configured_skills():
                name, description, tags = _SKILL_CARDS[skill_id]
                skills.append(
                    {
                        "id": skill_id,
                        "name": name,
                        "description": description,
                        "tags": tags,
                        "inputModes": ["application/json"],
                        "outputModes": ["application/json"],
                    }
                )
        return {
            "name": "Yeoman",
            "description": "Policy-controlled structured messaging capabilities.",
            "supportedInterfaces": [
                {
                    "url": self.config.public_url,
                    "protocolBinding": "JSONRPC",
                    "protocolVersion": "1.0",
                }
            ],
            "version": CONTRACT_RELEASE,
            "capabilities": {
                "streaming": False,
                "pushNotifications": False,
                "extendedAgentCard": False,
                "extensions": [
                    {
                        "uri": PROFILE_URI,
                        "description": "Hermes/Yeoman structured skill profile v1",
                        "required": True,
                    }
                ],
            },
            "securitySchemes": {"bearer": {"httpAuthSecurityScheme": {"scheme": "Bearer"}}},
            "securityRequirements": [{"schemes": {"bearer": {"list": []}}}],
            "defaultInputModes": ["application/json"],
            "defaultOutputModes": ["application/json"],
            "skills": skills,
        }

    def authenticate(self, peer_ip: str, authorization: str) -> bool:
        try:
            normalized = str(ipaddress.ip_address(peer_ip.split("%", 1)[0]))
            allowed = {
                str(ipaddress.ip_address(value.split("%", 1)[0]))
                for value in self.config.allowed_peer_ips
            }
        except ValueError:
            return False
        scheme, separator, token = authorization.partition(" ")
        return (
            normalized in allowed
            and separator == " "
            and scheme.lower() == "bearer"
            and bool(token)
            and hmac.compare_digest(token, self.config.bearer_secret)
        )

    def rate_allowed(self, peer_ip: str) -> bool:
        now = time.monotonic()
        with self._rate_lock:
            recent = [seen for seen in self._requests.get(peer_ip, []) if seen > now - 60]
            if len(recent) >= self.config.rate_limit_per_minute:
                self._requests[peer_ip] = recent
                return False
            recent.append(now)
            self._requests[peer_ip] = recent
            return True

    @staticmethod
    def rpc_error(request_id: Any, code: int, message: str) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}

    @staticmethod
    def _check_fields(value: Any, allowed: set[str], required: set[str]) -> dict[str, Any]:
        if not isinstance(value, dict) or set(value) - allowed or not required <= set(value):
            raise _RequestError(-32602, "Invalid params")
        return value

    def dispatch(self, request: Any) -> dict[str, Any]:
        request_id = request.get("id") if isinstance(request, dict) else None
        try:
            if (
                not isinstance(request, dict)
                or set(request) != {"jsonrpc", "id", "method", "params"}
                or request.get("jsonrpc") != "2.0"
                or isinstance(request.get("id"), bool)
                or not isinstance(request.get("id"), (str, int))
                or not isinstance(request.get("method"), str)
                or not isinstance(request.get("params"), dict)
            ):
                raise _RequestError(-32600, "Invalid Request")
            method = request["method"]
            if method in {"GetTask", "tasks/get"}:
                return self._get_task(request_id, request["params"])
            if method in {"SendMessage", "message/send"}:
                return self._send_message(request_id, request["params"])
            return self.rpc_error(request_id, -32601, "Method not found")
        except _RequestError as exc:
            return self.rpc_error(request_id, exc.rpc_code, exc.message)

    def _get_task(self, request_id: Any, params: dict[str, Any]) -> dict[str, Any]:
        self._check_fields(params, {"tenant", "id", "historyLength"}, {"id"})
        task_id = params["id"]
        if not _opaque(task_id):
            raise _RequestError(-32602, "Invalid params")
        if "tenant" in params and not isinstance(params["tenant"], str):
            raise _RequestError(-32602, "Invalid params")
        history_length = params.get("historyLength")
        if history_length is not None and (
            isinstance(history_length, bool)
            or not isinstance(history_length, int)
            or history_length < 0
        ):
            raise _RequestError(-32602, "Invalid params")
        task = self.store.get(self.config.peer_id, task_id)
        if task is None:
            return self.rpc_error(request_id, -32001, "Task not found")
        return {"jsonrpc": "2.0", "id": request_id, "result": task}

    def _send_message(self, request_id: Any, params: dict[str, Any]) -> dict[str, Any]:
        invocation, context_id, references = self._parse_send(params)
        skill = invocation.get("skill") if isinstance(invocation, dict) else None
        if not isinstance(skill, str) or not _SKILL_ID.fullmatch(skill):
            raise _RequestError(-32602, "Invalid params")
        try:
            self._validate_profile(invocation)
        except _ProfileRejectionError as exc:
            return self._rejected(request_id, skill, invocation, context_id, references, exc)

        key = invocation["input"]["idempotency_key"]
        claim = self.store.claim(
            peer=self.config.peer_id,
            invocation=invocation,
            context_id=context_id,
            reference_task_ids=references,
        )
        if claim.conflict or claim.task["status"]["state"] in _TERMINAL_STATES:
            return {"jsonrpc": "2.0", "id": request_id, "result": {"task": claim.task}}

        deadline = time.monotonic() + (self.config.timeout_seconds * 3) + 1
        lease_token: str | None = None
        task = claim.task
        while lease_token is None and time.monotonic() < deadline:
            lease_token, task = self.store.acquire(
                claim, self.config.peer_id, self.config.timeout_seconds * 2 + 0.5
            )
            if task["status"]["state"] in _TERMINAL_STATES:
                return {"jsonrpc": "2.0", "id": request_id, "result": {"task": task}}
            if lease_token is None:
                time.sleep(0.01)
        if lease_token is None:
            return {"jsonrpc": "2.0", "id": request_id, "result": {"task": task}}

        correlation = _correlation(
            claim.task_id,
            claim.context_id,
            claim.reference_task_ids,
            key,
        )
        try:
            result = self._invoke(
                invocation,
                claim.task_id,
                claim.context_id,
                claim.effect_id,
            )
            result = self._validated_runtime_result(
                skill, result, correlation, claim.reference_task_ids
            )
            state = {
                "completed": "TASK_STATE_COMPLETED",
                "rejected": "TASK_STATE_REJECTED",
                "failed": "TASK_STATE_FAILED",
            }.get(result["status"], "TASK_STATE_WORKING")
        except A2AContractValidationError, _IPCError, KeyError, TypeError:
            result = _profile_failure(
                skill,
                "failed",
                "INVALID_UPSTREAM_RESULT",
                "The local runtime returned an invalid structured result.",
                correlation,
            )
            state = "TASK_STATE_FAILED"
        task = _task(claim.task_id, claim.context_id, state, result)
        task = self.store.finish(
            peer=self.config.peer_id,
            claim=claim,
            lease_token=lease_token,
            task=task,
        )
        LOG.info("event=a2a_task task_id=%s skill=%s code=%s", claim.task_id, skill, state)
        return {"jsonrpc": "2.0", "id": request_id, "result": {"task": task}}

    def _parse_send(self, params: dict[str, Any]) -> tuple[dict[str, Any], str, list[str]]:
        self._check_fields(
            params,
            {"tenant", "message", "configuration", "metadata"},
            {"message"},
        )
        if "tenant" in params and not isinstance(params["tenant"], str):
            raise _RequestError(-32602, "Invalid params")
        if "metadata" in params and not isinstance(params["metadata"], dict):
            raise _RequestError(-32602, "Invalid params")
        self._parse_configuration(params.get("configuration"))
        message = self._check_fields(
            params["message"],
            {
                "messageId",
                "contextId",
                "taskId",
                "role",
                "parts",
                "metadata",
                "extensions",
                "referenceTaskIds",
            },
            {"messageId", "role", "parts"},
        )
        if message["role"] != "ROLE_USER" or not _opaque(message["messageId"]):
            raise _RequestError(-32602, "Invalid params")
        if "taskId" in message:
            if not _opaque(message["taskId"]):
                raise _RequestError(-32602, "Invalid params")
            raise _RequestError(-32602, "Invalid params")
        if "metadata" in message and not isinstance(message["metadata"], dict):
            raise _RequestError(-32602, "Invalid params")
        if "extensions" in message and (
            not isinstance(message["extensions"], list)
            or any(not isinstance(item, str) for item in message["extensions"])
        ):
            raise _RequestError(-32602, "Invalid params")

        context_id = message.get("contextId", "")
        if "contextId" in message and not _opaque(context_id):
            raise _RequestError(-32602, "Invalid params")
        references = message.get("referenceTaskIds", [])
        if (
            not isinstance(references, list)
            or len(references) > 20
            or any(not _opaque(value) for value in references)
            or len(set(references)) != len(references)
        ):
            raise _RequestError(-32602, "Invalid params")
        parts = message["parts"]
        if not isinstance(parts, list) or not parts:
            raise _RequestError(-32602, "Invalid params")
        authoritative: list[Any] = []
        for part in parts:
            parsed = self._check_fields(
                part,
                {"text", "raw", "url", "data", "metadata", "filename", "mediaType"},
                set(),
            )
            content = [key for key in ("text", "raw", "url", "data") if key in parsed]
            if len(content) != 1 or content[0] not in {"text", "data"}:
                raise _RequestError(-32602, "Invalid params")
            if "metadata" in parsed and not isinstance(parsed["metadata"], dict):
                raise _RequestError(-32602, "Invalid params")
            if "filename" in parsed and not isinstance(parsed["filename"], str):
                raise _RequestError(-32602, "Invalid params")
            if "mediaType" in parsed and not isinstance(parsed["mediaType"], str):
                raise _RequestError(-32602, "Invalid params")
            if content[0] == "text" and not isinstance(parsed["text"], str):
                raise _RequestError(-32602, "Invalid params")
            if content[0] == "data" and parsed.get("mediaType") == "application/json":
                authoritative.append(parsed["data"])
        if len(authoritative) != 1:
            raise _RequestError(-32602, "Invalid params")
        return authoritative[0], context_id, references

    @staticmethod
    def _parse_configuration(configuration: Any) -> None:
        if configuration is None:
            return
        if not isinstance(configuration, dict) or set(configuration) - {
            "acceptedOutputModes",
            "taskPushNotificationConfig",
            "historyLength",
            "returnImmediately",
        }:
            raise _RequestError(-32602, "Invalid params")
        modes = configuration.get("acceptedOutputModes")
        if modes is not None and (
            not isinstance(modes, list) or any(not isinstance(mode, str) for mode in modes)
        ):
            raise _RequestError(-32602, "Invalid params")
        history = configuration.get("historyLength")
        if history is not None and (
            isinstance(history, bool) or not isinstance(history, int) or history < 0
        ):
            raise _RequestError(-32602, "Invalid params")
        immediate = configuration.get("returnImmediately")
        if immediate is not None and not isinstance(immediate, bool):
            raise _RequestError(-32602, "Invalid params")
        if "taskPushNotificationConfig" in configuration:
            raise _RequestError(-32602, "Invalid params")

    def _validate_profile(self, invocation: dict[str, Any]) -> None:
        try:
            self.schemas.validate_invocation(invocation)
        except A2AContractValidationError as exc:
            raise _ProfileRejectionError(
                "INVALID_INVOCATION", "The invocation is invalid."
            ) from exc
        skill = invocation["skill"]
        if skill not in self._configured_skills():
            raise _ProfileRejectionError(
                "SKILL_NOT_ADVERTISED", "The requested skill is not advertised."
            )
        try:
            self.schemas.validate_request(skill, invocation["input"])
        except A2AContractValidationError as exc:
            raise _ProfileRejectionError(
                "INVALID_SKILL_INPUT", "The skill input is invalid."
            ) from exc
        if skill == "whatsapp.send":
            requested_types = {part["type"] for part in invocation["input"]["content"]}
            if not requested_types <= self.config.content_types:
                raise _ProfileRejectionError(
                    "CONTENT_TYPE_DENIED", "A requested content type is not enabled."
                )

    def _rejected(
        self,
        request_id: Any,
        skill: str,
        invocation: dict[str, Any],
        context_id: str,
        references: list[str],
        rejection: _ProfileRejectionError,
    ) -> dict[str, Any]:
        task_id = f"task-{uuid.uuid4().hex}"
        task_context = context_id or f"context-{uuid.uuid4().hex}"
        raw_input = invocation.get("input")
        raw_key = raw_input.get("idempotency_key") if isinstance(raw_input, dict) else None
        key = raw_key if isinstance(raw_key, str) and _PRIVATE_ID.fullmatch(raw_key) else None
        result = _profile_failure(
            skill,
            "rejected",
            rejection.code,
            rejection.message,
            _correlation(task_id, task_context, references, key),
        )
        self.schemas.validate_result(result)
        task = _task(task_id, task_context, "TASK_STATE_REJECTED", result)
        self.store.remember(self.config.peer_id, task, task_context, references)
        LOG.info(
            "event=a2a_task task_id=%s skill=%s code=%s",
            task_id,
            skill,
            rejection.code,
        )
        return {"jsonrpc": "2.0", "id": request_id, "result": {"task": task}}

    def _socket_call(self, request: dict[str, Any]) -> dict[str, Any]:
        encoded = (_json(request) + "\n").encode()
        if len(encoded) > self.config.max_body_bytes:
            raise _IPCError("IPC_REQUEST_TOO_LARGE")
        deadline = time.monotonic() + self.config.timeout_seconds
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(self.config.timeout_seconds)
                client.connect(str(self.config.socket_path))
                client.sendall(encoded)
                response = bytearray()
                while not response.endswith(b"\n"):
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise _IPCError("IPC_TIMEOUT")
                    client.settimeout(remaining)
                    chunk = client.recv(65_536)
                    if not chunk:
                        break
                    response.extend(chunk)
                    if len(response) > self.config.max_response_bytes:
                        raise _IPCError("IPC_RESPONSE_TOO_LARGE")
        except _IPCError:
            raise
        except (OSError, TimeoutError) as exc:
            raise _IPCError("IPC_UNAVAILABLE") from exc
        try:
            envelope = json.loads(response)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise _IPCError("IPC_INVALID_JSON") from exc
        if not isinstance(envelope, dict):
            raise _IPCError("IPC_INVALID_RESULT")
        return envelope

    def _invoke(
        self,
        invocation: dict[str, Any],
        task_id: str,
        context_id: str,
        effect_id: str,
    ) -> dict[str, Any]:
        envelope = self._socket_call(
            {
                "cmd": "a2a_invoke",
                "args": {
                    "peer": self.config.peer_id,
                    "skill": invocation["skill"],
                    "input": invocation["input"],
                    "task_id": task_id,
                    "context_id": context_id,
                    "effect_id": effect_id,
                },
            }
        )
        if envelope.get("status") == "error":
            raw_error = envelope.get("error")
            if not isinstance(raw_error, dict):
                raise _IPCError("IPC_INVALID_ERROR")
            correlation = _correlation(
                task_id,
                context_id,
                [],
                invocation["input"]["idempotency_key"],
            )
            candidate = {
                "skill": invocation["skill"],
                "status": "rejected",
                "error": raw_error,
                "correlation": correlation,
            }
            self.schemas.validate_result(candidate)
            return _profile_failure(
                invocation["skill"],
                "rejected",
                raw_error["code"],
                "The local runtime rejected the request.",
                correlation,
                retryable=raw_error["retryable"],
            )
        if envelope.get("status") != "ok":
            raise _IPCError("IPC_REJECTED")
        result = envelope.get("response")
        if not isinstance(result, dict):
            raise _IPCError("IPC_INVALID_RESULT")
        return result

    def _validated_runtime_result(
        self,
        skill: str,
        result: dict[str, Any],
        expected_correlation: dict[str, Any],
        reference_task_ids: list[str],
    ) -> dict[str, Any]:
        self.schemas.validate_result(result)
        gateway_correlation = dict(expected_correlation)
        gateway_correlation.pop("reference_task_ids", None)
        if result.get("skill") != skill or result.get("correlation") != gateway_correlation:
            raise A2AContractValidationError("$.correlation", "does not match invocation")
        status = result["status"]
        if status in {"completed", "accepted", "in_progress"}:
            output = result.get("output")
            if not isinstance(output, dict):
                raise A2AContractValidationError("$.output", "required")
            self.schemas.validate_response(skill, output)
        if reference_task_ids:
            result = {**result, "correlation": expected_correlation}
            self.schemas.validate_result(result)
        return result


def make_handler(service: RelayService) -> type[http.server.BaseHTTPRequestHandler]:
    class Handler(http.server.BaseHTTPRequestHandler):
        server_version = "Yeoman-A2A-Relay/1.0"

        def _send(self, status: int, body: dict[str, Any]) -> None:
            encoded = _json(body).encode()
            if len(encoded) > service.config.max_response_bytes:
                status = 500
                encoded = b'{"error":{"code":"RESPONSE_TOO_LARGE"}}'
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def do_GET(self) -> None:  # noqa: N802
            if self.path != "/.well-known/agent-card.json":
                self._send(404, {"error": {"code": "NOT_FOUND"}})
                return
            self._send(200, service.agent_card())

        def do_POST(self) -> None:  # noqa: N802
            peer_ip = str(self.client_address[0])
            if not service.authenticate(peer_ip, self.headers.get("Authorization", "")):
                self.send_response(401)
                self.send_header("WWW-Authenticate", "Bearer")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if not service.rate_allowed(peer_ip):
                self._send(429, {"error": {"code": "RATE_LIMITED"}})
                return
            if self.path != "/":
                self._send(404, {"error": {"code": "NOT_FOUND"}})
                return
            try:
                length = int(self.headers.get("Content-Length", ""))
            except ValueError:
                length = -1
            if length < 1 or length > service.config.max_body_bytes:
                self._send(413, {"error": {"code": "BODY_SIZE_INVALID"}})
                return
            if self.headers.get_content_type() != "application/json":
                self._send(415, {"error": {"code": "CONTENT_TYPE_INVALID"}})
                return
            try:
                self.connection.settimeout(service.config.timeout_seconds)
                raw = self.rfile.read(length)
                request = json.loads(raw)
            except TimeoutError:
                self._send(408, {"error": {"code": "BODY_TIMEOUT"}})
                return
            except UnicodeDecodeError, json.JSONDecodeError:
                self._send(200, service.rpc_error(None, -32700, "Parse error"))
                return
            self._send(200, service.dispatch(request))

        def log_message(self, _format: str, *_args: Any) -> None:
            return

    return Handler


class _RelayHTTPServer(http.server.ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


def create_server(
    service: RelayService, address: tuple[str, int] | None = None
) -> _RelayHTTPServer:
    bind = address or (service.config.bind_host, service.config.port)
    if ":" in bind[0]:

        class IPv6RelayHTTPServer(_RelayHTTPServer):
            address_family = socket.AF_INET6

        return IPv6RelayHTTPServer(bind, make_handler(service))
    return _RelayHTTPServer(bind, make_handler(service))


def serve(config: RelayConfig | None = None) -> None:
    runtime_config = config or RelayConfig.from_env()
    runtime_config.validate()
    server = create_server(RelayService(runtime_config))
    LOG.info("event=a2a_relay_started code=READY")
    try:
        server.serve_forever()
    finally:
        server.server_close()


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("YEOMAN_A2A_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    try:
        serve()
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
