"""Regression tests: LLM-Fehler muessen LLMError werfen, nie Fehlertext liefern.

Bug: API-/LLM-Fehlertexte wurden frueher als Briefing-String zurueckgegeben
und flossen als "Briefing" durch die Pipeline (Versand an Telegram).
"""
from __future__ import annotations

import json
import re

import pytest

from scripts import llm_briefing


class _FakeMessage:
    def __init__(self, content: str | None):
        self.content = content


class _FakeChoice:
    def __init__(self, content: str | None):
        self.message = _FakeMessage(content)


class _FakeResponse:
    def __init__(self, content: str | None):
        self.choices = [_FakeChoice(content)]


class _FakeCompletions:
    def __init__(self, *, content: str | None = None, error: Exception | None = None):
        self._content = content
        self._error = error

    def create(self, **kwargs):
        if self._error:
            raise self._error
        return _FakeResponse(self._content)


class _FakeChat:
    def __init__(self, **kwargs):
        self.completions = _FakeCompletions(**kwargs)


class _FakeClient:
    def __init__(self, **kwargs):
        self.chat = _FakeChat(**kwargs)


def test_success_returns_content(monkeypatch):
    monkeypatch.setattr(llm_briefing, "_get_client", lambda: _FakeClient(content="## Kurzlage\nOK"))
    result = llm_briefing.generate_briefing({}, {}, [], {}, mode="monday")
    assert result == "## Kurzlage\nOK"


def test_missing_api_key_raises_llm_error(monkeypatch):
    """Bug-Regression: fehlender API-Key -> Exception, nicht String zurueck."""
    monkeypatch.setattr(llm_briefing, "_load_env", lambda: None)
    monkeypatch.setattr(llm_briefing.os, "getenv", lambda key, default=None: None)
    with pytest.raises(llm_briefing.LLMError, match="API-Key"):
        llm_briefing.generate_briefing({}, {}, [], {}, mode="monday")


def test_api_error_after_retries_raises_llm_error(monkeypatch):
    """Bug-Regression: API-Fehler nach Retries -> Exception, nicht String."""
    monkeypatch.setattr(llm_briefing, "_get_client", lambda: _FakeClient(error=RuntimeError("HTTP 500")))
    monkeypatch.setattr(llm_briefing.time, "sleep", lambda seconds: None)
    with pytest.raises(llm_briefing.LLMError, match="HTTP 500"):
        llm_briefing.generate_briefing({}, {}, [], {}, mode="monday")


def test_empty_response_raises_llm_error(monkeypatch):
    """Bug-Regression: leere LLM-Antwort -> Exception, nicht String."""
    monkeypatch.setattr(llm_briefing, "_get_client", lambda: _FakeClient(content=None))
    monkeypatch.setattr(llm_briefing.time, "sleep", lambda seconds: None)
    with pytest.raises(llm_briefing.LLMError, match="Empty response"):
        llm_briefing.generate_briefing({}, {}, [], {}, mode="monday")


def _facts_package() -> dict:
    """Faktenpaket mit Rohdaten, die der Draft nicht sehen darf."""
    return {
        "portfolio": {
            "holdings": [
                {
                    "isin": "US0378331005",
                    "name": "Apple Inc.",
                    "quantity": 12,
                    "value_eur": 2400.0,
                    "category": "satellite",
                    "ticker": "AAPL",
                },
                {
                    "isin": "IE00BK5BQT80",
                    "name": "Vanguard FTSE All-World",
                    "quantity": 15,
                    "value_eur": 1850.0,
                    "category": "core",
                },
            ],
            "total_value_eur": 4250.0,
        },
        "analysis": {
            "checks": {
                "positions": {"total_value_eur": 4250.0, "positions": []},
                "core_satellite": {"core_ratio": 0.4353, "status": "green"},
                "sector_concentration": {"max_sector": "Unknown", "max_ratio": 0.5647, "status": "red"},
                "single_position": {"max_position": {"name": "Apple Inc.", "weight": 0.5647}, "status": "red"},
                "drift": {"drift": 0.3147, "status": "red"},
                "turnover": {"turnover_ratio": 0.0, "status": "green"},
                "thesis_deadlines": {"outdated": [], "status": "green"},
            }
        },
        "news": [
            {
                "title": "Apple Quartalszahlen",
                "summary": "AAPL +5%",
                "link": "https://example.com/a",
                "published": "2026-08-12",
            }
        ],
        "strategy": {
            "portfolio": {"core_pct": 75.0, "rebalancing": {"threshold_pct": 5.0}},
            "satellite_limits": {"max_position_pct": 5.0},
        },
        "transactions": [
            {"date": "2026-01-15", "isin": "US0378331005", "type": "buy", "quantity": 5, "price_eur": 195.0}
        ],
        "deterministic_summary": {
            "total_value_eur": 4250.0,
            "position_count": 2,
            "core_ratio": 0.4353,
            "max_position_weight": 0.5647,
            "max_position_name": "Apple Inc.",
            "max_sector": "Unknown",
            "max_sector_ratio": 0.5647,
            "drift": 0.3147,
            "turnover_ratio": 0.0,
            "outdated_theses": [],
            "red_checks": ["drift", "sector_concentration", "single_position"],
            "yellow_checks": [],
            "green_checks": ["core_satellite", "turnover"],
        },
        "strategy_thresholds_pct": {
            "core_pct": 75.0,
            "satellite_pct": 25.0,
            "threshold_pct": 5.0,
            "max_position_pct": 5.0,
            "max_sector_pct": 15.0,
            "max_turnover_annual_pct": 30.0,
        },
        "changes": {
            "has_previous": True,
            "positions": {"added": [], "removed": [], "changed": []},
            "totals": {
                "prev_total_value_eur": 4150.0,
                "total_value_eur": 4250.0,
                "delta_eur": 100.0,
                "delta_pct": 2.41,
                "prev_cash_eur": 0.0,
                "cash_eur": 0.0,
            },
            "transactions": {
                "prev_count": 0,
                "count": 1,
                "added_count": 1,
                "removed_count": 0,
                "added": [
                    {"date": "2026-08-12", "isin": "IE00B3XXRP09", "type": "buy", "quantity": 2.0, "price_eur": 50.0}
                ],
                "removed": [],
                "prev_volume_eur": 0.0,
                "volume_eur": 100.0,
                "delta_volume_eur": 100.0,
            },
            "strategy": {
                "has_changed": False,
                "has_previous": True,
                "changed_keys": [],
                "added_keys": [],
                "removed_keys": [],
            },
        },
    }


def test_draft_context_reduces_raw_data():
    """Draft-Kontext: nur reduzierte Holdings, Findings, News, Grenzwerte."""
    context = llm_briefing._draft_context(_facts_package())

    # Holdings-Metadaten ohne Positionswert-Berechnungsgrundlagen
    assert context["portfolio"] == [
        {"isin": "US0378331005", "name": "Apple Inc.", "ticker": "AAPL", "category": "satellite"},
        {"isin": "IE00BK5BQT80", "name": "Vanguard FTSE All-World", "ticker": None, "category": "core"},
    ]
    assert all("value_eur" not in h and "quantity" not in h for h in context["portfolio"])

    # News nur Titel/Summary
    assert context["news"] == [{"title": "Apple Quartalszahlen", "summary": "AAPL +5%"}]

    # Strategie = bereits deterministisch extrahierte Prozent-Grenzwerte
    assert context["strategy"] == _facts_package()["strategy_thresholds_pct"]

    # Analyse = deterministische Findings (Status-Listen + veraltete Thesen)
    assert context["analysis"] == {
        "red_checks": ["drift", "sector_concentration", "single_position"],
        "yellow_checks": [],
        "green_checks": ["core_satellite", "turnover"],
        "outdated_theses": [],
    }

    # Trigger deterministisch aus den Fakten (1 relevante Transaktions-Aenderung)
    assert context["triggers"] == {
        "has_boundary_violation": True,
        "has_relevant_changes": True,
        "has_thesis_news": False,
        "has_strategy_change": False,
        "has_any_trigger": True,
    }

    # Strategie-Diff wertefrei (nur Feld-Pfade), Datenqualitaet als Status/Issues
    assert context["strategy_diff"] == {}
    assert context["data_quality"] == {}

    # Changes = reduziertes Diff (facts.reduce_changes_for_llm): aggregierte
    # Diff-Werte bleiben, Roh-Transaktions-Records (Zeitpunkt/Menge/Preis) nicht
    assert context["changes"]["has_previous"] is True
    assert context["changes"]["transactions"] == {
        "prev_count": 0,
        "count": 1,
        "added_count": 1,
        "removed_count": 0,
        "prev_volume_eur": 0.0,
        "volume_eur": 100.0,
        "delta_volume_eur": 100.0,
    }
    assert "added" not in context["changes"]["transactions"]
    assert "removed" not in context["changes"]["transactions"]
    assert '"date"' not in json.dumps(context["changes"])

    # Keine Roh-Transactions und kein vollstaendiges Analyse-/Strategie-JSON
    assert "transactions" not in context
    assert "checks" not in context["analysis"]
    assert "rebalancing" not in context["strategy"]


def test_draft_prompt_excludes_raw_data(monkeypatch):
    """Bug-Regression: Draft-Prompt enthaelt keine Rohdaten mit unkontrollierten Werten.

    Ticker/ISIN bleiben sichtbar (Ticker/ISIN-Pruefung in verify), aber
    value_eur/quantity/transactions/vollstaendige Analysis fehlen.
    """
    captured: dict = {}

    class _CaptureCompletions:
        def create(self, **kwargs):
            captured["messages"] = kwargs["messages"]
            return _FakeResponse("## Kurzlage\nOK")

    class _CaptureChat:
        completions = _CaptureCompletions()

    class _CaptureClient:
        chat = _CaptureChat()

    monkeypatch.setattr(llm_briefing, "_get_client", lambda: _CaptureClient())
    monkeypatch.setattr(llm_briefing.time, "sleep", lambda seconds: None)

    result = llm_briefing.generate_draft(_facts_package(), mode="monday")

    assert result == "## Kurzlage\nOK"
    prompt = captured["messages"][1]["content"]

    # Ticker/ISIN-Pruefung bleibt moeglich (verify liest facts_package, LLM sieht sie im Prompt)
    assert "US0378331005" in prompt
    assert "AAPL" in prompt

    # Keine Positionswert-Berechnungsgrundlagen, keine Roh-Transaktions-Records
    assert '"value_eur"' not in prompt
    assert '"quantity"' not in prompt
    assert '"date"' not in prompt
    assert '"type"' not in prompt
    assert '"price_eur"' not in prompt

    # Keine vollstaendige Analyse-/Strategie-JSON
    assert '"checks"' not in prompt
    assert '"rebalancing"' not in prompt

    # Changes-Aggregate (reduziertes Diff) sind da, Roh-Transaktions-Records nicht
    assert '"added_count": 1' in prompt
    assert '"has_previous": true' in prompt

    # Erlaubte Daten sind da: deterministic_summary, Grenzwerte, Findings
    assert "deterministic_summary" in prompt
    assert '"core_pct": 75.0' in prompt
    assert "red_checks" in prompt


# --- ZULÄSSIGE ZAHLEN-Allowlist (Draft-Fix) ---


def _allowlist_line(prompt: str) -> str:
    for line in prompt.splitlines():
        if line.startswith("ZULÄSSIGE ZAHLEN:"):
            return line
    raise AssertionError("ZULÄSSIGE ZAHLEN fehlt im Draft-Prompt")


def test_allowed_pct_values_use_verify_source():
    """Allowlist nutzt dieselbe fachliche Quelle wie verify (keine abweichende Logik)."""
    from scripts import verify

    facts = _facts_package()
    assert llm_briefing._allowed_pct_values(facts) == (
        verify._summary_numbers_pct(facts["deterministic_summary"])
        + verify._strategy_thresholds_pct(facts["strategy_thresholds_pct"])
    )


def test_format_allowed_pct_list_one_decimal_sorted_deduped():
    """Deterministische Formatierung: 1 Dezimalstelle, sortiert, dedupliziert."""
    line = llm_briefing._format_allowed_pct_list(_facts_package())
    assert line == "ZULÄSSIGE ZAHLEN: 0.0%, 5.0%, 15.0%, 25.0%, 30.0%, 31.5%, 43.5%, 56.5%, 75.0%"


def test_format_allowed_pct_list_empty_is_fail_closed():
    """Keine zulaessigen Prozentwerte -> explizite (keine)-Liste, kein Leerstring."""
    facts = {"deterministic_summary": {}, "strategy_thresholds_pct": {}}
    assert llm_briefing._format_allowed_pct_list(facts) == (
        "ZULÄSSIGE ZAHLEN: (keine — keine Prozentwerte zulässig)"
    )


def test_draft_prompt_contains_formatted_allowlist(monkeypatch):
    """Draft-Prompt: Allowlist mit formatierten Prozentwerten, keine Roh-Dezimalwerte."""
    captured: dict = {}

    class _CaptureCompletions:
        def create(self, **kwargs):
            captured["messages"] = kwargs["messages"]
            return _FakeResponse("## Kurzlage\nOK")

    class _CaptureChat:
        completions = _CaptureCompletions()

    class _CaptureClient:
        chat = _CaptureChat()

    monkeypatch.setattr(llm_briefing, "_get_client", lambda: _CaptureClient())
    monkeypatch.setattr(llm_briefing.time, "sleep", lambda seconds: None)

    llm_briefing.generate_draft(_facts_package(), mode="monday")
    prompt = captured["messages"][1]["content"]
    line = _allowlist_line(prompt)
    # Formatierte Prozentwerte (1 Dezimalstelle) statt Roh-Dezimalwerten
    values = line.split("ZULÄSSIGE ZAHLEN: ", 1)[1].split(", ")
    assert values
    assert all(re.fullmatch(r"\d+\.\d%", v) for v in values)
    assert "43.5%" in line  # core_ratio 0.4353 -> 43.5%
    assert "0.0%" in line  # turnover_ratio 0.0 (echter Wert, kein Ableiten aus leerer Liste)
    assert "0.4353" not in line
    assert "0.5647" not in line


def test_mode_prompts_forbid_derived_pcts_and_zero_from_empty_lists():
    """Mode-Prompts: keine abgeleiteten/gerundeten Prozentwerte; leere Checks als 'keine', nie 0%."""
    for mode in ("monday", "friday", "monthly"):
        content = (llm_briefing.PROMPTS_DIR / f"{mode}.txt").read_text(encoding="utf-8")
        assert "ZULÄSSIGE ZAHLEN" in content
        assert "ableiten" in content
        assert "runden" in content
        assert "0%" in content  # Regel verbietet 0% aus leeren Listen
        assert "leeren Liste" in content
        assert 'als "keine" benennen' in content


def test_mode_prompts_use_five_section_contract():
    """Mode-Prompts folgen dem 5-Sektionen-Output-Contract. Die fruehere
    Ausbaustufen-Grenze (Verhaltens-Spiegel zu Trades, keine zeitliche
    Transaktionsinterpretation) ist in der aktuellen Ausbaustufe bewusst
    entfernt — die Prompts duerfen die alte Regel weder enthalten noch
    mit 'nicht ableitbar' formulieren (zeitliche Interpretation ist in
    dieser Struktur gar kein Regelungsgegenstand mehr)."""
    for mode in ("monday", "friday", "monthly"):
        content = (llm_briefing.PROMPTS_DIR / f"{mode}.txt").read_text(encoding="utf-8")
        # Neuer 5-Sektionen-Contract (entspricht verify.DRAFT_SECTIONS)
        for section in (
            "## Kurzlage",
            "## Datenqualität",
            "## Entscheidungsrelevante Punkte",
            "## Strategie-Abgleich",
            "## Relevante News & Veränderungen",
        ):
            assert section in content
        assert "nur die 5 Sektionen" in content
        # Bewusst entfernte Verhaltens-Spiegel-Regel: nicht wieder einfuehren
        assert "Verhaltens-Spiegel" not in content
        assert "Ausbaustufen-Grenze" not in content
        assert "zeitlich interpretiert" not in content
        assert "nicht ableitbar" not in content


def test_mode_prompts_contain_stil_rules():
    """Mode-Prompts: STIL-Regeln mit deutschen Lesarten der snake_case-Checknamen
    (core_satellite->Core-/Satelliten-Aufteilung, drift->Drift, turnover->Umschlag),
    Verbot der englischen Fachbegriffe und Fliesstext-Vorgaben."""
    for mode in ("monday", "friday", "monthly"):
        content = (llm_briefing.PROMPTS_DIR / f"{mode}.txt").read_text(encoding="utf-8")
        assert "STIL:" in content
        assert "Core-/Satelliten-Aufteilung" in content
        assert "Sektorkonzentration" in content
        assert "Einzelposition" in content
        assert "Thesen-Fristen" in content
        assert "Drift" in content
        assert "Umschlag" in content
        # Verbotene Begriffe sind als zu vermeidende Fachbegriffe benannt
        assert "Turnover-Ratio" in content
        assert "Fließtexte" in content or "Fliesstext" in content
        assert "Anlageempfehlungen" in content


# --- {changes} im Draft-Kontext (Oracle-Fund M1) ---


def test_mode_prompts_include_changes_placeholder():
    """Mode-Prompts: {changes}-Platzhalter (reduziertes Diff) in DATEN + Sektion 5."""
    for mode in ("monday", "friday", "monthly"):
        content = (llm_briefing.PROMPTS_DIR / f"{mode}.txt").read_text(encoding="utf-8")
        assert "{changes}" in content
        assert "reduziertes Diff" in content
        assert "keine Roh-Transaktionen" in content
        assert "Veränderungen ({changes})" in content


def test_load_prompt_serializes_changes():
    """_load_prompt serialisiert {changes} (reduziertes Diff) in alle Mode-Prompts."""
    context = {
        "portfolio": [],
        "analysis": {},
        "news": [],
        "strategy": {},
        "triggers": {},
        "strategy_diff": {},
        "data_quality": {},
        "changes": {"has_previous": True, "transactions": {"added_count": 1, "removed_count": 0}},
    }
    for mode in ("monday", "friday", "monthly"):
        prompt = llm_briefing._load_prompt(mode, context)
        assert '"added_count": 1' in prompt
        assert '"has_previous": true' in prompt


def test_load_prompt_changes_none_serializes_empty():
    """changes=None (Erstlauf/Dry-Run) -> leeres Objekt, kein Format-Crash.

    Wie strategy_diff/data_quality: fehlender Key im (Legacy-)Kontext darf
    template.format() nicht mit KeyError brechen lassen.
    """
    prompt = llm_briefing._load_prompt(
        "monday",
        {"portfolio": [], "analysis": {}, "news": [], "strategy": {}},
    )
    assert "- Veränderungen (reduziertes Diff, keine Roh-Transaktionen): {}" in prompt
    assert "{{" not in prompt  # kein unbehandelter Platzhalter


def test_draft_context_changes_uses_facts_reduce():
    """Draft-Kontext nutzt exakt facts.reduce_changes_for_llm (wie llm_revise)."""
    from scripts import facts as facts_module

    facts = _facts_package()
    context = llm_briefing._draft_context(facts)
    assert context["changes"] == facts_module.reduce_changes_for_llm(facts["changes"])


def test_draft_context_changes_none_for_first_run():
    """Erstlauf/Dry-Run (changes=None) -> changes None im Kontext, Prompt zeigt {}."""
    facts = _facts_package()
    facts["changes"] = None
    context = llm_briefing._draft_context(facts)
    assert context["changes"] is None

    captured: dict = {}

    class _CaptureCompletions:
        def create(self, **kwargs):
            captured["messages"] = kwargs["messages"]
            return _FakeResponse("## Kurzlage\nOK")

    class _CaptureChat:
        completions = _CaptureCompletions()

    class _CaptureClient:
        chat = _CaptureChat()

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(llm_briefing, "_get_client", lambda: _CaptureClient())
    monkeypatch.setattr(llm_briefing.time, "sleep", lambda seconds: None)
    try:
        llm_briefing.generate_draft(facts, mode="monday")
    finally:
        monkeypatch.undo()
    prompt = captured["messages"][1]["content"]
    assert "- Veränderungen (reduziertes Diff, keine Roh-Transaktionen): {}" in prompt
