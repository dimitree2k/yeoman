from __future__ import annotations

from typing import Any

import pytest
from yeoman_gateway.agent.tools.market_data import MarketQuoteTool


class _FakeResponse:
    def __init__(self, payload: dict[str, Any], status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self) -> dict[str, Any]:
        return self._payload


class _RecordingAsyncClient:
    calls: list[dict[str, Any]] = []
    response_payload: dict[str, Any] = {}

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.init_args = args
        self.init_kwargs = kwargs

    async def __aenter__(self) -> "_RecordingAsyncClient":
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def get(
        self,
        url: str,
        *,
        params: dict[str, Any],
        timeout: float,
    ) -> _FakeResponse:
        self.calls.append({"url": url, "params": params, "timeout": timeout})
        return _FakeResponse(self.response_payload)


@pytest.mark.asyncio
async def test_market_quote_requires_api_key(monkeypatch) -> None:
    monkeypatch.delenv("TWELVE_DATA_API_KEY", raising=False)
    tool = MarketQuoteTool(api_key=None)

    result = await tool.execute(symbols=["NVDA"])

    assert "not_configured" in result
    assert "TWELVE_DATA_API_KEY" in result


@pytest.mark.asyncio
async def test_market_quote_fetches_and_normalizes_single_quote(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _RecordingAsyncClient.calls = []
    _RecordingAsyncClient.response_payload = {
        "symbol": "NVDA",
        "name": "NVIDIA Corporation",
        "exchange": "NASDAQ",
        "mic_code": "XNAS",
        "currency": "USD",
        "datetime": "2026-06-09",
        "timestamp": 1781023440,
        "open": "210.62",
        "high": "212.00",
        "low": "198.20",
        "close": "200.80",
        "previous_close": "205.10",
        "change": "-4.30",
        "percent_change": "-2.096538",
        "volume": "123456789",
        "is_market_open": True,
    }
    monkeypatch.setattr(
        "yeoman_gateway.agent.tools.market_data.httpx.AsyncClient",
        _RecordingAsyncClient,
    )
    tool = MarketQuoteTool(api_key="td-key")

    result = await tool.execute(symbols=[" nvda "])

    assert "NVDA" in result
    assert '"price": 200.8' in result
    assert '"percent_change": -2.096538' in result
    assert '"units": {"price": "USD"' in result
    assert '"percent_change": "%"' in result
    assert '"source": "twelvedata"' in result
    assert _RecordingAsyncClient.calls == [
        {
            "url": "https://api.twelvedata.com/quote",
            "params": {"symbol": "NVDA", "apikey": "td-key"},
            "timeout": 10.0,
        }
    ]


@pytest.mark.asyncio
async def test_market_quote_fetches_batch_quotes(monkeypatch: pytest.MonkeyPatch) -> None:
    _RecordingAsyncClient.calls = []
    _RecordingAsyncClient.response_payload = {
        "NVDA": {
            "symbol": "NVDA",
            "close": "200.80",
            "previous_close": "205.10",
            "percent_change": "-2.09",
            "datetime": "2026-06-09",
        },
        "AMD": {
            "symbol": "AMD",
            "close": "478.05",
            "previous_close": "490.33",
            "percent_change": "-2.50",
            "datetime": "2026-06-09",
        },
    }
    monkeypatch.setattr(
        "yeoman_gateway.agent.tools.market_data.httpx.AsyncClient",
        _RecordingAsyncClient,
    )
    tool = MarketQuoteTool(api_key="td-key")

    result = await tool.execute(symbols=["NVDA", "AMD", "NVDA"])

    assert '"symbol": "NVDA"' in result
    assert '"symbol": "AMD"' in result
    assert '"percent_change": -2.5' in result
    assert _RecordingAsyncClient.calls[0]["params"]["symbol"] == "NVDA,AMD"


@pytest.mark.asyncio
async def test_market_quote_keeps_partial_quotes_when_one_symbol_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _RecordingAsyncClient.calls = []
    _RecordingAsyncClient.response_payload = {
        "AMD": {
            "symbol": "AMD",
            "close": "180.25",
            "previous_close": "175.00",
            "percent_change": "3.0",
            "datetime": "2026-06-13",
        },
        "BAD": {
            "code": 400,
            "message": "symbol not found",
            "status": "error",
        },
    }
    monkeypatch.setattr(
        "yeoman_gateway.agent.tools.market_data.httpx.AsyncClient",
        _RecordingAsyncClient,
    )
    tool = MarketQuoteTool(api_key="td-key")

    result = await tool.execute(symbols=["AMD", "BAD"])

    assert '"ok": true' in result
    assert '"partial": true' in result
    assert '"symbol": "AMD"' in result
    assert '"price": 180.25' in result
    assert '"symbol": "BAD"' in result
    assert "symbol not found" in result


class _FailingAsyncClient:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    async def __aenter__(self) -> "_FailingAsyncClient":
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def get(
        self,
        url: str,
        *,
        params: dict[str, Any],
        timeout: float,
    ) -> _FakeResponse:
        raise RuntimeError(
            "Client error '429 Too Many Requests' for url "
            "'https://api.twelvedata.com/quote?symbol=AMD&apikey=secret-key'"
        )


@pytest.mark.asyncio
async def test_market_quote_redacts_api_key_from_provider_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "yeoman_gateway.agent.tools.market_data.httpx.AsyncClient",
        _FailingAsyncClient,
    )
    tool = MarketQuoteTool(api_key="secret-key")

    result = await tool.execute(symbols=["AMD"])

    assert "secret-key" not in result
    assert "apikey=REDACTED" in result
