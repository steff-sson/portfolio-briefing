"""Tests für fundamentals.py — Parsing (gemockt), 52w, fail-open."""
from __future__ import annotations

import pytest

from scripts import fundamentals


def test_extract_parses_bands():
    info = {
        "forwardPE": 25.0,
        "marketCap": 250_000_000_000.0,
        "revenueGrowth": 0.12,
        "earningsGrowth": 0.05,
        "debtToEquity": 0.8,
        "currentPrice": 110.0,
        "fiftyTwoWeekLow": 100.0,
        "fiftyTwoWeekHigh": 120.0,
    }
    f = fundamentals._extract("AAPL", "US0378331005", info)
    assert f.available
    assert f.forward_pe == 25.0
    assert f.revenue_growth == 0.12
    assert f.position_52w == pytest.approx(0.5)  # (110-100)/(120-100)


def test_extract_fail_on_bad_value():
    f = fundamentals._extract("X", None, {"forwardPE": "nonsense"})
    assert f.available


def test_position_in_52w_bounds():
    assert fundamentals.position_in_52w(110.0, 100.0, 120.0) == pytest.approx(0.5)
    assert fundamentals.position_in_52w(95.0, 100.0, 120.0) == 0.0  # clamped
    assert fundamentals.position_in_52w(130.0, 100.0, 120.0) == 1.0
    assert fundamentals.position_in_52w(None, 100.0, 120.0) is None
    assert fundamentals.position_in_52w(110.0, 100.0, 100.0) is None  # zero range


class _FakeYf:
    """Simuliert yfinance: Ticker X wirft, Ticker AAPL liefert Info."""

    def __init__(self):
        self._info = {
            "AAPL": {"forwardPE": 30.0, "position_52w": None},
        }

    def Ticker(self, symbol):
        if symbol == "AAPL":
            return _FakeTicker(self._info["AAPL"])
        raise RuntimeError("rate-limited")


class _FakeTicker:
    def __init__(self, info):
        self._info = info

    @property
    def info(self):
        return self._info


def test_fetch_fundamentals_fail_open(monkeypatch):
    monkeypatch.setattr(fundamentals, "yf", _FakeYf())
    res = fundamentals.fetch_fundamentals(
        [{"ticker": "AAPL"}, {"ticker": "ASML"}], sleep_seconds=0
    )
    by_ticker = {f.ticker: f for f in res}
    assert by_ticker["AAPL"].available
    assert by_ticker["ASML"].error is not None  # fail-open, kein Crash


def test_fetch_eurusd_from_config(tmp_path):
    p = tmp_path / "pipeline.yaml"
    p.write_text("fx:\n  eurusd: 1.09\n", encoding="utf-8")
    assert fundamentals.fetch_eurusd(str(p)) == pytest.approx(1.09)


def test_fetch_eurusd_fallback_when_no_config(monkeypatch):
    # Kein config-File, yf ohne EURUSD → Fallback-Wert.
    monkeypatch.setattr(fundamentals, "yf", None)
    assert fundamentals.fetch_eurusd("/nonexistent/pipeline.yaml", default_fallback=1.05) == pytest.approx(
        1.05
    )
    assert fundamentals.fetch_eurusd("/nonexistent/pipeline.yaml") is None
