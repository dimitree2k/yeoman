"""Trends collector: feed parsing, verdict validation, card storage and one service pass."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from yeoman_gateway.trends.article import Article, parse_article
from yeoman_gateway.trends.models import Headline, TrendItem, TrendVerdict
from yeoman_gateway.trends.scorer import TrendScorer, parse_verdicts
from yeoman_gateway.trends.service import TrendsService
from yeoman_gateway.trends.sources import parse_google_trends
from yeoman_gateway.trends.store import TrendStore
from yeoman_gateway.trends.writer import LineWriter, apply_written

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

    assert first.as_dict() == {
        "fetched": 1, "scored": 1, "cards": 1, "fitted_chats": 0, "articles_read": 0,
        "dropped": 0,
        "failed_sources": ["broken"],
    }
    assert second.scored == 0
    assert len(client.calls) == 1
    prompt = client.calls[0][1]["content"]
    assert "std headline" in prompt and "url=https://example.org/a" in prompt
    system = client.calls[0][0]["content"]
    assert "German" in system
    assert "Sports are lame" in system and "women's competitions" in system
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


ARTICLE_HTML = """<html lang="de-DE"><head>
<meta property="article:published_time" content="2026-10-27T08:15:00+01:00">
</head><body><nav>Menu</nav><article><h1>Urteil</h1>
<p>Ein Gericht hat entschieden, dass Google Maps ein Haus auf Wunsch verpixeln muss.
Die Besitzerin hatte geklagt, weil Autoposer vor ihrem Haus posierten.</p>
<p>Der Fall geht wohl in die nächste Instanz, Google prüft das Urteil.</p>
</article></body></html>"""


def test_parse_article_reads_text_date_and_language() -> None:
    article = parse_article(ARTICLE_HTML)

    assert article is not None
    assert "Google Maps ein Haus auf Wunsch verpixeln" in article.text
    assert article.language == "de"
    assert article.published_at_ms == 1793085300000


def test_parse_article_falls_back_to_json_ld_and_time() -> None:
    ld = parse_article(
        '<html><head><script type="application/ld+json">{"datePublished": "2026-10-27"}'
        "</script></head><body><article><p>" + "Text. " * 40 + "</p></article></body></html>"
    )
    timed = parse_article(
        '<html><body><time datetime="Tue, 27 Oct 2026 07:00:00 GMT"></time>'
        "<article><p>" + "Text. " * 40 + "</p></article></body></html>"
    )

    assert ld is not None and ld.published_at_ms == 1793059200000
    assert timed is not None and timed.published_at_ms == 1793084400000
    assert parse_article("<html><body></body></html>") is None


def _verdict(key: str = "test:a", score: int = 9) -> TrendVerdict:
    return TrendVerdict(
        item_key=key, score=score, kind="absurd", hook="old hook", line="old line",
        url="https://example.org/a",
    )


def test_apply_written_rewrites_drops_or_keeps() -> None:
    verdict = _verdict()

    assert apply_written(verdict, {"keep": True, "hook": "new", "line": "neu"}).line == "neu"
    dropped = apply_written(verdict, {"keep": False, "line": "x"})
    assert (dropped.score, dropped.kind) == (0, "dropped")
    assert apply_written(verdict, {"keep": True, "line": "  "}) == verdict
    assert apply_written(verdict, None) == verdict


class _Reader:
    def __init__(self, articles: dict[str, Article | None]) -> None:
        self.articles = articles

    async def read(self, url: str) -> Article | None:
        return self.articles.get(url)


class _WriterClient:
    def __init__(self) -> None:
        self.calls: list[list[dict[str, str]]] = []

    async def chat_with_usage(self, messages, *, max_tokens, response_format=None, max_retries=None):
        self.calls.append(messages)
        return SimpleNamespace(content=json.dumps(
            {"keep": True, "hook": "Hook.", "line": "Das wird ein Feature."}
        ))


@pytest.mark.asyncio
async def test_service_rewrites_from_the_article_and_drops_stale_foreign_or_unreadable(tmp_path) -> None:
    now = 100 * 3600 * 1000
    items = [
        _item("fresh", "https://example.org/fresh"),
        _item("stale", "https://example.org/stale"),
        _item("french", "https://example.org/french"),
        _item("paywall", "https://example.org/paywall"),
        _item("boring", "https://example.org/boring"),
    ]
    reader = _Reader({
        "https://example.org/fresh": Article(text="Body.", published_at_ms=now - 3600_000, language="de"),
        "https://example.org/stale": Article(text="Body.", published_at_ms=now - 72 * 3600_000),
        "https://example.org/french": Article(text="Corps.", language="fr"),
        "https://example.org/paywall": None,
    })
    writer_client = _WriterClient()

    class _ScoreAll:
        async def chat_with_usage(self, messages, *, max_tokens, response_format=None, max_retries=None):
            return SimpleNamespace(content=json.dumps({"items": [
                {"index": i, "score": 2 if i == 4 else 9, "kind": "absurd", "line": "Alt.",
                 "url": items[i].headlines[0].url}
                for i in range(len(items))
            ]}))

    service = TrendsService(
        sources=[_Source(items)],
        scorer=TrendScorer(client=_ScoreAll()),
        store=TrendStore(tmp_path / "trends.db"),
        reader=reader,
        writer=LineWriter(client=writer_client),
        clock=lambda: now,
    )

    report = await service.run_once()
    cards = {card.title: card for card in service.store.active_cards(now_ms=now)}

    assert (report.articles_read, report.dropped, report.cards) == (3, 3, 1)
    assert cards["fresh"].line == "Das wird ein Feature."
    assert set(cards) == {"fresh"}
    kinds = {row["title"]: row["kind"] for row in service.store.recent_scores()}
    assert (kinds["stale"], kinds["french"], kinds["paywall"]) == ("stale", "foreign", "unreadable")
    assert len(writer_client.calls) == 1
    system, user = writer_client.calls[0]
    assert "Grillen im Tourbus" in system["content"] and "German" in system["content"]
    assert "Article:\nBody." in user["content"]
    await service.stop()


def test_news_feeds_rss_and_atom_become_items_with_their_link() -> None:
    from yeoman_gateway.trends.sources import parse_news_feed

    rss = """<rss><channel>
      <item><title>Mann klaut  Bagger</title><link>https://ex.de/a</link>
        <pubDate>Sat, 31 Oct 2026 18:17:07 +0100</pubDate></item>
      <item><title>ohne link</title></item>
      <item><title>kein web</title><link>javascript:alert(1)</link></item>
    </channel></rss>"""
    atom = """<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Florida man</title>
      <link href="https://ex.com/b"/><published>2026-10-31T10:00:00+00:00</published>
      </entry></feed>"""
    items = parse_news_feed(rss, source="rss:ex.de")
    assert [(item.title, item.urls) for item in items] == [("Mann klaut Bagger", {"https://ex.de/a"})]
    assert items[0].key == "rss:ex.de:https://ex.de/a"
    from datetime import UTC, datetime

    assert items[0].published_at_ms == int(datetime(2026, 10, 31, 17, 17, 7, tzinfo=UTC).timestamp() * 1000)
    assert [item.title for item in parse_news_feed(atom, source="rss:ex.com")] == ["Florida man"]


def test_configured_feeds_are_sources_next_to_google() -> None:
    from yeoman_gateway.trends.service import build_trends_service
    from yeoman_shared.config.schema import Config

    config = Config.model_validate({
        "models": {"profiles": {"w": {"kind": "chat", "model": "m", "provider": "openrouter"}},
                   "routes": {"trends.score": "w"}},
        "trends": {"enabled": True, "rssFeeds": ["https://www.n-tv.de/panorama/rss"]},
    })
    import os

    os.environ.setdefault("OPENROUTER_API_KEY", "test")
    service = build_trends_service(config, store_path=__import__("pathlib").Path(
        __import__("tempfile").mkdtemp()) / "t.db")
    assert [source.name for source in service.sources] == ["google_trends:DE", "rss:n-tv.de"]
    service.store.close()
