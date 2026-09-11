"""Fundamentalkennzahlen je Ticker via yfinance.

Quellenneutrales Basis-Set (~6 Felder) als qualitative Bänder statt absoluter
Mrd-Zahlen (Plan-Entscheidung: löst die offene Option-1-vs-2-Frage):
- forwardPE, marketCap, revenueGrowth, earningsGrowth, debtToEquity,
  52w-Positionierung

Fail-open: Fehler beim Fetch eines Tickers erzeugen einen ``warn``-Eintrag und
crash nie. ``sleep`` zwischen Abfragen reduziert das yfinance Rate-Limit. Kein
Cache-System (Plan: KEIN Cache).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

try:  # pragma: no cover - Import ist in Tests gemockt
    import yfinance as yf
except Exception:  # noqa: BLE001 - pragma: no cover
    yf = None  # type: ignore[assignment]


@dataclass
class Fundamental:
    """Normierte Fundamentalkennzahlen eines Tickers (qualitative Bänder)."""

    ticker: str
    isin: str | None
    forward_pe: float | None = None
    market_cap_eur: float | None = None
    revenue_growth: float | None = None  # als Dezimal, z.B. 0.12 = +12%
    earnings_growth: float | None = None
    debt_to_equity: float | None = None
    position_52w: float | None = None  # 0..1, wie weit innerhalb der 52w-Range
    error: str | None = None

    @property
    def available(self) -> bool:
        return self.error is None


def _to_float(v: Any) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def position_in_52w(current: float | None, low52: float | None, high52: float | None) -> float | None:
    """Anteil (0..1) des aktuellen Kurses innerhalb der 52-Wochen-Spanne."""
    if current is None or low52 is None or high52 is None or high52 <= low52:
        return None
    return max(0.0, min(1.0, (current - low52) / (high52 - low52)))


def fetch_fundamentals(tickers: list[dict[str, Any]], sleep_seconds: float = 1.0) -> list[Fundamental]:
    """Holt Fundamentaldaten für eine Liste von {ticker, isin}-Dicts. Fail-open.

    ``tickers``: Liste von Dicts mit mind. ``ticker``, optional ``isin``/``name``.
    Ein fehlschlagender Ticker liefert einen Fundamental mit ``error``-Feld,
    nie eine Exception.
    """
    if yf is None:  # pragma: no cover
        return [
            Fundamental(ticker=str(t.get("ticker")), isin=t.get("isin"), error="yfinance nicht verfügbar")
            for t in tickers
        ]

    results: list[Fundamental] = []
    for i, item in enumerate(tickers):
        ticker = str(item.get("ticker") or "").strip()
        if not ticker:
            continue
        if i > 0:
            time.sleep(sleep_seconds)
        try:
            info = yf.Ticker(ticker).info or {}
            results.append(_extract(ticker, item.get("isin"), info))
        except Exception as exc:  # noqa: BLE001 - fail-open
            results.append(
                Fundamental(ticker=ticker, isin=item.get("isin"), error=f"Fetch-Fehler: {exc}")
            )
    return results


def _extract(ticker: str, isin: str | None, info: dict[str, Any]) -> Fundamental:
    try:
        fpe = _to_float(info.get("forwardPE"))
        mcap = _to_float(info.get("marketCap"))
        rev_growth = _to_float(info.get("revenueGrowth"))
        earn_growth = _to_float(info.get("earningsGrowth"))
        dte = _to_float(info.get("debtToEquity"))
        current = _to_float(info.get("currentPrice")) or _to_float(info.get("regularMarketPrice"))
        pos = position_in_52w(
            current,
            _to_float(info.get("fiftyTwoWeekLow")),
            _to_float(info.get("fiftyTwoWeekHigh")),
        )
        return Fundamental(
            ticker=ticker,
            isin=isin,
            forward_pe=fpe,
            market_cap_eur=mcap,
            revenue_growth=rev_growth,
            earnings_growth=earn_growth,
            debt_to_equity=dte,
            position_52w=pos,
        )
    except Exception as exc:  # noqa: BLE001 - fail-open
        return Fundamental(ticker=ticker, isin=isin, error=f"Parse-Fehler: {exc}")


def fetch_eurusd(config_path: str | None = None,
                 default_fallback: float | None = None) -> float | None:
    """Liefert den EURUSD-Kurs (fail-open, config-getrieben).

    Priorität: 1) ``fx.eurusd`` aus config/pipeline.yaml (persönlich, optional),
    2) yfinance ``EURUSD=X``-Quote, 3) ``default_fallback``. Scheitert alles →
    None (Aufrufer schließt Nicht-EUR-Positionen dann ohne Umrechnung aus).
    """
    if config_path is None:
        config_path = "config/pipeline.yaml"
    try:
        from pathlib import Path

        import yaml

        p = Path(config_path)
        if p.exists():
            cfg = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
            fx = cfg.get("fx", {})
            if fx.get("eurusd") is not None:
                return _to_float(fx["eurusd"])
    except Exception:  # noqa: BLE001, S110 - fail-open
        pass

    if yf is not None:
        try:
            info = yf.Ticker("EURUSD=X").fast_info
            if info is not None and getattr(info, "last_price", None):
                return _to_float(info.last_price)
        except Exception:  # noqa: BLE001, S110 - fail-open
            pass

    return default_fallback if default_fallback is not None else None

