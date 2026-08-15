"""Deterministic pre-trade guardrail checks for portfolio-briefing.

Live-First: Portfolio kommt aus dem Snapshot-Modul (config/snapshot.current.json).
Kein Mock-/Seed-Fallback — fehlende Live-Config ergibt einen klaren Fehlerstatus.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml

from scripts import snapshot

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config"
VAULT = Path.home() / "docs" / "notizen" / "portfolio-theses"

SECTOR_FALLBACK = {
    "US0378331005": "Technology", "AAPL": "Technology",
    "NL0010273215": "Technology", "ASML": "Technology",
    "DE0007164600": "Technology", "SAP": "Technology",
    "DK0062498333": "Health Care", "NOVO-B": "Health Care",
}


def _load(path: Path, loader):
    with open(path, encoding="utf-8") as f:
        return loader(f)


def _load_portfolio() -> dict | None:
    """Live-Portfolio aus dem Snapshot-Modul (config/snapshot.current.json).

    Kein Mock-/Seed-Fallback: fehlender/korrupter Snapshot -> None; der
    Aufrufer liefert dann einen klaren Fehlerstatus statt Fake-Daten.
    """
    current = snapshot.read_snapshot(snapshot.CURRENT_PATH)
    if current is None:
        return None
    return current.get("portfolio")


def _sector(holding: dict, lookup: dict) -> str:
    isin, ticker = holding.get("isin", ""), holding.get("ticker", "")
    if isin in lookup: return lookup[isin].get("sector", "Unknown")
    if ticker in lookup: return lookup[ticker].get("sector", "Unknown")
    return SECTOR_FALLBACK.get(isin) or SECTOR_FALLBACK.get(ticker) or holding.get("sector", "Unknown")


def _find(portfolio: dict, lookup: dict, identifier: str):
    for h in portfolio.get("holdings", []):
        if h.get("ticker") == identifier or h.get("isin") == identifier or h.get("name") == identifier:
            return h
    if identifier in lookup:
        return {"isin": identifier, "name": lookup[identifier].get("name", identifier), "quantity": 0, "value_eur": 0.0, "category": "satellite"}
    return None


def _thesis_exists(ticker: str) -> bool:
    if not VAULT.exists(): return False
    return any(f.is_file() and f.suffix == ".md" and f.stem.upper() == ticker.upper() for f in VAULT.iterdir())


def check_buy(ticker: str, quantity: float, price: float) -> dict:
    """Run guardrail checks for a planned buy."""
    portfolio = _load_portfolio()
    if portfolio is None:
        return {
            "ticker": ticker,
            "error": "Keine Live-Config: config/snapshot.current.json fehlt oder ist korrupt "
                     "(Erstlauf/Seed-Migration noetig, kein Mock-Fallback).",
        }
    strategy = _load(CONFIG / "strategy.yaml", yaml.safe_load)
    lookup = _load(CONFIG / "etf_lookup.json", json.load)
    limits = strategy.get("satellite_limits", {})
    max_pos = float(limits.get("max_position_pct", 5.0))
    max_sec = float(limits.get("max_sector_pct", 15.0))
    max_count = int(limits.get("max_positions", 10))
    thesis_required = strategy.get("thesis", {}).get("required", True)

    holding = _find(portfolio, lookup, ticker)
    if not holding:
        return {"ticker": ticker, "error": f"Instrument {ticker} not found."}

    isin, resolved = holding.get("isin", ""), holding.get("ticker") or ticker
    existing = float(holding.get("value_eur", 0.0))
    trade = float(quantity) * float(price)
    new_total = float(portfolio.get("total_value_eur", 0.0)) + trade
    if new_total <= 0:
        return {"ticker": resolved, "error": "Portfolio total value must be positive."}

    pos_pct = (existing + trade) / new_total * 100
    sector = _sector(holding, lookup)
    sector_value = trade + sum(
        float(h.get("value_eur", 0.0)) for h in portfolio.get("holdings", [])
        if h.get("category", "satellite") == "satellite" and _sector(h, lookup) == sector
    )
    sec_pct = sector_value / new_total * 100

    sat_count = sum(1 for h in portfolio.get("holdings", []) if h.get("category", "satellite") == "satellite")
    if holding.get("quantity", 0) == 0: sat_count += 1

    thesis_ok = _thesis_exists(resolved) if thesis_required else True
    checks = {
        "sector": {"status": "green" if sec_pct <= max_sec else "red", "value": round(sec_pct, 2), "limit": max_sec},
        "position_size": {"status": "green" if pos_pct <= max_pos else ("yellow" if pos_pct <= max_pos * 2 else "red"), "value": round(pos_pct, 2), "limit": max_pos},
        "position_count": {"status": "green" if sat_count <= max_count else "red", "value": sat_count, "limit": max_count},
        "thesis": {"status": "green" if thesis_ok else "yellow", "value": "found" if thesis_ok else "missing", "limit": "required" if thesis_required else "optional"},
    }
    verdict = "block" if any(c["status"] == "red" for c in checks.values()) else "warn" if any(c["status"] == "yellow" for c in checks.values()) else "ok"

    return {
        "ticker": resolved, "isin": isin, "sector": sector,
        "new_position_pct": round(pos_pct, 2), "new_sector_pct": round(sec_pct, 2),
        "new_satellite_count": sat_count, "checks": checks, "verdict": verdict,
    }


if __name__ == "__main__":
    if len(sys.argv) != 7:
        print("Usage: python check_position.py --ticker TICKER --quantity Q --price P")
        sys.exit(2)
    kwargs = dict(zip(sys.argv[1::2], sys.argv[2::2]))
    result = check_buy(kwargs["--ticker"], float(kwargs["--quantity"]), float(kwargs["--price"]))
    print(json.dumps(result, indent=2, ensure_ascii=False))
    if result.get("error"):
        sys.exit(1)  # fehlende Live-Config ist ein Fehlerstatus, kein Warn-Exit
    if result.get("verdict") == "block":
        sys.exit(1)
