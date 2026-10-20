"""Trends collector: feed parsing, verdict validation, card storage and one service pass."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from yeoman_gateway.trends.models import Headline, TrendItem
from yeoman_gateway.trends.scorer import TrendScorer, parse_verdicts
from yeoman_gateway.trends.service import TrendsService
from yeoman_gateway.trends.sources import parse_google_trends
from yeoman_gateway.trends.store import TrendStore

FEED = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss xmlns:ht="https://trends.google.com/trending/rss" version="2.0"><channel>
<item>
  <title>sexually transmitted diarrhea</title>
  <ht:approx_traffic>2000+</ht:approx_traffic>
  <pubDate>Tue, 20 Oct 2026 07:50:00 -0700</pubDate>
  <ht:news_item>
    <ht:news_item_title>Doctors warn of sexually transmitted diarrhea</ht:news_item_title>
    <ht:news_item_url>https://example.org/std</ht:news_item_url>
    <ht:news_item_source>Example</ht:news_item_source>
  </ht:news_item>
</item>
<item>
  <title>luke littler</title>
  <ht:approx_traffic>500+</ht:approx_traffic>
  <pubDate>not a date</pubDate>
  <ht:news_item>
    <ht:news_item_title>Littler wins again</ht:news_item_title>
    <ht:news_item_url>https://example.org/darts</ht:news_item_url>
  </ht:news_item>
</item>
<item><title>   </title></item>
</channel></rss>"""


def _item(title: str, url: str = "https://example.org/a") -> TrendItem:
    return TrendItem(
        source="test",
        key=f"test:{title}",
        title=title,
        published_at_ms=0,
        headlines=(Headline(title=f"{title} headline", url=url),),
    )


def test_parse_google_trends_reads_items_and_headlines() -> None:
    items = parse_google_trends(FEED, source="google_trends:DE")

    assert [item.title for item in items] == ["sexually transmitted diarrhea", "luke littler"]
    first = items[0]
    assert first.key == "google_trends:DE:sexually transmitted diarrhea"
    assert first.traffic == "2000+"
    assert first.headlines == (
        Headline(
            title="Doctors warn of sexually transmitted diarrhea",
            url="https://example.org/std",
            outlet="Example",
        ),
    )
    assert first.published_at_ms == 1792507800000
    assert items[1].published_at_ms > 0


def test_parse_verdicts_validates_every_field() -> None:
    items = [_item("a", "https://example.org/a"), _item("b"), _item("c"), _item("d")]
    raw = json.dumps({"items": [
        {"index": 0, "score": 9, "kind": "absurd", "reject": "none", "reason": "odd",
         "hook": "Something odd.", "line": "Passt auf euch auf!",
         "url": "https://invented.example/x", "tags": ["Health", "health", "odd"]},
        {"index": 0, "score": 1, "kind": "funny"},
        {"index": 1, "score": 9, "kind": "funny", "reject": "tragedy", "reason": "death"},
        {"index": 2, "score": 8, "kind": "boring"},
        {"index": 9, "score": 10, "kind": "funny"},
        {"index": True, "score": 10, "kind": "funny"},
        "junk",
    ]})

    verdicts = parse_verdicts(f"```json\n{raw}\n```", items)

    assert [verdict.item_key for verdict in verdicts] == ["test:a", "test:b", "test:c"]
    first, rejected, unknown_kind = verdicts
    assert (first.score, first.kind, first.line) == (9, "absurd", "Passt auf euch auf!")
    assert first.url == "https://example.org/a"
    assert first.tags == ("health", "odd")
    assert (rejected.score, rejected.kind) == (0, "rejected")
    assert rejected.reason.startswith("tragedy")
    assert (unknown_kind.score, unknown_kind.kind) == (0, "none")


def test_parse_verdicts_tolerates_garbage() -> None:
    assert parse_verdicts("not json", [_item("a")]) == []
    assert parse_verdicts('{"items": "nope"}', [_item("a")]) == []


def test_store_keeps_only_cards_at_the_bar_and_tracks_use(tmp_path) -> None:
    store = TrendStore(tmp_path / "trends.db")
    items = [_item("good"), _item("meh")]
    verdicts = parse_verdicts(json.dumps({"items": [
        {"index": 0, "score": 8, "kind": "funny", "line": "Ha.", "url": "https://example.org/a"},
        {"index": 1, "score": 4, "kind": "cool", "line": "Hm.", "url": "https://example.org/a"},
    ]}), items)

    cards = store.record(items, verdicts, min_score=7, now_ms=1_000, ttl_ms=500)

    assert [card.title for card in cards] == ["good"]
    assert store.unseen([*items, _item("new"), _item("new")]) == [_item("new")]
    assert [card.title for card in store.unused_cards_for("wa", "g1", now_ms=1_100)] == ["good"]
    store.mark_used(cards[0].card_id, "wa", "g1", now_ms=1_200)
    assert store.unused_cards_for("wa", "g1", now_ms=1_300) == []
    assert len(store.unused_cards_for("wa", "g2", now_ms=1_300)) == 1
    store.prune(now_ms=1_500)
    assert store.active_cards(now_ms=1_000) == []
    store.close()


class _Source:
    name = "fake"

    def __init__(self, items: list[TrendItem]) -> None:
        self.items = items

    async def fetch(self) -> list[TrendItem]:
        return self.items


class _Broken:
    name = "broken"

    async def fetch(self) -> list[TrendItem]:
        raise RuntimeError("down")


class _Client:
    def __init__(self) -> None:
        self.calls: list[list[dict[str, str]]] = []

    async def chat_with_usage(self, messages, *, max_tokens, response_format=None, max_retries=None):
        self.calls.append(messages)
        return SimpleNamespace(content=json.dumps({"items": [
            {"index": 0, "score": 9, "kind": "absurd", "reject": "none",
             "line": "Passt gut auf euch auf, Jungs!", "url": "https://example.org/a"},
        ]}))


@pytest.mark.asyncio
async def test_service_scores_new_items_once_and_survives_a_broken_source(tmp_path) -> None:
    client = _Client()
    service = TrendsService(
        sources=[_Broken(), _Source([_item("std")])],
        scorer=TrendScorer(client=client),
        store=TrendStore(tmp_path / "trends.db"),
        clock=lambda: 10_000,
    )

    first = await service.run_once()
    second = await service.run_once()

    assert first.as_dict() == {"fetched": 1, "scored": 1, "cards": 1, "failed_sources": ["broken"]}
    assert second.scored == 0
    assert len(client.calls) == 1
    prompt = client.calls[0][1]["content"]
    assert "std headline" in prompt and "url=https://example.org/a" in prompt
    assert "German" in client.calls[0][0]["content"]
    await service.stop()


def test_trends_config_defaults_off_and_bootstrap_skips_it() -> None:
    from yeoman_gateway.app.bootstrap import _build_trends
    from yeoman_shared.config.schema import Config

    config = Config.model_validate({"trends": {"intervalMinutes": 60, "minScore": 8}})

    assert config.trends.enabled is False
    assert (config.trends.interval_minutes, config.trends.min_score) == (60, 8)
    assert _build_trends(config) is None
    missing_route = Config.model_validate({"trends": {"enabled": True, "route": "nope"}})
    assert _build_trends(missing_route) is None
