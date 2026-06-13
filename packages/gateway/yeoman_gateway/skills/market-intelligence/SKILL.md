---
name: market-intelligence
description: "Use for market price and move questions: current stock/ETF/forex/crypto/commodity quotes, very recent catalysts, sector context, sentiment, and macro/geopolitical market context."
metadata: {"yeoman":{"emoji":"$","always":true}}
---

# Market Intelligence

Use this skill for questions like:

- "Was ist gerade mit AMD los?"
- "Why is my depot bleeding?"
- "Welche SpaceX-adjacent stocks move today?"
- "Why is oil/gold/USD moving?"
- "Is this move company-specific or sector/macro?"

## Mandatory Workflow

1. For "why is it moving?" or market-context questions, call `market_intelligence` first.
2. For a simple quote-only question, call `market_quote` first.
3. Use structured quote tool results as the only source for prices, percent moves, previous-close comparisons, volume, or timestamps.
4. Use news, Finnhub, Tavily, or GDELT context only for catalysts and market narrative. Never turn a news snippet into a quote value.
5. If quote providers are rate-limited, partial, delayed, IEX-only, or unavailable, say that clearly and answer only from the data that was actually returned.

## Answer Shape

Answer in this order:

1. Current move: symbol, price, percent move, timestamp/source if present.
2. Context: sector/peer/index comparison if available.
3. Catalyst: the freshest company news, analyst/news item, sector theme, or macro/geopolitical driver.
4. Confidence: clear, likely, or unclear.
5. Caveat: delayed quote, Alpaca IEX-only, missing symbol, or provider rate-limit.

## Source Rules

- `market_intelligence.quotes` and `market_quote.quotes`: quote values.
- Finnhub/Tavily news: company or sector catalysts.
- GDELT: macro/geopolitical context only, especially oil, defense, shipping, tariffs, sanctions, elections, rates, and conflict risk.
- Web snippets are not prices.

## Fail Closed

If no live quote is available, do not guess. Say current quote data is unavailable, give any available recent catalyst separately, and avoid price/percent claims.
