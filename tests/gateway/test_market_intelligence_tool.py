from __future__ import annotations

import json
from typing import Any

import pytest
from yeoman_gateway.agent.tools.market_data import MarketIntelligenceTool


class _FakeResponse:
    def __init__(self, payload: Any, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code
        self.headers = {"content-type": "application/json"}

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self) -> Any:
        return self._payload


class _RoutingAsyncClient:
    calls: list[dict[str, Any]] = []

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    async def __aenter__(self) -> "_RoutingAsyncClient":
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def get(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        timeout: float,
    ) -> _FakeResponse:
        self.calls.append({"url": url, "params": params or {}, "headers": headers or {}, "timeout": timeout})
        if "twelvedata.com/quote" in url:
            return _FakeResponse({"code": 429, "message": "Too Many Requests", "status": "error"})
        if "alpaca.markets" in url:
            return _FakeResponse(
                {
                    "symbol": "AMD",
                    "latestTrade": {"p": 181.4, "t": "2026-06-13T14:01:02Z"},
                    "dailyBar": {"o": 178.0, "h": 183.0, "l": 176.5, "c": 181.4, "v": 12345},
                    "prevDailyBar": {"c": 175.0},
                }
            )
        if "finnhub.io/api/v1/company-news" in url:
            return _FakeResponse(
                [
                    {
                        "headline": "AMD rises as chip stocks rebound",
                        "source": "Reuters",
                        "datetime": 1781359200,
                        "url": "https://example.test/amd",
                        "summary": "Chip stocks rebounded after fresh AI server demand commentary.",
                    }
                ]
            )
        if "gdeltproject.org" in url:
            return _FakeResponse({"articles": [{"title": "Oil markets watch sanctions risk", "url": "https://example.test/oil"}]})
        raise AssertionError(f"unexpected URL {url}")

    async def post(
        self,
        url: str,
        *,
        json: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        timeout: float,
    ) -> _FakeResponse:
        self.calls.append({"url": url, "json": json or {}, "headers": headers or {}, "timeout": timeout})
        if "api.tavily.com/search" in url:
            return _FakeResponse(
                {
                    "results": [
                        {
                            "title": "AMD shares move after new AI chip demand report",
                            "url": "https://example.test/tavily-amd",
                            "content": "AMD moved with other AI chip names after a fresh demand report.",
                            "score": 0.91,
                            "published_date": "2026-06-13",
                        }
                    ]
                }
            )
        raise AssertionError(f"unexpected URL {url}")


class _SuccessfulTwelveDataClient:
    calls: list[dict[str, Any]] = []

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    async def __aenter__(self) -> "_SuccessfulTwelveDataClient":
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def get(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        timeout: float,
    ) -> _FakeResponse:
        self.calls.append({"url": url, "params": params or {}, "headers": headers or {}, "timeout": timeout})
        if "twelvedata.com/quote" in url:
            return _FakeResponse(
                {
                    "AMD": {
                        "symbol": "AMD",
                        "close": "181.40",
                        "previous_close": "175.00",
                        "percent_change": "3.6571",
                        "datetime": "2026-06-13",
                    }
                }
            )
        raise AssertionError(f"unexpected URL {url}")


class _VerboseMarketContextClient:
    calls: list[dict[str, Any]] = []

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    async def __aenter__(self) -> "_VerboseMarketContextClient":
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def get(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        timeout: float,
    ) -> _FakeResponse:
        self.calls.append({"url": url, "params": params or {}, "headers": headers or {}, "timeout": timeout})
        if "twelvedata.com/quote" in url:
            return _FakeResponse(
                {
                    "AMD": {
                        "symbol": "AMD",
                        "name": "Advanced Micro Devices, Inc.",
                        "exchange": "NASDAQ",
                        "mic_code": "XNGS",
                        "currency": "USD",
                        "datetime": "2026-06-13",
                        "timestamp": 1781359200,
                        "open": "499.69",
                        "high": "521.71",
                        "low": "494.00",
                        "close": "511.57",
                        "previous_close": "488.45",
                        "change": "23.12",
                        "percent_change": "4.73334",
                        "volume": "31553100",
                        "is_market_open": False,
                    }
                }
            )
        if "finnhub.io/api/v1/company-news" in url:
            return _FakeResponse(
                [
                    {
                        "headline": f"AMD long catalyst headline {index} about AI GPUs, Meta demand, and Citi upgrade",
                        "source": "Reuters",
                        "datetime": 1781359200 + index,
                        "url": f"https://example.test/finnhub/{index}",
                        "summary": "Very long summary " * 60,
                    }
                    for index in range(8)
                ]
            )
        if "gdeltproject.org" in url:
            return _FakeResponse(
                {
                    "articles": [
                        {
                            "title": f"Long macro geopolitical context headline {index} about tariffs and chip supply chains",
                            "url": f"https://example.test/gdelt/{index}",
                            "domain": "example.test",
                            "seendate": "20260613T080000Z",
                        }
                        for index in range(6)
                    ]
                }
            )
        raise AssertionError(f"unexpected URL {url}")

    async def post(
        self,
        url: str,
        *,
        json: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        timeout: float,
    ) -> _FakeResponse:
        self.calls.append({"url": url, "json": json or {}, "headers": headers or {}, "timeout": timeout})
        if "api.tavily.com/search" in url:
            return _FakeResponse(
                {
                    "results": [
                        {
                            "title": f"Tavily catalyst headline {index} about AMD and chip demand",
                            "url": f"https://example.test/tavily/{index}",
                            "content": "Verbose Tavily content " * 50,
                            "score": 0.9 - index / 100,
                            "published_date": "2026-06-13",
                        }
                        for index in range(8)
                    ]
                }
            )
        raise AssertionError(f"unexpected URL {url}")


@pytest.mark.asyncio
async def test_market_intelligence_falls_back_to_alpaca_and_fetches_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _RoutingAsyncClient.calls = []
    monkeypatch.setattr(
        "yeoman_gateway.agent.tools.market_data.httpx.AsyncClient",
        _RoutingAsyncClient,
    )
    tool = MarketIntelligenceTool(
        twelve_data_api_key="td-key",
        alpaca_api_key_id="alpaca-key",
        alpaca_api_secret_key="alpaca-secret",
        alpaca_data_feed="iex",
        finnhub_api_key="fh-key",
        tavily_api_key="tv-key",
    )

    result = json.loads(
        await tool.execute(
            query="why is AMD moving right now?",
            symbols=["AMD"],
            include_macro=True,
        )
    )

    assert result["ok"] is True
    assert result["quotes"][0]["symbol"] == "AMD"
    assert result["quotes"][0]["source"] == "alpaca_iex"
    assert result["quotes"][0]["price"] == 181.4
    assert result["quotes"][0]["pct"] == pytest.approx(3.6571, rel=0.001)
    assert result["news"][0]["source"] == "Reuters"
    assert "chip stocks rebound" in result["news"][0]["headline"]
    assert result["news"][1]["source"] == "tavily"
    assert "AI chip demand" in result["news"][1]["headline"]
    assert result["macro_context"][0]["source"] == "gdelt"
    assert "Do not use web/news snippets as price values" in result["guidance"]


@pytest.mark.asyncio
async def test_market_intelligence_reuses_cached_quotes(monkeypatch: pytest.MonkeyPatch) -> None:
    _SuccessfulTwelveDataClient.calls = []
    monkeypatch.setattr(
        "yeoman_gateway.agent.tools.market_data.httpx.AsyncClient",
        _SuccessfulTwelveDataClient,
    )
    tool = MarketIntelligenceTool(twelve_data_api_key="td-key")

    first = json.loads(await tool.execute(query="why AMD moving?", symbols=["AMD"]))
    second = json.loads(await tool.execute(query="why AMD moving again?", symbols=["AMD"]))

    assert first["quotes"][0]["source"] == "twelvedata"
    assert second["quotes"][0]["source"] == "twelvedata"
    twelve_calls = [call for call in _SuccessfulTwelveDataClient.calls if "twelvedata.com/quote" in call["url"]]
    assert len(twelve_calls) == 1


@pytest.mark.asyncio
async def test_market_intelligence_skips_twelve_when_batch_exceeds_local_minute_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _SuccessfulTwelveDataClient.calls = []
    monkeypatch.setattr(
        "yeoman_gateway.agent.tools.market_data.httpx.AsyncClient",
        _SuccessfulTwelveDataClient,
    )
    tool = MarketIntelligenceTool(twelve_data_api_key="td-key")
    symbols = ["AMD", "NVDA", "MSFT", "AAPL", "META", "GOOG", "AMZN", "TSLA", "AVGO"]

    result = json.loads(await tool.execute(query="why are mega cap tech stocks moving?", symbols=symbols))

    twelve_calls = [call for call in _SuccessfulTwelveDataClient.calls if "twelvedata.com/quote" in call["url"]]
    assert twelve_calls == []
    assert result["quote_errors"][0]["error"] == "local_rate_limited"
    assert result["quote_errors"][0]["provider"] == "twelvedata"


@pytest.mark.asyncio
async def test_market_intelligence_result_stays_complete_under_trace_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _VerboseMarketContextClient.calls = []
    monkeypatch.setattr(
        "yeoman_gateway.agent.tools.market_data.httpx.AsyncClient",
        _VerboseMarketContextClient,
    )
    tool = MarketIntelligenceTool(
        twelve_data_api_key="td-key",
        finnhub_api_key="fh-key",
        tavily_api_key="tv-key",
    )

    raw = await tool.execute(query="Was war diese Woche mit AMD los?", symbols=["AMD"], include_macro=True)
    result = json.loads(raw)

    assert len(raw) < 1800
    assert result["ok"] is True
    assert result["quotes"][0]["symbol"] == "AMD"
    assert result["news"]
    assert result["macro_context"]
    assert result["guidance"]
