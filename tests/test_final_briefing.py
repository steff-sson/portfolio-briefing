"""Fokussierte Tests fuer scripts.final_briefing (deterministischer Final-Renderer).

Phase 5.1: kurzer Output-Contract (identisch zu verify.SHORT_SECTIONS):

    ## Kurzlage
    ## Datenqualität
    ## Sell-/Reduce-Signale (bestehende Satellites)
    ## Watchlist-Signale
    ## Nächster Schritt

Abgedeckt: alle 5 Sektionen in fester Reihenfolge, keine Alt-Sektionen
(keine lange Portfoliotabelle, keine pauschale SELL-Gesamtempfehlung),
Fundamentaldaten-Disclaimer in JEDER Signal-Sektion, max. 3 Signale je
Sektion, leerer Fakten-Fallback. Keine echten API-/Netz-Aufrufe.
"""
from __future__ import annotations

import re

from scripts import final_briefing
from scripts.final_briefing import render_final_briefing

# Kurzer Output-Contract (Phase 5): exakt diese 5 Sektionen, feste Reihenfolge.
SHORT_SECTIONS = [
    "## Kurzlage",
    "## Datenqualität",
    "## Sell-/Reduce-Signale (bestehende Satellites)",
    "## Watchlist-Signale",
    "## Nächster Schritt",
]

# Alt-Sektionen des vorherigen langen Outputs — duerfen im kurzen Format NICHT
# mehr erscheinen (keine lange Portfoliotabelle, keine pauschale Empfehlung).
LEGACY_SECTIONS = [
    "## Entscheidungsrelevante Punkte",
    "## Strategie-Abgleich",
    "## Relevante News & Veränderungen",
    "## Empfehlung",
]


def _signal(
    isin: str,
    name: str,
    signal: str,
    score: int,
    dimensions: dict | None = None,
    reason: str = "Deterministische Begründung",
) -> dict:
    return {
        "isin": isin,
        "name": name,
        "signal": signal,
        "score": score,
        "dimensions": dimensions or {},
        "reason": reason,
        "fundamentals_used": False,
    }


def _valid_facts() -> dict:
    """Faktenpaket mit Signalen in allen Sektionen (deterministic_summary)."""
    return {
        "portfolio": {
            "holdings": [
                {"isin": "US0378331005", "name": "Apple Inc.", "ticker": "AAPL"},
                {"isin": "NL0010273215", "name": "ASML Holding", "ticker": "ASML"},
            ]
        },
        "data_quality": {"status": "ok", "issues": []},
        "deterministic_summary": {
            "red_checks": ["single_position"],
            "yellow_checks": ["drift"],
            "green_checks": ["thesis_deadlines"],
            # 4 Signale -> Renderer darf max. 3 zeigen (Phase 5, kurzer Output).
            "satellite_sell_signals": [
                _signal("US0378331005", "Apple Inc.", "SELL", -3),
                _signal("NL0010273215", "ASML Holding", "REDUCE", -2),
                _signal("DE0007164600", "SAP SE", "REDUCE", -1),
                _signal("IE00BK5BQT80", "Vanguard FTSE All-World", "SELL", -4),
            ],
            "watchlist_signals": [
                _signal(
                    "US5949724083",
                    "NVIDIA Corp.",
                    "BUY",
                    3,
                    dimensions={"strategy_fit": 1, "portfolio_fit": 1, "news_sentiment": 1, "price_development": 0},
                ),
                _signal(
                    "US88579Y1010",
                    "3M Co.",
                    "WATCH",
                    0,
                    dimensions={"strategy_fit": 0, "portfolio_fit": 0, "news_sentiment": 0, "price_development": 0},
                ),
                _signal("US0231351067", "Amazon.com Inc.", "BUY", 4),
                _signal("US5949181045", "Microsoft Corp.", "BUY", 5),
            ],
        },
    }


def _section(text: str, heading: str) -> str:
    """Sektionsinhalt (Ueberschrift bis zur naechsten '## ')."""
    match = re.search(rf"^{re.escape(heading)}$", text, re.MULTILINE)
    assert match is not None, f"Sektion {heading} fehlt"
    nxt = re.search(r"^## ", text[match.end():], re.MULTILINE)
    end = match.end() + nxt.start() if nxt else len(text)
    return text[match.start():end]


class TestShortSections:
    """Kurzer Output-Contract: 5 Sektionen, feste Reihenfolge, keine Alt-Sektionen."""

    def test_all_five_short_sections_present(self):
        text = render_final_briefing(_valid_facts(), mode="monday")
        for section in SHORT_SECTIONS:
            assert section in text

    def test_sections_in_fixed_order(self):
        text = render_final_briefing(_valid_facts(), mode="monday")
        positions = [text.index(s) for s in SHORT_SECTIONS]
        assert positions == sorted(positions)

    def test_legacy_sections_removed(self):
        """Keine lange Portfoliotabelle/keine pauschale SELL-Gesamtempfehlung:
        die Alt-Sektionen des langen Outputs duerfen nicht mehr gerendert werden."""
        text = render_final_briefing(_valid_facts(), mode="monday")
        for section in LEGACY_SECTIONS:
            assert section not in text

    def test_no_markdown_table(self):
        """Keine lange Portfoliotabelle: keine Markdown-Tabellen-Pipes im Output."""
        text = render_final_briefing(_valid_facts(), mode="monday")
        assert "|" not in text

    def test_no_pauschale_seLL_recommendation(self):
        """Kein pauschales SELL-Label als Gesamt-Empfehlung: nur positions-
        bezogene SELL/REDUCE-Signale in der Signal-Sektion."""
        text = render_final_briefing(_valid_facts(), mode="monday")
        # Signal-Sektion enthaelt positionsbezogene SELL-Signale (erlaubt).
        sell_section = _section(text, "## Sell-/Reduce-Signale (bestehende Satellites)")
        assert "SELL" in sell_section
        # Keine eigenstaendige Gesamt-Empfehlungs-Sektion/Label.
        assert "## Empfehlung" not in text

    def test_german_header_with_mode_label(self):
        text = render_final_briefing(_valid_facts(), mode="monday")
        assert text.startswith("# Portfolio-Briefing — Montag\n")
        text_friday = render_final_briefing(_valid_facts(), mode="friday")
        assert text_friday.startswith("# Portfolio-Briefing — Freitag\n")
        text_monthly = render_final_briefing(_valid_facts(), mode="monthly")
        assert text_monthly.startswith("# Portfolio-Briefing — Monatsrückblick\n")


class TestFundamentalsDisclaimer:
    """Fundamentaldaten-Disclaimer ist in JEDER Signal-Sektion sichtbar."""

    def test_disclaimer_in_sell_section(self):
        section = _section(render_final_briefing(_valid_facts()), "## Sell-/Reduce-Signale (bestehende Satellites)")
        assert final_briefing.FUNDAMENTALS_DISCLAIMER in section

    def test_disclaimer_in_watchlist_section(self):
        section = _section(render_final_briefing(_valid_facts()), "## Watchlist-Signale")
        assert final_briefing.FUNDAMENTALS_DISCLAIMER in section

    def test_disclaimer_visible_in_empty_signal_sections(self):
        """Auch ohne Signale bleibt der Disclaimer sichtbar (kein Signal ohne
        Fundamentaldaten-Hinweis)."""
        text = render_final_briefing({}, mode="monday")
        assert text.count(final_briefing.FUNDAMENTALS_DISCLAIMER) == 2

    def test_verify_disclaimer_substring_covered(self):
        """verify.py prueft den Disclaimer per Substring — der gerenderte Text
        muss diesen Substring enthalten (sonst blockt der Versand fail-closed)."""
        from scripts.verify import FUNDAMENTALS_DISCLAIMER as verify_disclaimer

        text = render_final_briefing(_valid_facts(), mode="monday")
        assert verify_disclaimer.lower() in text.lower()


class TestSellReduceSignals:
    def test_max_three_signals_rendered(self):
        section = _section(render_final_briefing(_valid_facts()), "## Sell-/Reduce-Signale (bestehende Satellites)")
        assert section.count("- ") == 3
        # Das 4. Signal (Vanguard) darf nicht erscheinen.
        assert "IE00BK5BQT80" not in section

    def test_signal_line_format(self):
        section = _section(render_final_briefing(_valid_facts()), "## Sell-/Reduce-Signale (bestehende Satellites)")
        assert "- Apple Inc. (US0378331005) — SELL" in section
        assert "- ASML Holding (NL0010273215) — REDUCE" in section

    def test_empty_sell_signals_show_closing_text(self):
        facts = _valid_facts()
        facts["deterministic_summary"]["satellite_sell_signals"] = []
        section = _section(render_final_briefing(facts), "## Sell-/Reduce-Signale (bestehende Satellites)")
        assert "Keine Sell-/Reduce-Signale." in section


class TestWatchlistSignals:
    def test_max_three_signals_rendered(self):
        section = _section(render_final_briefing(_valid_facts()), "## Watchlist-Signale")
        assert section.count("- ") == 3
        # Das 4. Signal (Microsoft) darf nicht erscheinen.
        assert "US5949181045" not in section

    def test_score_and_dimensions_rendered(self):
        section = _section(render_final_briefing(_valid_facts()), "## Watchlist-Signale")
        assert "Score +3 (3+/0-)" in section
        assert "Dimensionen: D1 Strategie-Fit +1, D2 Portfolio-Fit +1" in section

    def test_empty_watchlist_show_closing_text(self):
        facts = _valid_facts()
        facts["deterministic_summary"]["watchlist_signals"] = []
        section = _section(render_final_briefing(facts), "## Watchlist-Signale")
        assert "Keine Watchlist-Signale (NO SIGNAL für alle Positionen)." in section


class TestNaechsterSchritt:
    def test_sell_leads_to_check_action(self):
        section = _section(render_final_briefing(_valid_facts()), "## Nächster Schritt")
        assert "SELL prüfen" in section

    def test_buy_on_watchlist_leads_to_check_action(self):
        facts = _valid_facts()
        facts["deterministic_summary"]["satellite_sell_signals"] = []
        section = _section(render_final_briefing(facts), "## Nächster Schritt")
        assert "Watchlist-Position prüfen" in section

    def test_no_signals_leads_to_no_action(self):
        facts = _valid_facts()
        facts["deterministic_summary"]["satellite_sell_signals"] = []
        facts["deterministic_summary"]["watchlist_signals"] = []
        section = _section(render_final_briefing(facts), "## Nächster Schritt")
        assert "Nächste Woche neuer Lauf, keine Aktion erforderlich." in section


class TestKurzlageDatenqualitaet:
    def test_kurzlage_renders_check_lists(self):
        section = _section(render_final_briefing(_valid_facts()), "## Kurzlage")
        assert "Rote Punkte: Einzelposition." in section
        assert "Gelbe Punkte: Drift." in section
        assert "Grüne Punkte: Thesen-Fristen." in section
        assert "Datenqualität: ok." in section

    def test_datenqualitaet_section_ok(self):
        section = _section(render_final_briefing(_valid_facts()), "## Datenqualität")
        assert "Datenqualität: ok." in section


class TestEmptyFactsFallback:
    def test_empty_package_still_renders_all_short_sections(self):
        text = render_final_briefing({}, mode="monday")
        for section in SHORT_SECTIONS:
            assert section in text

    def test_empty_package_uses_safe_fallbacks(self):
        text = render_final_briefing({})
        assert "Keine Sell-/Reduce-Signale." in text
        assert "Keine Watchlist-Signale (NO SIGNAL für alle Positionen)." in text
        assert "Nächste Woche neuer Lauf, keine Aktion erforderlich." in text
        assert "Datenqualität: ok." in text

    def test_deterministic_across_calls(self):
        text1 = render_final_briefing(_valid_facts(), mode="monday")
        text2 = render_final_briefing(_valid_facts(), mode="monday")
        assert text1 == text2


# --- Regression: echter Mock-Lauf (Renderer + verify, kurzer Contract) ---------
# Renderer aus den echten Mock-Daten (sc_bridge.load_mock + analyze +
# facts.build_facts_package) erzeugen und verify.verify_draft darauf laufen
# lassen. Achtung: verify_draft prueft aktuell noch die ALTEN DRAFT_SECTIONS
# (Phase 5.1 = reiner Renderer/Output; die verify-Anpassung folgt separat).
# Dieser Regressionstest prueft deshalb nur die kurzen Signal-Sektionen +
# Disclaimer-Substring, die verify bereits heute erwartet.


def _mock_facts_package() -> dict:
    """Faktenpaket aus den echten Mock-Daten (wie Dry-Run, ohne Netz)."""
    from scripts import analyze, facts, sc_bridge

    portfolio, transactions = sc_bridge.load_mock()
    strategy = analyze.load_strategy()
    analysis = analyze.analyze_portfolio(portfolio, transactions, strategy)
    return facts.build_facts_package(
        portfolio, transactions, analysis, news=[], strategy=strategy, mode="monday"
    )


class TestRegressionRealMockData:
    def test_render_from_real_mock_has_short_sections_and_disclaimers(self):
        """Echter Mock-Render: 5 kurze Sektionen, keine Alt-Sektionen,
        Disclaimer-Substring in beiden Signal-Sektionen (verify-fest)."""
        from scripts.verify import FUNDAMENTALS_DISCLAIMER as verify_disclaimer

        package = _mock_facts_package()
        text = render_final_briefing(package, mode="monday")
        for section in SHORT_SECTIONS:
            assert section in text
        for section in LEGACY_SECTIONS:
            assert section not in text
        # verify.prueft den Disclaimer per Substring (fail-closed, blockt bei
        # fehlendem Hinweis). Der Renderer-Contract ist hier: der Substring
        # steht in beiden Signal-Sektionen des echten Mock-Renders. (Die
        # verify_draft-Anpassung auf die kurzen Sektionen folgt separat —
        # siehe Modul-Docstring.)
        assert verify_disclaimer.lower() in text.lower()
        for section in ("## Sell-/Reduce-Signale (bestehende Satellites)", "## Watchlist-Signale"):
            assert verify_disclaimer.lower() in _section(text, section).lower()

    def test_render_from_real_mock_no_pipe_table_no_legacy(self):
        """Keine lange Portfoliotabelle (keine '|'-Pipes), keine Alt-Sektionen."""
        package = _mock_facts_package()
        text = render_final_briefing(package, mode="monday")
        assert "|" not in text
        for section in LEGACY_SECTIONS:
            assert section not in text
