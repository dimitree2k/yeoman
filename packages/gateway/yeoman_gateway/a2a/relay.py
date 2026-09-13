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
from collections.abc import Callable
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
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")
_ALLOWED_CONTENT_TYPES = frozenset({"text", "image", "file", "voice"})
_CGNAT = ipaddress.ip_network("100.64.0.0/10")
_DB_LOCKS: dict[str, threading.RLock] = {}
_DB_LOCKS_GUARD = threading.Lock()


class RelayConfigurationError(ValueError):
    """The relay environment is unsafe or incomplete."""


class _RequestError(ValueError):
    def __init__(self, code: str, message: str, *, rpc_code: int = -32602) -> None:
        self.code = code
        self.message = message
        self.rpc_code = rpc_code
        super().__init__(message)


class _IPCError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


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
            raise RelayConfigurationError("bind host must be an explicit private IP address") from exc
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
        if not _IDENTIFIER.fullmatch(self.peer_id):
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
    task: dict[str, Any]
    conflict: bool = False


def _db_lock(path: Path) -> threading.RLock:
    key = str(path.resolve())
    with _DB_LOCKS_GUARD:
        return _DB_LOCKS.setdefault(key, threading.RLock())


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _audit() -> dict[str, str]:
    return {"profile": PROFILE_URI, "contractRelease": CONTRACT_RELEASE}


def _correlation(
    task_id: str,
    context_id: str,
    reference_task_ids: list[str],
    idempotency_key: str,
) -> dict[str, Any]:
    value: dict[str, Any] = {"task_id": task_id, "idempotency_key": idempotency_key}
    if context_id:
        value["context_id"] = context_id
    if reference_task_ids:
        value["reference_task_ids"] = reference_task_ids
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
    reference_task_ids: list[str],
    state: str,
    result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    task: dict[str, Any] = {
        "id": task_id,
        "contextId": context_id,
        "referenceTaskIds": reference_task_ids,
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
        self.lock = _db_lock(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    task_id TEXT PRIMARY KEY,
                    peer TEXT NOT NULL,
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
                    result_json TEXT,
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
        with self.lock, self._connect() as connection:
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
                    "SELECT task_json FROM tasks WHERE task_id=? AND peer=?", (task_id, peer)
                ).fetchone()
                if task_row is None:
                    raise RuntimeError("idempotency row has no task")
                return _Claim(task_id, effect_id, request_hash, json.loads(task_row[0]))

            task_id = f"task-{uuid.uuid4().hex}"
            context = context_id or f"context-{uuid.uuid4().hex}"
            effect_id = _effect_id(peer, skill, key)
            task = _task(
                task_id,
                context,
                reference_task_ids,
                "TASK_STATE_SUBMITTED",
            )
            connection.execute(
                "INSERT INTO tasks(task_id, peer, task_json) VALUES (?, ?, ?)",
                (task_id, peer, _json(task)),
            )
            connection.execute(
                """INSERT INTO idempotency
                   (peer, skill, idempotency_key, request_hash, task_id, effect_id)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (peer, skill, key, request_hash, task_id, effect_id),
            )
            return _Claim(task_id, effect_id, request_hash, task)

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
            "SELECT task_json FROM tasks WHERE task_id=? AND peer=?", (task_id, peer)
        ).fetchone()
        if existing is not None:
            task = json.loads(existing[0])
        else:
            correlation = _correlation(task_id, context_id, reference_task_ids, key)
            result = _profile_failure(
                skill,
                "rejected",
                "IDEMPOTENCY_CONFLICT",
                "The idempotency key was already used for a different request.",
                correlation,
            )
            task = _task(
                task_id,
                context_id,
                reference_task_ids,
                "TASK_STATE_REJECTED",
                result,
            )
            connection.execute(
                "INSERT INTO tasks(task_id, peer, task_json) VALUES (?, ?, ?)",
                (task_id, peer, _json(task)),
            )
        return _Claim(task_id, "", request_hash, task, conflict=True)

    def finish(
        self,
        *,
        peer: str,
        skill: str,
        key: str,
        claim: _Claim,
        task: dict[str, Any],
        result: dict[str, Any],
    ) -> None:
        with self.lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE tasks SET task_json=? WHERE task_id=? AND peer=?",
                (_json(task), claim.task_id, peer),
            )
            connection.execute(
                """UPDATE idempotency SET result_json=?
                   WHERE peer=? AND skill=? AND idempotency_key=? AND request_hash=?""",
                (_json(result), peer, skill, key, claim.request_hash),
            )

    def get(self, peer: str, task_id: str) -> dict[str, Any] | None:
        with self.lock, self._connect() as connection:
            row = connection.execute(
                "SELECT task_json FROM tasks WHERE peer=? AND task_id=?", (peer, task_id)
            ).fetchone()
        return json.loads(row[0]) if row else None


class RelayService:
    """Protocol logic shared by the thin HTTP handler."""

    def __init__(
        self,
        config: RelayConfig,
        *,
        voice_probe: Callable[[], bool] | None = None,
        schemas: ContractSchemas | None = None,
    ) -> None:
        self.config = config
        self.store = _RelayStore(config.state_path)
        self.schemas = schemas or ContractSchemas.load()
        self.voice_probe = voice_probe or (lambda: False)
        self._rate_lock = threading.Lock()
        self._requests: dict[str, list[float]] = {}

    def _skills(self) -> tuple[str, ...]:
        skills: list[str] = []
        if self.config.whatsapp_enabled:
            skills.append("whatsapp.send")
        try:
            voice_ready = self.config.voice_enabled and self.voice_probe() is True
        except Exception:
            voice_ready = False
        if voice_ready:
            skills.append("media.voice.generate")
        return tuple(skills)

    def agent_card(self) -> dict[str, Any]:
        descriptions = {
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
        skills = []
        for skill_id in self._skills():
            name, description, tags = descriptions[skill_id]
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
            "url": self.config.public_url,
            "version": CONTRACT_RELEASE,
            "protocolVersion": "1.0",
            "supportedInterfaces": [
                {
                    "url": self.config.public_url,
                    "protocolBinding": "JSONRPC",
                    "protocolVersion": "1.0",
                }
            ],
            "capabilities": {
                "streaming": False,
                "pushNotifications": False,
                "stateTransitionHistory": False,
                "extendedAgentCard": False,
            },
            "defaultInputModes": ["application/json"],
            "defaultOutputModes": ["application/json"],
            "extensions": [
                {
                    "uri": PROFILE_URI,
                    "description": "Hermes/Yeoman structured skill profile v1",
                    "required": True,
                }
            ],
            "skills": skills,
            "securitySchemes": {"bearer": {"type": "http", "scheme": "bearer"}},
            "security": [{"bearer": []}],
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

    def dispatch(self, request: Any) -> dict[str, Any]:
        if not isinstance(request, dict):
            raise _RequestError("INVALID_REQUEST", "The JSON-RPC request must be an object.")
        request_id = request.get("id")
        if request.get("jsonrpc") != "2.0":
            return self.rpc_error(
                request_id,
                _RequestError("INVALID_REQUEST", "jsonrpc must be 2.0.", rpc_code=-32600),
            )
        method = request.get("method")
        params = request.get("params", {})
        if not isinstance(params, dict):
            return self.rpc_error(
                request_id, _RequestError("INVALID_REQUEST", "params must be an object.")
            )
        if method in {"GetTask", "tasks/get"}:
            return self._get_task(request_id, params)
        if method in {"SendMessage", "message/send"}:
            return self._send_message(request_id, params)
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": -32601, "message": "Method not found"},
        }

    def _get_task(self, request_id: Any, params: dict[str, Any]) -> dict[str, Any]:
        task_id = params.get("id", params.get("taskId"))
        if not isinstance(task_id, str) or not _IDENTIFIER.fullmatch(task_id):
            return self.rpc_error(
                request_id,
                _RequestError("INVALID_IDENTIFIER", "The task identifier is malformed."),
            )
        task = self.store.get(self.config.peer_id, task_id)
        if task is None:
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32001, "message": "Task not found"},
            }
        return {"jsonrpc": "2.0", "id": request_id, "result": task}

    def _send_message(self, request_id: Any, params: dict[str, Any]) -> dict[str, Any]:
        try:
            invocation, context_id, references = self._validated_invocation(params)
        except _RequestError as exc:
            return self.rpc_error(request_id, exc)
        skill = invocation["skill"]
        key = invocation["input"]["idempotency_key"]
        with self.store.lock:
            claim = self.store.claim(
                peer=self.config.peer_id,
                invocation=invocation,
                context_id=context_id,
                reference_task_ids=references,
            )
            if claim.conflict or claim.task["status"]["state"] != "TASK_STATE_SUBMITTED":
                return {"jsonrpc": "2.0", "id": request_id, "result": {"task": claim.task}}
            task_context = claim.task["contextId"]
            correlation = _correlation(claim.task_id, task_context, references, key)
            try:
                result = self._invoke(
                    invocation,
                    claim.task_id,
                    task_context,
                    references,
                    claim.effect_id,
                )
                self._validate_runtime_result(skill, result, correlation)
                state = {
                    "completed": "TASK_STATE_COMPLETED",
                    "rejected": "TASK_STATE_REJECTED",
                    "failed": "TASK_STATE_FAILED",
                }.get(result["status"], "TASK_STATE_WORKING")
            except (A2AContractValidationError, _IPCError, KeyError, TypeError):
                result = _profile_failure(
                    skill,
                    "failed",
                    "INVALID_UPSTREAM_RESULT",
                    "The local runtime returned an invalid structured result.",
                    correlation,
                )
                state = "TASK_STATE_FAILED"
            task = _task(claim.task_id, task_context, references, state, result)
            self.store.finish(
                peer=self.config.peer_id,
                skill=skill,
                key=key,
                claim=claim,
                task=task,
                result=result,
            )
        LOG.info(
            "event=a2a_task task_id=%s context_id=%s skill=%s code=%s",
            claim.task_id,
            task_context,
            skill,
            result["status"],
        )
        return {"jsonrpc": "2.0", "id": request_id, "result": {"task": task}}

    def _validated_invocation(
        self, params: dict[str, Any]
    ) -> tuple[dict[str, Any], str, list[str]]:
        message = params.get("message")
        if not isinstance(message, dict):
            raise _RequestError("INVALID_REQUEST", "params.message must be an object.")
        context_id = message.get("contextId", params.get("contextId", ""))
        if context_id != "" and (
            not isinstance(context_id, str) or not _IDENTIFIER.fullmatch(context_id)
        ):
            raise _RequestError("INVALID_IDENTIFIER", "The context identifier is malformed.")
        references = message.get("referenceTaskIds", [])
        if (
            not isinstance(references, list)
            or len(references) > 20
            or any(not isinstance(value, str) or not _IDENTIFIER.fullmatch(value) for value in references)
            or len(set(references)) != len(references)
        ):
            raise _RequestError("INVALID_IDENTIFIER", "Reference task identifiers are malformed.")
        parts = message.get("parts")
        if not isinstance(parts, list):
            raise _RequestError("INVALID_REQUEST", "Message parts must be a list.")
        if any(isinstance(part, dict) and "data" in part and "text" in part for part in parts):
            raise _RequestError("INVALID_REQUEST", "A DataPart cannot also be a text part.")
        authoritative = [
            part["data"]
            for part in parts
            if isinstance(part, dict)
            and part.get("mediaType") == "application/json"
            and "data" in part
        ]
        if len(authoritative) != 1:
            raise _RequestError(
                "INVALID_REQUEST", "Exactly one application/json DataPart is required."
            )
        invocation = authoritative[0]
        try:
            self.schemas.validate_invocation(invocation)
        except A2AContractValidationError as exc:
            raise _RequestError("INVALID_REQUEST", "The invocation is invalid.") from exc
        skill = invocation["skill"]
        if skill not in self._skills():
            raise _RequestError("CAPABILITY_UNAVAILABLE", "The requested skill is unavailable.")
        try:
            self.schemas.validate_request(skill, invocation["input"])
        except A2AContractValidationError as exc:
            raise _RequestError("INVALID_REQUEST", "The skill input is invalid.") from exc
        if skill == "whatsapp.send":
            requested_types = {part["type"] for part in invocation["input"]["content"]}
            if not requested_types <= self.config.content_types:
                raise _RequestError(
                    "CAPABILITY_UNAVAILABLE", "A requested content type is unavailable."
                )
        return invocation, context_id, references

    def _invoke(
        self,
        invocation: dict[str, Any],
        task_id: str,
        context_id: str,
        references: list[str],
        effect_id: str,
    ) -> dict[str, Any]:
        request = {
            "cmd": "a2a_invoke",
            "args": {
                "peer": self.config.peer_id,
                "skill": invocation["skill"],
                "input": invocation["input"],
                "task_id": task_id,
                "context_id": context_id,
                "reference_task_ids": references,
                "effect_id": effect_id,
                "audit": _audit(),
            },
        }
        encoded = (_json(request) + "\n").encode()
        if len(encoded) > self.config.max_body_bytes:
            raise _IPCError("IPC_REQUEST_TOO_LARGE")
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(self.config.timeout_seconds)
                client.connect(str(self.config.socket_path))
                client.sendall(encoded)
                response = bytearray()
                while not response.endswith(b"\n"):
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
        if envelope.get("status") == "error":
            raw_error = envelope.get("error")
            if not isinstance(raw_error, dict):
                raise _IPCError("IPC_INVALID_ERROR")
            correlation = _correlation(
                task_id,
                context_id,
                references,
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

    def _validate_runtime_result(
        self, skill: str, result: dict[str, Any], expected_correlation: dict[str, Any]
    ) -> None:
        self.schemas.validate_result(result)
        if result.get("skill") != skill or result.get("correlation") != expected_correlation:
            raise A2AContractValidationError("$.correlation", "does not match invocation")
        status = result["status"]
        if status in {"completed", "accepted", "in_progress"}:
            output = result.get("output")
            if not isinstance(output, dict):
                raise A2AContractValidationError("$.output", "required")
            self.schemas.validate_response(skill, output)

    def rpc_error(self, request_id: Any, error: _RequestError) -> dict[str, Any]:
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {
                "code": error.rpc_code,
                "message": error.message,
                "data": {
                    "error": {
                        "code": error.code,
                        "message": error.message,
                        "retryable": False,
                    },
                    "audit": _audit(),
                },
            },
        }


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
            except (UnicodeDecodeError, json.JSONDecodeError):
                self._send(400, {"error": {"code": "INVALID_JSON"}})
                return
            try:
                response = service.dispatch(request)
            except _RequestError as exc:
                response = service.rpc_error(None, exc)
            self._send(200, response)

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


def serve(
    config: RelayConfig | None = None,
    *,
    voice_probe: Callable[[], bool] | None = None,
) -> None:
    runtime_config = config or RelayConfig.from_env()
    runtime_config.validate()
    server = create_server(RelayService(runtime_config, voice_probe=voice_probe))
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
