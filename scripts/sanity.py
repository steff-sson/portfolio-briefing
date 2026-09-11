"""Sanity-Check der LLM-Ausgabe gegen die Input-Daten (~80 LOC).

Jede Ticker-/ISIN-Referenz und JEDE Zahl im LLM-Output (EUR-Beträge, Mengen,
Prozente, auch deutsch-formatiert wie "3.500") muss im Input existieren.
Fail-closed: nicht auffindbare Referenzen werden als Verletzung gemeldet.
Prosa ohne Zahlen bleibt ohne Prüfung.
"""

from __future__ import annotations

import re
from typing import Any

_ISIN_RE = re.compile(r"\b[A-Z]{2}[A-Z0-9]{9}[0-9]\b")
_NUM_RE = re.compile(r"\d[\d.,]*")

KNOWN_SKIP = {"EUR", "USD", "US", "CORP", "INC", "LTD", "PLC", "AKTIEN", "ETF"}


def _norm(x: float) -> float:
    """Normiert eine Zahl auf 2 Dezimalstellen für Vergleichbarkeit."""
    return round(x, 2)


def _leaf_numbers(obj: Any, out: set[float]) -> None:
    """Sammelt alle numerischen Leaf-Werte des Input-Pakets (normiert)."""
    if isinstance(obj, dict):
        for v in obj.values():
            _leaf_numbers(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _leaf_numbers(v, out)
    elif isinstance(obj, (int, float)) and not isinstance(obj, bool):
        out.add(_norm(float(obj)))


def _number_interpretations(token: str) -> set[float]:
    """Deutsch/Englisch-robustes Parsen eines Zahlen-Tokens → Interpretationsmenge.

    Berücksichtigt Tausendertrennzeichen ('.'/',') und Dezimaltrenner (','/'.'):
    "3.500" → {3500, 3.5}; "1,5" → {15, 1.5}; "13.7" → {13.7, 137}; "3500" → {3500}.
    """
    token = token.strip()
    if not token:
        return set()
    results: set[float] = set()
    ncomma = token.count(",")
    ndot = token.count(".")

    if ncomma == 0 and ndot == 0:
        try:
            results.add(float(token))
        except ValueError:
            pass
        return results

    # Beide Trennzeichen: letzter ist Dezimaltrenner, davor Tausender entfernen.
    if ncomma and ndot:
        last = max(token.rfind(","), token.rfind("."))
        rest = token[:last].replace(",", "").replace(".", "") + "." + token[last + 1:]
        try:
            results.add(float(rest))
        except ValueError:
            pass
        return results

    # Nur Komma (deutscher Dezimaltrenner): parseInt ohne Komma + Dezimalpunkt-Variante.
    if ncomma:
        try:
            results.add(float(token.replace(",", "")))
        except ValueError:
            pass
        try:
            results.add(float(token.replace(",", ".")))
        except ValueError:
            pass
        return results

    # Nur Punkt: als Dezimal und als Tausender entfernt.
    if ndot:
        try:
            results.add(float(token))
        except ValueError:
            pass
        try:
            results.add(float(token.replace(".", "")))
        except ValueError:
            pass
        return results
    return results


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
    numbers: set[float] = set()
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

    # JEDE Zahl im Output (EUR-Beträge, Mengen, Prozente, deutsch-formatiert)
    # muss als normierter Leaf-Wert im Input existieren (fail-closed).
    for m in _NUM_RE.finditer(text):
        tok = m.group(0)
        interps = _number_interpretations(tok)
        if not interps:
            continue
        if not any(_norm(v) in numbers for v in interps):
            violations.append(f"Zahl {tok} ohne Input-Basis")

    return violations
