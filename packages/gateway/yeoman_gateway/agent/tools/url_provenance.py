"""Canonical URL and redirect provenance rules for web tools."""

from __future__ import annotations

import ipaddress
from urllib.parse import SplitResult, urlsplit, urlunsplit

_DEFAULT_PORTS = {"http": 80, "https": 443}
_PROSE_CLOSERS = ".,。 ，、！？；：”’»"
_CLOSING_PAIRS = {")": "(", "]": "[", "}": "{"}


def _canonical_host(host: str, *, strip_www: bool) -> str | None:
    candidate = host.rstrip(".").lower()
    if not candidate:
        return None
    try:
        canonical = ipaddress.ip_address(candidate).compressed.lower()
    except ValueError:
        try:
            canonical = candidate.encode("idna").decode("ascii").lower()
        except UnicodeError:
            return None
    if strip_www and canonical.startswith("www."):
        canonical = canonical[4:]
    return canonical or None


def _parse_http_url(raw_url: str) -> tuple[SplitResult, str] | None:
    try:
        parsed = urlsplit(raw_url)
        scheme = parsed.scheme.lower()
        if (
            scheme not in _DEFAULT_PORTS
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
        ):
            return None
        host = _canonical_host(parsed.hostname, strip_www=False)
        if host is None:
            return None
        parsed.port
    except ValueError:
        return None
    return parsed, host


def trim_explicit_url_token(raw_token: str) -> str:
    """Remove unambiguous prose/markdown wrappers, not valid URL punctuation."""
    candidate = raw_token.strip("`")
    while candidate and candidate[-1] in _PROSE_CLOSERS:
        candidate = candidate[:-1]
    while candidate and candidate[-1] in _CLOSING_PAIRS:
        closing = candidate[-1]
        opening = _CLOSING_PAIRS[closing]
        if candidate.count(closing) <= candidate.count(opening):
            break
        candidate = candidate[:-1]
    try:
        parsed = urlsplit(candidate)
    except ValueError:
        parsed = None
    if (
        parsed is not None
        and parsed.query
        and set(parsed.query) <= {"!", "?"}
    ):
        candidate = candidate[: candidate.index("?")]
    return candidate.rstrip("`")


def normalize_http_url(raw_url: str, *, trim_token: bool = False) -> str | None:
    candidate = (
        trim_explicit_url_token(raw_url)
        if trim_token
        else raw_url
    )
    parsed_result = _parse_http_url(candidate)
    if parsed_result is None:
        return None
    parsed, host = parsed_result
    canonical_host = _canonical_host(host, strip_www=True)
    if canonical_host is None:
        return None
    if ":" in canonical_host:
        netloc = f"[{canonical_host}]"
    else:
        netloc = canonical_host
    port = parsed.port
    if port is not None and port != _DEFAULT_PORTS[parsed.scheme.lower()]:
        netloc += f":{port}"
    return urlunsplit(
        (
            parsed.scheme.lower(),
            netloc,
            parsed.path,
            parsed.query,
            "",
        )
    )


def provenance_transition_error(
    requested_url: str,
    final_url: str,
) -> str | None:
    """Return a stable error code unless final URL is a safe same-host transition."""
    requested_result = _parse_http_url(requested_url)
    final_result = _parse_http_url(final_url)
    if requested_result is None or final_result is None:
        return "invalid_provenance_url"
    requested, requested_host = requested_result
    final, final_host = final_result
    if (
        _canonical_host(requested_host, strip_www=True)
        != _canonical_host(final_host, strip_www=True)
    ):
        return "cross_host_redirect"
    requested_scheme = requested.scheme.lower()
    final_scheme = final.scheme.lower()
    if (
        requested.port is not None
        and requested.port != _DEFAULT_PORTS[requested_scheme]
    ) or (
        final.port is not None
        and final.port != _DEFAULT_PORTS[final_scheme]
    ):
        return "unsafe_port_redirect"
    requested_port = requested.port or _DEFAULT_PORTS[requested_scheme]
    final_port = final.port or _DEFAULT_PORTS[final_scheme]
    if requested_scheme == "https" and final_scheme == "http":
        return "scheme_downgrade"
    if requested_scheme == final_scheme:
        return (
            None
            if requested_port == final_port
            else "unsafe_port_redirect"
        )
    if requested_scheme == "http" and final_scheme == "https":
        return (
            None
            if requested_port == 80 and final_port == 443
            else "unsafe_port_redirect"
        )
    return "invalid_provenance_url"
