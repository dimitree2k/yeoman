"""Value types shared by trend sources, the scorer and the card store."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class Headline:
    """One news article a source links to a trending topic."""

    title: str
    url: str
    outlet: str = ""


@dataclass(frozen=True, slots=True)
class TrendItem:
    """One raw topic from a source, before anyone judged whether it is fun."""

    source: str
    key: str
    title: str
    published_at_ms: int
    traffic: str = ""
    headlines: tuple[Headline, ...] = ()

    @property
    def urls(self) -> frozenset[str]:
        return frozenset(headline.url for headline in self.headlines if headline.url)


@dataclass(frozen=True, slots=True)
class TrendVerdict:
    """The scorer's view of one item; only verdicts at or above the bar become cards."""

    item_key: str
    score: int
    kind: str
    reason: str = ""
    hook: str = ""
    line: str = ""
    url: str = ""
    tags: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class TrendCard:
    """A topic worth bringing into a quiet group, with its source link."""

    card_id: str
    item_key: str
    source: str
    title: str
    score: int
    kind: str
    hook: str
    line: str
    url: str
    created_at_ms: int
    expires_at_ms: int
    tags: tuple[str, ...] = field(default=())
