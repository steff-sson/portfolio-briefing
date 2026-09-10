"""Regressionstests fuer das deterministische Faktenbriefing (Phase D, Laiensicht).

Fokus: reiner Text (keine Markdown-Tabellen/Sternchen/Emoji), einheitliche
Ampel-Labels ([GRÜN]/[GELB]/[ROT]) am Zeilenanfang, Gesamtstatus in der ersten
Kurzlage-Zeile, Leerzeilen zwischen Stichpunkten, Kuerzung (Positionen nur als
Name + Anteil + Status, keine ISIN-Flut), relativierte Sektor-Anteile
("% der Satellite-Positionen") mit Datenluecken-Hinweis, Anlagethesen-Klarheit,
genau EIN Fundamentaldaten-Disclaimer und die beibehaltene
Option-2-Watchlist-Beobachtung. Kein LLM-Call, keine Secrets.
"""
from __future__ import annotations

import re

from scripts import fallback_briefing, verify


def _package() -> dict:
    """Realistisches Faktenpaket mit Core/Satellite-Position und Unknown-Sektor."""
    return {
        "portfolio": {
            "holdings": [
                {"isin": "IE00BKM4GZ66", "name": "Welt Core", "category": "core"},
                {"isin": "US67066G1040", "name": "NVIDIA", "category": "satellite"},
            ]
        },
        "deterministic_summary": {
            "total_value_eur": 10000.0,
            "position_count": 2,
            "core_ratio": 0.881,
            "satellite_ratio": 0.119,
            "traffic_lights": {
                "core_satellite": {"status": "red", "reason": "Core-Anteil 88.1% weicht vom Ziel 70.0% ab."},
                "sector_concentration": {"status": "red", "reason": "Strongster Sektor 100.0% der Satellite-Positionen."},
                "single_position": {"status": "yellow", "reason": "NVIDIA zwischen Ziel und Maximum."},
                "thesis_deadlines": {"status": "green", "reason": "Keine abgelaufenen Thesen."},
                "turnover": {"status": "green", "reason": "Umschlag unauffaellig."},
                "trades_per_quarter": {"status": "green", "reason": "Trades unter dem Limit."},
                "data_quality": {"status": "green", "reason": "Datenqualität ok."},
            },
            "recommendation": {"label": "WATCH", "reason": "2 rot, 4 grün von 7 Kategorien — Gesamt-Empfehlung WATCH."},
            "positions_detail": [
                {
                    "name": "Welt Core", "isin": "IE00BKM4GZ66", "category": "core",
                    "value_eur": 8810.0, "weight": 0.881, "limit_pct": None, "status": "ok",
                },
                {
                    "name": "NVIDIA", "isin": "US67066G1040", "category": "satellite",
                    "value_eur": 1190.0, "weight": 0.119, "limit_pct": 10.0, "status": "gelb",
                },
            ],
            "sectors_detail": [
                {
                    "name": "Unknown", "value_eur": 1190.0, "ratio": 1.0,
                    "scope": "satellite", "limit_pct": 20.0, "status": "red",
                },
            ],
            "satellite_sell_signals": [],
            "watchlist_signals": [],
            "position_actions": [],
            "data_quality_status": "ok",
            "data_quality_issues": [],
        },
        "strategy_thresholds_pct": {
            "core_pct": 70.0,
            "satellite_pct": 30.0,
            "threshold_pct": 5.0,
            "max_position_pct": 10.0,
            "max_sector_pct": 20.0,
            "max_turnover_annual_pct": 30.0,
        },
        "data_quality": {"status": "ok", "issues": []},
    }


_EMOJI_RE = re.compile("[\U0001F000-\U0001FAFF\u2600-\u26FF\u2700-\u27BF\u2B00-\u2BFF\uFE0F]")


def test_fallback_is_plain_text_without_markdown_markers():
    """Der Fallback traegt keine Tabellen/Markdown-/Emoji-Marker, behaelt aber
    die 6 Pflicht-Ueberschriften."""
    text = fallback_briefing.build_fallback_briefing(_package())
    assert "|" not in text
    assert "**" not in text
    assert not re.search(r"^\s*\*\s", text, re.MULTILINE)
    assert "---" not in text
    assert not _EMOJI_RE.search(text)
    for section in verify.DRAFT_SECTIONS:
        assert re.search(rf"^{re.escape(section)}$", text, re.MULTILINE)


def test_fallback_starts_with_overall_ampel_status():
    """Erste Zeile der Kurzlage = Gesamtstatus als Ampel-Label; danach der
    Fallback-Marker. Vor den Aufzaehlungspunkten steht das Ein-Satz-Fazit."""
    text = fallback_briefing.build_fallback_briefing(_package())
    kurzlage = verify._extract_section(text, "## Kurzlage")
    assert kurzlage is not None
    body_lines = [line for line in (verify._section_content(kurzlage) or "").splitlines() if line.strip()]
    assert body_lines[0].startswith("[GELB] Gesamturteil: WATCH.")
    assert body_lines[1].startswith(fallback_briefing.FALLBACK_MARKER)


def test_fallback_ampel_labels_are_consistent_and_ok_is_green():
    """Ampel-Labels [GRÜN]/[GELB]/[ROT] stehen am Zeilenanfang; Status 'ok'
    wird [GRÜN] (kein Wort 'ok'). Gesamtstatus prominent in Zeile 1."""
    text = fallback_briefing.build_fallback_briefing(_package())
    assert "[GRÜN]" in text
    assert "[GELB]" in text
    assert "[ROT]" in text
    # Position mit Status "ok" -> [GRÜN], nicht das Wort "ok".
    assert "[GRÜN] Welt Core: 88.1% des Gesamtportfolios." in text
    assert "Status ok" not in text
    # Datenqualitaet "ok" -> [GRÜN] "in Ordnung".
    assert "[GRÜN] Datenqualität: in Ordnung." in text
    # Ampelzeile in der Kurzlage nennt kein "ok".
    assert "[GRÜN] Datenqualität unauffällig." in text
    # Gesamtstatus-Label in der ersten Kurzlage-Zeile.
    assert "[GELB] Gesamturteil: WATCH." in text


def test_fallback_has_blank_lines_between_bullets_and_after_headers():
    """Leerzeilen zwischen allen Stichpunkten und direkt nach jeder
    Sektions-Überschrift (Laiensicht-Lesbarkeit)."""
    text = fallback_briefing.build_fallback_briefing(_package())
    assert "\n\n[ROT] Core-/Satelliten-Aufteilung:" in text
    assert "\n\n[GELB] NVIDIA: 11.9% des Gesamtportfolios." in text
    assert "\n\n[GRÜN] Welt Core: 88.1% des Gesamtportfolios." in text
    for section in verify.DRAFT_SECTIONS:
        assert f"{section}\n\n" in text, f"keine Leerzeile nach {section}"


def test_fallback_positions_have_no_isin_flood():
    """Kurzform: Positionen nennen nur Name + Anteil + Status — keine
    vollstaendige ISIN-Liste im Fliesstext."""
    text = fallback_briefing.build_fallback_briefing(_package())
    assert "IE00BKM4GZ66" not in text
    assert "US67066G1040" not in text


def test_fallback_satellite_sector_is_relativized_to_satellite_scope():
    """'Unknown 100%' erscheint als Anteil der Satellite-Positionen (mit
    Datenluecken-Hinweis), nicht als Gesamtportfolio-Konzentration; die
    Limit-Zeile heisst 'Max. Anteil' (kein 'Limit')."""
    text = fallback_briefing.build_fallback_briefing(_package())
    assert "100.0% der Satellite-Positionen" in text
    assert "Anteil am Satellite-Umfang" in text
    assert "Max. Anteil" in text
    assert "Datenlücke" in text
    assert "keine echte Übergewichtung" in text
    assert "| Limit |" not in text


def test_fallback_sector_unknown_is_data_gap_not_concentration():
    """Sektor 'Unknown' wird als Datenlücke erklärt, nicht als echte
    Sektor-Konzentration (kein falscher Alarm)."""
    text = fallback_briefing.build_fallback_briefing(_package())
    assert "[ROT] Satellite-Sektor Unknown: 100.0% der Satellite-Positionen." in text
    assert "keine Sektordaten hinterlegt" in text
    assert "Datenlücke, keine echte Übergewichtung" in text


def test_fallback_explains_expired_theses():
    """Anlagethesen-Klarheit: Eine rote Thesen-Ampel erklaert laienverständlich,
    was 'abgelaufene These' heißt."""
    package = _package()
    package["deterministic_summary"]["traffic_lights"]["thesis_deadlines"] = {
        "status": "red",
        "reason": "Abgelaufene Thesen: nvidia-thesis.md.",
    }
    package["deterministic_summary"]["outdated_theses"] = [
        {"file": "nvidia-thesis.md", "created": "2025-01-01"}
    ]
    text = fallback_briefing.build_fallback_briefing(package)
    assert "[ROT] Thesen-Fristen:" in text
    assert "Abgelaufene These heißt:" in text
    assert "geprüft oder erneuert" in text


def test_fallback_recommendation_is_label_plus_one_sentence():
    """Empfehlungs-Herleitung gestrichen: nur Label + ein Satz."""
    text = fallback_briefing.build_fallback_briefing(_package())
    empfehlung = verify._section_content(
        verify._extract_section(text, "## Empfehlung")
    ) or ""
    assert empfehlung.count("WATCH") == 1
    assert "[GELB] WATCH —" in empfehlung
    assert "Kategorien" not in empfehlung  # keine Herleitung
    # Die Herleitung steht nur EINMAL (Kurzlage), nicht erneut in der Empfehlung.
    assert text.count("2 rot, 4 grün von 7 Kategorien") == 1


def test_fallback_contains_disclaimer_exactly_once():
    """Der Fundamentaldaten-Disclaimer steht genau einmal (kein Duplikat)."""
    text = fallback_briefing.build_fallback_briefing(_package())
    assert text.count(verify.FUNDAMENTALS_DISCLAIMER) == 1


def test_fallback_has_no_generic_counterargument_line():
    """Keine unverstaendliche generische 'Gegenargument'-Zeile."""
    text = fallback_briefing.build_fallback_briefing(_package())
    assert "gegenargument" not in text.lower()


def test_fallback_passes_own_verify_gate():
    """Der reine-Text-Fallback besteht verify_draft + final_gate (fail-closed
    bleibt intakt)."""
    package = _package()
    text = fallback_briefing.build_fallback_briefing(package)
    findings = verify.verify_draft(package, text)
    assert findings == []
    assert verify.final_gate(findings).allow_send is True


def _package_with_watchlist() -> dict:
    """Paket mit einem nicht-leeren Watchlist-Signal (Option-2-Sektion)."""
    package = _package()
    package["deterministic_summary"]["watchlist_signals"] = [
        {
            "signal": "WATCH",
            "isin": "IE00B1XNHC34",
            "name": "Global Clean Energy",
            "score": 0,
            "reason": "Strategie-Fit neutral, Portfolio-Fit positiv.",
        }
    ]
    return package


def test_fallback_option2_watchlist_observation_and_no_forecast():
    """Option 2: Der Fallback listet Watchlist-Kandidaten als Beobachtung/
    Review und macht ausdruecklich keine Kauf-Empfehlung/Gewinn-Prognose."""
    package = _package_with_watchlist()
    text = fallback_briefing.build_fallback_briefing(package)
    watchlist = verify._section_content(
        verify._extract_section(text, verify.WATCHLIST_SIGNALS_SECTION)
    ) or ""
    assert "Beobachtung/Review" in watchlist
    assert "keine Kauf-Empfehlung" in watchlist
    assert "keine Gewinn-Prognose" in watchlist
    assert "Fundamentaldaten (Umsatz, Gewinn, Bewertung)" in watchlist
    # Option-2-Gate: keine positive Prognose-/Versprechens-Formulierung.
    assert verify._forecast_promise_violations(text) == []
    # Reiner Text bleibt gewahrt (keine Tabellen/Sternchen/Emoji).
    assert "|" not in text
    assert "**" not in text
    assert not _EMOJI_RE.search(text)


def test_fallback_with_watchlist_passes_own_verify_gate():
    """Das Option-2-erweiterte Fallback besteht verify_draft + final_gate."""
    package = _package_with_watchlist()
    text = fallback_briefing.build_fallback_briefing(package)
    findings = verify.verify_draft(package, text)
    assert findings == []
    assert verify.final_gate(findings).allow_send is True


def test_fallback_next_step_does_not_repeat_action_signal():
    """Redundanz-Reduktion: eine Position, die im Naechster-Schritt bereits als
    Positionsvorschlag steht, wird nicht zusaetzlich als Signal-Zeile wiederholt."""
    package = _package()
    package["deterministic_summary"]["position_actions"] = [
        {"action": "reduzieren", "isin": "US67066G1040", "name": "NVIDIA", "category": "akut", "reason": "Über dem Ziel."}
    ]
    package["deterministic_summary"]["satellite_sell_signals"] = [
        {"signal": "REDUCE", "isin": "US67066G1040", "name": "NVIDIA", "score": -1, "reason": "Über dem Ziel."}
    ]
    text = fallback_briefing.build_fallback_briefing(package)
    naechster = verify._section_content(verify._extract_section(text, "## Nächster Schritt")) or ""
    assert "Reduzieren NVIDIA (US67066G1040)" in naechster
    assert "Signal adressieren" not in naechster  # kein Duplikat derselben Position

