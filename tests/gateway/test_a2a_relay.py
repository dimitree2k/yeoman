from __future__ import annotations

import hashlib
import http.client
import json
import logging
import multiprocessing
import socket
import sqlite3
import threading
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast

import pytest
from yeoman_gateway.a2a import relay


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
    if request["cmd"] == "a2a_capabilities":
        return {
            "status": "ok",
            "response": {"skills": ["whatsapp.send"], "content_types": ["text"]},
        }
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
                "idempotency_key": args["input"]["idempotency_key"],
            },
        },
    }


def _config(tmp_path: Path, socket_path: Path, **changes: Any) -> relay.RelayConfig:
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
        "content_types": frozenset({"text"}),
        "managed_outgoing_root": tmp_path,
        "timeout_seconds": 1.0,
        "max_body_bytes": 65_536,
        "max_response_bytes": 65_536,
        "rate_limit_per_minute": 100,
    }
    values.update(changes)
    return relay.RelayConfig(**values)


@contextmanager
def _running(service: Any) -> Iterator[tuple[str, int]]:
    server = relay.create_server(service, ("127.0.0.1", 0))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield cast(tuple[str, int], server.server_address)
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


def _request_bytes(
    address: tuple[str, int], path: str, *, token: str | None = "test-secret-value"
) -> tuple[int, dict[str, str], bytes]:
    headers = {}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    connection = http.client.HTTPConnection(*address, timeout=2)
    connection.request("GET", path, headers=headers)
    response = connection.getresponse()
    raw = response.read()
    result_headers = {name.lower(): value for name, value in response.getheaders()}
    connection.close()
    return response.status, result_headers, raw


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
    return {
        "jsonrpc": "2.0",
        "id": "rpc-1",
        "method": "SendMessage",
        "params": {"message": message},
    }


def _profile_result(task: dict[str, Any]) -> dict[str, Any]:
    return task["artifacts"][0]["parts"][0]["data"]


def _dispatch_in_process(
    config: Any,
    payload: dict[str, Any],
    ready: Any,
    start: Any,
    results: Any,
) -> None:
    service = relay.RelayService(config)
    ready.put(True)
    start.wait(2)
    results.put(service.dispatch(payload))


def _voice_gateway(audio_path: Path) -> Callable[[dict[str, Any]], dict[str, Any]]:
    def respond(request: dict[str, Any]) -> dict[str, Any]:
        if request["cmd"] == "a2a_capabilities":
            return {
                "status": "ok",
                "response": {
                    "skills": ["media.voice.generate", "whatsapp.send"],
                    "content_types": ["text", "voice"],
                },
            }
        args = request["args"]
        correlation = {
            "task_id": args["task_id"],
            "context_id": args["context_id"],
            "idempotency_key": args["input"]["idempotency_key"],
        }
        if args["skill"] == "media.voice.generate":
            audio = audio_path.read_bytes()
            response = {
                "skill": args["skill"],
                "status": "completed",
                "output": {
                    "internal_artifact": {
                        "path": str(audio_path),
                        "mime_type": "audio/wav",
                        "duration_ms": 100,
                        "sha256": hashlib.sha256(audio).hexdigest(),
                        "size_bytes": len(audio),
                        "expires_at": time.time() + 120,
                        "filename": audio_path.name,
                    }
                },
                "correlation": correlation,
            }
        else:
            response = {
                "skill": args["skill"],
                "status": "completed",
                "output": {
                    "delivery_id": args["effect_id"],
                    "status": "sent",
                    "recipient": args["input"]["recipient"],
                },
                "correlation": correlation,
            }
        return {"status": "ok", "response": response}

    return respond


def test_voice_capability_requires_gateway_and_configured_artifact_serving(
    tmp_path: Path,
) -> None:
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()
    audio_path = artifact_root / "voice.wav"
    audio_path.write_bytes(b"wav-bytes")
    socket_path = tmp_path / "gateway.sock"

    with FakeUnixGateway(socket_path, _voice_gateway(audio_path)):
        enabled = relay.RelayService(
            _config(
                tmp_path,
                socket_path,
                artifact_root=artifact_root,
                content_types=frozenset({"text", "voice"}),
            )
        ).agent_card()
        disabled = relay.RelayService(
            _config(tmp_path, socket_path, state_path=tmp_path / "disabled.sqlite3")
        ).agent_card()
        unusable = relay.RelayService(
            _config(
                tmp_path,
                socket_path,
                state_path=tmp_path / "unusable.sqlite3",
                artifact_root=tmp_path / "missing-artifacts",
                content_types=frozenset({"text", "voice"}),
            )
        ).agent_card()

    assert [skill["id"] for skill in enabled["skills"]] == [
        "media.voice.generate",
        "whatsapp.send",
    ]
    assert [skill["id"] for skill in disabled["skills"]] == ["whatsapp.send"]
    assert [skill["id"] for skill in unusable["skills"]] == ["whatsapp.send"]


def test_voice_capability_requires_artifact_root_beneath_managed_outgoing_root(
    caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    managed = tmp_path / "managed"
    managed.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    audio_path = outside / "voice.wav"
    audio_path.write_bytes(b"voice")
    socket_path = tmp_path / "gateway.sock"

    with (
        FakeUnixGateway(socket_path, _voice_gateway(audio_path)),
        caplog.at_level(logging.INFO, logger="yeoman.a2a.relay"),
    ):
        service = relay.RelayService(
            _config(
                tmp_path,
                socket_path,
                artifact_root=outside,
                managed_outgoing_root=managed,
                content_types=frozenset({"text", "voice"}),
            )
        )
        card = service.agent_card()
        rejected = service.dispatch(
            _send_payload(_invocation(skill="media.voice.generate", key="outside-root"))
        )

    assert [skill["id"] for skill in card["skills"]] == ["whatsapp.send"]
    assert _profile_result(rejected["result"]["task"])["error"]["code"] == "SKILL_NOT_ADVERTISED"
    captured = "\n".join(record.getMessage() for record in caplog.records)
    assert str(outside) not in captured
    assert str(managed) not in captured


def test_generated_voice_is_registered_and_served_only_with_authentication(
    tmp_path: Path,
) -> None:
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()
    audio_path = artifact_root / "voice.wav"
    audio = b"real-wave-fixture"
    audio_path.write_bytes(audio)
    audio_path.chmod(0o600)
    socket_path = tmp_path / "gateway.sock"
    config = _config(
        tmp_path,
        socket_path,
        artifact_root=artifact_root,
        artifact_ttl_seconds=60,
        max_artifact_bytes=10_000,
        content_types=frozenset({"text", "voice"}),
    )
    invocation = _invocation(skill="media.voice.generate", key="voice-1", text="Hello")

    with FakeUnixGateway(socket_path, _voice_gateway(audio_path)) as gateway:
        service = relay.RelayService(config)
        with _running(service) as address:
            response = service.dispatch(_send_payload(invocation))
            output = _profile_result(response["result"]["task"])["output"]
            uri = output["artifact"]["uri"]
            path = "/artifacts/" + uri.rsplit("/", 1)[-1]
            unauthenticated = _request_bytes(address, path, token=None)
            status, headers, body = _request_bytes(address, path)

    service.schemas.validate_response("media.voice.generate", output)
    assert output["mime_type"] == "audio/wav"
    assert output["duration_ms"] == 100
    assert output["artifact"]["sha256"] == hashlib.sha256(audio).hexdigest()
    assert unauthenticated[0] == 401
    assert status == 200
    assert body == audio
    assert headers["content-type"] == "audio/wav"
    assert headers["content-length"] == str(len(audio))
    assert headers["cache-control"] == "no-store"
    assert [request["cmd"] for request in gateway.requests] == ["a2a_invoke"]


def test_generation_retry_reuses_task_and_artifact_without_second_gateway_call(
    tmp_path: Path,
) -> None:
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()
    audio_path = artifact_root / "voice.wav"
    audio_path.write_bytes(b"voice")
    socket_path = tmp_path / "gateway.sock"
    config = _config(
        tmp_path,
        socket_path,
        artifact_root=artifact_root,
        artifact_ttl_seconds=60,
        content_types=frozenset({"text", "voice"}),
    )
    payload = _send_payload(_invocation(skill="media.voice.generate", key="voice-retry"))

    with FakeUnixGateway(socket_path, _voice_gateway(audio_path)) as gateway:
        service = relay.RelayService(config)
        first = service.dispatch(payload)
        second = service.dispatch(payload)

    assert second == first
    assert [request["cmd"] for request in gateway.requests] == ["a2a_invoke"]


def test_explicit_voice_send_passes_relay_owned_artifact_path_separately(
    tmp_path: Path,
) -> None:
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()
    audio_path = artifact_root / "voice.wav"
    audio_path.write_bytes(b"voice")
    socket_path = tmp_path / "gateway.sock"
    config = _config(
        tmp_path,
        socket_path,
        artifact_root=artifact_root,
        artifact_ttl_seconds=60,
        content_types=frozenset({"text", "voice"}),
    )
    with FakeUnixGateway(socket_path, _voice_gateway(audio_path)) as gateway:
        service = relay.RelayService(config)
        generated = service.dispatch(
            _send_payload(_invocation(skill="media.voice.generate", key="voice-source"))
        )
        uri = _profile_result(generated["result"]["task"])["output"]["artifact"]["uri"]
        invocation = {
            "skill": "whatsapp.send",
            "input": {
                "recipient": {"type": "group", "alias": "team-example"},
                "content": [{"type": "voice", "uri": uri, "mime_type": "audio/wav"}],
                "idempotency_key": "voice-send",
            },
        }
        sent = service.dispatch(_send_payload(invocation))

    assert _profile_result(sent["result"]["task"])["status"] == "completed"
    send_args = gateway.requests[1]["args"]
    assert send_args["input"] == invocation["input"]
    assert send_args["resolved_artifacts"] == [
        {
            "peer": "hermes-test",
            "uri": uri,
            "path": str(audio_path.resolve()),
            "mime_type": "audio/wav",
            "duration_ms": 100,
            "sha256": hashlib.sha256(b"voice").hexdigest(),
            "size_bytes": 5,
            "expires_at": pytest.approx(time.time() + 60, abs=2),
        }
    ]


def test_terminal_voice_send_retry_returns_original_after_artifact_expiry(
    tmp_path: Path,
) -> None:
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()
    audio_path = artifact_root / "voice.wav"
    audio_path.write_bytes(b"voice")
    socket_path = tmp_path / "gateway.sock"
    config = _config(
        tmp_path,
        socket_path,
        artifact_root=artifact_root,
        content_types=frozenset({"text", "voice"}),
    )
    with FakeUnixGateway(socket_path, _voice_gateway(audio_path)) as gateway:
        service = relay.RelayService(config)
        generated = service.dispatch(
            _send_payload(_invocation(skill="media.voice.generate", key="retry-source"))
        )
        uri = _profile_result(generated["result"]["task"])["output"]["artifact"]["uri"]
        invocation = {
            "skill": "whatsapp.send",
            "input": {
                "recipient": {"type": "group", "alias": "team-example"},
                "content": [{"type": "voice", "uri": uri, "mime_type": "audio/wav"}],
                "idempotency_key": "voice-terminal-retry",
            },
        }
        payload = _send_payload(invocation)
        first = service.dispatch(payload)
        with sqlite3.connect(config.state_path) as connection:
            connection.execute("UPDATE artifacts SET expires_at=0")
        retry = service.dispatch(payload)
        invocation["input"]["idempotency_key"] = "voice-new-after-expiry"
        fresh = service.dispatch(_send_payload(invocation))

    assert retry == first
    assert _profile_result(fresh["result"]["task"])["error"]["code"] == "ARTIFACT_DENIED"
    assert [request["cmd"] for request in gateway.requests] == [
        "a2a_invoke",
        "a2a_invoke",
    ]


def test_voice_invocation_is_rejected_when_artifact_serving_is_disabled(
    tmp_path: Path,
) -> None:
    service = relay.RelayService(_config(tmp_path, tmp_path / "missing.sock"))

    response = service.dispatch(
        _send_payload(_invocation(skill="media.voice.generate", key="disabled-voice"))
    )

    result = _profile_result(response["result"]["task"])
    assert result["status"] == "rejected"
    assert result["error"]["code"] == "SKILL_NOT_ADVERTISED"


def test_voice_send_rejects_unknown_and_traversal_artifact_urls(tmp_path: Path) -> None:
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()
    service = relay.RelayService(
        _config(
            tmp_path,
            tmp_path / "missing.sock",
            artifact_root=artifact_root,
            content_types=frozenset({"text", "voice"}),
        )
    )
    for uri in (
        "https://external.example/voice.wav",
        "https://relay.example.test/a2a/artifacts/../private",
        "https://relay.example.test/a2a/artifacts/unknown",
    ):
        invocation = {
            "skill": "whatsapp.send",
            "input": {
                "recipient": {"type": "group", "alias": "team-example"},
                "content": [{"type": "voice", "uri": uri, "mime_type": "audio/wav"}],
                "idempotency_key": "bad-" + hashlib.sha256(uri.encode()).hexdigest()[:8],
            },
        }

        response = service.dispatch(_send_payload(invocation))

        result = _profile_result(response["result"]["task"])
        assert result["status"] == "rejected"
        assert result["error"]["code"] == "ARTIFACT_DENIED"


def test_artifact_get_rejects_other_peer_and_expired_records(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    root.mkdir()
    path = root / "voice.wav"
    path.write_bytes(b"voice")
    service = relay.RelayService(
        _config(tmp_path, tmp_path / "missing.sock", artifact_root=root)
    )
    common = {
        "effect_id": "a2a-effect-" + "1" * 40,
        "path": str(path),
        "mime_type": "audio/wav",
        "duration_ms": 100,
        "sha256": hashlib.sha256(b"voice").hexdigest(),
        "size_bytes": 5,
    }
    service.store.register_artifact(
        {**common, "opaque_id": "a" * 48, "peer": "other", "expires_at": time.time() + 60}
    )
    service.store.register_artifact(
        {
            **common,
            "opaque_id": "b" * 48,
            "peer": "hermes-test",
            "effect_id": "a2a-effect-" + "2" * 40,
            "expires_at": time.time() - 1,
        }
    )

    with _running(service) as address:
        other = _request_bytes(address, "/artifacts/" + "a" * 48)
        expired = _request_bytes(address, "/artifacts/" + "b" * 48)

    assert other[0] == 404
    assert expired[0] == 404


def test_artifact_registry_cleanup_is_expired_only_and_bounded(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    root.mkdir()
    path = root / "voice.wav"
    path.write_bytes(b"voice")
    service = relay.RelayService(
        _config(tmp_path, tmp_path / "missing.sock", artifact_root=root)
    )
    expired_path = root / ("a2a-effect-" + "0" * 40 + ".wav")
    expired_path.write_bytes(b"voice")
    expired_sidecar = root / ("a2a-effect-" + "0" * 40 + ".json")
    expired_sidecar.write_text(
        json.dumps(
            {
                "path": str(expired_path),
                "filename": expired_path.name,
                "expires_at": time.time() - 1,
            }
        )
    )
    common = {
        "peer": "hermes-test",
        "path": str(path),
        "mime_type": "audio/wav",
        "duration_ms": 100,
        "sha256": hashlib.sha256(b"voice").hexdigest(),
        "size_bytes": 5,
    }
    for index in range(70):
        indexed_path = expired_path if index == 0 else path
        service.store.register_artifact(
            {
                **common,
                "path": str(indexed_path),
                "opaque_id": f"{index:048x}",
                "effect_id": "a2a-effect-" + f"{index:040x}",
                "expires_at": time.time() - 1,
            }
        )
    service.store.register_artifact(
        {
            **common,
            "opaque_id": "f" * 48,
            "effect_id": "a2a-effect-" + "f" * 40,
            "expires_at": time.time() + 60,
        }
    )

    assert service.artifact_response("0" * 48) is None
    with sqlite3.connect(service.config.state_path) as connection:
        expired = connection.execute(
            "SELECT count(*) FROM artifacts WHERE expires_at <= ?", (time.time(),)
        ).fetchone()[0]
        fresh = connection.execute(
            "SELECT count(*) FROM artifacts WHERE expires_at > ?", (time.time(),)
        ).fetchone()[0]

    assert 0 < expired <= 6
    assert fresh == 1
    assert not expired_path.exists()
    assert not expired_sidecar.exists()


def test_voice_artifact_path_and_uri_are_never_logged(
    caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    root = tmp_path / "private-artifacts"
    root.mkdir()
    path = root / "secret-voice.wav"
    path.write_bytes(b"voice")
    socket_path = tmp_path / "gateway.sock"
    config = _config(
        tmp_path,
        socket_path,
        artifact_root=root,
        content_types=frozenset({"text", "voice"}),
    )
    with (
        FakeUnixGateway(socket_path, _voice_gateway(path)),
        caplog.at_level(logging.INFO, logger="yeoman.a2a.relay"),
    ):
        response = relay.RelayService(config).dispatch(
            _send_payload(_invocation(skill="media.voice.generate", key="log-safe"))
        )

    uri = _profile_result(response["result"]["task"])["output"]["artifact"]["uri"]
    captured = "\n".join(f"{record.getMessage()} {record.args!r}" for record in caplog.records)
    assert str(path) not in captured
    assert uri not in captured


def test_config_reads_explicit_private_runtime_settings_without_revealing_secret(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("YEOMAN_A2A_BIND_HOST", "100.64.1.2")
    monkeypatch.setenv("YEOMAN_A2A_PORT", "9911")
    monkeypatch.setenv("YEOMAN_A2A_ALLOWED_PEER_IPS", "100.64.1.3,100.64.1.4")
    monkeypatch.setenv("YEOMAN_A2A_PEER_ID", "hermes")
    monkeypatch.setenv("YEOMAN_A2A_BEARER_SECRET", "not-for-repr")
    monkeypatch.setenv("YEOMAN_A2A_SOCKET_PATH", str(tmp_path / "gateway.sock"))
    monkeypatch.setenv("YEOMAN_A2A_STATE_PATH", str(tmp_path / "state.sqlite3"))
    monkeypatch.setenv("YEOMAN_A2A_PUBLIC_URL", "https://relay.example.test/a2a")
    monkeypatch.setenv("YEOMAN_A2A_WHATSAPP_ENABLED", "true")
    monkeypatch.setenv("YEOMAN_A2A_CONTENT_TYPES", "text")

    config = relay.RelayConfig.from_env()
    config.validate()

    assert config.allowed_peer_ips == frozenset({"100.64.1.3", "100.64.1.4"})
    assert config.content_types == frozenset({"text"})
    assert "not-for-repr" not in repr(config)
    with pytest.raises(relay.RelayConfigurationError, match="explicit private"):
        relay.RelayConfig(**{**config.__dict__, "bind_host": "0.0.0.0"}).validate()
    with pytest.raises(relay.RelayConfigurationError, match="public URL"):
        relay.RelayConfig(
            **{**config.__dict__, "public_url": "https://localhost:9900/a2a"}
        ).validate()


def test_config_accepts_legacy_service_environment_names_needed_for_cutover(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in (
        "YEOMAN_A2A_BIND_HOST",
        "YEOMAN_A2A_ALLOWED_PEER_IPS",
        "YEOMAN_A2A_BEARER_SECRET",
        "YEOMAN_A2A_SOCKET_PATH",
        "YEOMAN_A2A_STATE_PATH",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("YEOMAN_A2A_HOST", "100.64.1.2")
    monkeypatch.setenv("YEOMAN_A2A_ALLOWED_PEERS", "100.64.1.3")
    monkeypatch.setenv("YEOMAN_A2A_TOKEN", "legacy-secret")
    monkeypatch.setenv("YEOMAN_A2A_PEER_ID", "hermes")
    monkeypatch.setenv(
        "YEOMAN_A2A_PUBLIC_URL", "http://moltypython.example.ts.net:9900"
    )

    config = relay.RelayConfig.from_env()

    assert config.bind_host == "100.64.1.2"
    assert config.allowed_peer_ips == frozenset({"100.64.1.3"})
    assert config.bearer_secret == "legacy-secret"
    assert config.socket_path == Path("~/.yeoman/run/gateway.sock").expanduser()
    assert config.state_path == Path("~/.yeoman/data/a2a/relay.db").expanduser()
    assert config.public_url == "http://moltypython.example.ts.net:9900"


def test_agent_card_only_exposes_live_structured_skills(tmp_path: Path) -> None:
    socket_path = tmp_path / "gateway.sock"
    config = _config(tmp_path, socket_path)
    with FakeUnixGateway(socket_path, _success) as gateway:
        service = relay.RelayService(config)
        with _running(service) as address:
            status, card = _request(address, "GET", "/.well-known/agent-card.json", token=None)

    assert status == 200
    assert set(card) == {
        "name",
        "description",
        "supportedInterfaces",
        "version",
        "capabilities",
        "securitySchemes",
        "securityRequirements",
        "defaultInputModes",
        "defaultOutputModes",
        "skills",
    }
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
        "extendedAgentCard": False,
        "extensions": [
            {
                "uri": "urn:hermes-yeoman:a2a-profile:v1",
                "description": "Hermes/Yeoman structured skill profile v1",
                "required": True,
            }
        ],
    }
    assert card["securitySchemes"] == {"bearer": {"httpAuthSecurityScheme": {"scheme": "Bearer"}}}
    assert card["securityRequirements"] == [{"schemes": {"bearer": {"list": []}}}]
    assert card["defaultInputModes"] == ["application/json"]
    assert card["defaultOutputModes"] == ["application/json"]
    assert [skill["id"] for skill in card["skills"]] == ["whatsapp.send"]
    assert gateway.requests == [{"cmd": "a2a_capabilities", "args": {}}]
    serialized = json.dumps(card)
    for private_value in (
        config.bearer_secret,
        config.peer_id,
        str(config.socket_path),
        str(config.state_path),
        "127.0.0.1",
    ):
        assert private_value not in serialized


def test_card_omits_configured_skills_when_gateway_capabilities_are_unavailable(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path, tmp_path / "missing.sock")

    assert relay.RelayService(config).agent_card()["skills"] == []


def test_card_intersects_gateway_capabilities_with_relay_config(tmp_path: Path) -> None:
    socket_path = tmp_path / "gateway.sock"
    with FakeUnixGateway(socket_path, _success) as gateway:
        skills = relay.RelayService(
            _config(tmp_path, socket_path, whatsapp_enabled=False)
        ).agent_card()["skills"]

    assert skills == []
    assert gateway.requests == [{"cmd": "a2a_capabilities", "args": {}}]


@pytest.mark.parametrize(
    "gateway_response",
    [
        {"status": "error", "error": {"code": "UNAVAILABLE"}},
        {"status": "ok", "response": {}},
        {"status": "ok", "response": {"skills": "whatsapp.send", "content_types": ["text"]}},
        {
            "status": "ok",
            "response": {"skills": ["whatsapp.send"], "content_types": ["image"]},
        },
    ],
)
def test_card_fails_closed_on_unusable_gateway_capabilities(
    tmp_path: Path, gateway_response: dict[str, Any]
) -> None:
    socket_path = tmp_path / "gateway.sock"
    with FakeUnixGateway(socket_path, lambda _request: gateway_response) as gateway:
        skills = relay.RelayService(_config(tmp_path, socket_path)).agent_card()["skills"]

    assert skills == []
    assert gateway.requests == [{"cmd": "a2a_capabilities", "args": {}}]


def test_valid_structured_text_invocation_returns_valid_task_and_exact_correlation(
    tmp_path: Path,
) -> None:
    socket_path = tmp_path / "gateway.sock"
    payload = _send_payload(_invocation())
    payload["params"]["configuration"] = {
        "acceptedOutputModes": ["application/json"],
        "historyLength": 0,
        "returnImmediately": False,
    }
    payload["params"]["metadata"] = {"caller": "synthetic"}
    with FakeUnixGateway(socket_path, _success) as gateway:
        service = relay.RelayService(_config(tmp_path, socket_path))
        with _running(service) as address:
            status, body = _request(address, "POST", "/", payload=payload)

    task = body["result"]["task"]
    result = _profile_result(task)
    assert status == 200
    assert task["contextId"] == "context-1"
    assert "referenceTaskIds" not in task
    assert task["status"]["state"] == "TASK_STATE_COMPLETED"
    assert result["status"] == "completed"
    assert result["output"]["status"] == "sent"
    assert task["metadata"] == {
        "profile": "urn:hermes-yeoman:a2a-profile:v1",
        "contractRelease": "1.0.0",
    }
    assert set(task) == {"id", "contextId", "status", "artifacts", "metadata"}
    assert len(gateway.requests) == 1
    ipc = gateway.requests[0]
    assert ipc["cmd"] == "a2a_invoke"
    assert set(ipc["args"]) == {
        "peer",
        "skill",
        "input",
        "task_id",
        "context_id",
        "effect_id",
    }
    assert ipc["args"]["peer"] == "hermes-test"
    assert ipc["args"]["context_id"] == "context-1"
    assert ipc["args"]["skill"] == "whatsapp.send"
    assert ipc["args"]["input"] == _invocation()["input"]
    assert ipc["args"]["effect_id"].startswith("a2a-effect-")


def test_logs_never_capture_raw_caller_correlation_or_private_values(
    caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    socket_path = tmp_path / "gateway.sock"
    denied = (
        "49123456789@s.whatsapp.net",
        "https://private.invalid/file?X-Amz-Signature=secret",
        "test-secret-value",
    )
    payload = _send_payload(
        _invocation(),
        contextId=denied[0],
        referenceTaskIds=[denied[1]],
    )
    with (
        FakeUnixGateway(socket_path, _success) as gateway,
        caplog.at_level(logging.INFO, logger="yeoman.a2a.relay"),
    ):
        response = relay.RelayService(_config(tmp_path, socket_path)).dispatch(payload)

    assert "result" in response
    assert len(gateway.requests) == 1
    captured = "\n".join(f"{record.getMessage()} {record.args!r}" for record in caplog.records)
    assert all(value not in captured for value in denied)


@pytest.mark.parametrize(
    "payload",
    [
        _send_payload(
            _invocation(),
            parts=[{"text": '{"operation":"send_whatsapp"}', "mediaType": "text/plain"}],
        ),
        _send_payload(
            _invocation(),
            parts=[
                {"data": _invocation(), "mediaType": "application/json"},
                {"data": _invocation(), "mediaType": "application/json"},
            ],
        ),
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
    ],
)
def test_invalid_or_legacy_invocations_never_reach_ipc(
    tmp_path: Path, payload: dict[str, Any]
) -> None:
    socket_path = tmp_path / "gateway.sock"
    with FakeUnixGateway(socket_path, _success) as gateway:
        service = relay.RelayService(_config(tmp_path, socket_path))
        with _running(service) as address:
            status, body = _request(address, "POST", "/", payload=payload)

    assert status == 200
    assert body["error"]["code"] == -32602
    assert body["error"]["message"] == "Invalid params"
    assert gateway.requests == []


@pytest.mark.parametrize(
    ("invocation", "code"),
    [
        ({"skill": "unknown.skill", "input": {}}, "SKILL_NOT_ADVERTISED"),
        ({"skill": "whatsapp.send"}, "INVALID_INVOCATION"),
        (
            {"skill": "whatsapp.send", "input": _invocation()["input"], "extra": True},
            "INVALID_INVOCATION",
        ),
        (
            {
                "skill": "whatsapp.send",
                "input": {**_invocation()["input"], "extra": True},
            },
            "INVALID_SKILL_INPUT",
        ),
        (
            _invocation(text="image", key="image-1")
            | {
                "input": {
                    "recipient": {"type": "group", "alias": "team-example"},
                    "content": [
                        {"type": "image", "uri": "artifact://image/1", "mime_type": "image/png"}
                    ],
                    "idempotency_key": "image-1",
                }
            },
            "CONTENT_TYPE_DENIED",
        ),
    ],
)
def test_profile_validation_failures_return_terminal_contract_rejections(
    tmp_path: Path, invocation: dict[str, Any], code: str
) -> None:
    service = relay.RelayService(_config(tmp_path, tmp_path / "gateway.sock"))

    response = service.dispatch(_send_payload(invocation))

    task = response["result"]["task"]
    result = _profile_result(task)
    assert task["status"]["state"] == "TASK_STATE_REJECTED"
    assert result["error"]["code"] == code
    service.schemas.validate_result(result)


@pytest.mark.parametrize(
    ("payload", "rpc_code"),
    [
        (
            {
                **_send_payload(_invocation()),
                "unexpected": True,
            },
            -32600,
        ),
        (
            {
                **_send_payload(_invocation()),
                "params": {
                    "message": _send_payload(_invocation())["params"]["message"],
                    "extra": True,
                },
            },
            -32602,
        ),
        (
            _send_payload(_invocation(), role="ROLE_AGENT"),
            -32602,
        ),
        (
            _send_payload(_invocation(), messageId=""),
            -32602,
        ),
        (
            {
                **_send_payload(_invocation()),
                "params": {
                    "message": {
                        key: value
                        for key, value in _send_payload(_invocation())["params"]["message"].items()
                        if key != "messageId"
                    }
                },
            },
            -32602,
        ),
        (
            _send_payload(_invocation(), extra=True),
            -32602,
        ),
        (
            _send_payload(
                _invocation(),
                parts=[
                    {
                        "data": _invocation(),
                        "url": "https://example.test/file",
                        "mediaType": "application/json",
                    }
                ],
            ),
            -32602,
        ),
        (
            _send_payload(
                _invocation(),
                parts=[
                    {
                        "data": _invocation(),
                        "mediaType": "application/json",
                        "unexpected": True,
                    }
                ],
            ),
            -32602,
        ),
        (
            {
                "jsonrpc": "2.0",
                "id": "rpc-get",
                "method": "GetTask",
                "params": {"taskId": "task-1"},
            },
            -32602,
        ),
        (
            {
                "jsonrpc": "2.0",
                "id": "rpc-get",
                "method": "GetTask",
                "params": {"id": "task-1", "historyLength": "zero"},
            },
            -32602,
        ),
    ],
)
def test_protojson_parser_rejects_wrong_roles_oneofs_and_unknown_fields(
    tmp_path: Path, payload: dict[str, Any], rpc_code: int
) -> None:

    response = relay.RelayService(_config(tmp_path, tmp_path / "gateway.sock")).dispatch(payload)

    assert response["error"]["code"] == rpc_code


def test_optional_text_part_cannot_override_authoritative_data(tmp_path: Path) -> None:
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
    service = relay.RelayService(_config(tmp_path, tmp_path / "gateway.sock"))
    with _running(service) as address:
        with socket.create_connection(address, timeout=1) as client:
            client.sendall(
                b"POST / HTTP/1.1\r\nHost: relay\r\nContent-Length: 20\r\nConnection: close\r\n\r\n"
            )
            response = client.recv(4096)

    assert response.startswith(b"HTTP/1.0 401")


def test_malformed_json_returns_jsonrpc_parse_error(tmp_path: Path) -> None:
    service = relay.RelayService(_config(tmp_path, tmp_path / "gateway.sock"))
    with _running(service) as address:
        connection = http.client.HTTPConnection(*address, timeout=2)
        connection.request(
            "POST",
            "/",
            body=b"{broken",
            headers={
                "Authorization": "Bearer test-secret-value",
                "Content-Type": "application/json",
            },
        )
        response = connection.getresponse()
        body = json.loads(response.read())
        connection.close()

    assert response.status == 200
    assert body == {
        "jsonrpc": "2.0",
        "id": None,
        "error": {"code": -32700, "message": "Parse error"},
    }


def test_authenticated_partial_body_is_time_bounded(tmp_path: Path) -> None:
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
            too_large_status, _ = _request(address, "POST", "/", payload={"padding": "x" * 64})
            limited_status, limited = _request(address, "POST", "/", payload={})

    assert too_large_status == 413
    assert limited_status == 429
    assert limited["error"]["code"] == "RATE_LIMITED"
    assert gateway.requests == []


def test_same_idempotency_key_is_atomic_and_conflicts_are_deterministic(tmp_path: Path) -> None:
    socket_path = tmp_path / "gateway.sock"
    with FakeUnixGateway(socket_path, _success) as gateway:
        service = relay.RelayService(_config(tmp_path, socket_path))
        with _running(service) as address:
            request = _send_payload(_invocation())
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [
                    pool.submit(_request, address, "POST", "/", payload=request) for _ in range(2)
                ]
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


def test_submitted_retry_uses_original_persisted_correlation(tmp_path: Path) -> None:
    socket_path = tmp_path / "gateway.sock"
    config = _config(tmp_path, socket_path)
    service = relay.RelayService(config)
    claim = service.store.claim(
        peer=config.peer_id,
        invocation=_invocation(),
        context_id=" original/context ",
        reference_task_ids=[" original reference "],
    )
    retry = _send_payload(
        _invocation(),
        contextId="retry-context",
        referenceTaskIds=["retry-reference"],
    )

    with FakeUnixGateway(socket_path, _success) as gateway:
        response = relay.RelayService(config).dispatch(retry)

    task = response["result"]["task"]
    assert task["id"] == claim.task_id
    assert task["contextId"] == " original/context "
    assert "referenceTaskIds" not in task
    assert gateway.requests[0]["args"]["context_id"] == " original/context "
    assert _profile_result(task)["correlation"]["reference_task_ids"] == [" original reference "]


def test_sqlite_claim_serializes_effect_across_processes(tmp_path: Path) -> None:
    socket_path = tmp_path / "gateway.sock"
    config = _config(tmp_path, socket_path)
    context = multiprocessing.get_context("spawn")
    ready = context.Queue()
    start = context.Event()
    results = context.Queue()
    processes = [
        context.Process(
            target=_dispatch_in_process,
            args=(config, _send_payload(_invocation()), ready, start, results),
        )
        for _ in range(2)
    ]

    with FakeUnixGateway(socket_path, _success) as gateway:
        for process in processes:
            process.start()
        for _ in processes:
            assert ready.get(timeout=3) is True
        start.set()
        responses = [results.get(timeout=5) for _ in processes]
        for process in processes:
            process.join(5)
            assert process.exitcode == 0

    assert responses[0]["result"]["task"] == responses[1]["result"]["task"]
    assert len(gateway.requests) == 1


def test_get_task_is_peer_scoped_and_rejects_malformed_ids(tmp_path: Path) -> None:
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
                payload={
                    "jsonrpc": "2.0",
                    "id": "rpc-get",
                    "method": "tasks/get",
                    "params": {"id": task_id, "historyLength": 0},
                },
            )
            _, malformed = _request(
                address,
                "POST",
                "/",
                payload={
                    "jsonrpc": "2.0",
                    "id": "rpc-get",
                    "method": "GetTask",
                    "params": {"id": ""},
                },
            )

        other = _config(
            tmp_path, socket_path, peer_id="different-peer", bearer_secret="other-secret"
        )
        with _running(relay.RelayService(other)) as address:
            _, hidden = _request(
                address,
                "POST",
                "/",
                token="other-secret",
                payload={
                    "jsonrpc": "2.0",
                    "id": "rpc-get",
                    "method": "GetTask",
                    "params": {"id": task_id},
                },
            )

    assert found["result"] == sent["result"]["task"]
    assert malformed["error"]["code"] == -32602
    assert hidden["error"]["code"] == -32001

    opaque = relay.RelayService(config).dispatch(
        {
            "jsonrpc": "2.0",
            "id": "rpc-get",
            "method": "GetTask",
            "params": {"id": " opaque/task id "},
        }
    )
    assert opaque["error"]["code"] == -32001


@pytest.mark.parametrize(
    "message_changes",
    [
        {"contextId": ""},
        {"contextId": "x" * 201},
        {"referenceTaskIds": [""]},
        {"referenceTaskIds": ["x" * 201]},
        {"referenceTaskIds": ["duplicate", "duplicate"]},
        {"referenceTaskIds": [{}]},
    ],
)
def test_malformed_correlation_identifiers_are_rejected_without_repair(
    tmp_path: Path, message_changes: dict[str, Any]
) -> None:
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

    assert body["error"]["code"] == -32602
    assert gateway.requests == []


def test_opaque_correlation_identifiers_are_preserved_without_normalization(tmp_path: Path) -> None:
    socket_path = tmp_path / "gateway.sock"
    payload = _send_payload(
        _invocation(),
        contextId=" context/opaque ",
        referenceTaskIds=[" reference one ", "reference/two"],
    )
    with FakeUnixGateway(socket_path, _success) as gateway:
        service = relay.RelayService(_config(tmp_path, socket_path))
        with _running(service) as address:
            _, body = _request(address, "POST", "/", payload=payload)

    task = body["result"]["task"]
    result = _profile_result(task)
    assert task["contextId"] == " context/opaque "
    assert result["correlation"]["reference_task_ids"] == [
        " reference one ",
        "reference/two",
    ]
    assert gateway.requests[0]["args"]["context_id"] == " context/opaque "


def test_invalid_outgoing_result_becomes_terminal_profile_failure(tmp_path: Path) -> None:
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


def test_return_immediately_true_is_rejected_before_ipc(tmp_path: Path) -> None:
    socket_path = tmp_path / "gateway.sock"
    payload = _send_payload(_invocation())
    payload["params"]["configuration"] = {"returnImmediately": True}
    with FakeUnixGateway(socket_path, _success) as gateway:
        body = relay.RelayService(_config(tmp_path, socket_path)).dispatch(payload)

    assert body["error"] == {"code": -32602, "message": "Invalid params"}
    assert gateway.requests == []


def test_unsupported_jsonrpc_method_returns_controlled_error(tmp_path: Path) -> None:
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
