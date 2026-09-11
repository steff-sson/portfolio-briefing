"""Snapshot-Loader + Validatoren (quellenneutral).

Liest die von Phase A (MCP-Pull / sc-Fallback) geschriebenen JSON-Snapshots
unter ``data/`` und normalisiert sie in ein quellenneutrales Modell. Dieselben
Lader/Validator-Funktionen laufen im Dry-Run gegen Fixtures, damit Tests die
echte Verarbeitungspipeline abdecken ohne Live-Daten.

Live-MCP-Struktur (P0-Beund 4/5) wird toleriert:
- ``holdings[]``: Equity/Fund mit ``isin/name/savingsPlan/currentQuote{currency,
  midPrice,timestamp}/position{blocked,filled,pending}``
- ``cryptoHoldings[].etpPositions[]``: ~40 ETPs, praktisch alle mit Bestand 0,
  nur BTC real → Nullfälle werden als ``info``-Klasse geführt, nicht als Fehler.
- ``watchlist[]``: 21 Einträge (ETF/STOCK) mit ``isin/name/securityType/
  currentQuote{midPrice,currency,timestampUtc,isOutdated}``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Normierte Feldnamen der internen Sektionen.
REQUIRED_DATA_FILES = ("portfolio.json", "watchlist.json")
OPTIONAL_DATA_FILES = ("quotes.json", "news.json")


class Issue:
    """Eine Validierungsmeldung mit Schweregrad error|warn|info."""

    __slots__ = ("message", "severity")

    def __init__(self, severity: str, message: str) -> None:
        self.severity = severity
        self.message = message

    def __repr__(self) -> str:  # pragma: no cover - Debug-Hilfe
        return f"Issue({self.severity}, {self.message!r})"


@dataclass
class Snapshot:
    """Normiertes, quellenneutrales Portfolio-/Watchlist-/News-Bild."""

    captured_at: str | None = None
    holdings: list[dict[str, Any]] = field(default_factory=list)
    watchlist: list[dict[str, Any]] = field(default_factory=list)
    quotes: list[dict[str, Any]] = field(default_factory=list)
    news: list[dict[str, Any]] = field(default_factory=list)
    total_value_eur: float = 0.0
    cash_eur: float = 0.0
    issues: list[Issue] = field(default_factory=list)

    @property
    def has_fatal(self) -> bool:
        return any(i.severity == "error" for i in self.issues)


def _d(v: Any) -> float:
    """Bestand null-sicher auf float ziehen."""
    if v is None:
        return 0.0
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _normalize_position(raw: dict[str, Any]) -> dict[str, Any]:
    """Normalisiert eine Equity/Fund-Holding aus MCP/sc in das interne Modell."""
    isin = str(raw.get("isin") or "").strip()
    quote = raw.get("currentQuote") or {}
    position = raw.get("position") or {}
    value = _d(raw.get("value_eur") or _d(raw.get("valuation")))
    mid = quote.get("midPrice")
    mid = mid.get("value") if isinstance(mid, dict) else mid
    filled = _d(position.get("filled") if position else raw.get("quantity"))
    # Fallback: Wert aus Menge × Mid-Kurs, wenn kein direkter Wert geliefert wird.
    if value <= 0 and mid is not None and filled:
        value = filled * _d(mid)
    return {
        "isin": isin,
        "name": str(raw.get("name") or ""),
        "security_type": str(raw.get("securityType") or raw.get("security_type") or "UNKNOWN"),
        "category": str(raw.get("category") or "unknown").lower(),
        "ticker": raw.get("ticker"),
        "value_eur": value,
        "quantity": filled,
        "currency": quote.get("currency") or raw.get("valuation_currency") or "EUR",
        "mid_price": mid,
        "timestamp_utc": quote.get("timestamp") or quote.get("timestampUtc"),
        "is_outdated": bool(quote.get("isOutdated", False)),
        "savings_plan": bool(raw.get("savingsPlan", False)),
        "is_crypto_etp_zero": False,
    }


def _normalize_etp(raw: dict[str, Any]) -> dict[str, Any] | None:
    """Normiert einen Crypto-ETP aus ``cryptoHoldings[].etpPositions[]``.

    Liefert None bei irrelevantem Nullfall (Bestand 0 oder fehlender ISIN),
    damit der Validator Nullbestände als ``info`` statt als Position führen kann.
    """
    isin = str(raw.get("isin") or "").strip()
    if not isin:
        return None
    position = raw.get("position") or {}
    filled = _d(position.get("filled") if position else raw.get("quantity"))
    zero = filled <= 0
    return {
        "isin": isin,
        "name": str(raw.get("name") or ""),
        "security_type": "CRYPTO_ETP",
        "category": "unknown",
        "ticker": raw.get("ticker"),
        "value_eur": _d(raw.get("value_eur") or raw.get("valuation")),
        "quantity": filled,
        "currency": "EUR",
        "mid_price": None,
        "timestamp_utc": None,
        "is_outdated": False,
        "savings_plan": False,
        "is_crypto_etp_zero": zero,
    }


def _normalize_watchlist_item(raw: dict[str, Any]) -> dict[str, Any]:
    quote = raw.get("currentQuote") or {}
    mid = quote.get("midPrice")
    mid = mid.get("value") if isinstance(mid, dict) else mid
    return {
        "isin": str(raw.get("isin") or "").strip(),
        "name": str(raw.get("name") or ""),
        "security_type": str(raw.get("securityType") or raw.get("security_type") or "UNKNOWN"),
        "ticker": raw.get("ticker"),
        "currency": quote.get("currency") or "EUR",
        "mid_price": mid,
        "timestamp_utc": quote.get("timestampUtc") or quote.get("timestamp"),
        "is_outdated": bool(quote.get("isOutdated", False)),
    }


def parse_portfolio(raw: dict[str, Any]) -> tuple[list[dict[str, Any]], list[Issue]]:
    """Normalisiert das Portfolio-JSON (holdings + cryptoHoldings) → Positionen."""
    holdings: list[dict[str, Any]] = []
    issues: list[Issue] = []
    seen: set[str] = set()

    for h in raw.get("holdings") or []:
        pos = _normalize_position(h)
        if not pos["isin"]:
            issues.append(Issue("warn", "Portfolio-Position ohne ISIN übersprungen"))
            continue
        if pos["isin"] in seen:
            issues.append(Issue("warn", f"Doppelte Position {pos['isin']} — letzte gewinnt"))
        seen.add(pos["isin"])
        holdings.append(pos)

    for ch in raw.get("cryptoHoldings") or []:
        for etp in ch.get("etpPositions") or []:
            pos = _normalize_etp(etp)
            if pos is None:
                continue
            if pos["is_crypto_etp_zero"]:
                issues.append(
                    Issue("info", f"Crypto-ETP {pos['isin']} mit Bestand 0 — ignoriert")
                )
                continue
            if pos["isin"] in seen:
                continue
            seen.add(pos["isin"])
            holdings.append(pos)

    return holdings, issues


def parse_watchlist(raw: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[Issue]]:
    items: list[dict[str, Any]] = []
    issues: list[Issue] = []
    for w in raw:
        item = _normalize_watchlist_item(w)
        if not item["isin"]:
            issues.append(Issue("warn", "Watchlist-Eintrag ohne ISIN übersprungen"))
            continue
        items.append(item)
    return items, issues


def apply_strategy_classification(
    snap: Snapshot, strategy: dict[str, Any] | None
) -> None:
    """Überschreibt die Holding-Kategorie mit der Strategie-Klassifikation.

    ``holdings_classification.isins`` (confirmed: true) aus strategy.yaml ist
    der autoritative Core/Satellite/Legacy-Intent. Unbestätigte/fehlende
    Einträge bleiben "unknown" (fail-closed, keine Heuristik).
    """
    if not strategy:
        return
    classes = (strategy.get("holdings_classification") or {}).get("isins") or {}
    for h in snap.holdings:
        entry = classes.get(h["isin"])
        if entry and entry.get("confirmed"):
            h["category"] = str(entry.get("category") or "unknown").lower()


def load_snapshot(data_dir: str | Path) -> Snapshot:
    """Lädt + validiert die JSON-Snapshots aus ``data_dir`` (quellenneutral)."""
    data_dir = Path(data_dir)
    snap = Snapshot()
    issues: list[Issue] = []

    def read(name: str) -> dict[str, Any]:
        p = data_dir / name
        if not p.exists():
            return {}
        return json.loads(p.read_text(encoding="utf-8"))

    portfolio = read("portfolio.json")
    watch_raw = read("watchlist.json")
    quotes_raw = read("quotes.json")
    news_raw = read("news.json")

    snap.captured_at = portfolio.get("captured_at") or str(datetime.now(timezone.utc).isoformat())

    for name in REQUIRED_DATA_FILES:
        if not (data_dir / name).exists():
            issues.append(Issue("error", f"Pflicht-Datei fehlt: {name} (kein Snapshot)"))
    if not portfolio:
        if not any(i.message.startswith("Pflicht-Datei") for i in issues):
            issues.append(Issue("error", "Porfolio-Daten fehlen oder leer"))
    else:
        holdings, h_issues = parse_portfolio(portfolio)
        issues.extend(h_issues)
        # Schlüssel 0:0 falls keiner vorhanden
        snap.total_value_eur = _d(
            portfolio.get("total_value_eur")
            or sum(h["value_eur"] for h in holdings)
        )
        snap.cash_eur = _d(portfolio.get("cash_eur"))
        snap.holdings = holdings

    if isinstance(watch_raw, list):
        items, w_issues = parse_watchlist(watch_raw)
        issues.extend(w_issues)
        snap.watchlist = items
    elif isinstance(watch_raw.get("items"), list):
        items, w_issues = parse_watchlist(watch_raw["items"])
        issues.extend(w_issues)
        snap.watchlist = items

    if isinstance(quotes_raw, list):
        snap.quotes = quotes_raw
    elif isinstance(quotes_raw, dict) and isinstance(quotes_raw.get("quotes"), list):
        snap.quotes = quotes_raw["quotes"]

    if isinstance(news_raw, list):
        snap.news = news_raw

    snap.issues = issues
    return snap
