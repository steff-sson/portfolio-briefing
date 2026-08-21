"""Fetch RSS feeds and filter news relevant to portfolio holdings.

Zusaetzlich zur Bestands-Filterung (Plan §6a): gezielte Recherche fuer
unbekannte Wertpapiere (``fetch_news_for_keywords``) und Quellen-/
Duplikatpruefung (``evaluate_news_independence``). Yahoo Finance zaehlt
nicht automatisch als unabhaengig (Aggregator-Charakter).
"""
from __future__ import annotations

import calendar
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import feedparser

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
MAX_NEWS = 15
MAX_AGE_DAYS = 7

# Publisher, die NICHT automatisch als unabhaengige Quelle zaehlen
# (Aggregator-Charakter). Yahoo Finance darf als eine von zwei Quellen
# dienen, aber nicht allein die Unabhaengigkeit begruenden.
NON_INDEPENDENT_SOURCES = {"yahoo_finance", "yahoo"}

# Mindestanzahl unabhaengiger Quellen fuer eine Neukaufidee (Plan §6a).
MIN_INDEPENDENT_SOURCES = 2


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
        return datetime.fromtimestamp(calendar.timegm(pp), tz=timezone.utc)
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


def _fetch_feed(feed: dict, keywords: list[str]) -> list[dict]:
    """Einen Feed parsen und passende, aktuelle Eintraege extrahieren."""
    try:
        parsed = feedparser.parse(feed["url"])
    except Exception:  # noqa: BLE001 - Feed-Fehler duerfen die Recherche nicht blockieren
        return []
    entries = []
    for entry in parsed.get("entries", []):
        if not _is_recent(entry):
            continue
        if keywords and not _is_match(entry, keywords):
            continue
        entries.append({
            "title": entry.get("title", ""),
            "link": entry.get("link", ""),
            "summary": entry.get("summary", entry.get("description", ""))[:300],
            "published": entry.get("published", ""),
            "source": feed.get("name", "unknown"),
        })
    return entries


def fetch_news_for_keywords(keywords: list[str]) -> list[dict]:
    """Gezielte Recherche ueber alle Feeds fuer beliebige Keywords (Plan §6a).

    Erweiterung fuer Neukaufideen: sucht NICHT nur Bestandspositionen,
    sondern beliebige Wertpapiere/ISINs/Ticker. Kein Netzwerk im Dry-Run
    (nur in der produktiven Pipeline relevant).
    """
    feeds = load_feeds()
    candidates = []
    for feed in feeds:
        candidates.extend(_fetch_feed(feed, keywords))
    candidates = _deduplicate(candidates)
    candidates.sort(key=lambda x: x.get("published", ""), reverse=True)
    return candidates[:MAX_NEWS]


def fetch_and_filter_news(portfolio: dict) -> list[dict]:
    keywords = _portfolio_keywords(portfolio)
    feeds = load_feeds()
    candidates = []
    for feed in feeds:
        candidates.extend(_fetch_feed(feed, keywords))
    candidates = _deduplicate(candidates)
    candidates.sort(key=lambda x: x.get("published", ""), reverse=True)
    return candidates[:MAX_NEWS]


def _unique_publishers(news: list[dict]) -> set[str]:
    """Eindeutige Publisher (Feed-Namen), Yahoo Finance herausgefiltert."""
    publishers = set()
    for n in news:
        if not isinstance(n, dict):
            continue
        source = str(n.get("source", "")).lower()
        if source in NON_INDEPENDENT_SOURCES:
            continue
        if source:
            publishers.add(source)
    return publishers


def evaluate_news_independence(news: list[dict]) -> dict:
    """Quellen-/Duplikatpruefung fuer Neukaufideen (Plan §6a).

    Unabhaengig = mind. MIN_INDEPENDENT_SOURCES verschiedene Publisher, die
    erkennbar nicht auf derselben Agenturmeldung/Pressemitteilung basieren.
    Duplikate (gleiche Headline ueber verschiedene Feeds) zaehlen nur einmal.
    Bei Unsicherheit gilt die Regel als nicht erfuellt.

    Rueckgabe: {"independent": bool, "publishers": [...], "duplicate_titles": [...]}.
    """
    unique_titles: dict[str, dict] = {}
    duplicates: list[str] = []
    for n in news:
        if not isinstance(n, dict):
            continue
        title = str(n.get("title", "")).strip().lower()
        if not title:
            continue
        if title in unique_titles:
            duplicates.append(title)
            continue
        unique_titles[title] = n
    publishers = _unique_publishers(list(unique_titles.values()))
    return {
        "independent": len(publishers) >= MIN_INDEPENDENT_SOURCES,
        "publishers": sorted(publishers),
        "duplicate_titles": sorted(set(duplicates)),
        "min_sources": MIN_INDEPENDENT_SOURCES,
    }


def build_news_ideas_base(news: list[dict]) -> dict:
    """Deterministische Neukauf-Basis fuer den Draft (Plan §6a).

    Liefert fuer potenzielle Neukaufideen die News mit Quellen-/Duplikat-
    pruefung: nur News mit mind. 2 unabhaengigen Publishern sind eine
    gueltige Basis; bei Unsicherheit keine Idee (fail-closed).

    ``news`` kann bereits gezielt recherchierte Eintraege fuer unbekannte
    Wertpapiere enthalten (``fetch_news_for_keywords``) — die Basis bewertet
    sie genauso wie Bestands-News (Unabhaengigkeits-/Duplikatpruefung).
    """
    evaluation = evaluate_news_independence(news)
    return {
        "independent": evaluation["independent"],
        "publishers": evaluation["publishers"],
        "duplicate_titles": evaluation["duplicate_titles"],
        "news": [
            {"title": n.get("title"), "source": n.get("source"), "published": n.get("published")}
            for n in news
            if isinstance(n, dict) and str(n.get("source", "")).lower() not in NON_INDEPENDENT_SOURCES
        ],
    }


def fetch_news_for_unlisted_ideas(
    portfolio: dict,
    candidate_isins: list[str] | None = None,
    candidate_names: list[str] | None = None,
) -> list[dict]:
    """Gezielte News-Recherche fuer Neukauf-Kandidaten (Plan §6a).

    Sucht ueber alle Feeds nach unbekannten Wertpapieren (nicht im Portfolio):
    Kandidaten-ISINs/-Namen (falls gegeben) oder, falls leer, die ISINs der
    Holdings, die NICHT im etf_lookup gemappt sind (potenzielle Neukauf-
    Kandidaten). Liefert die deduplizierte, nach Datum sortierte News-Basis
    (Publisher + Headline + Datum) fuer die Quellen-/Duplikatpruefung.

    Kein Netzwerk im Dry-Run (nur in der produktiven Pipeline relevant).
    """
    from scripts import sc_bridge  # lazy: kein Import-Zyklus

    portfolio_isins = {
        str(h.get("isin", ""))
        for h in portfolio.get("holdings", [])
        if isinstance(h, dict) and h.get("isin")
    }
    lookup = sc_bridge.load_etf_lookup()
    if not candidate_isins and not candidate_names:
        # Kandidaten: unbekannte ISINs im Portfolio ohne Lookup-Mapping sind
        # potenzielle Einzelwerte — plus explizite Kandidaten aus dem Draft.
        candidate_isins = [
            isin
            for isin in portfolio_isins
            if isin not in lookup and len(isin) >= 10
        ]
    keywords: list[str] = list(candidate_names or [])
    keywords.extend(str(isin) for isin in (candidate_isins or []))
    if not keywords:
        return []
    return fetch_news_for_keywords(keywords)


if __name__ == "__main__":
    portfolio = json.loads((CONFIG_DIR / "portfolio.json").read_text())
    news = fetch_and_filter_news(portfolio)
    print(json.dumps(news, indent=2, ensure_ascii=False))
