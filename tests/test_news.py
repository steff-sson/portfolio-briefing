"""Tests für news.py — RSS + Ticker/ISIN-Match (gemockt)."""
from __future__ import annotations

from scripts import news as news_mod


def test_matches_isin():
    assert news_mod._matches("Apple US0378331005 news", set(), {"US0378331005"})
    assert news_mod._matches("apple us0378331005 falls", set(), {"US0378331005"})  # case-insens


def test_matches_ticker():
    assert news_mod._matches("NVDA climbs to record", {"NVDA"}, set())
    assert not news_mod._matches("apple today", {"AAPL"}, set())


class _FakeEntry(dict):
    """FeedParserDict-ähnliches Entry (unterstützt .get)."""

    def __init__(self, title, link):
        super().__init__(title=title, link=link)


class _FakeFeed:
    def __init__(self, entries):
        self.entries = entries


class _FakeFeedparser:
    def __init__(self, feeds):
        self._feeds = feeds

    def parse(self, url):
        return _FakeFeed([_FakeEntry(t, url) for t in self._feeds.get(url, [])])


def test_fetch_rss_filters_by_ticker(monkeypatch):
    feed = {"name": "reuters", "url": "http://feed"}
    fake = _FakeFeedparser({"http://feed": ["NVDA GPU boom, chip maker", "Reisebranche up"]})
    monkeypatch.setattr(news_mod, "feedparser", fake)
    items = news_mod.fetch_rss([feed], {"NVDA"}, set())
    assert len(items) == 1
    assert "NVDA" in items[0]["title"]
    assert items[0]["source"] == "rss:reuters"


def test_fetch_rss_feed_failure_fail_open(monkeypatch):
    feed = {"name": "broken", "url": "http://broken"}
    monkeypatch.setattr(news_mod, "feedparser", _FakeFeedparser({}))
    # parse wirft; fetch_rss muss fail-open einen Fehler-Treffer liefern.
    orig = news_mod.feedparser.parse
    news_mod.feedparser.parse = lambda url: (_ for _ in ()).throw(RuntimeError("net down"))
    try:
        items = news_mod.fetch_rss([feed], {"NVDA"}, set())
    finally:
        news_mod.feedparser.parse = orig
    assert len(items) == 1
    assert items[0].get("error") is True


def test_collect_news_combines_mcp_and_rss(monkeypatch):
    feed = {"name": "cnbc", "url": "http://cnbc"}
    fake = _FakeFeedparser({"http://cnbc": ["SAP upgrade"]})
    monkeypatch.setattr(news_mod, "feedparser", fake)
    mcp = [{"title": "Apple news", "isin": "US0378331005", "ticker": "AAPL"}]
    items = news_mod.collect_news(mcp, [feed], {"SAP"}, {"DE0007164600"})
    sources = {i["source"] for i in items}
    assert "mcp" in sources
    assert any(i["source"].startswith("rss:") for i in items)
