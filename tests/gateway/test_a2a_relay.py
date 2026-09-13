from __future__ import annotations

import http.client
import json
import socket
import threading
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest


def _relay_module():
    from yeoman_gateway.a2a import relay

    return relay


class FakeUnixGateway:
    def __init__(
        self,
        path: Path,
        responder: Callable[[dict[str, Any]], dict[str, Any]],
    ) -> None:
        self.path = path
        self.responder = responder
        self.requests: list[dict[str, Any]] = []
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def __enter__(self) -> FakeUnixGateway:
        self._thread.start()
        assert self._ready.wait(2)
        return self

    def __exit__(self, *_exc: object) -> None:
        self._stop.set()
        try:
            socket.socket(socket.AF_UNIX, socket.SOCK_STREAM).connect(str(self.path))
        except OSError:
            pass
        self._thread.join(2)
        self.path.unlink(missing_ok=True)

    def _run(self) -> None:
        self.path.unlink(missing_ok=True)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
            server.bind(str(self.path))
            server.listen()
            server.settimeout(0.1)
            self._ready.set()
            while not self._stop.is_set():
                try:
                    connection, _ = server.accept()
                except TimeoutError:
                    continue
                with connection:
                    data = bytearray()
                    while not data.endswith(b"\n"):
                        chunk = connection.recv(65_536)
                        if not chunk:
                            break
                        data.extend(chunk)
                    if not data:
                        continue
                    request = json.loads(data)
                    self.requests.append(request)
                    response = self.responder(request)
                    connection.sendall(json.dumps(response).encode() + b"\n")


def _success(request: dict[str, Any]) -> dict[str, Any]:
    args = request["args"]
    return {
        "status": "ok",
        "response": {
            "skill": args["skill"],
            "status": "completed",
            "output": {
                "delivery_id": "delivery-1",
                "status": "sent",
                "recipient": args["input"]["recipient"],
            },
            "correlation": {
                "task_id": args["task_id"],
                "context_id": args["context_id"],
                "reference_task_ids": args["reference_task_ids"],
                "idempotency_key": args["input"]["idempotency_key"],
            },
        },
    }


def _config(tmp_path: Path, socket_path: Path, **changes: Any):
    relay = _relay_module()
    values: dict[str, Any] = {
        "bind_host": "127.0.0.1",
        "port": 0,
        "allowed_peer_ips": frozenset({"127.0.0.1"}),
        "peer_id": "hermes-test",
        "bearer_secret": "test-secret-value",
        "socket_path": socket_path,
        "state_path": tmp_path / "relay.sqlite3",
        "public_url": "https://relay.example.test/a2a",
        "whatsapp_enabled": True,
        "voice_enabled": False,
        "content_types": frozenset({"text"}),
        "timeout_seconds": 1.0,
        "max_body_bytes": 65_536,
        "max_response_bytes": 65_536,
        "rate_limit_per_minute": 100,
    }
    values.update(changes)
    return relay.RelayConfig(**values)


@contextmanager
def _running(service: Any) -> Iterator[tuple[str, int]]:
    relay = _relay_module()
    server = relay.create_server(service, ("127.0.0.1", 0))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


def _request(
    address: tuple[str, int],
    method: str,
    path: str,
    *,
    token: str | None = "test-secret-value",
    payload: Any = None,
) -> tuple[int, dict[str, Any]]:
    body = None if payload is None else json.dumps(payload).encode()
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    connection = http.client.HTTPConnection(*address, timeout=2)
    connection.request(method, path, body=body, headers=headers)
    response = connection.getresponse()
    raw = response.read()
    connection.close()
    return response.status, json.loads(raw) if raw else {}


def _invocation(
    *,
    key: str = "send-001",
    text: str = "Hello",
    skill: str = "whatsapp.send",
) -> dict[str, Any]:
    input_value: dict[str, Any]
    if skill == "whatsapp.send":
        input_value = {
            "recipient": {"type": "group", "alias": "team-example"},
            "content": [{"type": "text", "text": text}],
            "idempotency_key": key,
        }
    else:
        input_value = {"text": text, "idempotency_key": key}
    return {"skill": skill, "input": input_value}


def _send_payload(invocation: dict[str, Any], **message_changes: Any) -> dict[str, Any]:
    message = {
        "role": "ROLE_USER",
        "messageId": "message-1",
        "contextId": "context-1",
        "referenceTaskIds": ["previous-1", "previous-2"],
        "parts": [
            {"text": "Human context only", "mediaType": "text/plain"},
            {"data": invocation, "mediaType": "application/json"},
        ],
    }
    message.update(message_changes)
    return {"jsonrpc": "2.0", "id": "rpc-1", "method": "SendMessage", "params": {"message": message}}


def _profile_result(task: dict[str, Any]) -> dict[str, Any]:
    return task["artifacts"][0]["parts"][0]["data"]


def test_config_reads_explicit_private_runtime_settings_without_revealing_secret(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    relay = _relay_module()
    monkeypatch.setenv("YEOMAN_A2A_BIND_HOST", "100.64.1.2")
    monkeypatch.setenv("YEOMAN_A2A_PORT", "9911")
    monkeypatch.setenv("YEOMAN_A2A_ALLOWED_PEER_IPS", "100.64.1.3,100.64.1.4")
    monkeypatch.setenv("YEOMAN_A2A_PEER_ID", "hermes")
    monkeypatch.setenv("YEOMAN_A2A_BEARER_SECRET", "not-for-repr")
    monkeypatch.setenv("YEOMAN_A2A_SOCKET_PATH", str(tmp_path / "gateway.sock"))
    monkeypatch.setenv("YEOMAN_A2A_STATE_PATH", str(tmp_path / "state.sqlite3"))
    monkeypatch.setenv("YEOMAN_A2A_PUBLIC_URL", "https://relay.example.test/a2a")
    monkeypatch.setenv("YEOMAN_A2A_WHATSAPP_ENABLED", "true")
    monkeypatch.setenv("YEOMAN_A2A_VOICE_ENABLED", "false")
    monkeypatch.setenv("YEOMAN_A2A_CONTENT_TYPES", "text,image")

    config = relay.RelayConfig.from_env()
    config.validate()

    assert config.allowed_peer_ips == frozenset({"100.64.1.3", "100.64.1.4"})
    assert config.content_types == frozenset({"text", "image"})
    assert "not-for-repr" not in repr(config)
    with pytest.raises(relay.RelayConfigurationError, match="explicit private"):
        relay.RelayConfig(**{**config.__dict__, "bind_host": "0.0.0.0"}).validate()
    with pytest.raises(relay.RelayConfigurationError, match="public URL"):
        relay.RelayConfig(
            **{**config.__dict__, "public_url": "https://localhost:9900/a2a"}
        ).validate()


def test_agent_card_only_exposes_live_structured_skills(tmp_path: Path) -> None:
    relay = _relay_module()
    config = _config(tmp_path, tmp_path / "gateway.sock")
    service = relay.RelayService(config, voice_probe=lambda: False)

    with _running(service) as address:
        status, card = _request(address, "GET", "/.well-known/agent-card.json", token=None)

    assert status == 200
    assert card["supportedInterfaces"] == [
        {
            "url": "https://relay.example.test/a2a",
            "protocolBinding": "JSONRPC",
            "protocolVersion": "1.0",
        }
    ]
    assert card["capabilities"] == {
        "streaming": False,
        "pushNotifications": False,
        "stateTransitionHistory": False,
        "extendedAgentCard": False,
    }
    assert card["defaultInputModes"] == ["application/json"]
    assert card["defaultOutputModes"] == ["application/json"]
    assert [skill["id"] for skill in card["skills"]] == ["whatsapp.send"]
    assert card["extensions"][0]["uri"] == "urn:hermes-yeoman:a2a-profile:v1"
    serialized = json.dumps(card)
    for private_value in (
        config.bearer_secret,
        config.peer_id,
        str(config.socket_path),
        str(config.state_path),
        "127.0.0.1",
    ):
        assert private_value not in serialized


def test_voice_is_advertised_only_when_enabled_and_runtime_probe_succeeds(tmp_path: Path) -> None:
    relay = _relay_module()
    config = _config(tmp_path, tmp_path / "gateway.sock", voice_enabled=True)
    disabled = relay.RelayService(config, voice_probe=lambda: False)
    enabled = relay.RelayService(config, voice_probe=lambda: True)

    assert [item["id"] for item in disabled.agent_card()["skills"]] == ["whatsapp.send"]
    assert [item["id"] for item in enabled.agent_card()["skills"]] == [
        "whatsapp.send",
        "media.voice.generate",
    ]


def test_valid_structured_text_invocation_returns_valid_task_and_exact_correlation(
    tmp_path: Path,
) -> None:
    relay = _relay_module()
    socket_path = tmp_path / "gateway.sock"
    with FakeUnixGateway(socket_path, _success) as gateway:
        service = relay.RelayService(_config(tmp_path, socket_path))
        with _running(service) as address:
            status, body = _request(address, "POST", "/", payload=_send_payload(_invocation()))

    task = body["result"]["task"]
    result = _profile_result(task)
    assert status == 200
    assert task["contextId"] == "context-1"
    assert task["referenceTaskIds"] == ["previous-1", "previous-2"]
    assert task["status"]["state"] == "TASK_STATE_COMPLETED"
    assert result["status"] == "completed"
    assert result["output"]["status"] == "sent"
    assert task["metadata"] == {
        "profile": "urn:hermes-yeoman:a2a-profile:v1",
        "contractRelease": "1.0.0",
    }
    assert len(gateway.requests) == 1
    ipc = gateway.requests[0]
    assert ipc["cmd"] == "a2a_invoke"
    assert ipc["args"]["peer"] == "hermes-test"
    assert ipc["args"]["context_id"] == "context-1"
    assert ipc["args"]["reference_task_ids"] == ["previous-1", "previous-2"]
    assert ipc["args"]["skill"] == "whatsapp.send"
    assert ipc["args"]["input"] == _invocation()["input"]
    assert ipc["args"]["effect_id"].startswith("a2a-effect-")


@pytest.mark.parametrize(
    ("payload", "code"),
    [
        (
            _send_payload(
                _invocation(),
                parts=[{"text": '{"operation":"send_whatsapp"}', "mediaType": "text/plain"}],
            ),
            "INVALID_REQUEST",
        ),
        (
            _send_payload(
                _invocation(),
                parts=[
                    {"data": _invocation(), "mediaType": "application/json"},
                    {"data": _invocation(), "mediaType": "application/json"},
                ],
            ),
            "INVALID_REQUEST",
        ),
        (_send_payload({"skill": "unknown.skill", "input": {}}), "CAPABILITY_UNAVAILABLE"),
        (_send_payload({"skill": "whatsapp.send"}), "INVALID_REQUEST"),
        (
            _send_payload({"skill": "whatsapp.send", "input": _invocation()["input"], "extra": True}),
            "INVALID_REQUEST",
        ),
        (
            _send_payload(
                _invocation(),
                parts=[
                    {
                        "data": _invocation(),
                        "text": '{"operation":"send_whatsapp"}',
                        "mediaType": "application/json",
                    }
                ],
            ),
            "INVALID_REQUEST",
        ),
    ],
)
def test_invalid_or_legacy_invocations_never_reach_ipc(
    tmp_path: Path, payload: dict[str, Any], code: str
) -> None:
    relay = _relay_module()
    socket_path = tmp_path / "gateway.sock"
    with FakeUnixGateway(socket_path, _success) as gateway:
        service = relay.RelayService(_config(tmp_path, socket_path))
        with _running(service) as address:
            status, body = _request(address, "POST", "/", payload=payload)

    assert status == 200
    assert body["error"]["code"] == -32602
    assert body["error"]["data"]["error"]["code"] == code
    assert gateway.requests == []


def test_optional_text_part_cannot_override_authoritative_data(tmp_path: Path) -> None:
    relay = _relay_module()
    socket_path = tmp_path / "gateway.sock"
    payload = _send_payload(_invocation(text="Authoritative"))
    payload["params"]["message"]["parts"][0]["text"] = json.dumps(
        {"operation": "send_whatsapp", "text": "Override"}
    )
    with FakeUnixGateway(socket_path, _success) as gateway:
        service = relay.RelayService(_config(tmp_path, socket_path))
        with _running(service) as address:
            _request(address, "POST", "/", payload=payload)

    assert gateway.requests[0]["args"]["input"]["content"][0]["text"] == "Authoritative"


def test_post_authentication_happens_before_body_read(tmp_path: Path) -> None:
    relay = _relay_module()
    service = relay.RelayService(_config(tmp_path, tmp_path / "gateway.sock"))
    with _running(service) as address:
        with socket.create_connection(address, timeout=1) as client:
            client.sendall(
                b"POST / HTTP/1.1\r\nHost: relay\r\nContent-Length: 20\r\nConnection: close\r\n\r\n"
            )
            response = client.recv(4096)

    assert response.startswith(b"HTTP/1.0 401")


def test_authenticated_partial_body_is_time_bounded(tmp_path: Path) -> None:
    relay = _relay_module()
    config = _config(tmp_path, tmp_path / "gateway.sock", timeout_seconds=0.05)
    service = relay.RelayService(config)
    with _running(service) as address:
        with socket.create_connection(address, timeout=1) as client:
            client.sendall(
                b"POST / HTTP/1.1\r\nHost: relay\r\n"
                b"Authorization: Bearer test-secret-value\r\n"
                b"Content-Type: application/json\r\nContent-Length: 20\r\n\r\n{"
            )
            response = client.recv(4096)

    assert response.startswith(b"HTTP/1.0 408")


def test_body_and_rate_limits_run_before_ipc(tmp_path: Path) -> None:
    relay = _relay_module()
    socket_path = tmp_path / "gateway.sock"
    with FakeUnixGateway(socket_path, _success) as gateway:
        config = _config(
            tmp_path,
            socket_path,
            max_body_bytes=32,
            rate_limit_per_minute=1,
        )
        service = relay.RelayService(config)
        with _running(service) as address:
            too_large_status, _ = _request(
                address, "POST", "/", payload={"padding": "x" * 64}
            )
            limited_status, limited = _request(address, "POST", "/", payload={})

    assert too_large_status == 413
    assert limited_status == 429
    assert limited["error"]["code"] == "RATE_LIMITED"
    assert gateway.requests == []


def test_same_idempotency_key_is_atomic_and_conflicts_are_deterministic(tmp_path: Path) -> None:
    relay = _relay_module()
    socket_path = tmp_path / "gateway.sock"
    with FakeUnixGateway(socket_path, _success) as gateway:
        service = relay.RelayService(_config(tmp_path, socket_path))
        with _running(service) as address:
            request = _send_payload(_invocation())
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(_request, address, "POST", "/", payload=request) for _ in range(2)]
            responses = [future.result()[1] for future in futures]
            _, conflict = _request(
                address,
                "POST",
                "/",
                payload=_send_payload(_invocation(text="Different")),
            )
            _, repeated_conflict = _request(
                address,
                "POST",
                "/",
                payload=_send_payload(_invocation(text="Different")),
            )

    assert responses[0] == responses[1]
    assert len(gateway.requests) == 1
    conflict_task = conflict["result"]["task"]
    assert conflict_task == repeated_conflict["result"]["task"]
    assert conflict_task["status"]["state"] == "TASK_STATE_REJECTED"
    assert _profile_result(conflict_task)["error"]["code"] == "IDEMPOTENCY_CONFLICT"


def test_terminal_result_survives_restart_without_reexecution(tmp_path: Path) -> None:
    relay = _relay_module()
    socket_path = tmp_path / "gateway.sock"
    config = _config(tmp_path, socket_path)
    with FakeUnixGateway(socket_path, _success) as gateway:
        with _running(relay.RelayService(config)) as address:
            _, original = _request(address, "POST", "/", payload=_send_payload(_invocation()))
        with _running(relay.RelayService(config)) as address:
            _, replay = _request(address, "POST", "/", payload=_send_payload(_invocation()))

    assert replay == original
    assert len(gateway.requests) == 1


def test_submitted_task_retries_with_same_deterministic_effect_id_after_restart(
    tmp_path: Path,
) -> None:
    relay = _relay_module()
    socket_path = tmp_path / "gateway.sock"
    config = _config(tmp_path, socket_path)
    invocation = _invocation()
    service = relay.RelayService(config)
    claim = service.store.claim(
        peer=config.peer_id,
        invocation=invocation,
        context_id="context-1",
        reference_task_ids=["previous-1", "previous-2"],
    )
    expected_effect_id = claim.effect_id

    with FakeUnixGateway(socket_path, _success) as gateway:
        with _running(relay.RelayService(config)) as address:
            _, body = _request(address, "POST", "/", payload=_send_payload(invocation))

    assert body["result"]["task"]["id"] == claim.task_id
    assert gateway.requests[0]["args"]["effect_id"] == expected_effect_id


def test_get_task_is_peer_scoped_and_rejects_malformed_ids(tmp_path: Path) -> None:
    relay = _relay_module()
    socket_path = tmp_path / "gateway.sock"
    config = _config(tmp_path, socket_path)
    with FakeUnixGateway(socket_path, _success):
        with _running(relay.RelayService(config)) as address:
            _, sent = _request(address, "POST", "/", payload=_send_payload(_invocation()))
            task_id = sent["result"]["task"]["id"]
            _, found = _request(
                address,
                "POST",
                "/",
                payload={"jsonrpc": "2.0", "id": "rpc-get", "method": "tasks/get", "params": {"id": task_id}},
            )
            _, malformed = _request(
                address,
                "POST",
                "/",
                payload={"jsonrpc": "2.0", "id": "rpc-get", "method": "GetTask", "params": {"id": " bad "}},
            )

        other = _config(tmp_path, socket_path, peer_id="different-peer", bearer_secret="other-secret")
        with _running(relay.RelayService(other)) as address:
            _, hidden = _request(
                address,
                "POST",
                "/",
                token="other-secret",
                payload={"jsonrpc": "2.0", "id": "rpc-get", "method": "GetTask", "params": {"id": task_id}},
            )

    assert found["result"] == sent["result"]["task"]
    assert malformed["error"]["data"]["error"]["code"] == "INVALID_IDENTIFIER"
    assert hidden["error"]["code"] == -32001


@pytest.mark.parametrize(
    "message_changes",
    [
        {"contextId": " context-1"},
        {"contextId": "context/1"},
        {"referenceTaskIds": ["good", " bad"]},
        {"referenceTaskIds": ["duplicate", "duplicate"]},
        {"referenceTaskIds": [{}]},
    ],
)
def test_malformed_correlation_identifiers_are_rejected_without_repair(
    tmp_path: Path, message_changes: dict[str, Any]
) -> None:
    relay = _relay_module()
    socket_path = tmp_path / "gateway.sock"
    with FakeUnixGateway(socket_path, _success) as gateway:
        service = relay.RelayService(_config(tmp_path, socket_path))
        with _running(service) as address:
            _, body = _request(
                address,
                "POST",
                "/",
                payload=_send_payload(_invocation(), **message_changes),
            )

    assert body["error"]["data"]["error"]["code"] == "INVALID_IDENTIFIER"
    assert gateway.requests == []


def test_invalid_outgoing_result_becomes_terminal_profile_failure(tmp_path: Path) -> None:
    relay = _relay_module()
    socket_path = tmp_path / "gateway.sock"

    def invalid_result(request: dict[str, Any]) -> dict[str, Any]:
        args = request["args"]
        return {
            "status": "ok",
            "response": {
                "skill": "whatsapp.send",
                "status": "completed",
                "output": {"status": "sent"},
                "correlation": {"task_id": args["task_id"]},
            },
        }

    with FakeUnixGateway(socket_path, invalid_result) as gateway:
        service = relay.RelayService(_config(tmp_path, socket_path))
        with _running(service) as address:
            payload = _send_payload(_invocation())
            _, first = _request(address, "POST", "/", payload=payload)
            _, second = _request(address, "POST", "/", payload=payload)

    task = first["result"]["task"]
    assert first == second
    assert len(gateway.requests) == 1
    assert task["status"]["state"] == "TASK_STATE_FAILED"
    assert _profile_result(task)["error"] == {
        "code": "INVALID_UPSTREAM_RESULT",
        "message": "The local runtime returned an invalid structured result.",
        "retryable": False,
    }


def test_structured_ipc_error_is_sanitized_and_persisted_as_rejected(tmp_path: Path) -> None:
    relay = _relay_module()
    socket_path = tmp_path / "gateway.sock"

    def denied(_request: dict[str, Any]) -> dict[str, Any]:
        return {
            "status": "error",
            "error": {
                "code": "POLICY_DENIED",
                "message": "private 49123456789@s.whatsapp.net",
                "retryable": False,
            },
        }

    with FakeUnixGateway(socket_path, denied) as gateway:
        service = relay.RelayService(_config(tmp_path, socket_path))
        with _running(service) as address:
            _, body = _request(address, "POST", "/", payload=_send_payload(_invocation()))

    task = body["result"]["task"]
    assert len(gateway.requests) == 1
    assert task["status"]["state"] == "TASK_STATE_REJECTED"
    assert _profile_result(task)["error"] == {
        "code": "POLICY_DENIED",
        "message": "The local runtime rejected the request.",
        "retryable": False,
    }
    assert "49123456789" not in json.dumps(body)


def test_disabled_voice_skill_is_not_invokable(tmp_path: Path) -> None:
    relay = _relay_module()
    socket_path = tmp_path / "gateway.sock"
    with FakeUnixGateway(socket_path, _success) as gateway:
        service = relay.RelayService(
            _config(tmp_path, socket_path, voice_enabled=True), voice_probe=lambda: False
        )
        with _running(service) as address:
            _, body = _request(
                address,
                "POST",
                "/",
                payload=_send_payload(_invocation(skill="media.voice.generate")),
            )

    assert body["error"]["data"]["error"]["code"] == "CAPABILITY_UNAVAILABLE"
    assert gateway.requests == []


def test_unsupported_jsonrpc_method_returns_controlled_error(tmp_path: Path) -> None:
    relay = _relay_module()
    service = relay.RelayService(_config(tmp_path, tmp_path / "gateway.sock"))
    with _running(service) as address:
        _, body = _request(
            address,
            "POST",
            "/",
            payload={"jsonrpc": "2.0", "id": "rpc-1", "method": "message/stream", "params": {}},
        )

    assert body["error"]["code"] == -32601
    assert body["error"]["message"] == "Method not found"
