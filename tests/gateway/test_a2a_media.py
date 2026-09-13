from __future__ import annotations

import hashlib
import json
import logging
import socket
import threading
from pathlib import Path
from typing import Any

import httpx
import pytest
from yeoman_gateway.a2a.media import MediaStager, MediaStagingError
from yeoman_gateway.app import bootstrap

_EFFECT_ID = "a2a-effect-" + "1" * 40
_REQUEST_HASH = "a" * 64
_PUBLIC_DNS = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]
_BODIES = {
    "image/png": b"\x89PNG\r\n\x1a\nfixture",
    "image/jpeg": b"\xff\xd8\xff\xe0fixture\xff\xd9",
    "image/gif": b"GIF89afixture",
    "image/webp": b"RIFF\x08\x00\x00\x00WEBPfixture",
    "application/pdf": b"%PDF-1.7\nfixture\n%%EOF",
    "text/plain": b"plain text fixture\n",
}


def _resolver(*_args: object, **_kwargs: object) -> list[tuple[Any, ...]]:
    return _PUBLIC_DNS


def _part(
    mime_type: str = "image/png", **changes: object
) -> dict[str, object]:
    value: dict[str, object] = {
        "type": "image" if mime_type.startswith("image/") else "file",
        "uri": "https://media.example.test/fixture",
        "mime_type": mime_type,
        "filename": "fixture" + {
            "image/png": ".png",
            "image/jpeg": ".jpg",
            "image/gif": ".gif",
            "image/webp": ".webp",
            "application/pdf": ".pdf",
            "text/plain": ".txt",
        }[mime_type],
    }
    value.update(changes)
    return value


def _stager(
    tmp_path: Path,
    handler: Any,
    **changes: object,
) -> MediaStager:
    arguments: dict[str, object] = {
        "root": tmp_path / "outgoing" / "a2a",
        "allowed_origins": frozenset({"https://media.example.test"}),
        "max_bytes": 1_024,
        "ttl_seconds": 60,
        "timeout_seconds": 1.0,
        "resolver": _resolver,
        "transport": httpx.MockTransport(handler),
        "clock": lambda: 1_000.0,
    }
    arguments.update(changes)
    return MediaStager(**arguments)


@pytest.mark.parametrize(("mime_type", "body"), list(_BODIES.items()))
def test_stages_supported_media_atomically_and_reuses_valid_metadata(
    tmp_path: Path, mime_type: str, body: bytes
) -> None:
    calls = 0

    def respond(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            headers={"Content-Type": mime_type, "Content-Length": str(len(body))},
            content=body,
        )

    stager = _stager(tmp_path, respond)
    first = stager.stage(_EFFECT_ID, _REQUEST_HASH, _part(mime_type))
    second = stager.stage(_EFFECT_ID, _REQUEST_HASH, _part(mime_type))

    path = Path(str(first["path"]))
    assert first == second
    assert calls == 1
    assert path.parent == (tmp_path / "outgoing" / "a2a").resolve()
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.read_bytes() == body
    assert first["sha256"] == hashlib.sha256(body).hexdigest()
    assert first["size_bytes"] == len(body)
    assert first["expires_at"] == 1_060.0
    assert not list(path.parent.glob("*.tmp"))


@pytest.mark.parametrize(
    "uri",
    [
        "http://media.example.test/fixture",
        "file:///private/fixture.png",
        "https://other.example.test/fixture.png",
        "https://user:password@media.example.test/fixture.png",
        "https://media.example.test/../private.png",
    ],
)
def test_rejects_untrusted_urls_before_request(tmp_path: Path, uri: str) -> None:
    calls = 0

    def respond(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, content=_BODIES["image/png"])

    with pytest.raises(MediaStagingError):
        _stager(tmp_path, respond).stage(
            _EFFECT_ID, _REQUEST_HASH, _part(uri=uri)
        )

    assert calls == 0


@pytest.mark.parametrize(
    "address",
    ["127.0.0.1", "10.0.0.1", "100.64.0.1", "169.254.1.1", "192.0.2.1"],
)
def test_rejects_private_or_reserved_dns_before_request(
    tmp_path: Path, address: str
) -> None:
    calls = 0

    def respond(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, content=_BODIES["image/png"])

    def resolve(*_args: object, **_kwargs: object) -> list[tuple[Any, ...]]:
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443))]

    with pytest.raises(MediaStagingError):
        _stager(tmp_path, respond, resolver=resolve).stage(
            _EFFECT_ID, _REQUEST_HASH, _part()
        )

    assert calls == 0


def test_rejects_private_connected_peer_after_public_dns_preflight(
    tmp_path: Path,
) -> None:
    class PrivateStream:
        @staticmethod
        def get_extra_info(name: str) -> object:
            return ("127.0.0.1", 443) if name == "server_addr" else None

    response = httpx.Response(
        200,
        headers={"Content-Type": "image/png"},
        content=_BODIES["image/png"],
        extensions={"network_stream": PrivateStream()},
    )

    with pytest.raises(MediaStagingError):
        _stager(tmp_path, lambda _request: response).stage(
            _EFFECT_ID, _REQUEST_HASH, _part()
        )


def test_public_dns_result_is_pinned_before_connect(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    attempted_hosts: list[str] = []

    def connect(
        address: tuple[str, int],
        _timeout: float | None = None,
        source_address: tuple[str, int] | None = None,
    ) -> socket.socket:
        del source_address
        attempted_hosts.append(address[0])
        raise OSError("connection stopped by test")

    monkeypatch.setattr(socket, "create_connection", connect)

    with pytest.raises(MediaStagingError):
        MediaStager(
            tmp_path / "outgoing" / "a2a",
            allowed_origins={"https://media.example.test"},
            max_bytes=1_024,
            ttl_seconds=60,
            timeout_seconds=1,
            resolver=_resolver,
        ).stage(_EFFECT_ID, _REQUEST_HASH, _part())

    assert attempted_hosts == ["93.184.216.34"]


def test_dns_resolution_obeys_total_staging_deadline(tmp_path: Path) -> None:
    release = threading.Event()
    errors: list[Exception] = []

    def blocked_resolver(*_args: object, **_kwargs: object) -> list[tuple[Any, ...]]:
        release.wait(1)
        return _PUBLIC_DNS

    stager = _stager(
        tmp_path,
        lambda _request: httpx.Response(
            200,
            headers={"Content-Type": "image/png"},
            content=_BODIES["image/png"],
        ),
        resolver=blocked_resolver,
        timeout_seconds=0.02,
    )

    def stage() -> None:
        try:
            stager.stage(_EFFECT_ID, _REQUEST_HASH, _part())
        except Exception as exc:  # noqa: BLE001 - captured for the worker assertion
            errors.append(exc)

    worker = threading.Thread(target=stage, daemon=True)
    worker.start()
    worker.join(0.2)
    finished_before_release = not worker.is_alive()
    release.set()
    worker.join(1)

    assert finished_before_release
    assert len(errors) == 1
    assert isinstance(errors[0], MediaStagingError)


def test_repeated_dns_timeouts_bound_concurrent_resolver_workers(
    tmp_path: Path,
) -> None:
    release = threading.Event()
    drained = threading.Event()
    lock = threading.Lock()
    started = 0
    active = 0

    def blocked_resolver(*_args: object, **_kwargs: object) -> list[tuple[Any, ...]]:
        nonlocal active, started
        with lock:
            started += 1
            active += 1
        try:
            release.wait(2)
            return _PUBLIC_DNS
        finally:
            with lock:
                active -= 1
                if active == 0:
                    drained.set()

    stager = _stager(
        tmp_path,
        lambda _request: httpx.Response(
            200,
            headers={"Content-Type": "image/png"},
            content=_BODIES["image/png"],
        ),
        resolver=blocked_resolver,
        timeout_seconds=0.05,
    )

    try:
        for _ in range(12):
            with pytest.raises(MediaStagingError):
                stager.stage(_EFFECT_ID, _REQUEST_HASH, _part())
    finally:
        release.set()
        assert drained.wait(1)

    assert started <= 4


def test_drip_feed_obeys_total_staging_deadline(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    now = [0.0]

    class DripStream(httpx.SyncByteStream):
        def __iter__(self):  # type: ignore[no-untyped-def]
            now[0] = 0.6
            yield b"\x89PNG\r\n\x1a\n"
            now[0] = 1.1
            yield b"fixture"

    monkeypatch.setattr("yeoman_gateway.a2a.media.time.monotonic", lambda: now[0])
    response = httpx.Response(
        200,
        headers={"Content-Type": "image/png"},
        stream=DripStream(),
    )

    with pytest.raises(MediaStagingError):
        _stager(tmp_path, lambda _request: response).stage(
            _EFFECT_ID, _REQUEST_HASH, _part()
        )

    assert not list((tmp_path / "outgoing" / "a2a").glob(f"{_EFFECT_ID}.*"))


@pytest.mark.parametrize(
    ("case", "response", "part"),
    [
        (
            "redirect",
            httpx.Response(302, headers={"Location": "https://other.example.test/private"}),
            _part(),
        ),
        ("empty", httpx.Response(200, headers={"Content-Type": "image/png"}, content=b""), _part()),
        (
            "declared-mime",
            httpx.Response(200, headers={"Content-Type": "image/jpeg"}, content=_BODIES["image/png"]),
            _part(),
        ),
        (
            "actual-mime",
            httpx.Response(200, headers={"Content-Type": "image/png"}, content=b"not a png"),
            _part(),
        ),
        (
            "header-limit",
            httpx.Response(
                200,
                headers={"Content-Type": "image/png", "Content-Length": "1025"},
                content=_BODIES["image/png"],
            ),
            _part(),
        ),
        (
            "body-limit",
            httpx.Response(
                200,
                headers={"Content-Type": "image/png"},
                content=_BODIES["image/png"] + b"x" * 1_024,
            ),
            _part(),
        ),
    ],
)
def test_rejects_unsafe_http_responses(
    tmp_path: Path,
    case: str,
    response: httpx.Response,
    part: dict[str, object],
) -> None:
    del case

    with pytest.raises(MediaStagingError):
        _stager(tmp_path, lambda _request: response).stage(
            _EFFECT_ID, _REQUEST_HASH, part
        )

    root = tmp_path / "outgoing" / "a2a"
    assert not list(root.glob(f"{_EFFECT_ID}.*"))


def test_rejects_timeout_without_partial_file(tmp_path: Path) -> None:
    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("private timeout detail", request=request)

    with pytest.raises(MediaStagingError, match="MEDIA_STAGING_FAILED"):
        _stager(tmp_path, timeout).stage(_EFFECT_ID, _REQUEST_HASH, _part())

    assert not list((tmp_path / "outgoing" / "a2a").glob("*"))


@pytest.mark.parametrize("filename", ["../private.png", "/private.png", "folder/file.png"])
def test_rejects_traversal_filename_before_request(
    tmp_path: Path, filename: str
) -> None:
    calls = 0

    def respond(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, content=_BODIES["image/png"])

    with pytest.raises(MediaStagingError):
        _stager(tmp_path, respond).stage(
            _EFFECT_ID, _REQUEST_HASH, _part(filename=filename)
        )

    assert calls == 0


def test_rejects_symlink_destination(tmp_path: Path) -> None:
    root = tmp_path / "outgoing" / "a2a"
    root.mkdir(parents=True)
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"untouched")
    (root / f"{_EFFECT_ID}.png").symlink_to(outside)

    with pytest.raises(MediaStagingError):
        _stager(
            tmp_path,
            lambda _request: httpx.Response(
                200, headers={"Content-Type": "image/png"}, content=_BODIES["image/png"]
            ),
        ).stage(_EFFECT_ID, _REQUEST_HASH, _part())

    assert outside.read_bytes() == b"untouched"


@pytest.mark.parametrize("stale", ["expired", "hash"])
def test_expired_or_hash_mismatched_metadata_is_never_reused(
    tmp_path: Path, stale: str
) -> None:
    calls = 0

    def respond(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        body = _BODIES["image/png"]
        return httpx.Response(
            200, headers={"Content-Type": "image/png"}, content=body
        )

    stager = _stager(tmp_path, respond)
    first = stager.stage(_EFFECT_ID, _REQUEST_HASH, _part())
    if stale == "expired":
        sidecar = stager.root / f"{_EFFECT_ID}.media.json"
        value = json.loads(sidecar.read_text())
        value["expires_at"] = 999.0
        sidecar.write_text(json.dumps(value))
    else:
        Path(str(first["path"])).write_bytes(b"tampered")

    refreshed = stager.stage(_EFFECT_ID, _REQUEST_HASH, _part())

    assert calls == 2
    assert Path(str(refreshed["path"])).read_bytes() == _BODIES["image/png"]


def test_errors_and_logs_never_reveal_raw_url(
    caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    secret_uri = "https://media.example.test/file.png?X-Amz-Signature=private-secret"

    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"failed for {request.url}", request=request)

    with (
        caplog.at_level(logging.DEBUG),
        pytest.raises(MediaStagingError) as caught,
    ):
        _stager(tmp_path, fail).stage(
            _EFFECT_ID, _REQUEST_HASH, _part(uri=secret_uri)
        )

    captured = "\n".join(record.getMessage() for record in caplog.records)
    assert secret_uri not in str(caught.value)
    assert secret_uri not in captured


def test_successful_download_does_not_log_raw_url(
    caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    secret_uri = "https://media.example.test/file.png?X-Amz-Signature=private-secret"

    with caplog.at_level(logging.DEBUG):
        _stager(
            tmp_path,
            lambda _request: httpx.Response(
                200,
                headers={"Content-Type": "image/png"},
                content=_BODIES["image/png"],
            ),
        ).stage(_EFFECT_ID, _REQUEST_HASH, _part(uri=secret_uri))

    captured = "\n".join(record.getMessage() for record in caplog.records)
    assert secret_uri not in captured


def test_bootstrap_resolves_media_root_without_requiring_voice_generation(
    tmp_path: Path,
) -> None:
    managed = tmp_path / "outgoing"
    managed.mkdir()

    assert bootstrap.resolve_a2a_artifact_root(managed / "a2a", managed) == (
        managed / "a2a"
    ).resolve()
    assert bootstrap.resolve_a2a_artifact_root(managed, managed) is None
    assert bootstrap.resolve_a2a_artifact_root(tmp_path / "outside", managed) is None
