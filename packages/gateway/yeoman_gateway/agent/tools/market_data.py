"""Structured market-data tools."""

from __future__ import annotations

import json
import os
import re
import time
from datetime import date, timedelta
from typing import Any

import httpx

from yeoman_gateway.agent.tools.base import Tool

_TWELVE_DATA_QUOTE_URL = "https://api.twelvedata.com/quote"
_ALPACA_STOCK_SNAPSHOT_URL = "https://data.alpaca.markets/v2/stocks/{symbol}/snapshot"
_FINNHUB_COMPANY_NEWS_URL = "https://finnhub.io/api/v1/company-news"
_GDELT_DOC_URL = "https://api.gdeltproject.org/api/v2/doc/doc"
_TAVILY_SEARCH_URL = "https://api.tavily.com/search"
_DEFAULT_TIMEOUT_SECONDS = 10.0
_MAX_SYMBOLS = 20
_TWELVE_DATA_MAX_CREDITS_PER_MINUTE = 8
_QUOTE_CACHE_TTL_SECONDS = 75.0
_SECRET_PARAM_RE = re.compile(r"(?i)(apikey|token|api_key|access_key)=([^&'\"\s]+)")
_SYMBOL_RE = re.compile(r"\b[A-Z]{1,5}\b")
_IGNORE_SYMBOL_WORDS = {
    "A",
    "AN",
    "API",
    "CEO",
    "CFO",
    "DAX",
    "ETF",
    "EU",
    "GDP",
    "IPO",
    "IT",
    "US",
    "USA",
    "USD",
    "WHY",
}


def _redact_secrets(text: str) -> str:
    return _SECRET_PARAM_RE.sub(lambda match: f"{match.group(1)}=REDACTED", text)


def _as_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _clean_symbols(raw_symbols: Any) -> list[str]:
    if isinstance(raw_symbols, str):
        parts = raw_symbols.split(",")
    elif isinstance(raw_symbols, list):
        parts = [str(symbol) for symbol in raw_symbols]
    else:
        parts = []

    symbols: list[str] = []
    seen: set[str] = set()
    for part in parts:
        symbol = part.strip().upper()
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        symbols.append(symbol)
        if len(symbols) >= _MAX_SYMBOLS:
            break
    return symbols


def _extract_symbols_from_query(query: str) -> list[str]:
    symbols: list[str] = []
    seen: set[str] = set()
    for match in _SYMBOL_RE.findall(query.upper()):
        if match in _IGNORE_SYMBOL_WORDS or match in seen:
            continue
        seen.add(match)
        symbols.append(match)
        if len(symbols) >= _MAX_SYMBOLS:
            break
    return symbols


def _normalize_quote(raw: dict[str, Any]) -> dict[str, Any]:
    return {
        "symbol": str(raw.get("symbol") or "").upper() or None,
        "name": raw.get("name"),
        "exchange": raw.get("exchange"),
        "mic_code": raw.get("mic_code"),
        "currency": raw.get("currency"),
        "datetime": raw.get("datetime"),
        "timestamp": _as_int(raw.get("timestamp")),
        "price": _as_float(raw.get("close")),
        "open": _as_float(raw.get("open")),
        "high": _as_float(raw.get("high")),
        "low": _as_float(raw.get("low")),
        "previous_close": _as_float(raw.get("previous_close")),
        "change": _as_float(raw.get("change")),
        "percent_change": _as_float(raw.get("percent_change")),
        "volume": _as_int(raw.get("volume")),
        "is_market_open": raw.get("is_market_open"),
        "source": "twelvedata",
    }


class MarketQuoteTool(Tool):
    """Fetch structured market quotes from Twelve Data."""

    name = "market_quote"
    description = (
        "Fetch structured current market quotes for stocks, ETFs, indices, forex, or crypto "
        "symbols via Twelve Data. Use this before answering market price, intraday move, "
        "or 'what is bleeding today' questions. Returns price, percent change, previous "
        "close, market-open status, timestamp, and source metadata."
    )
    parameters = {
        "type": "object",
        "properties": {
            "symbols": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1,
                "maxItems": _MAX_SYMBOLS,
                "description": "Ticker symbols or instrument symbols, e.g. NVDA, AMD, QQQ, EUR/USD, BTC/USD.",
            },
        },
        "required": ["symbols"],
    }

    def __init__(self, api_key: str | None = None, timeout: float = _DEFAULT_TIMEOUT_SECONDS) -> None:
        self.api_key = (api_key or os.getenv("TWELVE_DATA_API_KEY") or "").strip()
        self.timeout = timeout

    async def execute(self, **kwargs: Any) -> str:
        symbols = _clean_symbols(kwargs.get("symbols"))
        if not symbols:
            return json.dumps(
                {
                    "ok": False,
                    "error": "missing_symbols",
                    "message": "Provide at least one market symbol.",
                },
                ensure_ascii=False,
            )

        if not self.api_key:
            return json.dumps(
                {
                    "ok": False,
                    "error": "not_configured",
                    "message": "Set TWELVE_DATA_API_KEY before using market_quote.",
                    "symbols": symbols,
                    "source": "twelvedata",
                },
                ensure_ascii=False,
            )

        params = {"symbol": ",".join(symbols), "apikey": self.api_key}
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    _TWELVE_DATA_QUOTE_URL,
                    params=params,
                    timeout=self.timeout,
                )
                response.raise_for_status()
                payload = response.json()
        except Exception as exc:
            return json.dumps(
                {
                    "ok": False,
                    "error": "provider_error",
                    "message": _redact_secrets(str(exc)),
                    "symbols": symbols,
                    "source": "twelvedata",
                },
                ensure_ascii=False,
            )

        quotes: list[dict[str, Any]] = []
        provider_errors: list[dict[str, Any]] = []
        if all(symbol in payload and isinstance(payload.get(symbol), dict) for symbol in symbols):
            raw_quotes = [payload[symbol] for symbol in symbols]
        else:
            raw_quotes = [payload]

        for requested_symbol, raw_quote in zip(symbols, raw_quotes, strict=False):
            if not isinstance(raw_quote, dict):
                continue
            if raw_quote.get("code") or raw_quote.get("status") == "error":
                provider_errors.append(
                    {
                        "symbol": raw_quote.get("symbol") or requested_symbol,
                        "code": raw_quote.get("code"),
                        "message": raw_quote.get("message") or raw_quote.get("status"),
                    }
                )
                continue
            quotes.append(_normalize_quote(raw_quote))

        return json.dumps(
            {
                "ok": bool(quotes),
                "partial": bool(quotes) and bool(provider_errors),
                "source": "twelvedata",
                "requested_symbols": symbols,
                "quotes": quotes,
                "errors": provider_errors,
                "guidance": (
                    "Use these structured quote values for prices and percent moves. "
                    "Use available quotes even when partial is true. If a symbol is missing or rate-limited, say so explicitly. "
                    "Do not infer prices from web_search."
                ),
            },
            ensure_ascii=False,
        )


class MarketIntelligenceTool(Tool):
    """Gather quote, recent news, and macro context for market questions."""

    name = "market_intelligence"
    description = (
        "Answer market-move questions by gathering structured quotes, very recent ticker news, "
        "and optional macro/geopolitical context. Uses quote providers for numbers and news sources "
        "only for catalysts."
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "minLength": 1,
                "description": "The user's market question, e.g. why is AMD moving right now?",
            },
            "symbols": {
                "type": "array",
                "items": {"type": "string"},
                "maxItems": _MAX_SYMBOLS,
                "description": "Known ticker/instrument symbols. If omitted, the tool extracts likely uppercase tickers from query.",
            },
            "include_macro": {
                "type": "boolean",
                "description": "Whether to include broad macro/geopolitical context from GDELT.",
            },
        },
        "required": ["query"],
    }
    _gdelt_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}

    def __init__(
        self,
        *,
        twelve_data_api_key: str | None = None,
        alpaca_api_key_id: str | None = None,
        alpaca_api_secret_key: str | None = None,
        alpaca_data_feed: str | None = None,
        finnhub_api_key: str | None = None,
        tavily_api_key: str | None = None,
        timeout: float = _DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self.twelve_data_api_key = (twelve_data_api_key or os.getenv("TWELVE_DATA_API_KEY") or "").strip()
        self.alpaca_api_key_id = (
            alpaca_api_key_id
            or os.getenv("ALPACA_API_KEY_ID")
            or os.getenv("APCA_API_KEY_ID")
            or ""
        ).strip()
        self.alpaca_api_secret_key = (
            alpaca_api_secret_key
            or os.getenv("ALPACA_API_SECRET_KEY")
            or os.getenv("APCA_API_SECRET_KEY")
            or ""
        ).strip()
        self.alpaca_data_feed = (alpaca_data_feed or os.getenv("ALPACA_DATA_FEED") or "iex").strip()
        self.finnhub_api_key = (finnhub_api_key or os.getenv("FINNHUB_API_KEY") or "").strip()
        self.tavily_api_key = (tavily_api_key or os.getenv("TAVILY_API_KEY") or "").strip()
        self.timeout = timeout
        self._quote_cache: dict[tuple[str, str], tuple[float, dict[str, Any]]] = {}
        self._twelve_credit_timestamps: list[float] = []

    async def execute(self, **kwargs: Any) -> str:
        query = str(kwargs.get("query") or "").strip()
        symbols = _clean_symbols(kwargs.get("symbols")) or _extract_symbols_from_query(query)
        include_macro = bool(kwargs.get("include_macro", False))
        if not query:
            return json.dumps({"ok": False, "error": "missing_query"}, ensure_ascii=False)

        quote_errors: list[dict[str, Any]] = []
        quotes = await self._fetch_twelve_quotes(symbols, quote_errors)
        missing_symbols = [
            symbol
            for symbol in symbols
            if symbol not in {str(quote.get("symbol") or "").upper() for quote in quotes}
        ]
        if missing_symbols:
            quotes.extend(await self._fetch_alpaca_snapshots(missing_symbols, quote_errors))

        news = [
            *await self._fetch_finnhub_news(symbols),
            *await self._fetch_tavily_catalysts(query, symbols),
        ]
        macro_context = await self._fetch_gdelt_macro_context(query) if include_macro else []
        return json.dumps(
            {
                "ok": bool(quotes) or bool(news) or bool(macro_context),
                "query": query,
                "symbols": symbols,
                "quotes": quotes,
                "quote_errors": quote_errors,
                "news": news,
                "macro_context": macro_context,
                "guidance": (
                    "Use quote rows as the only source for price and percent move values. "
                    "Do not use web/news snippets as price values. Use news and macro_context only for catalysts. "
                    "Label Alpaca free-feed quotes as IEX-only when source is alpaca_iex."
                ),
            },
            ensure_ascii=False,
        )

    async def _fetch_twelve_quotes(
        self,
        symbols: list[str],
        errors: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        if not symbols or not self.twelve_data_api_key:
            if symbols:
                errors.append({"provider": "twelvedata", "error": "not_configured", "symbols": symbols})
            return []
        now = time.monotonic()
        cached_quotes: list[dict[str, Any]] = []
        missing_symbols: list[str] = []
        for symbol in symbols:
            cached = self._quote_cache.get(("twelvedata", symbol))
            if cached and cached[0] > now:
                cached_quotes.append(cached[1])
            else:
                missing_symbols.append(symbol)
        if not missing_symbols:
            return cached_quotes

        self._twelve_credit_timestamps = [
            timestamp for timestamp in self._twelve_credit_timestamps if now - timestamp < 60.0
        ]
        available_credits = _TWELVE_DATA_MAX_CREDITS_PER_MINUTE - len(self._twelve_credit_timestamps)
        if len(missing_symbols) > available_credits:
            errors.append(
                {
                    "provider": "twelvedata",
                    "error": "local_rate_limited",
                    "symbols": missing_symbols,
                    "message": (
                        "Skipped Twelve Data request because requested symbols exceed local per-minute credit budget."
                    ),
                }
            )
            return cached_quotes

        self._twelve_credit_timestamps.extend([now] * len(missing_symbols))
        params = {"symbol": ",".join(missing_symbols), "apikey": self.twelve_data_api_key}
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(_TWELVE_DATA_QUOTE_URL, params=params, timeout=self.timeout)
                response.raise_for_status()
                payload = response.json()
        except Exception as exc:
            errors.append(
                {
                    "provider": "twelvedata",
                    "error": "provider_error",
                    "message": _redact_secrets(str(exc)),
                    "symbols": missing_symbols,
                }
            )
            return cached_quotes

        if isinstance(payload, dict) and (payload.get("code") or payload.get("status") == "error"):
            errors.append(
                {
                    "provider": "twelvedata",
                    "error": payload.get("code") or payload.get("status") or "provider_error",
                    "message": payload.get("message") or "provider_error",
                    "symbols": missing_symbols,
                }
            )
            return cached_quotes

        if isinstance(payload, dict) and all(
            symbol in payload and isinstance(payload.get(symbol), dict) for symbol in missing_symbols
        ):
            raw_quotes = [(symbol, payload[symbol]) for symbol in missing_symbols]
        else:
            raw_quotes = [(missing_symbols[0], payload)] if missing_symbols and isinstance(payload, dict) else []

        quotes: list[dict[str, Any]] = [*cached_quotes]
        for symbol, raw_quote in raw_quotes:
            if raw_quote.get("code") or raw_quote.get("status") == "error":
                errors.append(
                    {
                        "provider": "twelvedata",
                        "symbol": raw_quote.get("symbol") or symbol,
                        "error": raw_quote.get("code") or raw_quote.get("status"),
                        "message": raw_quote.get("message") or "provider_error",
                    }
                )
                continue
            normalized = _normalize_quote(raw_quote)
            quotes.append(normalized)
            normalized_symbol = str(normalized.get("symbol") or symbol).upper()
            self._quote_cache[("twelvedata", normalized_symbol)] = (
                now + _QUOTE_CACHE_TTL_SECONDS,
                normalized,
            )
        return quotes

    async def _fetch_alpaca_snapshots(
        self,
        symbols: list[str],
        errors: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        if not symbols or not self.alpaca_api_key_id or not self.alpaca_api_secret_key:
            if symbols:
                errors.append({"provider": "alpaca", "error": "not_configured", "symbols": symbols})
            return []
        headers = {
            "APCA-API-KEY-ID": self.alpaca_api_key_id,
            "APCA-API-SECRET-KEY": self.alpaca_api_secret_key,
        }
        quotes: list[dict[str, Any]] = []
        async with httpx.AsyncClient() as client:
            for symbol in symbols:
                try:
                    response = await client.get(
                        _ALPACA_STOCK_SNAPSHOT_URL.format(symbol=symbol),
                        params={"feed": self.alpaca_data_feed},
                        headers=headers,
                        timeout=self.timeout,
                    )
                    response.raise_for_status()
                    payload = response.json()
                except Exception as exc:
                    errors.append(
                        {
                            "provider": "alpaca",
                            "symbol": symbol,
                            "error": "provider_error",
                            "message": _redact_secrets(str(exc)),
                        }
                    )
                    continue
                quote = self._normalize_alpaca_snapshot(symbol, payload)
                if quote:
                    quotes.append(quote)
        return quotes

    def _normalize_alpaca_snapshot(self, symbol: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        latest_trade = payload.get("latestTrade") or {}
        daily_bar = payload.get("dailyBar") or {}
        previous_bar = payload.get("prevDailyBar") or {}
        price = _as_float(latest_trade.get("p")) or _as_float(daily_bar.get("c"))
        previous_close = _as_float(previous_bar.get("c"))
        if price is None:
            return None
        change = price - previous_close if previous_close else None
        percent_change = (change / previous_close * 100.0) if change is not None and previous_close else None
        return {
            "symbol": symbol.upper(),
            "price": price,
            "open": _as_float(daily_bar.get("o")),
            "high": _as_float(daily_bar.get("h")),
            "low": _as_float(daily_bar.get("l")),
            "previous_close": previous_close,
            "change": change,
            "percent_change": percent_change,
            "volume": _as_int(daily_bar.get("v")),
            "datetime": latest_trade.get("t") or daily_bar.get("t"),
            "source": f"alpaca_{self.alpaca_data_feed}",
            "feed": self.alpaca_data_feed,
        }

    async def _fetch_finnhub_news(self, symbols: list[str]) -> list[dict[str, Any]]:
        if not symbols or not self.finnhub_api_key:
            return []
        today = date.today()
        start = today - timedelta(days=2)
        news: list[dict[str, Any]] = []
        async with httpx.AsyncClient() as client:
            for symbol in symbols[:5]:
                try:
                    response = await client.get(
                        _FINNHUB_COMPANY_NEWS_URL,
                        params={
                            "symbol": symbol,
                            "from": start.isoformat(),
                            "to": today.isoformat(),
                            "token": self.finnhub_api_key,
                        },
                        timeout=self.timeout,
                    )
                    response.raise_for_status()
                    payload = response.json()
                except Exception:
                    continue
                if not isinstance(payload, list):
                    continue
                for item in payload[:3]:
                    if not isinstance(item, dict):
                        continue
                    news.append(
                        {
                            "symbol": symbol,
                            "headline": item.get("headline"),
                            "source": item.get("source"),
                            "datetime": item.get("datetime"),
                            "url": item.get("url"),
                            "summary": item.get("summary"),
                        }
                    )
        return news[:8]

    async def _fetch_tavily_catalysts(self, query: str, symbols: list[str]) -> list[dict[str, Any]]:
        if not self.tavily_api_key:
            return []
        symbol_text = " ".join(symbols[:5])
        search_query = f"{symbol_text} {query} stock market move today catalyst".strip()
        payload = {
            "query": search_query,
            "topic": "news",
            "time_range": "day",
            "search_depth": "basic",
            "max_results": 3,
            "include_answer": False,
        }
        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    _TAVILY_SEARCH_URL,
                    json=payload,
                    headers={
                        "Authorization": f"Bearer {self.tavily_api_key}",
                        "Content-Type": "application/json",
                    },
                    timeout=self.timeout,
                )
                response.raise_for_status()
                data = response.json()
        except Exception:
            return []
        results = data.get("results") if isinstance(data, dict) else []
        if not isinstance(results, list):
            return []
        catalysts: list[dict[str, Any]] = []
        for item in results[:3]:
            if not isinstance(item, dict):
                continue
            catalysts.append(
                {
                    "source": "tavily",
                    "headline": item.get("title"),
                    "url": item.get("url"),
                    "summary": item.get("content"),
                    "published_date": item.get("published_date"),
                    "score": item.get("score"),
                }
            )
        return catalysts

    async def _fetch_gdelt_macro_context(self, query: str) -> list[dict[str, Any]]:
        cache_key = query.strip().lower()[:180]
        cached = self._gdelt_cache.get(cache_key)
        now = time.monotonic()
        if cached and cached[0] > now:
            return cached[1]
        gdelt_query = self._build_gdelt_query(query)
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    _GDELT_DOC_URL,
                    params={
                        "query": gdelt_query,
                        "mode": "ArtList",
                        "format": "json",
                        "timespan": "3h",
                        "maxrecords": 5,
                    },
                    timeout=self.timeout,
                )
                response.raise_for_status()
                payload = response.json()
        except Exception:
            self._gdelt_cache[cache_key] = (now + 300.0, [])
            return []
        articles = payload.get("articles") if isinstance(payload, dict) else []
        context = [
            {
                "source": "gdelt",
                "title": article.get("title"),
                "url": article.get("url"),
                "domain": article.get("domain"),
                "seen_date": article.get("seendate"),
            }
            for article in articles[:5]
            if isinstance(article, dict)
        ]
        self._gdelt_cache[cache_key] = (now + 300.0, context)
        return context

    @staticmethod
    def _build_gdelt_query(query: str) -> str:
        cleaned = " ".join(query.split())[:180]
        macro_terms = "oil OR sanctions OR rates OR inflation OR war OR tariff OR supply chain"
        if not cleaned:
            return macro_terms
        return f"({cleaned}) ({macro_terms})"
