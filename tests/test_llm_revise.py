"""Unit tests for scripts.llm_revise: text contract, fail-closed errors.

Der Fake-Client spielt die OpenAI-kompatible API nach — keine echten
API-Aufrufe. Leere Antwort, API-Fehler und fehlender API-Key muessen
LLMError werfen.
"""
from __future__ import annotations

import json

import pytest

from scripts import llm_revise


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
        self.kwargs: dict = {}

    def create(self, **kwargs):
        self.kwargs = kwargs
        if self._error:
            raise self._error
        return _FakeResponse(self._content)


class _FakeChat:
    def __init__(self, **kwargs):
        self.completions = _FakeCompletions(**kwargs)


class _FakeClient:
    def __init__(self, **kwargs):
        self.chat = _FakeChat(**kwargs)


def _review() -> dict:
    return {"findings": [{"severity": "major", "issue": "x", "evidence": "y", "correction": "z"}], "overall_verdict": "revise"}


def test_success_returns_revised_draft():
    content = "## Struktur-Check\nKorrigiert.\n\n## Nachbericht\n—\n\n## Verhaltens-Spiegel\n—\n\n## Blinder Fleck\n—"
    client = _FakeClient(content=content)
    result = llm_revise.revise_draft({"meta": {}}, "alter Draft", _review(), client=client)
    assert result == content


def test_uses_deepseek_v4_flash_and_injects_findings():
    client = _FakeClient(content="revidiert")
    llm_revise.revise_draft({"meta": {"mode": "monday"}}, "Draft", _review(), client=client)
    kwargs = client.chat.completions.kwargs
    assert kwargs["model"] == "deepseek-v4-flash"
    user_msg = kwargs["messages"][1]["content"]
    assert "Faktenpaket" in user_msg
    assert "Draft" in user_msg
    assert '"overall_verdict": "revise"' in user_msg


def test_revise_prompt_uses_five_section_contract():
    """revise.txt folgt dem 5-Sektionen-Output-Contract. Die fruehere
    Ausbaustufen-Grenze (Verhaltens-Spiegel zu Trades, keine zeitliche
    Transaktionsinterpretation) ist in der aktuellen Ausbaustufe bewusst
    entfernt — der Revisor muss den Entwurf ohne diese Regel umbauen
    (zeitliche Interpretation ist kein Regelungsgegenstand mehr)."""
    content = llm_revise.REVISE_PROMPT_PATH.read_text(encoding="utf-8")
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


def test_revise_prompt_contains_stil_rules():
    """revise.txt: STIL-Regeln mit deutschen Lesarten der snake_case-Checknamen,
    Verbot der englischen Fachbegriffe und Fliesstext-Vorgaben."""
    content = llm_revise.REVISE_PROMPT_PATH.read_text(encoding="utf-8")
    assert "STIL:" in content
    assert "Core-/Satelliten-Aufteilung" in content
    assert "Sektorkonzentration" in content
    assert "Einzelposition" in content
    assert "Thesen-Fristen" in content
    assert "Drift" in content
    assert "Umschlag" in content
    assert "Turnover-Ratio" in content
    assert "Anlageempfehlungen" in content


def test_empty_response_raises_llm_error():
    client = _FakeClient(content=None)
    with pytest.raises(llm_revise.LLMError, match="leere Antwort"):
        llm_revise.revise_draft({}, "Draft", _review(), client=client)


def test_api_error_raises_llm_error():
    client = _FakeClient(error=RuntimeError("HTTP 500"))
    with pytest.raises(llm_revise.LLMError, match="HTTP 500"):
        llm_revise.revise_draft({}, "Draft", _review(), client=client)


def test_missing_api_key_raises_llm_error(monkeypatch):
    """Kein Client uebergeben + kein API-Key -> LLMError, kein API-Aufruf."""
    monkeypatch.setattr(llm_revise, "_load_env", lambda: None)
    monkeypatch.setattr(llm_revise.os, "getenv", lambda key, default=None: None)
    with pytest.raises(llm_revise.LLMError, match="NEURALWATT_API_KEY"):
        llm_revise.revise_draft({}, "Draft", _review())


def test_revise_facts_context_strips_raw_transaction_records():
    """changes im Revise-Faktenkontext ohne Roh-Transaktions-Records (MVP-GRENZE)."""
    changes = {
        "has_previous": True,
        "positions": {"added": [], "removed": [], "changed": []},
        "totals": {"delta_eur": 100.0, "delta_pct": 1.0},
        "transactions": {
            "prev_count": 4,
            "count": 5,
            "added_count": 1,
            "removed_count": 0,
            "added": [{"date": "2026-08-01", "isin": "IE00B3XXRP09", "type": "buy", "quantity": 2.0, "price_eur": 50.0}],
            "removed": [],
        },
    }
    context = llm_revise._revise_facts_context({"meta": {}, "changes": changes})
    dumped = json.dumps(context)

    # Keine Roh-Transaktions-Records (Zeitpunkt/Menge/Preis) im LLM-Kontext
    assert '"date"' not in dumped
    assert '"price_eur"' not in dumped
    assert '"quantity"' not in dumped
    # Aggregierte Diff-Werte bleiben erhalten
    assert '"added_count": 1' in dumped
    assert '"has_previous": true' in dumped


def _facts_package_with_sensitive_data() -> dict:
    """Faktenpaket mit Top-Level-Transaktionen, Positionswerten und Roh-Checks,
    die der Revise-Prompt NICHT enthalten darf (wie Draft/Review)."""
    return {
        "meta": {"mode": "monday", "pipeline_version": "2.0"},
        "portfolio": {
            "holdings": [
                {
                    "isin": "US0378331005",
                    "name": "Apple Inc.",
                    "quantity": 12,
                    "value_eur": 2400.0,
                    "category": "satellite",
                    "ticker": "AAPL",
                }
            ],
            "total_value_eur": 2400.0,
        },
        "analysis": {
            "checks": {
                "positions": {
                    "total_value_eur": 2400.0,
                    "positions": [{"isin": "US0378331005", "weight": 1.0}],
                },
                "single_position": {"status": "red", "max_position": {"weight": 1.0, "name": "Apple Inc."}},
            }
        },
        "news": [
            {"title": "Apple Quartalszahlen", "summary": "AAPL +5%", "link": "https://example.com/a"}
        ],
        "strategy": {"portfolio": {"core_pct": 75.0}},
        "transactions": [
            {"date": "2026-01-15", "isin": "US0378331005", "type": "buy", "quantity": 5, "price_eur": 195.0},
        ],
        "changes": {
            "has_previous": True,
            "totals": {"delta_eur": 100.0, "delta_pct": 1.0},
            "transactions": {
                "prev_count": 0,
                "count": 1,
                "added_count": 1,
                "removed_count": 0,
                "added": [{"date": "2026-01-15", "isin": "US0378331005", "type": "buy", "quantity": 5, "price_eur": 195.0}],
                "removed": [],
            },
        },
        "deterministic_summary": {
            "total_value_eur": 2400.0,
            "position_count": 1,
            "core_ratio": 0.0,
            "max_position_weight": 1.0,
            "max_position_name": "Apple Inc.",
            "max_sector": "Technology",
            "max_sector_ratio": 1.0,
            "drift": 0.3,
            "turnover_ratio": 0.0,
            "outdated_theses": [],
            "red_checks": ["single_position"],
            "yellow_checks": [],
            "green_checks": [],
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


def test_revise_prompt_excludes_transactions_positions_and_raw_checks():
    """Der finale Revise-Prompt enthaelt wie Draft/Review keine Top-Level-
    Transaktionen, keine Positionswerte (value_eur/quantity) und keine Roh-
    Checks aus analysis. Behalten bleiben Summary, Changes-Aggregate,
    erlaubte Holdings-Metadaten und News."""
    client = _FakeClient(content="revidiert")
    llm_revise.revise_draft(
        _facts_package_with_sensitive_data(), "## Struktur-Check\nOK", _review(), client=client
    )
    user_msg = client.chat.completions.kwargs["messages"][1]["content"]

    # Keine Roh-Transaktionsdaten (weder Top-Level noch im changes-Diff)
    assert '"date"' not in user_msg
    assert '"type"' not in user_msg
    assert '"price_eur"' not in user_msg
    # Keine Positionswert-Berechnungsgrundlagen
    assert '"value_eur"' not in user_msg
    assert '"quantity"' not in user_msg
    # Keine Roh-Checks aus analysis, kein vollstaendiges Strategie-JSON
    assert '"checks"' not in user_msg
    # Summary (einzige erlaubte Zahlenquelle) bleibt erhalten
    assert '"deterministic_summary"' in user_msg
    assert "red_checks" in user_msg
    # Changes-Aggregate bleiben erhalten
    assert '"added_count": 1' in user_msg
    assert '"has_previous": true' in user_msg
    # Erlaubte Holdings-Metadaten und News bleiben erhalten
    assert "US0378331005" in user_msg
    assert "Apple Inc." in user_msg
    assert "Apple Quartalszahlen" in user_msg
    # Draft und Review bleiben substituiert
    assert "## Struktur-Check" in user_msg
    assert '"overall_verdict": "revise"' in user_msg
