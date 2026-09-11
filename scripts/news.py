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


def _expand_mcp_news(item: dict[str, Any]) -> list[dict[str, Any]]:
    """Expands one raw MCP-news item into quellenneutrale flat entries.

    Real-Schema: ``{isin, name, published_at, summary, sources:[{headline, source,
    published}]}``. Fixture/schon-flaches Schema: ``{isin, ticker, title, url,
    publishedAt, source}``.

    - Titel/Quelle/Datum werden aus dem Rohdatensatz gemappt (headline→title,
      source→Verlag, published→Datum).
    - Items ohne Titel werden übersprungen (keine leeren News-Zeilen).
    - ``mcp`` als source nur als Fallback, wenn kein Verlag der Quelle vorliegt.
    """
    out: list[dict[str, Any]] = []
    sources = item.get("sources")
    if isinstance(sources, list):
        # Echtes MCP-Schema: je Quelle ein Eintrag.
        for src in sources:
            title = str(src.get("headline") or "").strip()
            if not title:
                continue
            source = str(src.get("source") or "").strip() or "mcp"
            out.append(
                {
                    "source": source,
                    "title": title,
                    "url": str(item.get("url") or item.get("link") or ""),
                    "published": str(src.get("published") or item.get("published_at") or ""),
                    "isin": item.get("isin"),
                    "ticker": item.get("ticker"),
                }
            )
        return out

    # Fixture/schon-flaches Schema.
    title = str(item.get("title") or item.get("headline") or "").strip()
    if not title:
        return out
    out.append(
        {
            "source": str(item.get("source") or "").strip() or "mcp",
            "title": title,
            "url": str(item.get("url") or item.get("link") or ""),
            "published": str(item.get("publishedAt") or item.get("published") or ""),
            "isin": item.get("isin"),
            "ticker": item.get("ticker"),
        }
    )
    return out


def collect_news(
    mcp_news: list[dict[str, Any]],
    feeds: list[dict[str, str]],
    tickers: set[str],
    isins: set[str],
) -> list[dict[str, Any]]:
    """Kombiniert MCP-News (je Wertpapier) mit frischen RSS-Treffern."""
    items: list[dict[str, Any]] = []
    for n in mcp_news:
        items.extend(_expand_mcp_news(n))
    items.extend(fetch_rss(feeds, tickers, isins))
    return items
