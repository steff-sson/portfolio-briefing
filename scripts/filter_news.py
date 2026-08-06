"""Fetch RSS feeds and filter news relevant to portfolio holdings."""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import feedparser


ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
MAX_NEWS = 15
MAX_AGE_DAYS = 7


def load_feeds() -> list[dict]:
    with open(CONFIG_DIR / "feeds.json", encoding="utf-8") as f:
        return json.load(f).get("feeds", [])


def _portfolio_keywords(portfolio: dict) -> list[str]:
    keywords = []
    for h in portfolio.get("holdings", []):
        name = h.get("name", "")
        isin = h.get("isin", "")
        ticker = h.get("ticker", "")
        for token in name.split():
            if len(token) >= 3:
                keywords.append(token)
        if isin:
            keywords.append(isin)
        if ticker:
            keywords.append(ticker)
    return list(set(keywords))


def _parse_published(entry: dict) -> datetime | None:
    pp = entry.get("published_parsed")
    if pp:
        return datetime.fromtimestamp(
            float(pp.tm_year), tz=timezone.utc
        ).replace(year=pp.tm_year)
    published = entry.get("published")
    if published:
        for fmt in ("%a, %d %b %Y %H:%M:%S %z", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ"):
            try:
                return datetime.strptime(published, fmt)
            except ValueError:
                continue
    return None


def _is_recent(entry: dict) -> bool:
    pub = _parse_published(entry)
    if not pub:
        return False
    cutoff = datetime.now(tz=timezone.utc) - timedelta(days=MAX_AGE_DAYS)
    if pub.tzinfo is None:
        cutoff = cutoff.replace(tzinfo=None)
    return pub >= cutoff


def _is_match(entry: dict, keywords: list[str]) -> bool:
    text = " ".join([
        entry.get("title", ""),
        entry.get("summary", ""),
        entry.get("description", ""),
    ]).lower()
    return any(kw.lower() in text for kw in keywords)


def _deduplicate(candidates: list[dict]) -> list[dict]:
    seen: set[str] = set()
    out = []
    for c in candidates:
        key = c.get("title", "") + c.get("link", "")
        if key not in seen:
            seen.add(key)
            out.append(c)
    return out


def fetch_and_filter_news(portfolio: dict) -> list[dict]:
    keywords = _portfolio_keywords(portfolio)
    feeds = load_feeds()
    candidates = []
    for feed in feeds:
        try:
            parsed = feedparser.parse(feed["url"])
        except Exception:
            continue
        for entry in parsed.get("entries", []):
            if not _is_recent(entry):
                continue
            if not _is_match(entry, keywords):
                continue
            candidates.append({
                "title": entry.get("title", ""),
                "link": entry.get("link", ""),
                "summary": entry.get("summary", entry.get("description", ""))[:300],
                "published": entry.get("published", ""),
                "source": feed.get("name", "unknown"),
            })
    candidates = _deduplicate(candidates)
    candidates.sort(key=lambda x: x.get("published", ""), reverse=True)
    return candidates[:MAX_NEWS]


if __name__ == "__main__":
    portfolio = json.loads((CONFIG_DIR / "portfolio.json").read_text())
    news = fetch_and_filter_news(portfolio)
    print(json.dumps(news, indent=2, ensure_ascii=False))
