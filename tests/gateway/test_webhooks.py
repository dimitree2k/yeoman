"""Tests for webhook HMAC verification and normalization."""

import hashlib
import hmac

from yeoman_gateway.api.webhooks import normalize_webhook, verify_hmac_signature


def test_hmac_valid_signature() -> None:
    secret = "test-secret-123"
    body = b'{"action": "opened"}'
    sig = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    assert verify_hmac_signature(body, sig, secret) is True


def test_hmac_invalid_signature() -> None:
    assert verify_hmac_signature(b"body", "sha256=bad", "secret") is False


def test_hmac_missing_prefix() -> None:
    assert verify_hmac_signature(b"body", "nope", "secret") is False


def test_normalize_github_push() -> None:
    payload = {
        "repository": {"full_name": "user/repo"},
        "ref": "refs/heads/main",
        "commits": [{"id": "abc"}, {"id": "def"}],
    }
    result = normalize_webhook("github", "push", payload)
    assert "[GitHub]" in result
    assert "user/repo" in result
    assert "2 commit(s)" in result
    assert "main" in result


def test_normalize_github_pull_request() -> None:
    payload = {
        "repository": {"full_name": "user/repo"},
        "action": "opened",
        "pull_request": {"number": 42, "title": "Fix bug"},
    }
    result = normalize_webhook("github", "pull_request.opened", payload)
    assert "PR #42" in result
    assert "opened" in result


def test_normalize_unknown_source_truncates() -> None:
    payload = {"data": "x" * 2000}
    result = normalize_webhook("custom", "event", payload)
    assert "[Webhook: custom]" in result
    assert "...[truncated]" in result
    assert len(result) < 1200
