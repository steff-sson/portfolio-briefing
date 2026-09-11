"""Sanity-Check der LLM-Ausgabe gegen die Input-Daten (~50 LOC).

Jede Ticker-/ISIN-Referenz und jede Zahl im Render-Output muss im Input
existieren. Verhindert halluzinierte Wertpapiere oder Kennzahlen. Fail-closed:
Nicht-auffindbare Referenzen werden als Verletzung gemeldet.
"""

from __future__ import annotations

import re
from typing import Any

_ISIN_RE = re.compile(r"\b[A-Z]{2}[A-Z0-9]{9}[0-9]\b")
_PCT_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*%")

KNOWN_SKIP = {"EUR", "USD", "US", "CORP", "INC", "LTD", "PLC", "AKTIEN", "ETF"}


def _leaf_numbers(obj: Any, out: set[str]) -> None:
    if isinstance(obj, dict):
        for v in obj.values():
            _leaf_numbers(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _leaf_numbers(v, out)
    elif isinstance(obj, (int, float)) and not isinstance(obj, bool):
        out.add(f"{obj:.2f}".rstrip("0").rstrip("."))
        out.add(f"{obj:.0f}")
        out.add(f"{obj:.1f}")


def known_isins(data: dict[str, Any]) -> set[str]:
    isins: set[str] = set()
    for coll in ("holdings", "watchlist"):
        for item in data.get(coll, []):
            if item.get("isin"):
                isins.add(str(item["isin"]).upper())
    return isins


def known_tickers(data: dict[str, Any]) -> set[str]:
    tickers: set[str] = set()
    for coll in ("holdings", "watchlist", "fundamentals"):
        for item in data.get(coll, []):
            if item.get("ticker"):
                tickers.add(str(item["ticker"]).upper())
    return tickers


def check_sanity(text: str, data: dict[str, Any]) -> list[str]:
    """Liefert Liste der Sanity-Verletzungen (leer = OK)."""
    violations: list[str] = []
    isins = known_isins(data)
    tickers = known_tickers(data)
    numbers: set[str] = set()
    _leaf_numbers(data, numbers)

    for m in _ISIN_RE.finditer(text.upper()):
        if m.group(0) not in isins:
            violations.append(f"Unbekannte ISIN im Output: {m.group(0)}")

    # Unbekannte Ticker: nur ALL-CAPS-Kürzel im Quelltext gelten als Ticker.
    # Gemischte Prosa ("Keine", "Aktion") löst keine Ticker-Prüfung aus.
    for tok in text.split():
        cleaned = "".join(ch for ch in tok if ch.isalnum())
        if not cleaned or len(cleaned) < 2 or len(cleaned) > 6:
            continue
        if cleaned != cleaned.upper():  # nicht all-caps → Prosa, kein Ticker
            continue
        if cleaned in tickers or cleaned in isins or cleaned in KNOWN_SKIP:
            continue
        if cleaned.isalpha():
            violations.append(f"Unbekannter Ticker im Output: {cleaned}")

    # Jede Prozentzahl muss als (gerundete) Größe im Input existieren.
    for m in _PCT_RE.finditer(text):
        val = m.group(1).replace(",", ".")
        fval = float(val)
        candidates = {f"{fval:.0f}", f"{fval:.1f}", f"{fval:.2f}"}
        if not (candidates & numbers):
            violations.append(f"Prozentzahl {val}% ohne Input-Basis")

    return violations
