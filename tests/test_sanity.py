"""Tests für sanity.py — Output-Referenzen müssen im Input existieren."""
from __future__ import annotations

from scripts import sanity

DATA = {
    "holdings": [
        {"isin": "IE00BK5BQT80", "ticker": None, "value_eur": 5000.0},
        {"isin": "US0378331005", "ticker": "AAPL", "value_eur": 600.0},
    ],
    "watchlist": [{"isin": "US5949724083", "ticker": "NVDA"}],
    "fundamentals": [{"ticker": "AAPL", "forward_pe": 30.0, "position_52w": 0.5}],
    "signals": [{"severity": "red", "message": "warn", "pct": 5.0}],
}


def test_known_isins():
    isins = sanity.known_isins(DATA)
    assert "US0378331005" in isins


def test_known_tickers():
    tickers = sanity.known_tickers(DATA)
    assert "AAPL" in tickers and "NVDA" in tickers


def test_unknown_isin_flagged():
    issues = sanity.check_sanity("Kaufe US1234567890 und HALT", DATA)
    assert any("US1234567890" in i for i in issues)


def test_unknown_allcaps_ticker_flagged():
    issues = sanity.check_sanity("TSLA überbewertet", DATA)
    assert any("TSLA" in i for i in issues)


def test_prose_not_flagged_as_ticker():
    # Deutsche Prosa (gemischt/großgeschrieben am Satzanfang) → kein Ticker.
    issues = sanity.check_sanity("Keine Aktion nötig.", DATA)
    assert not any("Unbekannter Ticker" in i for i in issues)


def test_percent_must_have_input_basis():
    # "5.0" existiert in signal-Message → ok; "99" existiert nicht → Verletzung.
    ok = sanity.check_sanity("Anteil 5.0%", DATA)
    assert not any("ohne Input-Basis" in i and "5.0" in i for i in ok)
    bad = sanity.check_sanity("Anteil 99.5%", DATA)
    assert any("ohne Input-Basis" in i for i in bad)


def test_eur_amount_must_match_input():
    # 5.000 EUR existiert als value_eur (5000.0) im Input → ok.
    ok = sanity.check_sanity("Wert ca. 5.000 EUR", DATA)
    assert not any("ohne Input-Basis" in i for i in ok)


def test_hallucinated_amount_flagged():
    # 1.234 EUR existiert nicht im Input → Verletzung.
    bad = sanity.check_sanity("Wert ca. 1.234 EUR", DATA)
    assert any("ohne Input-Basis" in i for i in bad)


def test_quantity_must_match_input():
    # 600 existiert als value_eur (600.0) → ok (deutscher Dezimaltrenner 600,0 nicht im Input).
    ok = sanity.check_sanity("Menge 600", DATA)
    assert not any("ohne Input-Basis" in i and "600" in i for i in ok)


def test_number_prose_without_numbers_ok():
    # Prosa ohne Zahlen bleibt ohne Prüfung.
    assert sanity.check_sanity("Keine Aktion nötig.", DATA) == []
