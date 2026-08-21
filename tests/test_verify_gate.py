"""Unit tests for scripts.verify: verify_draft (Stage 3) + final_gate (Stage 6).

Kritische/major Findings blockieren den Versand, info/minor nicht.
Malformed Reviews werden fail-closed geblockt. Keine echten API-Aufrufe.
"""
from __future__ import annotations

import copy

import pytest

from scripts import verify
from scripts.llm_briefing import LLMError

VALID_DRAFT = (
    "## Kurzlage\n"
    "Apple (AAPL, US0378331005) bei 24.8%.\n\n"
    "## Datenqualität\n"
    "Datenqualität: ok\n\n"
    "## Entscheidungsrelevante Punkte\n"
    "keine entscheidungsrelevanten Punkte\n\n"
    "## Strategie-Abgleich\n"
    "Core-Ziel 75%, Sektor-Max 15%.\n\n"
    "## Relevante News & Veränderungen\n"
    "—\n\n"
    "## Sell-/Reduce-Signale (bestehende Satellites)\n"
    "Keine Sell-/Reduce-Signale.\n\n"
    "Hinweis: Fundamentaldaten (Umsatz, Gewinn, Cashflow, Verschuldung, Bewertung) nicht automatisch verfügbar und fließen nicht in das Signal ein. Signale basieren ausschließlich auf Strategie-Fit, Portfolio-Fit, 7-Tage-RSS-News und sc-Kursen.\n\n"
    "## Watchlist-Signale\n"
    "Keine Watchlist-Signale.\n\n"
    "Hinweis: Fundamentaldaten (Umsatz, Gewinn, Cashflow, Verschuldung, Bewertung) nicht automatisch verfügbar und fließen nicht in das Signal ein. Signale basieren ausschließlich auf Strategie-Fit, Portfolio-Fit, 7-Tage-RSS-News und sc-Kursen.\n\n"
    "## Nächster Schritt\n"
    "Nächste Woche neuer Lauf, keine Aktion erforderlich.\n\n"
    "## Empfehlung\n"
    "WATCH — 1 rot, 5 grün von 7 Kategorien."
)

VALID_FACTS = {
    "portfolio": {
        "holdings": [
            {"ticker": "AAPL", "isin": "US0378331005", "name": "Apple Inc."},
            {"ticker": "ASML", "isin": "NL0010273215", "name": "ASML"},
        ]
    },
    "news": [],
    "deterministic_summary": {
        "core_ratio": 0.2857,
        "max_position_weight": 0.2479,
        "max_sector_ratio": 0.564,
        "drift": 0.0643,
        "turnover_ratio": 0.0,
        "traffic_lights": {
            "core_satellite": {"status": "green", "reason": "Core-Ratio ok."},
            "sector_concentration": {"status": "red", "reason": "Sektor über Grenze."},
            "single_position": {"status": "yellow", "reason": "Einzelposition über Ziel."},
            "thesis_deadlines": {"status": "green", "reason": "Keine abgelaufenen Thesen."},
            "turnover": {"status": "green", "reason": "Umschlag ok."},
            "trades_per_quarter": {"status": "green", "reason": "Trades unter Limit."},
            "data_quality": {"status": "green", "reason": "Datenqualität: ok."},
        },
        "recommendation": {"label": "WATCH", "reason": "1 rot, 5 grün von 7 Kategorien — WATCH."},
        "position_actions": [],
    },
    "strategy_thresholds_pct": {
        "core_pct": 75.0,
        "satellite_pct": 25.0,
        "threshold_pct": 5.0,
        "max_position_pct": 5.0,
        "max_sector_pct": 15.0,
        "max_turnover_annual_pct": 30.0,
    },
}


def _finding(severity: str, issue: str = "x") -> dict:
    return {"severity": severity, "issue": issue, "evidence": "e", "correction": "c"}


def _facts_with_status_checks(red: list | None = None, yellow: list | None = None) -> dict:
    """VALID_FACTS + deterministische red/yellow_checks fuer die STATUS-REGELN-Checks."""
    facts = copy.deepcopy(VALID_FACTS)
    facts["deterministic_summary"]["red_checks"] = list(red or [])
    facts["deterministic_summary"]["yellow_checks"] = list(yellow or [])
    return facts


def _draft_with_phrase(phrase: str = "Ruhige Woche. Alle Grenzen eingehalten.") -> str:
    """Konformitaetsphrase in die KURZLAGE-Sektion einfuegen (Plan Phase 2.6)."""
    return VALID_DRAFT.replace(
        "Apple (AAPL, US0378331005) bei 24.8%.",
        f"Apple (AAPL, US0378331005) bei 24.8%. {phrase}",
    )


class TestVerifyDraft:
    def test_valid_draft_returns_no_findings(self):
        assert verify.verify_draft(VALID_FACTS, VALID_DRAFT) == []

    def test_missing_section_is_critical(self):
        draft = "## Kurzlage\nOK\n\n## Datenqualität\n—"
        findings = verify.verify_draft(VALID_FACTS, draft)
        assert findings
        assert all(f["severity"] == "critical" for f in findings)
        assert any("Sell-/Reduce-Signale" in f["issue"] for f in findings)

    @pytest.mark.parametrize(
        "bad_heading",
        [
            "## Kurzlage: Zusammenfassung",  # Doppelpunkt
            "## **Kurzlage**",  # Fettdruck
            "## Kurzlage (Montag)",  # Inline-Zusatz auf der Ueberschriftszeile
            "# Kurzlage",  # falsche Ebene
        ],
    )
    def test_section_heading_variant_is_missing(self, bad_heading):
        """Nur eine exakte Zeile '## <Sektion>' zaehlt — Varianten sind fehlend."""
        draft = VALID_DRAFT.replace("## Kurzlage", bad_heading)
        findings = verify.verify_draft(VALID_FACTS, draft)
        assert any(f["severity"] == "critical" and "Kurzlage" in f["issue"] for f in findings)

    def test_section_heading_mid_text_is_missing(self):
        """Ueberschrift inline im Fliesstext (nicht als eigene Zeile) zaehlt nicht."""
        draft = VALID_DRAFT.replace("## Kurzlage\n", "Die Kurzlage sagt: ")
        findings = verify.verify_draft(VALID_FACTS, draft)
        assert any(f["severity"] == "critical" and "Kurzlage" in f["issue"] for f in findings)

    def test_hallucinated_number_is_critical(self):
        draft = VALID_DRAFT.replace("24.8%", "88.8%")
        findings = verify.verify_draft(VALID_FACTS, draft)
        assert findings
        assert any(f["severity"] == "critical" and "88.8" in f["issue"] for f in findings)

    def test_strategy_threshold_reference_passes(self):
        """Echte Strategie-Grenzwerte (75/5/15%) sind erlaubt, keine Findings."""
        draft = VALID_DRAFT.replace(
            "Apple (AAPL, US0378331005) bei 24.8%.",
            "Apple (AAPL, US0378331005) bei 24.8%. Core-Ziel 75%, Satellite-Max 5%, Sektor-Max 15%.",
        )
        assert verify.verify_draft(VALID_FACTS, draft) == []

    def test_unknown_number_stays_critical_with_thresholds(self):
        """Unbekannte Zahl bleibt critical, auch wenn Strategie-Grenzwerte erlaubt sind."""
        draft = VALID_DRAFT.replace("24.8%", "88.8%")
        findings = verify.verify_draft(VALID_FACTS, draft)
        assert any(f["severity"] == "critical" and "88.8" in f["issue"] for f in findings)

    def test_empty_thresholds_stay_fail_closed(self):
        """Kein strategy_thresholds_pct -> Allowlist unveraendert, unbekannte Zahl bleibt critical."""
        facts = copy.deepcopy(VALID_FACTS)
        facts["strategy_thresholds_pct"] = {}
        draft = VALID_DRAFT.replace("24.8%", "88.8%")
        findings = verify.verify_draft(facts, draft)
        assert any(f["severity"] == "critical" and "88.8" in f["issue"] for f in findings)

    def test_number_matching_allowed_value_exactly_passes(self):
        # 24.8 == max_position_weight 0.2479 -> 24.8% (1:1 auf 1 Dezimalstelle)
        assert verify.verify_draft(VALID_FACTS, VALID_DRAFT) == []

    def test_number_within_old_tolerance_but_not_matching_is_critical(self):
        """Post-Live-Fix P0.3: 25.2% lag frueher innerhalb der ±0.5pp-Toleranz
        zu 24.8% — jetzt zwingend 1:1-Match auf 1 Dezimalstelle -> critical."""
        draft = VALID_DRAFT.replace("24.8%", "25.2%")
        findings = verify.verify_draft(VALID_FACTS, draft)
        assert any(f["severity"] == "critical" and "25.2" in f["issue"] for f in findings)

    def test_extra_precision_number_is_normalized(self):
        """24.79% (deterministic_summary exakt) wird auf die 1-Dezimal-
        Schreibweise der ZULÄSSIGE-ZAHLEN-Liste normalisiert -> pass."""
        draft = VALID_DRAFT.replace("24.8%", "24.79%")
        assert verify.verify_draft(VALID_FACTS, draft) == []

    def test_comma_decimal_percentage_is_checked(self):
        """P0.3: Komma-Dezimalen werden erkannt — korrekter Wert passiert,
        halluzinierter Wert (88,8%) bleibt critical (kein Umgehen der Pruefung)."""
        draft = VALID_DRAFT.replace("24.8%", "24,8%")
        assert verify.verify_draft(VALID_FACTS, draft) == []
        draft = VALID_DRAFT.replace("24.8%", "88,8%")
        findings = verify.verify_draft(VALID_FACTS, draft)
        assert any(f["severity"] == "critical" and "88.8" in f["issue"] for f in findings)

    def test_unknown_ticker_is_major(self):
        draft = VALID_DRAFT.replace("AAPL", "MSFT")
        findings = verify.verify_draft(VALID_FACTS, draft)
        assert any(f["severity"] == "major" and "MSFT" in f["issue"] for f in findings)

    def test_unknown_isin_is_major(self):
        draft = VALID_DRAFT.replace("US0378331005", "US0000000000")
        findings = verify.verify_draft(VALID_FACTS, draft)
        assert any(f["severity"] == "major" and "US0000000000" in f["issue"] for f in findings)

    def test_se_and_isin_tokens_are_not_tickers(self):
        """Regression: 'SE' (Rechtsform) und 'ISIN' (Bezeichner) sind keine Ticker —
        'Apple SE (AAPL, ISIN US0378331005)' bleibt ohne Ticker-Finding."""
        draft = VALID_DRAFT.replace(
            "Apple (AAPL, US0378331005)",
            "Apple SE (AAPL, ISIN US0378331005)",
        )
        assert verify.verify_draft(VALID_FACTS, draft) == []

    @pytest.mark.parametrize(
        "token",
        ["SE", "ISIN", "WKN", "AG", "KG", "SA", "NV", "BV", "PLC", "LTD", "INC", "CORP", "CO"],
    )
    def test_financial_legal_form_token_is_not_a_ticker(self, token):
        """Alle Token der erweiterten Nicht-Ticker-Blocklist erzeugen kein Ticker-Finding."""
        draft = VALID_DRAFT.replace(
            "Apple (AAPL, US0378331005) bei 24.8%.",
            f"Apple (AAPL) bei 24.8%. {token} ist kein Ticker.",
        )
        findings = verify.verify_draft(VALID_FACTS, draft)
        assert not any(f["issue"].startswith("Ticker/ISIN") for f in findings)

    def test_signal_and_legacy_labels_are_not_tickers(self):
        """Regression: Gerenderte Signal-/Legacy-Texte enthalten die Nicht-
        Ticker-Woerter AVOID und REDUCE (Signal-Labels, final_briefing.
        _SIGNAL_LABELS) sowie SUSE (Legacy-Holdingname) — sie duerfen keine
        falschen Ticker/ISIN-Findings erzeugen."""
        facts = copy.deepcopy(VALID_FACTS)
        facts["portfolio"] = {
            "holdings": [
                {"ticker": "AAPL", "isin": "US0378331005", "name": "Apple Inc."},
                {"isin": "LU2722255754", "name": "SUSE"},
            ]
        }
        draft = VALID_DRAFT.replace(
            "Keine Sell-/Reduce-Signale.",
            "SUSE (LU2722255754) — REDUCE: Reduktionsbedarf wegen Illiquidität.",
        ).replace(
            "Keine Watchlist-Signale.",
            "Neue Kandidatin AVOID: SUSE bleibt Legacy-Bestand, kein Neukauf.",
        )
        findings = verify.verify_draft(facts, draft)
        assert not any(f["issue"].startswith("Ticker/ISIN") for f in findings)

    def test_real_tickers_still_detected(self):
        """Regression: Echte Ticker bleiben erkannt — AAPL/ASML im Portfolio ohne
        Finding, MSFT (nicht im Portfolio) weiterhin major, auch mit Rechtsform-Token."""
        draft = VALID_DRAFT.replace(
            "Apple (AAPL, US0378331005) bei 24.8%.",
            "Apple Inc. (AAPL, US0378331005) bei 24.8%.",
        )
        assert verify.verify_draft(VALID_FACTS, draft) == []
        draft = VALID_DRAFT.replace(
            "Apple (AAPL, US0378331005) bei 24.8%.",
            "Microsoft Corp. (MSFT) bei 24.8%.",
        )
        findings = verify.verify_draft(VALID_FACTS, draft)
        assert any(f["severity"] == "major" and "MSFT" in f["issue"] for f in findings)
        assert not any("CORP" in f["issue"] for f in findings)

    # --- Live-Fehler: Holding-Namen-Tokens (SRI/IMI/ADR) ohne Ticker-Feld -----
    # Holdings stehen im Portfolio nur über ISINs (kein Ticker-Feld). Echte
    # Namens-Bestandteile wie 'SRI'/'IMI'/'ADR' sind keine erfundenen Ticker —
    # sie werden über die Portfolio-ISIN der zugehörigen Holding freigegeben.

    def _holding_only_facts(self) -> dict:
        """Faktenpaket mit Holdings NUR über ISIN (kein Ticker-Feld), wie im
        Live-Portfolio (config/snapshot.current.json)."""
        facts = copy.deepcopy(VALID_FACTS)
        facts["portfolio"] = {
            "holdings": [
                {"isin": "IE00BYX2JD69", "name": "iShares MSCI World SRI (Acc)"},
                {"isin": "IE00BKM4GZ66", "name": "iShares Core MSCI Emerging Markets IMI (Acc)"},
                {"isin": "US09075V1026", "name": "BioNTech ADR"},
                {"ticker": "AAPL", "isin": "US0378331005", "name": "Apple Inc."},
            ]
        }
        return facts

    def test_msci_world_sri_token_not_flagged(self):
        """Regression: 'SRI' (iShares MSCI World SRI, nur ISIN im Portfolio)
        erzeugt kein Ticker-Finding — Token stammt aus einem echten Holdingnamen."""
        facts = self._holding_only_facts()
        draft = VALID_DRAFT.replace(
            "Apple (AAPL, US0378331005) bei 24.8%.",
            "Der iShares MSCI World SRI (IE00BYX2JD69) ist die grösste Core-Position.",
        )
        assert verify.verify_draft(facts, draft) == []

    def test_emerging_markets_imi_token_not_flagged(self):
        """Regression: 'IMI' (iShares Core MSCI Emerging Markets IMI, nur ISIN)
        erzeugt kein Ticker-Finding — Token stammt aus einem echten Holdingnamen."""
        facts = self._holding_only_facts()
        draft = VALID_DRAFT.replace(
            "Apple (AAPL, US0378331005) bei 24.8%.",
            "Der iShares Core MSCI Emerging Markets IMI (IE00BKM4GZ66) deckt die Emerging Markets ab.",
        )
        assert verify.verify_draft(facts, draft) == []

    def test_biontech_adr_token_not_flagged(self):
        """Regression: 'ADR' (BioNTech ADR, nur ISIN) erzeugt kein Ticker-Finding —
        Token stammt aus einem echten Holdingnamen."""
        facts = self._holding_only_facts()
        draft = VALID_DRAFT.replace(
            "Apple (AAPL, US0378331005) bei 24.8%.",
            "Die BioNTech ADR (US09075V1026) notiert in New York.",
        )
        assert verify.verify_draft(facts, draft) == []

    def test_unknown_ticker_stays_blocked_with_holding_name_allowlist(self):
        """Fail-closed bleibt: 'MSFT' ohne Portfolio-Ticker/ISIN und ohne
        Bestandteil eines echten Holdingnamens ist weiterhin major."""
        facts = self._holding_only_facts()
        draft = VALID_DRAFT.replace(
            "Apple (AAPL, US0378331005) bei 24.8%.",
            "Microsoft (MSFT) ist keine Position, aber im Fokus.",
        )
        findings = verify.verify_draft(facts, draft)
        assert any(f["severity"] == "major" and "MSFT" in f["issue"] for f in findings)

    def test_holding_name_tokens_require_portfolio_isin(self):
        """Fail-closed: Ein Token ohne zugehoerige Portfolio-ISIN (Holding fehlt
        im Portfolio) bleibt major — die Allowlist greift nur für echte Bestände."""
        facts = copy.deepcopy(VALID_FACTS)  # Portfolio kennt nur AAPL/ASML
        draft = VALID_DRAFT.replace(
            "Apple (AAPL, US0378331005) bei 24.8%.",
            "Der iShares MSCI World SRI (IE00BYX2JD69) ist nicht im Portfolio.",
        )
        findings = verify.verify_draft(facts, draft)
        assert any(f["severity"] == "major" and "SRI" in f["issue"] for f in findings)
        assert any(f["severity"] == "major" and "IE00BYX2JD69" in f["issue"] for f in findings)

    def test_no_input_mutation_with_holding_names(self):
        facts = self._holding_only_facts()
        draft = VALID_DRAFT.replace(
            "Apple (AAPL, US0378331005) bei 24.8%.",
            "Die BioNTech ADR (US09075V1026) notiert in New York.",
        )
        facts_before = copy.deepcopy(facts)
        verify.verify_draft(facts, draft)
        assert facts == facts_before

    def test_verify_briefing_holding_name_tokens_do_not_warn(self):
        """verify_briefing (Warnpfad): SRI/IMI/ADR aus echten Holdingnamen ohne
        Ticker-Feld erzeugen keine Warnung; unbekanntes MSFT warnt weiterhin."""
        portfolio = {
            "holdings": [
                {"isin": "IE00BYX2JD69", "name": "iShares MSCI World SRI (Acc)"},
                {"isin": "IE00BKM4GZ66", "name": "iShares Core MSCI Emerging Markets IMI (Acc)"},
                {"isin": "US09075V1026", "name": "BioNTech ADR"},
            ]
        }
        analysis = {"checks": {"positions": {"positions": []}}}
        text = (
            "iShares MSCI World SRI und iShares Core MSCI Emerging Markets IMI "
            "im Core, BioNTech ADR im Satelliten. Microsoft (MSFT) im Fokus."
        )
        warnings = verify.verify_briefing(text, analysis, [], portfolio)
        assert not any("SRI" in w for w in warnings)
        assert not any("IMI" in w for w in warnings)
        assert not any("ADR" in w for w in warnings)
        assert any("MSFT" in w for w in warnings)

    def test_missing_news_reference_is_minor(self):
        facts = copy.deepcopy(VALID_FACTS)
        facts["news"] = [{"title": "Reuters meldet Quartalszahlen"}]
        findings = verify.verify_draft(facts, VALID_DRAFT)
        assert any(f["severity"] == "minor" for f in findings)

    def test_news_reference_avoids_finding(self):
        facts = copy.deepcopy(VALID_FACTS)
        facts["news"] = [{"title": "Reuters meldet Quartalszahlen"}]
        draft = VALID_DRAFT + "\nReuters meldet Quartalszahlen."
        assert verify.verify_draft(facts, draft) == []

    def test_llm_error_string_raises(self):
        with pytest.raises(LLMError, match="LLM-Fehlertext"):
            verify.verify_draft(VALID_FACTS, "Briefing-Generierung fehlgeschlagen: API-Key fehlt.")

    def test_no_input_mutation(self):
        facts_before = copy.deepcopy(VALID_FACTS)
        draft_before = VALID_DRAFT
        verify.verify_draft(VALID_FACTS, VALID_DRAFT)
        assert VALID_FACTS == facts_before
        assert VALID_DRAFT == draft_before


class TestStatusConformity:
    """STATUS-REGELN: Konformitaetsphrase ("Alle Grenzen eingehalten"/"Ruhige Woche")
    nur ohne rote/gelbe Checks. Phrase + nicht-leere red/yellow_checks -> critical."""

    def test_red_checks_with_phrase_is_critical(self):
        facts = _facts_with_status_checks(red=["drift"])
        findings = verify.verify_draft(facts, _draft_with_phrase())
        assert any(
            f["severity"] == "critical" and "Konformitaetsphrase" in f["issue"] for f in findings
        )

    def test_yellow_checks_with_phrase_is_critical(self):
        facts = _facts_with_status_checks(yellow=["turnover"])
        findings = verify.verify_draft(facts, _draft_with_phrase())
        assert any(
            f["severity"] == "critical" and "Konformitaetsphrase" in f["issue"] for f in findings
        )

    def test_red_and_yellow_checks_with_phrase_is_critical(self):
        facts = _facts_with_status_checks(red=["drift"], yellow=["turnover"])
        findings = verify.verify_draft(facts, _draft_with_phrase())
        assert any(f["severity"] == "critical" for f in findings)

    @pytest.mark.parametrize(
        "phrase",
        [
            "Ruhige Woche. Alle Grenzen eingehalten.",
            "Alle Grenzen eingehalten.",
            "Ruhige Woche.",
        ],
    )
    def test_empty_checks_with_phrase_has_no_finding(self, phrase):
        """Beide Listen leer -> Phrase erlaubt, kein Konformitaets-Finding."""
        facts = _facts_with_status_checks()
        assert verify.verify_draft(facts, _draft_with_phrase(phrase)) == []

    @pytest.mark.parametrize(
        "phrase",
        [
            "Ruhige Woche. Alle Grenzen eingehalten.",
            "RUHIGE WOCHE. ALLE GRENZEN EINGEHALTEN.",
            "Ruhige WOCHE und alle Grenzen eingehalten.",
            "ruhige woche",
        ],
    )
    def test_marker_match_is_case_insensitive(self, phrase):
        facts = _facts_with_status_checks(red=["drift"])
        findings = verify.verify_draft(facts, _draft_with_phrase(phrase))
        assert any(f["severity"] == "critical" for f in findings)

    def test_red_checks_without_phrase_has_no_conformity_finding(self):
        facts = _facts_with_status_checks(red=["drift"])
        assert verify.verify_draft(facts, VALID_DRAFT) == []

    def test_conformity_finding_blocks_final_gate(self):
        """Critical-Konformitaets-Finding blockt ueber das bestehende final_gate."""
        facts = _facts_with_status_checks(red=["drift"])
        verification = verify.verify_draft(facts, _draft_with_phrase())
        gate = verify.final_gate(verification, {"findings": [], "overall_verdict": "pass"})
        assert gate.allow_send is False

    def test_no_input_mutation(self):
        facts = _facts_with_status_checks(red=["drift"], yellow=["turnover"])
        draft = _draft_with_phrase()
        facts_before = copy.deepcopy(facts)
        verify.verify_draft(facts, draft)
        assert facts == facts_before
        assert draft == _draft_with_phrase()

    def test_kein_handlungsbedarf_without_triggers_passes(self):
        """'Kein Handlungsbedarf' ist erlaubt, wenn keiner der Optionen-Trigger
        vorliegt (keine roten/gelben Checks, keine Aenderungen/News/Strategieaenderung)."""
        facts = _facts_with_status_checks()
        assert verify.verify_draft(facts, _draft_with_phrase("Kein Handlungsbedarf.")) == []

    def test_kein_handlungsbedarf_with_strategy_change_is_critical(self):
        """'Kein Handlungsbedarf' trotz Strategieaenderung -> critical (Trigger vorhanden)."""
        facts = copy.deepcopy(VALID_FACTS)
        facts["strategy_diff"] = {"has_changed": True, "changed_keys": ["portfolio.core_pct"]}
        findings = verify.verify_draft(facts, _draft_with_phrase("Kein Handlungsbedarf."))
        assert any(
            f["severity"] == "critical" and "Kein Handlungsbedarf" in f["issue"]
            for f in findings
        )

    def test_alle_grenzen_eingehalten_with_strategy_change_passes(self):
        """'Alle Grenzen eingehalten' bleibt bei Strategieaenderung ohne rote/gelbe
        Checks zulaessig (Grenz-Kontext, keine roten/gelben Punkte)."""
        facts = copy.deepcopy(VALID_FACTS)
        facts["strategy_diff"] = {"has_changed": True, "changed_keys": ["portfolio.core_pct"]}
        assert verify.verify_draft(facts, _draft_with_phrase("Alle Grenzen eingehalten.")) == []

    def test_conformity_phrase_outside_kurzlage_is_not_a_marker(self):
        """Konformitaetsphrase ausserhalb der KURZLAGE zaehlt nicht als Marker
        (Plan Phase 2.6: Phrasen nur im Kontext von 'Kurzlage')."""
        facts = _facts_with_status_checks(red=["drift"])
        draft = VALID_DRAFT.replace(
            "Core-Ziel 75%, Sektor-Max 15%.",
            "Core-Ziel 75%. Alle Grenzen eingehalten.",
        )
        findings = verify.verify_draft(facts, draft)
        assert not any("Konformitaetsphrase" in f["issue"] for f in findings)


class TestStyleGates:
    """Stil-Fix: deterministische Stil-Gates (nicht-lockernd). Rohe bekannte
    snake_case-Checknamen blocken als critical, verbotene englische Fachbegriffe
    ('MVP'/'Zeitfenster-Logik') als major. Deutsche Lesarten sind erlaubt."""

    def _draft(self, body: str) -> str:
        return (
            "## Kurzlage\n"
            + body
            + "\n\n## Datenqualität\nDatenqualität: ok\n\n"
            "## Sell-/Reduce-Signale (bestehende Satellites)\n"
            "Keine Sell-/Reduce-Signale.\n\n"
            "Fundamentaldaten (Umsatz, Gewinn, Cashflow, Verschuldung, Bewertung) nicht automatisch verfügbar und fließen nicht in das Signal ein.\n\n"
            "## Watchlist-Signale\n"
            "Keine Watchlist-Signale.\n\n"
            "Fundamentaldaten (Umsatz, Gewinn, Cashflow, Verschuldung, Bewertung) nicht automatisch verfügbar und fließen nicht in das Signal ein.\n\n"
            "## Nächster Schritt\n"
            "Nächste Woche neuer Lauf, keine Aktion erforderlich.\n\n"
            "## Empfehlung\nWATCH — kein Handlungsbedarf."
        )

    @pytest.mark.parametrize(
        "token",
        [
            "core_satellite",
            "sector_concentration",
            "single_position",
            "thesis_deadlines",
            "drift",
            "turnover",
        ],
    )
    def test_raw_snake_case_checkname_is_critical(self, token):
        findings = verify.verify_draft(VALID_FACTS, self._draft(f"{token} verletzt"))
        assert any(
            f["severity"] == "critical" and "Checkname" in f["issue"] and token in f["issue"]
            for f in findings
        )

    def test_raw_snake_case_variant_casing_is_critical(self):
        """Auch andere Schreibweisen (Core_Satellite) sind technische Bezeichner."""
        findings = verify.verify_draft(VALID_FACTS, self._draft("Core_Satellite verletzt"))
        assert any(f["severity"] == "critical" and "Core_Satellite" in f["issue"] for f in findings)

    @pytest.mark.parametrize("term", ["MVP", "Zeitfenster-Logik"])
    def test_forbidden_style_term_is_major(self, term):
        findings = verify.verify_draft(VALID_FACTS, self._draft(f"nicht Bestandteil des {term}"))
        assert any(f["severity"] == "major" and "Stil-Begriff" in f["issue"] for f in findings)

    def test_turnover_ratio_variant_is_blocked(self):
        """'Turnover-Ratio' (englischer Fachbegriff) wird ueber 'turnover' blockt."""
        findings = verify.verify_draft(VALID_FACTS, self._draft("Turnover-Ratio bei 0%"))
        assert any(f["severity"] == "critical" and "Turnover" in f["issue"] for f in findings)

    def test_german_readings_are_allowed(self):
        """Deutsche Lesarten erzeugen keine Stil-Findings und keine falschen Ticker-Findings."""
        draft = self._draft(
            "Die Core-/Satelliten-Aufteilung bleibt unverändert. "
            "Sektorkonzentration 56.4%, Einzelposition (Apple, AAPL, US0378331005) 24.8%. "
            "Die Drift beträgt 6.4%, der Umschlag 0.0%. Thesen-Fristen eingehalten."
        )
        assert verify.verify_draft(VALID_FACTS, draft) == []

    def test_capitalized_drift_is_not_a_violation(self):
        """'Drift' (deutsche Lesart) ist erlaubt — nur klein 'drift' blockt."""
        findings = verify.verify_draft(VALID_FACTS, self._draft("Die Drift beträgt 6.4%."))
        assert not any("Checkname" in f["issue"] for f in findings)

    def test_style_findings_do_not_break_ticker_check(self):
        """Stil-Gates sind unabhaengig vom Ticker-Check: MVP bleibt Nicht-Ticker,
        echte Portfolio-Ticker weiterhin ohne Finding."""
        draft = self._draft("Apple (AAPL) bei 24.8% ist kein MVP-Fall.")
        findings = verify.verify_draft(VALID_FACTS, draft)
        assert any(f["severity"] == "major" and "MVP" in f["issue"] for f in findings)
        assert not any(f["issue"].startswith("Ticker/ISIN") for f in findings)

    def test_style_critical_blocks_final_gate(self):
        verification = verify.verify_draft(VALID_FACTS, self._draft("drift verletzt"))
        gate = verify.final_gate(verification, {"findings": [], "overall_verdict": "pass"})
        assert gate.allow_send is False

    def test_style_major_blocks_final_gate(self):
        verification = verify.verify_draft(VALID_FACTS, self._draft("nicht Bestandteil des MVP"))
        gate = verify.final_gate(verification, {"findings": [], "overall_verdict": "pass"})
        assert gate.allow_send is False

    def test_no_input_mutation(self):
        facts_before = copy.deepcopy(VALID_FACTS)
        draft = self._draft("drift und MVP sind verboten")
        verify.verify_draft(VALID_FACTS, draft)
        assert VALID_FACTS == facts_before


class TestOptionContract:
    """Optionen-Contract (Plan Phase 2.4/2.8): Optionen nur bei deterministischen
    Triggern, pro Option Begruendung + Gegenargument/Risiko; imperative
    Kauf-/Verkaufsanweisung bleibt critical; Empfehlungen ausserhalb der
    Optionen-Sektion bleiben critical."""

    @staticmethod
    def _option_draft(*, reason: bool, counter: bool) -> str:
        section = (
            "## Entscheidungsrelevante Punkte\n"
            "- Punkt: Drift über der Grenze.\n"
            "- Option: reduzieren\n"
        )
        if reason:
            section += "- Begründung: Einzelposition Apple 24.8% nahe der Strategiegrenze.\n"
        if counter:
            section += "- Gegenargument/Risiko: Verkauf realisiert Kursgewinne steuerlich.\n"
        return (
            "## Kurzlage\nApple (AAPL, US0378331005) bei 24.8%.\n\n"
            "## Datenqualität\nDatenqualität: ok\n\n"
            + section
            + "\n## Sell-/Reduce-Signale (bestehende Satellites)\n"
            "Keine Sell-/Reduce-Signale.\n\n"
            "Fundamentaldaten (Umsatz, Gewinn, Cashflow, Verschuldung, Bewertung) nicht automatisch verfügbar und fließen nicht in das Signal ein.\n\n"
            "## Watchlist-Signale\n"
            "Keine Watchlist-Signale.\n\n"
            "Fundamentaldaten (Umsatz, Gewinn, Cashflow, Verschuldung, Bewertung) nicht automatisch verfügbar und fließen nicht in das Signal ein.\n\n"
            "## Nächster Schritt\n"
            "Nächste Woche neuer Lauf, keine Aktion erforderlich.\n\n"
            "## Empfehlung\nWATCH — kein Handlungsbedarf."
        )

    def test_option_without_reasoning_is_major(self):
        facts = _facts_with_status_checks(red=["drift"])
        findings = verify.verify_draft(facts, self._option_draft(reason=False, counter=True))
        assert any(f["severity"] == "major" and "Begründung" in f["issue"] for f in findings)

    def test_option_without_counterargument_is_major(self):
        facts = _facts_with_status_checks(red=["drift"])
        findings = verify.verify_draft(facts, self._option_draft(reason=True, counter=False))
        assert any(f["severity"] == "major" and "Gegenargument" in f["issue"] for f in findings)

    def test_option_without_trigger_is_major(self):
        facts = _facts_with_status_checks()  # keine Checks/Aenderungen/News/Strategieaenderung
        findings = verify.verify_draft(facts, self._option_draft(reason=True, counter=True))
        assert any(f["severity"] == "major" and "Trigger" in f["issue"] for f in findings)

    def test_option_within_section_passes(self):
        facts = _facts_with_status_checks(red=["drift"])
        assert verify.verify_draft(facts, self._option_draft(reason=True, counter=True)) == []

    def test_imperative_recommendation_is_critical(self):
        """'kaufen Sie' bleibt critical — auch wenn eine wohlgeformte Option vorliegt."""
        facts = _facts_with_status_checks(red=["drift"])
        draft = self._option_draft(reason=True, counter=True).replace(
            "## Kurzlage\nApple (AAPL, US0378331005) bei 24.8%.",
            "## Kurzlage\nApple (AAPL, US0378331005) bei 24.8%. Bitte kaufen Sie weitere Anteile.",
        )
        findings = verify.verify_draft(facts, draft)
        assert any(f["severity"] == "critical" and "Imperative" in f["issue"] for f in findings)

    def test_recommendation_outside_section_is_critical(self):
        """Empfehlung ('Sie sollten') ausserhalb der Optionen-Sektion -> critical."""
        facts = _facts_with_status_checks(red=["drift"])
        draft = self._option_draft(reason=True, counter=True).replace(
            "## Kurzlage\nApple (AAPL, US0378331005) bei 24.8%.",
            "## Kurzlage\nApple (AAPL, US0378331005) bei 24.8%. Sie sollten die Position reduzieren.",
        )
        findings = verify.verify_draft(facts, draft)
        assert any(
            f["severity"] == "critical" and "Handlungsempfehlung" in f["issue"] for f in findings
        )

    def test_data_quality_blocks_option_trigger(self):
        """Datenqualitaet hat Vorrang: bei status != 'ok' werden Portfolio-
        Grenzverletzungen nicht als Trigger gewertet -> Optionen sind major."""
        facts = copy.deepcopy(VALID_FACTS)
        facts["data_quality"] = {"status": "stale", "issues": ["Snapshot älter als 7 Tage"]}
        facts["deterministic_summary"]["red_checks"] = ["drift"]
        findings = verify.verify_draft(facts, self._option_draft(reason=True, counter=True))
        assert any(f["severity"] == "major" and "Trigger" in f["issue"] for f in findings)

    def test_explicit_triggers_field_is_honored(self):
        """Das triggers-Feld im Faktenpaket (Fakten-Lane) hat Vorrang vor dem Fallback."""
        facts = copy.deepcopy(VALID_FACTS)
        facts["triggers"] = {
            "has_boundary_violation": False,
            "has_relevant_changes": False,
            "has_thesis_news": False,
            "has_strategy_change": True,
            "has_any_trigger": True,
        }
        findings = verify.verify_draft(facts, self._option_draft(reason=True, counter=True))
        assert findings == []
        facts["triggers"] = {
            "has_boundary_violation": False,
            "has_relevant_changes": False,
            "has_thesis_news": False,
            "has_strategy_change": False,
            "has_any_trigger": False,
        }
        findings = verify.verify_draft(facts, self._option_draft(reason=True, counter=True))
        assert any(f["severity"] == "major" and "Trigger" in f["issue"] for f in findings)

    # --- Post-Live-Fix P0.3: Pro-Options-Block-Pruefung ---

    @staticmethod
    def _two_option_draft(*, second_counter: bool) -> str:
        """Zwei Optionen in getrennten Absaetzen (Leerzeile getrennt).

        Erster Block vollstaendig (Begruendung + Gegenargument), zweiter Block
        nur mit Begruendung — Gegenargument des ersten Blocks darf NICHT
        fuer den zweiten zaehlen.
        """
        second = (
            "- Option: reduzieren\n"
            "- Begründung: Einzelposition 24.8% nahe der Grenze.\n"
            + ("- Gegenargument/Risiko: Verkauf realisiert Kursgewinne steuerlich.\n" if second_counter else "")
        )
        return (
            "## Kurzlage\nApple (AAPL, US0378331005) bei 24.8%.\n\n"
            "## Datenqualität\nDatenqualität: ok\n\n"
            "## Entscheidungsrelevante Punkte\n"
            "- Option: halten\n"
            "- Begründung: Drift 6.4% über der Grenze.\n"
            "- Gegenargument/Risiko: Drift ist nur ein gelber Punkt.\n"
            "\n"
            + second
            + "\n## Sell-/Reduce-Signale (bestehende Satellites)\n"
            "Keine Sell-/Reduce-Signale.\n\n"
            "Fundamentaldaten (Umsatz, Gewinn, Cashflow, Verschuldung, Bewertung) nicht automatisch verfügbar und fließen nicht in das Signal ein.\n\n"
            "## Watchlist-Signale\n"
            "Keine Watchlist-Signale.\n\n"
            "Fundamentaldaten (Umsatz, Gewinn, Cashflow, Verschuldung, Bewertung) nicht automatisch verfügbar und fließen nicht in das Signal ein.\n\n"
            "## Nächster Schritt\n"
            "Nächste Woche neuer Lauf, keine Aktion erforderlich.\n\n"
            "## Empfehlung\nWATCH — kein Handlungsbedarf."
        )

    def test_option_missing_counter_in_its_own_paragraph_is_major(self):
        """Gegenargument einer ANDEREN Option zaehlt nicht: der zweite Block
        ohne Gegenargument bleibt major, obwohl die Sektion einen Marker hat."""
        facts = _facts_with_status_checks(red=["drift"])
        draft = self._two_option_draft(second_counter=False)
        findings = verify.verify_draft(facts, draft)
        assert any(
            f["severity"] == "major" and "Gegenargument" in f["issue"] for f in findings
        )
        assert not any("Begründung" in f["issue"] for f in findings)

    def test_option_missing_reason_in_its_own_paragraph_is_major(self):
        """Begruendung einer ANDEREN Option zaehlt nicht fuer den Block ohne
        eigene Begruendung."""
        facts = _facts_with_status_checks(red=["drift"])
        draft = (
            "## Kurzlage\nApple (AAPL, US0378331005) bei 24.8%.\n\n"
            "## Datenqualität\nDatenqualität: ok\n\n"
            "## Entscheidungsrelevante Punkte\n"
            "- Option: halten\n"
            "- Begründung: Drift 6.4% über der Grenze.\n"
            "- Gegenargument/Risiko: Drift ist nur ein gelber Punkt.\n"
            "\n"
            "- Option: reduzieren\n"
            "- Gegenargument/Risiko: Verkauf realisiert Kursgewinne steuerlich.\n"
            "\n## Sell-/Reduce-Signale (bestehende Satellites)\n"
            "Keine Sell-/Reduce-Signale.\n\n"
            "Fundamentaldaten (Umsatz, Gewinn, Cashflow, Verschuldung, Bewertung) nicht automatisch verfügbar und fließen nicht in das Signal ein.\n\n"
            "## Watchlist-Signale\n"
            "Keine Watchlist-Signale.\n\n"
            "Fundamentaldaten (Umsatz, Gewinn, Cashflow, Verschuldung, Bewertung) nicht automatisch verfügbar und fließen nicht in das Signal ein.\n\n"
            "## Nächster Schritt\n"
            "Nächste Woche neuer Lauf, keine Aktion erforderlich.\n\n"
            "## Empfehlung\nWATCH — kein Handlungsbedarf."
        )
        findings = verify.verify_draft(facts, draft)
        assert any(
            f["severity"] == "major" and "Begründung" in f["issue"] for f in findings
        )

    def test_options_in_separate_paragraphs_each_complete_pass(self):
        """Zwei Optionen, jeder Block vollstaendig -> keine Findings."""
        facts = _facts_with_status_checks(red=["drift"])
        draft = self._two_option_draft(second_counter=True)
        assert verify.verify_draft(facts, draft) == []


# --- Finding 1: Neukaufideen-Verify (These + Risiko, robuste Erkennung) -------


def _idea_draft(*, thesis: bool, risk: bool, marker: str = "Neukauf-Idee") -> str:
    """Draft mit einer Neukaufidee in der Entscheidungsrelevante-Punkte-Sektion."""
    section = "## Entscheidungsrelevante Punkte\n"
    section += f"- {marker}: Alphabet (US0378331005) als neues Investment.\n"
    if thesis:
        section += "- These: Alphabet profitiert von Cloud-Wachstum.\n"
    if risk:
        section += "- Risiko: Bewertung hoch, Regulierung unsicher.\n"
    return (
        "## Kurzlage\nOK\n\n"
        "## Datenqualität\nDatenqualität: ok\n\n"
        + section
        + "\n## Sell-/Reduce-Signale (bestehende Satellites)\n"
        "Keine Sell-/Reduce-Signale.\n\n"
        "Fundamentaldaten (Umsatz, Gewinn, Cashflow, Verschuldung, Bewertung) nicht automatisch verfügbar und fließen nicht in das Signal ein.\n\n"
        "## Watchlist-Signale\n"
        "Keine Watchlist-Signale.\n\n"
        "Fundamentaldaten (Umsatz, Gewinn, Cashflow, Verschuldung, Bewertung) nicht automatisch verfügbar und fließen nicht in das Signal ein.\n\n"
        "## Relevante News & Veränderungen\n"
        "Alphabet News 1 (Reuters), Alphabet News 2 (CNBC).\n\n"
        "## Nächster Schritt\n"
        "Nächste Woche neuer Lauf, keine Aktion erforderlich.\n\n"
        "## Empfehlung\nWATCH — kein Handlungsbedarf."
    )


def _idea_facts(news=None) -> dict:
    facts = copy.deepcopy(VALID_FACTS)
    facts["news"] = news or [
        {"title": "Alphabet News 1", "source": "reuters", "published": "2026-08-01"},
        {"title": "Alphabet News 2", "source": "cnbc", "published": "2026-08-01"},
    ]
    return facts


class TestGlmIdeas:
    def test_idea_with_thesis_and_risk_passes(self):
        """Neukaufidee mit These + Risiko-Skizze + 2 unabhaengigen Quellen -> ok."""
        draft = _idea_draft(thesis=True, risk=True)
        assert verify.verify_draft(_idea_facts(), draft) == []

    def test_idea_without_risk_is_major(self):
        """Finding 1: Neukaufidee ohne Risiko-Skizze -> major-Finding."""
        draft = _idea_draft(thesis=True, risk=False)
        findings = verify.verify_draft(_idea_facts(), draft)
        assert any(f["severity"] == "major" and "Risiko-Skizze" in f["issue"] for f in findings)

    def test_idea_without_thesis_is_major(self):
        """Neukaufidee ohne Investmentthese -> major-Finding."""
        draft = _idea_draft(thesis=False, risk=True)
        findings = verify.verify_draft(_idea_facts(), draft)
        assert any(f["severity"] == "major" and "Investmentthese" in f["issue"] for f in findings)

    def test_idea_without_independent_sources_is_major(self):
        """Neukaufidee mit nur einer unabhaengigen Quelle (Yahoo) -> major."""
        draft = _idea_draft(thesis=True, risk=True)
        findings = verify.verify_draft(_idea_facts(news=[
            {"title": "Alphabet News 1", "source": "yahoo_finance", "published": "2026-08-01"},
            {"title": "Alphabet News 2", "source": "yahoo_finance", "published": "2026-08-01"},
        ]), draft)
        assert any(f["severity"] == "major" and "unabhaengige Quellenbasis" in f["issue"] for f in findings)

    def test_idea_detected_via_unknown_isin_without_word_idee(self):
        """Finding (Zusatz): konkrete Neukaufidee wird auch OHNE das Wort 'Idee'
        erkannt — unbekannte ISIN in der Sektion reicht (kein Wort-Marker noetig).

        Die unbekannte ISIN erzeugt zusaetzlich das bestehende Portfolio-Gate
        (major: ISIN nicht im Portfolio) — das ist korrekt (unbekannte
        Wertpapiere duerfen nur mit News-Basis als Idee erscheinen). Der Test
        prueft, dass die Ideen-Pruefung (These+Risiko) trotzdem greift: Ohne
        Risiko-Skizze gibt es ein Risiko-Finding."""
        draft = _idea_draft(thesis=True, risk=False, marker="Kaufkandidat")
        draft = draft.replace("Neukauf-Idee", "Kaufkandidat")
        draft = draft.replace("US0378331005", "US5949724083")
        findings = verify.verify_draft(_idea_facts(), draft)
        # Ideen-Erkennung ueber die unbekannte ISIN -> Risiko-Skizze wird geprueft.
        assert any(f["severity"] == "major" and "Risiko-Skizze" in f["issue"] for f in findings)

    def test_generic_idee_word_without_neukauf_context_not_blocked(self):
        """Erlaubter Text mit generischem 'Idee' (kein Neukauf-Bezug, keine
        unbekannte ISIN) wird nicht als Neukaufidee blockiert."""
        draft = (
            "## Kurzlage\nOK\n\n"
            "## Datenqualität\nDatenqualität: ok\n\n"
            "## Entscheidungsrelevante Punkte\n"
            "- Diese Idee wurde bereits geprueft, keine Aenderung noetig.\n\n"
            "## Strategie-Abgleich\n—\n\n"
            "## Relevante News & Veränderungen\n"
            "Alphabet News 1 (Reuters), Alphabet News 2 (CNBC).\n\n"
            "## Empfehlung\nWATCH — kein Handlungsbedarf."
        )
        findings = verify.verify_draft(_idea_facts(), draft)
        # Kein Ideen-Finding (weder These noch Risiko noch Quellen) — generisches
        # "Idee" ohne Neukauf-Kontext wird nicht als Neukaufidee gewertet.
        assert not any("Neukaufidee" in f["issue"] for f in findings)

    def test_more_than_two_ideas_is_critical(self):
        """Mehr als 2 Neukaufideen -> critical."""
        section = "## Entscheidungsrelevante Punkte\n"
        for i, isin in enumerate(["US5949724083", "US02079K3059", "DE0007164600"]):
            section += f"- Neukauf-Idee {i + 1}: ISIN {isin}, These: Wachstum, Risiko: Bewertung.\n"
        draft = (
            "## Kurzlage\nOK\n\n## Datenqualität\nDatenqualität: ok\n\n"
            + section
            + "\n## Strategie-Abgleich\n—\n\n## Relevante News & Veränderungen\n—\n\n"
            "## Empfehlung\nWATCH — kein Handlungsbedarf."
        )
        findings = verify.verify_draft(_idea_facts(), draft)
        assert any(f["severity"] == "critical" and "Mehr als 2" in f["issue"] for f in findings)


class TestFinalGate:
    def test_pass_allows_send(self):
        gate = verify.final_gate([], {"findings": [], "overall_verdict": "pass"})
        assert gate.allow_send is True

    def test_critical_verify_finding_blocks(self):
        gate = verify.final_gate([_finding("critical")], {"findings": [], "overall_verdict": "pass"})
        assert gate.allow_send is False

    def test_major_verify_finding_blocks(self):
        gate = verify.final_gate([_finding("major")], {"findings": [], "overall_verdict": "pass"})
        assert gate.allow_send is False

    def test_review_critical_finding_blocks(self):
        review = {"findings": [_finding("critical")], "overall_verdict": "pass"}
        assert verify.final_gate([], review).allow_send is False

    def test_review_major_finding_blocks(self):
        review = {"findings": [_finding("major")], "overall_verdict": "pass"}
        assert verify.final_gate([], review).allow_send is False

    def test_revise_verdict_blocks(self):
        review = {"findings": [], "overall_verdict": "revise"}
        gate = verify.final_gate([], review)
        assert gate.allow_send is False
        assert "revise" in gate.reason

    def test_block_verdict_blocks(self):
        review = {"findings": [], "overall_verdict": "block"}
        gate = verify.final_gate([], review)
        assert gate.allow_send is False
        assert "block" in gate.reason

    def test_info_and_minor_do_not_block(self):
        verification = [_finding("minor"), _finding("info")]
        review = {"findings": [_finding("minor")], "overall_verdict": "pass"}
        assert verify.final_gate(verification, review).allow_send is True

    def test_missing_verdict_blocks(self):
        review = {"findings": []}
        gate = verify.final_gate([], review)
        assert gate.allow_send is False
        assert "overall_verdict" in gate.reason

    def test_invalid_verdict_blocks(self):
        review = {"findings": [], "overall_verdict": "approve"}
        assert verify.final_gate([], review).allow_send is False

    def test_no_input_mutation(self):
        review = {"findings": [_finding("critical")], "overall_verdict": "block"}
        review_before = copy.deepcopy(review)
        verification = [_finding("critical")]
        verification_before = copy.deepcopy(verification)
        verify.final_gate(verification, review)
        assert review == review_before
        assert verification == verification_before
