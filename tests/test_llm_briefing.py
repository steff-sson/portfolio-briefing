"""Regression tests: LLM-Fehler muessen LLMError werfen, nie Fehlertext liefern.

Bug: API-/LLM-Fehlertexte wurden frueher als Briefing-String zurueckgegeben
und flossen als "Briefing" durch die Pipeline (Versand an Telegram).
"""
from __future__ import annotations

import json
from datetime import datetime

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


def test_draft_prompt_contains_serialized_facts_package(monkeypatch):
    """1-Call-Architektur: Draft-Prompt (briefing.txt) enthaelt das serialisierte
    Faktenpaket (inkl. Rohdaten) — der einzige LLM-Call sieht die volle
    deterministische Quelle, kein reduziertes Kontext-/Summary-Format mehr."""
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

    # briefing.txt-Template mit {facts}-Platzhalter ersetzt durch das Paket.
    assert prompt.startswith("Du bist ein Finanz-Briefing-Autor.")
    assert "{facts}" not in prompt

    # Serialisiertes Paket: deterministische Quelle 1:1 im Prompt.
    assert '"total_value_eur": 4250.0' in prompt
    assert '"US0378331005"' in prompt
    assert '"deterministic_summary"' in prompt
    assert '"strategy_thresholds_pct"' in prompt
    assert "## Empfehlung" in prompt  # Sektions-Contract aus briefing.txt

    # Auch Rohdaten des Pakets sind sichtbar (1-Call-Architektur: das Paket
    # ist die einzige Faktenquelle, verify prueft 1:1 dagegen).
    assert '"value_eur": 2400.0' in prompt
    assert '"quantity": 12' in prompt


# --- ZULÄSSIGE ZAHLEN-Allowlist (Draft-Fix, Legacy-Helfer) ---


def test_allowed_pct_values_use_verify_source():
    """Allowlist nutzt dieselbe fachliche Quelle wie verify (keine abweichende Logik)."""
    from scripts import verify

    facts = _facts_package()
    assert llm_briefing._allowed_pct_values(facts) == (
        verify._summary_numbers_pct(facts["deterministic_summary"])
        + verify._strategy_thresholds_pct(facts["strategy_thresholds_pct"])
        + verify._holdings_weights_pct(facts)
    )


def test_format_allowed_pct_list_one_decimal_sorted_deduped():
    """Deterministische Formatierung: 1 Dezimalstelle, sortiert, dedupliziert.

    Entspricht exakt der verify-Allowlist (deterministic_summary + thresholds
    + Holdings-Gewichte aus value_eur 2400/1850 -> 56.5%/43.5%).
    """
    line = llm_briefing._format_allowed_pct_list(_facts_package())
    assert line == "ZULÄSSIGE ZAHLEN: 0.0%, 5.0%, 15.0%, 25.0%, 30.0%, 31.5%, 43.5%, 56.5%, 75.0%"


def test_format_allowed_pct_list_empty_is_fail_closed():
    """Keine zulaessigen Prozentwerte -> explizite (keine)-Liste, kein Leerstring."""
    facts = {"deterministic_summary": {}, "strategy_thresholds_pct": {}}
    assert llm_briefing._format_allowed_pct_list(facts) == (
        "ZULÄSSIGE ZAHLEN: (keine — keine Prozentwerte zulässig)"
    )


def test_draft_prompt_contains_formatted_allowlist(monkeypatch):
    """Draft-Prompt: der ZULÄSSIGE-ZAHLEN-Block haengt nach der Fakten-
    Serialisierung an briefing.txt an (gleiche Quelle wie verify) — inkl.
    Holdings-Gewichtswerte aus value_eur (56.5%/43.5% fuer 2400/1850)."""
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

    # Der Prompt ist briefing.txt + ZULÄSSIGE-ZAHLEN-Block (nach der
    # Fakten-Serialisierung) und enthaelt das serialisierte Paket.
    assert "Du bist ein Finanz-Briefing-Autor." in prompt
    assert "## ZULÄSSIGE ZAHLEN (nur diese dürfen im Briefing vorkommen)" in prompt
    assert '"core_pct": 75.0' in prompt  # Paket-Werte sind da (1:1-Quelle)
    assert '"turnover_ratio": 0.0' in prompt

    # Allowlist enthaelt Holdings-Gewichtswerte (value_eur 2400/1850 -> 56.5%/43.5%)
    # und die Serialisierung liegt VOR dem Block.
    assert prompt.index('"value_eur": 2400.0') < prompt.index("## ZULÄSSIGE ZAHLEN")
    assert "56.5" in prompt
    assert "43.5" in prompt


def test_briefing_prompt_has_six_section_contract():
    """briefing.txt folgt dem 1-Call-Output-Contract: die 6 Pflichtsektionen
    (verify.DRAFT_SECTIONS) in exakter Reihenfolge + {facts}-Platzhalter."""
    content = (llm_briefing.PROMPTS_DIR / "briefing.txt").read_text(encoding="utf-8")
    sections = [
        "## Kurzlage",
        "## Datenqualität",
        "## Sell-/Reduce-Signale (bestehende Satellites)",
        "## Watchlist-Signale",
        "## Empfehlung",
        "## Nächster Schritt",
    ]
    pos = -1
    for section in sections:
        idx = content.index(section)
        assert idx > pos, f"Sektion {section} nicht in Contract-Reihenfolge"
        pos = idx
    assert "{facts}" in content  # einziger Platzhalter = serialisiertes Paket


def test_briefing_prompt_enforces_verbatim_numbers_and_labels():
    """briefing.txt: Zahlen/ISIN/Ticker/Labels 1:1 aus dem Paket, keine neuen
    Fakten, kein Erfinden — Fail-closed-Vorgaben an das LLM."""
    content = (llm_briefing.PROMPTS_DIR / "briefing.txt").read_text(encoding="utf-8")
    assert "wortgleich" in content
    assert "nie erfinden" in content
    assert "KEINE neuen Fakten" in content
    assert "KEINE neuen Zahlen" in content
    assert "1:1 aus deterministic_summary.recommendation" in content
    assert "Fundamentaldaten" in content


def test_briefing_prompt_contains_disclaimer_wording_and_news_instruction():
    """briefing.txt: exakter verify-Disclaimer-Wortlaut und News-Referenz-
    Anweisung sind im Prompt enthalten (hart formuliert, kein weiches 'kann')."""
    from scripts import verify

    content = (llm_briefing.PROMPTS_DIR / "briefing.txt").read_text(encoding="utf-8")
    assert verify.FUNDAMENTALS_DISCLAIMER in content
    assert "## Sell-/Reduce-Signale (bestehende Satellites)" in content
    assert "## Watchlist-Signale" in content
    assert "referenziere mindestens 1-2" in content
    assert "news" in content.lower()


def test_only_single_prompt_file_and_setup_exists():
    """1-Call-Architektur: nur briefing.txt + q4_tax_context.txt + setup_system.txt —
    keine geloeschten Mode-/Humanize-/Review-/Revise-Prompts."""
    prompts = sorted(p.name for p in llm_briefing.PROMPTS_DIR.iterdir())
    assert prompts == ["briefing.txt", "q4_tax_context.txt", "setup_system.txt"]


def test_load_prompt_fills_facts_placeholder():
    """_load_prompt laedt briefing.txt und ersetzt {facts} mit dem serialisierten
    Kontext (JSON, ensure_ascii=False)."""
    context = {
        "facts": {
            "portfolio": {"holdings": [{"isin": "US0378331005", "name": "Apple Inc."}]},
            "deterministic_summary": {"recommendation": {"label": "WATCH"}},
        }
    }
    prompt = llm_briefing._load_prompt("monday", context)
    assert prompt.startswith("Du bist ein Finanz-Briefing-Autor.")
    assert '"US0378331005"' in prompt
    assert '"label": "WATCH"' in prompt
    assert "{facts}" not in prompt
    assert "{{" not in prompt  # kein unbehandelter Platzhalter


def test_load_prompt_facts_none_serializes_empty():
    """context ohne 'facts' -> leeres Objekt, kein Format-Crash (defensiv)."""
    prompt = llm_briefing._load_prompt("monday", {})
    assert "{}" in prompt
    assert "{facts}" not in prompt


def _patch_month(monkeypatch, month: int):
    """Ersetzt llm_briefing.datetime.now() durch einen fake Monat (Q4-Check)."""
    fake = type("_FakeDT", (), {"now": staticmethod(lambda: datetime(2026, month, 15, 10, 0, 0))})
    monkeypatch.setattr(llm_briefing, "datetime", fake)


def test_load_prompt_appends_q4_tax_context_in_q4(monkeypatch):
    """q4-Anhang-Mechanismus: Okt-Dez haengt q4_tax_context.txt an briefing.txt an."""
    _patch_month(monkeypatch, 11)
    tax_text = (llm_briefing.PROMPTS_DIR / "q4_tax_context.txt").read_text(encoding="utf-8")
    prompt = llm_briefing._load_prompt("monday", {"facts": {}})
    assert tax_text.strip() in prompt
    assert "STEUER-CHECK Q4" in prompt


def test_load_prompt_skips_q4_tax_context_outside_q4(monkeypatch):
    """Ausserhalb Okt-Dez: kein q4-Anhang (z.B. im Juni)."""
    _patch_month(monkeypatch, 6)
    prompt = llm_briefing._load_prompt("monday", {"facts": {}})
    assert "STEUER-CHECK Q4" not in prompt


def test_load_prompt_serializes_changes():
    """_load_prompt serialisiert den {facts}-Kontext — changes-Daten des Pakets
    sind Teil des serialisierten Faktenpakets (1-Call-Architektur)."""
    context = {
        "facts": {
            "changes": {"has_previous": True, "transactions": {"added_count": 1, "removed_count": 0}}
        }
    }
    prompt = llm_briefing._load_prompt("monday", context)
    assert '"added_count": 1' in prompt
    assert '"has_previous": true' in prompt


def test_load_prompt_changes_none_serializes_empty():
    """changes=None (Erstlauf/Dry-Run) im Paket -> 'changes': null im JSON,
    kein Format-Crash durch template.format()."""
    prompt = llm_briefing._load_prompt("monday", {"facts": {"changes": None}})
    assert '"changes": null' in prompt
    assert "{facts}" not in prompt
    assert "{{" not in prompt  # kein unbehandelter Platzhalter


def test_draft_context_changes_uses_facts_reduce():
    """Draft-Kontext nutzt exakt facts.reduce_changes_for_llm (wie llm_revise)."""
    from scripts import facts as facts_module

    facts = _facts_package()
    context = llm_briefing._draft_context(facts)
    assert context["changes"] == facts_module.reduce_changes_for_llm(facts["changes"])


def test_draft_context_changes_none_for_first_run():
    """Erstlauf/Dry-Run (changes=None) -> changes None im Kontext."""
    facts = _facts_package()
    facts["changes"] = None
    context = llm_briefing._draft_context(facts)
    assert context["changes"] is None
