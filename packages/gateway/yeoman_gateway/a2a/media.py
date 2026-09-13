"""Validated staging for remote A2A image and file content."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import queue
import re
import socket
import threading
import time
import uuid
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

import httpcore
import httpx

_EFFECT_ID = re.compile(r"^a2a-effect-[a-f0-9]{40}$")
_REQUEST_HASH = re.compile(r"^[a-f0-9]{64}$")
_FORMATS = {
    "image/png": ("image", ".png"),
    "image/jpeg": ("image", ".jpg"),
    "image/gif": ("image", ".gif"),
    "image/webp": ("image", ".webp"),
    "application/pdf": ("file", ".pdf"),
    "text/plain": ("file", ".txt"),
}
_SIDECAR_MAX_BYTES = 16_384


class MediaStagingError(RuntimeError):
    """A sanitized media trust-boundary failure."""


class _DeadlineStream(httpcore.NetworkStream):
    def __init__(
        self,
        stream: httpcore.NetworkStream,
        deadline: float,
        monotonic: Callable[[], float],
    ) -> None:
        self.stream = stream
        self.deadline = deadline
        self.monotonic = monotonic

    def _timeout(
        self, requested: float | None, error: type[httpcore.TimeoutException]
    ) -> float:
        remaining = self.deadline - self.monotonic()
        if remaining <= 0:
            raise error("media staging deadline exceeded")
        return remaining if requested is None else min(requested, remaining)

    def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        return self.stream.read(max_bytes, self._timeout(timeout, httpcore.ReadTimeout))

    def write(self, buffer: bytes, timeout: float | None = None) -> None:
        self.stream.write(buffer, self._timeout(timeout, httpcore.WriteTimeout))

    def close(self) -> None:
        self.stream.close()

    def start_tls(
        self,
        ssl_context: Any,
        server_hostname: str | None = None,
        timeout: float | None = None,
    ) -> httpcore.NetworkStream:
        stream = self.stream.start_tls(
            ssl_context,
            server_hostname,
            self._timeout(timeout, httpcore.ConnectTimeout),
        )
        return _DeadlineStream(stream, self.deadline, self.monotonic)

    def get_extra_info(self, info: str) -> Any:
        return self.stream.get_extra_info(info)


class _PinnedBackend(httpcore.SyncBackend):
    def __init__(
        self,
        host: str,
        port: int,
        addresses: tuple[str, ...],
        deadline: float,
        monotonic: Callable[[], float],
    ) -> None:
        self.host = host
        self.port = port
        self.addresses = addresses
        self.deadline = deadline
        self.monotonic = monotonic

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.NetworkStream:
        if host != self.host or port != self.port:
            raise httpcore.ConnectError("media endpoint changed")
        last_error: httpcore.NetworkError | None = None
        for address in self.addresses:
            remaining = self.deadline - self.monotonic()
            if remaining <= 0:
                raise httpcore.ConnectTimeout("media staging deadline exceeded")
            try:
                stream = super().connect_tcp(
                    address,
                    port,
                    timeout=remaining if timeout is None else min(timeout, remaining),
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except httpcore.NetworkError as exc:
                last_error = exc
                continue
            return _DeadlineStream(stream, self.deadline, self.monotonic)
        if last_error is not None:
            raise last_error
        raise httpcore.ConnectError("media endpoint unavailable")


class _PinnedTransport(httpx.HTTPTransport):
    def __init__(self, backend: _PinnedBackend) -> None:
        super().__init__(trust_env=False)
        self._pool._network_backend = backend  # type: ignore[attr-defined]


def normalized_media_origins(origins: Iterable[str]) -> frozenset[str]:
    """Validate explicitly configured public HTTPS origins."""
    normalized: set[str] = set()
    for value in origins:
        parsed = urlparse(str(value).strip())
        try:
            port = parsed.port
        except ValueError as exc:
            raise ValueError("invalid media origin") from exc
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.path not in {"", "/"}
            or parsed.params
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("invalid media origin")
        host = parsed.hostname.lower().rstrip(".")
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
        if host in {"localhost", "ip6-localhost"} or (
            address is not None and not address.is_global
        ):
            raise ValueError("invalid media origin")
        normalized.add(f"https://{host}" + (f":{port}" if port not in {None, 443} else ""))
    return frozenset(normalized)


class MediaStager:
    """Download one allowlisted public artifact into a confined managed directory."""

    def __init__(
        self,
        root: Path,
        *,
        allowed_origins: Iterable[str],
        max_bytes: int,
        ttl_seconds: int,
        timeout_seconds: float,
        resolver: Callable[..., list[tuple[Any, ...]]] | None = None,
        transport: httpx.BaseTransport | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        raw_root = root.expanduser()
        if raw_root.is_symlink():
            raise MediaStagingError("MEDIA_STAGING_FAILED")
        raw_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        raw_root.chmod(0o700)
        self.root = raw_root.resolve(strict=True)
        self.allowed_origins = normalized_media_origins(allowed_origins)
        if not self.allowed_origins or max_bytes < 1 or ttl_seconds < 1 or timeout_seconds <= 0:
            raise MediaStagingError("MEDIA_STAGING_FAILED")
        self.max_bytes = int(max_bytes)
        self.ttl_seconds = int(ttl_seconds)
        self.timeout_seconds = float(timeout_seconds)
        self.resolver = resolver or socket.getaddrinfo
        self.transport = transport
        self.clock = clock or time.time
        self.monotonic = time.monotonic

    def stage(
        self,
        effect_id: str,
        request_hash: str,
        part: Mapping[str, Any],
    ) -> dict[str, object]:
        """Return deterministic metadata for one validated staged artifact."""
        if not _EFFECT_ID.fullmatch(effect_id) or not _REQUEST_HASH.fullmatch(request_hash):
            raise MediaStagingError("MEDIA_STAGING_FAILED")
        deadline = self.monotonic() + self.timeout_seconds
        mime_type = str(part.get("mime_type") or "").lower()
        try:
            expected_type, suffix = _FORMATS[mime_type]
        except KeyError as exc:
            raise MediaStagingError("MEDIA_STAGING_FAILED") from exc
        if part.get("type") != expected_type:
            raise MediaStagingError("MEDIA_STAGING_FAILED")
        filename = part.get("filename")
        if filename is not None and (
            not isinstance(filename, str)
            or not filename
            or Path(filename).name != filename
            or "\\" in filename
        ):
            raise MediaStagingError("MEDIA_STAGING_FAILED")
        uri = part.get("uri")
        if not isinstance(uri, str):
            raise MediaStagingError("MEDIA_STAGING_FAILED")
        host, port = self._validate_url(uri)
        addresses = self._validate_public_dns(host, port, deadline)

        path = self.root / f"{effect_id}{suffix}"
        sidecar = self.root / f"{effect_id}.media.json"
        if path.is_symlink() or sidecar.is_symlink():
            raise MediaStagingError("MEDIA_STAGING_FAILED")
        existing = self._load(sidecar)
        if existing is not None:
            if existing.pop("request_hash", None) != request_hash:
                raise MediaStagingError("MEDIA_STAGING_FAILED")
            if self._matches(existing, path, mime_type):
                return existing

        temp = self.root / f".{effect_id}-{uuid.uuid4().hex}.tmp"
        try:
            metadata = self._download(
                uri, temp, path, mime_type, host, port, addresses, deadline
            )
            self._save(sidecar, {"request_hash": request_hash, **metadata})
            return metadata
        except MediaStagingError:
            temp.unlink(missing_ok=True)
            raise
        except (OSError, ValueError, TypeError) as exc:
            temp.unlink(missing_ok=True)
            raise MediaStagingError("MEDIA_STAGING_FAILED") from exc

    def _validate_url(self, uri: str) -> tuple[str, int]:
        parsed = urlparse(uri)
        try:
            port = parsed.port or 443
        except ValueError as exc:
            raise MediaStagingError("MEDIA_STAGING_FAILED") from exc
        path = unquote(parsed.path)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.fragment
            or "\\" in path
            or ".." in path.split("/")
        ):
            raise MediaStagingError("MEDIA_STAGING_FAILED")
        host = parsed.hostname.lower().rstrip(".")
        origin = f"https://{host}" + (f":{port}" if port != 443 else "")
        if origin not in self.allowed_origins:
            raise MediaStagingError("MEDIA_STAGING_FAILED")
        return host, port

    def _validate_public_dns(
        self, host: str, port: int, deadline: float
    ) -> tuple[str, ...]:
        result: queue.Queue[object] = queue.Queue(maxsize=1)

        def resolve() -> None:
            try:
                result.put(self.resolver(host, port, type=socket.SOCK_STREAM))
            except Exception as exc:  # noqa: BLE001 - sanitized at the trust boundary
                result.put(exc)

        remaining = deadline - self.monotonic()
        if remaining <= 0:
            raise MediaStagingError("MEDIA_STAGING_FAILED")
        threading.Thread(target=resolve, daemon=True).start()
        try:
            answers = result.get(timeout=remaining)
            if isinstance(answers, Exception):
                raise answers
            addresses = {ipaddress.ip_address(str(answer[4][0])) for answer in answers}
        except (OSError, TypeError, ValueError, IndexError, queue.Empty) as exc:
            raise MediaStagingError("MEDIA_STAGING_FAILED") from exc
        if (
            self.monotonic() >= deadline
            or not addresses
            or any(not address.is_global for address in addresses)
        ):
            raise MediaStagingError("MEDIA_STAGING_FAILED")
        return tuple(sorted(str(address) for address in addresses))

    def _download(
        self,
        uri: str,
        temp: Path,
        path: Path,
        mime_type: str,
        host: str,
        port: int,
        addresses: tuple[str, ...],
        deadline: float,
    ) -> dict[str, object]:
        descriptor = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        size = 0
        digest = hashlib.sha256()
        try:
            transport = self.transport or _PinnedTransport(
                _PinnedBackend(host, port, addresses, deadline, self.monotonic)
            )
            with (
                os.fdopen(descriptor, "wb") as output,
                httpx.Client(
                    timeout=max(deadline - self.monotonic(), 0.001),
                    follow_redirects=False,
                    transport=transport,
                    trust_env=False,
                ) as client,
                client.stream("GET", uri) as response,
            ):
                if self.monotonic() >= deadline:
                    raise MediaStagingError("MEDIA_STAGING_FAILED")
                if response.is_redirect or response.status_code != 200:
                    raise MediaStagingError("MEDIA_STAGING_FAILED")
                self._validate_connected_peer(response, addresses)
                declared = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
                if declared != mime_type:
                    raise MediaStagingError("MEDIA_STAGING_FAILED")
                raw_length = response.headers.get("Content-Length")
                if raw_length is not None:
                    length = int(raw_length)
                    if length < 1 or length > self.max_bytes:
                        raise MediaStagingError("MEDIA_STAGING_FAILED")
                for chunk in response.iter_bytes():
                    if self.monotonic() >= deadline:
                        raise MediaStagingError("MEDIA_STAGING_FAILED")
                    size += len(chunk)
                    if size > self.max_bytes:
                        raise MediaStagingError("MEDIA_STAGING_FAILED")
                    output.write(chunk)
                    digest.update(chunk)
                if size < 1:
                    raise MediaStagingError("MEDIA_STAGING_FAILED")
                if self.monotonic() >= deadline:
                    raise MediaStagingError("MEDIA_STAGING_FAILED")
                output.flush()
                os.fsync(output.fileno())
        except (httpx.HTTPError, OSError, ValueError) as exc:
            temp.unlink(missing_ok=True)
            raise MediaStagingError("MEDIA_STAGING_FAILED") from exc

        try:
            body = temp.read_bytes()
            if self._detected_mime(body) != mime_type:
                raise MediaStagingError("MEDIA_STAGING_FAILED")
            if path.is_symlink():
                raise MediaStagingError("MEDIA_STAGING_FAILED")
            temp.replace(path)
            path.chmod(0o600)
        except (OSError, ValueError) as exc:
            temp.unlink(missing_ok=True)
            raise MediaStagingError("MEDIA_STAGING_FAILED") from exc
        return {
            "path": str(path),
            "mime_type": mime_type,
            "filename": path.name,
            "sha256": digest.hexdigest(),
            "size_bytes": size,
            "expires_at": self.clock() + self.ttl_seconds,
        }

    def _validate_connected_peer(
        self, response: httpx.Response, addresses: tuple[str, ...]
    ) -> None:
        stream = response.extensions.get("network_stream")
        if stream is None:
            if self.transport is None:
                raise MediaStagingError("MEDIA_STAGING_FAILED")
            return
        try:
            peer = stream.get_extra_info("server_addr")
            address = ipaddress.ip_address(str(peer[0]))
        except (AttributeError, IndexError, TypeError, ValueError) as exc:
            raise MediaStagingError("MEDIA_STAGING_FAILED") from exc
        if not address.is_global or str(address) not in addresses:
            raise MediaStagingError("MEDIA_STAGING_FAILED")

    @staticmethod
    def _detected_mime(body: bytes) -> str | None:
        if body.startswith(b"\x89PNG\r\n\x1a\n"):
            return "image/png"
        if body.startswith(b"\xff\xd8\xff") and body.endswith(b"\xff\xd9"):
            return "image/jpeg"
        if body.startswith((b"GIF87a", b"GIF89a")):
            return "image/gif"
        if len(body) >= 12 and body.startswith(b"RIFF") and body[8:12] == b"WEBP":
            return "image/webp"
        if body.startswith(b"%PDF-") and b"%%EOF" in body[-1_024:]:
            return "application/pdf"
        try:
            body.decode("utf-8")
        except UnicodeDecodeError:
            return None
        return "text/plain" if b"\x00" not in body else None

    def _load(self, sidecar: Path) -> dict[str, Any] | None:
        try:
            if sidecar.stat().st_size > _SIDECAR_MAX_BYTES:
                return None
            value = json.loads(sidecar.read_text())
        except (OSError, json.JSONDecodeError):
            return None
        required = {
            "request_hash",
            "path",
            "mime_type",
            "filename",
            "sha256",
            "size_bytes",
            "expires_at",
        }
        return value if isinstance(value, dict) and set(value) == required else None

    def _matches(
        self, metadata: Mapping[str, Any], path: Path, mime_type: str
    ) -> bool:
        try:
            return (
                Path(str(metadata["path"])) == path
                and str(metadata["filename"]) == path.name
                and metadata["mime_type"] == mime_type
                and not path.is_symlink()
                and path.is_file()
                and float(metadata["expires_at"]) > self.clock()
                and path.stat().st_size == int(metadata["size_bytes"])
                and hashlib.sha256(path.read_bytes()).hexdigest() == metadata["sha256"]
            )
        except (KeyError, OSError, TypeError, ValueError):
            return False

    def _save(self, sidecar: Path, value: Mapping[str, object]) -> None:
        temp = self.root / f".{sidecar.name}-{uuid.uuid4().hex}.tmp"
        try:
            descriptor = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                json.dump(value, output, sort_keys=True, separators=(",", ":"))
                output.flush()
                os.fsync(output.fileno())
            if sidecar.is_symlink():
                raise MediaStagingError("MEDIA_STAGING_FAILED")
            temp.replace(sidecar)
            sidecar.chmod(0o600)
        except OSError as exc:
            temp.unlink(missing_ok=True)
            raise MediaStagingError("MEDIA_STAGING_FAILED") from exc


__all__ = ["MediaStager", "MediaStagingError", "normalized_media_origins"]
