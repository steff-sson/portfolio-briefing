"""News-Aggregation: MCP-News + bestehende RSS-Feeds aus config/feeds.json.

Ticker/ISIN-Match: RSS-Titel werden mit den Ticker- und ISINs aus Portfolio +
Watchlist abgeglichen. Nur treffende Artikel werden weitergegeben (die MCP-News
sind bereits je Wertpapier bezogen und bleiben als solche markiert).
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

try:  # pragma: no cover - feedparser optional zur Laufzeit
    import feedparser
except Exception:  # noqa: BLE001 - pragma: no cover
    feedparser = None  # type: ignore[assignment]


def load_feeds(path: str | Path | None = None) -> list[dict[str, str]]:
    """Lädt konfigurierte RSS-Feeds aus config/feeds.json."""
    if path is None:
        path = Path("config") / "feeds.json"
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    feeds = data.get("feeds", data) if isinstance(data, dict) else data
    return [f for f in feeds if isinstance(f, dict) and f.get("url")]


def _matches(text: str, tickers: set[str], isins: set[str]) -> bool:
    tl = text.lower()
    for isin in isins:
        if isin and isin.lower() in tl:
            return True
    for t in tickers:
        # Nur ganze, kurze Ticker matchen (keine Substring-Falschtreffer).
        if t and len(t) >= 2 and t.lower() in tl:
            return True
    return False


def fetch_rss(feeds: list[dict[str, str]], tickers: set[str], isins: set[str],
              limit_per_feed: int = 30, sleep_seconds: float = 0.0) -> list[dict[str, Any]]:
    """Holt RSS-Feeds und filtert Treffer per Ticker/ISIN-Match. Fail-open."""
    if feedparser is None:  # pragma: no cover
        return []
    items: list[dict[str, Any]] = []
    for i, feed in enumerate(feeds):
        if i > 0 and sleep_seconds:
            time.sleep(sleep_seconds)
        name = feed.get("name", "rss")
        try:
            parsed = feedparser.parse(feed["url"])
            for entry in parsed.entries[:limit_per_feed]:
                title = entry.get("title", "")
                link = entry.get("link", "")
                if not _matches(f"{title} {link}", tickers, isins):
                    continue
                items.append(
                    {
                        "source": f"rss:{name}",
                        "title": title,
                        "url": link,
                        "published": entry.get("published", ""),
                    }
                )
        except Exception as exc:  # noqa: BLE001 - fail-open
            items.append(
                {
                    "source": f"rss:{name}",
                    "title": f"(Feed-Fehler {name}: {exc})",
                    "url": feed.get("url", ""),
                    "published": "",
                    "error": True,
                }
            )
    return items


def collect_news(
    mcp_news: list[dict[str, Any]],
    feeds: list[dict[str, str]],
    tickers: set[str],
    isins: set[str],
) -> list[dict[str, Any]]:
    """Kombiniert MCP-News (je Wertpapier) mit frischen RSS-Treffern."""
    items: list[dict[str, Any]] = []
    for n in mcp_news:
        items.append(
            {
                "source": "mcp",
                "title": str(n.get("title") or n.get("headline") or ""),
                "url": str(n.get("url") or n.get("link") or ""),
                "published": str(n.get("publishedAt") or n.get("timestamp") or ""),
                "isin": n.get("isin"),
                "ticker": n.get("ticker"),
            }
        )
    items.extend(fetch_rss(feeds, tickers, isins))
    return items
