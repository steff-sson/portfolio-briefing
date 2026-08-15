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
    "—"
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
        assert any("Entscheidungsrelevante Punkte" in f["issue"] for f in findings)

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

    def test_number_within_tolerance_passes(self):
        # 24.8 vs max_position_weight 0.2479 -> 24.79% (+0.01pp, innerhalb ±0.5pp)
        assert verify.verify_draft(VALID_FACTS, VALID_DRAFT) == []

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
            "## Entscheidungsrelevante Punkte\nkeine entscheidungsrelevanten Punkte\n\n"
            "## Strategie-Abgleich\n—\n\n## Relevante News & Veränderungen\n—"
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
            + "\n## Strategie-Abgleich\nCore-Ziel 75%, Sektor-Max 15%.\n\n"
            "## Relevante News & Veränderungen\n—"
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
