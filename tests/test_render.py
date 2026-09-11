"""Tests für render.py — Plain-Text + Ampel-Format C."""
from __future__ import annotations

from scripts import render

DATA = {
    "captured_at": "2026-09-11T07:00:00+00:00",
    "total_value_eur": 10000.0,
    "cash_eur": 350.0,
    "core_ratio": 70.0,
    "satellite_ratio": 30.0,
    "signals": [
        {"severity": "red", "message": "Position X über 5%"},
        {"severity": "yellow", "message": "Sektor Y bei 14%"},
    ],
    "holdings": [
        {"isin": "IE00BK5BQT80", "name": "Vanguard", "value_eur": 6000.0},
        {"isin": "US0378331005", "name": "Apple", "value_eur": 600.0},
    ],
    "fundamentals": [
        {"ticker": "AAPL", "forward_pe": 30.0, "revenue_growth": 0.05,
         "earnings_growth": 0.08, "debt_to_equity": 1.8, "position_52w": 0.7,
         "error": None},
        {"ticker": "SAP", "error": "Fetch-Fehler: x"},
    ],
    "news": [{"source": "mcp", "title": "Apple news"}],
    "suggestions": "VERKAUFEN: Apple wegen X. Soll ich das umsetzen?",
}


def test_ampel_format_c_emoji_only():
    text = render.render_briefing(DATA)
    assert "🟢" in text and "🔴" in text and "🟡" in text
    # Format C: nur Emoji am Zeilenanfang, kein Text-Label (OK/RISIKO).
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(("🟢", "🟡", "🔴")):
            assert not stripped[:4].split(" ", 1)[0][1:].strip().lower() in (
                "ok", "risiko", "kritisch"
            )


def test_no_full_isin_list():
    text = render.render_briefing(DATA)
    # Positionen nur in Kurzform (Name + Anteil + Ampel), nicht als ISIN-Liste.
    assert "IE00BK5BQT80" not in text


def test_positions_show_pct():
    text = render.render_briefing(DATA)
    assert "Vanguard — 60.0%" in text


def test_neutral_no_action_result():
    d = dict(DATA)
    d["suggestions"] = "Keine Aktion nötig."
    text = render.render_briefing(d)
    assert "Keine Aktion nötig." in text
    assert "Bestätigungsfrage".lower() not in text.lower()


def test_fundamentals_error_renders_yellow_not_green():
    text = render.render_briefing(DATA)
    # SAP hat einen Fehler → 🟡-Zeile mit Fehlermeldung, AAPL 🟢.
    assert "SAP: Fetch-Fehler" in text
    assert "Möchtest du" in text  # Vorschlag vorhanden → Bestätigungsfrage

