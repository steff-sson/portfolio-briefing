"""Unit tests for scripts.llm_review: strict JSON contract, fail-closed errors.

Der Fake-Client spielt die OpenAI-kompatible API nach — keine echten
API-Aufrufe. Ungueltiges JSON, fehlende Felder, unbekannte severity
(warning), fehlender/ungueltiger overall_verdict, fehlender Key, zweimal
leerer Content und API-Fehler muessen LLMError werfen. Fehlende oder
pauschale Evidenz bei critical/major Findings wirft LLMError ohne Retry
(kein weiterer LLM-Call); info/minor bleiben weniger strikt. Leerer
Content faellt auf reasoning_content zurueck; sind beide leer, ist der
Versuch retrybar (2 Versuche) und endet in LLMError.
"""
from __future__ import annotations

import json

import pytest

from scripts import llm_review


class _FakeMessage:
    def __init__(self, content: str | None, reasoning_content: str | None = None):
        self.content = content
        self.reasoning_content = reasoning_content


class _FakeChoice:
    def __init__(self, content: str | None, reasoning_content: str | None = None):
        self.message = _FakeMessage(content, reasoning_content)


class _FakeResponse:
    def __init__(self, content: str | None, reasoning_content: str | None = None):
        self.choices = [_FakeChoice(content, reasoning_content)]


class _FakeCompletions:
    def __init__(
        self,
        *,
        content: str | None = None,
        contents: list[str | None] | None = None,
        reasoning_content: str | None = None,
        error: Exception | None = None,
    ):
        self._content = content
        self._contents = contents
        self._reasoning_content = reasoning_content
        self._error = error
        self.kwargs: dict = {}
        self.calls = 0

    def create(self, **kwargs):
        self.calls += 1
        self.kwargs = kwargs
        if self._error:
            raise self._error
        if self._contents is not None:
            index = min(self.calls - 1, len(self._contents) - 1)
            return _FakeResponse(self._contents[index], self._reasoning_content)
        return _FakeResponse(self._content, self._reasoning_content)


class _FakeChat:
    def __init__(self, **kwargs):
        self.completions = _FakeCompletions(**kwargs)


class _FakeClient:
    def __init__(self, **kwargs):
        self.chat = _FakeChat(**kwargs)


def _review_content(severity: str, verdict: str) -> str:
    return json.dumps(
        {
            "findings": [
                {
                    "severity": severity,
                    "issue": "Zahl weicht vom Faktenpaket ab",
                    "evidence": "Draft: 'Apple bei 25.3%' — Faktenpaket: 24.79%",
                    "correction": "Apple bei 24.8%",
                }
            ],
            "overall_verdict": verdict,
        }
    )


def test_success_returns_validated_review():
    client = _FakeClient(content=_review_content("major", "revise"))
    result = llm_review.review_draft({"meta": {}}, "Draft", client=client)
    assert set(result) == {"findings", "overall_verdict"}
    assert result["findings"][0]["severity"] == "major"
    assert result["overall_verdict"] == "revise"
    assert result["findings"][0]["issue"]
    assert result["findings"][0]["evidence"]
    assert result["findings"][0]["correction"]


@pytest.mark.parametrize(
    ("severity", "verdict"),
    [
        ("critical", "block"),
        ("major", "revise"),
        ("minor", "pass"),
        ("info", "pass"),
    ],
)
def test_valid_severities_and_verdicts_accepted(severity, verdict):
    client = _FakeClient(content=_review_content(severity, verdict))
    result = llm_review.review_draft({"meta": {}}, "Draft", client=client)
    assert result["findings"][0]["severity"] == severity
    assert result["overall_verdict"] == verdict


def test_warning_severity_raises_llm_error():
    client = _FakeClient(content=_review_content("warning", "revise"))
    with pytest.raises(llm_review.LLMError, match="Unbekannte severity"):
        llm_review.review_draft({}, "Draft", client=client)


def test_missing_overall_verdict_raises_llm_error():
    content = json.dumps({"findings": [{"severity": "info", "issue": "x", "evidence": "y", "correction": "z"}]})
    client = _FakeClient(content=content)
    with pytest.raises(llm_review.LLMError, match="overall_verdict"):
        llm_review.review_draft({}, "Draft", client=client)


def test_invalid_overall_verdict_raises_llm_error():
    client = _FakeClient(content=_review_content("info", "approve"))
    with pytest.raises(llm_review.LLMError, match="overall_verdict"):
        llm_review.review_draft({}, "Draft", client=client)


def test_prompt_loaded_from_review_file():
    """User-Prompt kommt aus config/prompts/review.txt (semantische Pruefbereiche, JSON-Contract)."""
    client = _FakeClient(content=_review_content("info", "pass"))
    llm_review.review_draft({"meta": {"mode": "monday"}}, "## Struktur-Check\nOK", client=client)
    user_msg = client.chat.completions.kwargs["messages"][1]["content"]
    # Semantische Pruefbereiche aus review.txt
    assert "Semantische Inkonsistenzen" in user_msg
    assert "Handlungsempfehlungen" in user_msg
    assert "ignoriert wurden" in user_msg
    # JSON-Contract mit Severity-/Verdict-Regeln und Evidence-Anforderung
    assert "critical|major|minor|info" in user_msg
    assert "pass|revise|block" in user_msg
    assert "belastbare Evidenz" in user_msg
    # Substituierte Eingaben
    assert '"mode": "monday"' in user_msg
    assert "## Struktur-Check" in user_msg


def test_prompt_responsibility_boundaries():
    """Verantwortungsgrenzen: Sektionen/Zahlen/Ticker-ISIN prueft Python deterministisch,
    nicht GLM — kein Auftrag, sie als Finding zu melden. Zeitliche Transaktions-
    interpretation ist ausgeschlossen (kein deterministisches Zeitfenster im
    Faktenpaket); GLM bleibt bei semantischer Konsistenz, Empfehlungen und News."""
    client = _FakeClient(content=_review_content("info", "pass"))
    llm_review.review_draft({"meta": {}}, "Draft", client=client)
    user_msg = client.chat.completions.kwargs["messages"][1]["content"]
    assert "VERANTWORTUNGSGRENZEN" in user_msg
    assert "deterministisch" in user_msg
    assert "Sektionen" in user_msg and "Zahlen" in user_msg and "Ticker/ISIN" in user_msg
    assert "erneut prüfen" in user_msg
    # Keine zeitliche Transaktionsinterpretation: kein Zeitfenster, keine
    # Transaktionszeitraeume/zeitlichen Schlussfolgerungen bewerten.
    assert "Zeitfenster" in user_msg
    assert "Transaktionszeiträume" in user_msg
    assert "zeitlichen Schlussfolgerungen" in user_msg
    # GLM bleibt bei: semantischer Konsistenz, unerlaubten Empfehlungen, relevanten News.
    assert "semantischer Konsistenz" in user_msg
    assert "unerlaubten Empfehlungen" in user_msg
    assert "relevanten News" in user_msg
    # Alte GLM-Auftraege (Zahlen-Halluzinationscheck, fehlende Sektionen) sind entfernt.
    assert "Halluzination" not in user_msg
    assert "Fehlende Sektionen" not in user_msg


def test_prompt_style_violations_as_minor_not_delegated():
    """Stil-Fix: Stil-Verstoeße prueft Python deterministisch (verify.py Stil-Gates);
    GLM darf sie hoechstens als minor benennen, nie als major/critical. Determi-
    nistische Fakten-/Status-Pruefungen werden nicht an GLM delegiert."""
    client = _FakeClient(content=_review_content("info", "pass"))
    llm_review.review_draft({"meta": {}}, "Draft", client=client)
    user_msg = client.chat.completions.kwargs["messages"][1]["content"]
    # Stil-Verstoeße sind in der Verantwortungsgrenze als Python-Check benannt
    assert "Stil-Verstöße" in user_msg
    assert "deterministisch" in user_msg
    # ... und duerfen hoechstens als minor gemeldet werden
    assert "höchstens als minor" in user_msg
    assert "nie als major/critical" in user_msg
    # Deterministische Pruefungen werden nicht delegiert
    assert "NICHT an dich delegiert" in user_msg
    # Severity-Regeln: Stil-Verstoeße unter minor gelistet
    assert "minor: Formatierung, Stil-Verstöße" in user_msg


def _facts_package_with_transactions() -> dict:
    """Faktenpaket mit Roh-Transaktionsdaten und Positionswerten, die der
    Review-Kontext NICHT enthalten darf."""
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
        "news": [
            {"title": "Apple Quartalszahlen", "summary": "AAPL +5%", "link": "https://example.com/a"}
        ],
        "strategy": {"portfolio": {"core_pct": 75.0}},
        "transactions": [
            {"date": "2026-01-15", "isin": "US0378331005", "type": "buy", "quantity": 5, "price_eur": 195.0},
            {"date": "2026-02-10", "isin": "US0378331005", "type": "sell", "quantity": 2, "price_eur": 210.0},
        ],
        "deterministic_summary": {
            "total_value_eur": 2400.0,
            "position_count": 1,
            "core_ratio": 0.5,
            "max_position_weight": 1.0,
            "max_position_name": "Apple Inc.",
            "max_sector": "Technology",
            "max_sector_ratio": 1.0,
            "drift": 0.3,
            "turnover_ratio": 0.0,
            "outdated_theses": [],
            "red_checks": ["drift"],
            "yellow_checks": [],
            "green_checks": ["core_satellite"],
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


def test_review_prompt_contains_no_transaction_details():
    """Review-Payload: keine Transaktionsdetails, keine Roh-Transaktionsdaten
    und keine Positionswerte — GLM kann so keine Transaktionszeitraeume oder
    Verhaltensaussagen zu Trades bewerten. Behalten bleiben die deterministischen
    Summary-/Status-/Grenzwerte sowie reduzierte Holdings/News."""
    client = _FakeClient(content=_review_content("info", "pass"))
    llm_review.review_draft(_facts_package_with_transactions(), "## Struktur-Check\nOK", client=client)
    user_msg = client.chat.completions.kwargs["messages"][1]["content"]

    # Kein transactions-Schluessel, keine Roh-Transaktionsfelder
    assert '"transactions"' not in user_msg
    assert '"date"' not in user_msg
    assert '"type"' not in user_msg
    assert '"price_eur"' not in user_msg
    # Keine Positionswert-Berechnungsgrundlagen (value_eur/quantity)
    assert '"value_eur"' not in user_msg
    assert '"quantity"' not in user_msg
    # Kein vollstaendiges Analysis-/Strategie-JSON
    assert '"checks"' not in user_msg
    # Deterministische Summary-/Status-/Grenzwerte bleiben fuer die semantische Pruefung
    assert '"deterministic_summary"' in user_msg
    assert '"strategy_thresholds_pct"' in user_msg
    assert "red_checks" in user_msg
    assert "green_checks" in user_msg
    # Reduzierte Holdings/News bleiben sichtbar (semantische Zuordnung)
    assert "US0378331005" in user_msg
    assert "Apple Inc." in user_msg
    assert "Apple Quartalszahlen" in user_msg
    # Draft bleibt substituiert
    assert "## Struktur-Check" in user_msg


def test_uses_glm_5_2():
    client = _FakeClient(content=_review_content("info", "pass"))
    llm_review.review_draft({}, "Draft", client=client)
    assert client.chat.completions.kwargs["model"] == "glm-5.2"


def test_request_includes_reasoning_effort_none():
    """NeuralWatt-kompatibel: Reasoning ist im Request explizit aus (top-level reasoning_effort)."""
    client = _FakeClient(content=_review_content("info", "pass"))
    llm_review.review_draft({}, "Draft", client=client)
    assert client.chat.completions.kwargs["reasoning_effort"] == "none"
    # Alte extra_body-Syntax ist entfernt.
    assert "extra_body" not in client.chat.completions.kwargs


def test_empty_content_falls_back_to_reasoning_content():
    """Leerer content + gueltiger reasoning_content -> Erfolg ohne Retry."""
    client = _FakeClient(content=None, reasoning_content=_review_content("minor", "pass"))
    result = llm_review.review_draft({}, "Draft", client=client)
    assert client.chat.completions.calls == 1
    assert result["overall_verdict"] == "pass"


def test_empty_content_retries_then_succeeds(monkeypatch):
    """Leerer Content ist retrybar: 1. Versuch leer, 2. Versuch gueltig -> Erfolg."""
    monkeypatch.setattr(llm_review.time, "sleep", lambda seconds: None)
    client = _FakeClient(contents=[None, _review_content("minor", "pass")])
    result = llm_review.review_draft({}, "Draft", client=client)
    assert client.chat.completions.calls == 2
    assert result["overall_verdict"] == "pass"


def test_empty_content_and_reasoning_twice_raises_llm_error(monkeypatch):
    """Content und reasoning_content zweimal leer -> LLMError nach 2 Versuchen."""
    monkeypatch.setattr(llm_review.time, "sleep", lambda seconds: None)
    client = _FakeClient(contents=[None, None])
    with pytest.raises(llm_review.LLMError, match="Empty response"):
        llm_review.review_draft({}, "Draft", client=client)
    assert client.chat.completions.calls == 2


def test_invalid_json_raises_llm_error():
    client = _FakeClient(content="das ist kein json {")
    with pytest.raises(llm_review.LLMError, match="kein gueltiges JSON"):
        llm_review.review_draft({}, "Draft", client=client)


def test_empty_response_raises_llm_error(monkeypatch):
    client = _FakeClient(content=None)
    monkeypatch.setattr(llm_review.time, "sleep", lambda seconds: None)
    with pytest.raises(llm_review.LLMError, match="Empty response"):
        llm_review.review_draft({}, "Draft", client=client)


def test_missing_findings_key_raises_llm_error():
    client = _FakeClient(content='{"overall_verdict": "pass"}')
    with pytest.raises(llm_review.LLMError, match="findings"):
        llm_review.review_draft({}, "Draft", client=client)


def test_findings_not_a_list_raises_llm_error():
    client = _FakeClient(content='{"findings": {"severity": "info"}, "overall_verdict": "pass"}')
    with pytest.raises(llm_review.LLMError, match="keine Liste"):
        llm_review.review_draft({}, "Draft", client=client)


def test_finding_missing_field_raises_llm_error():
    content = json.dumps(
        {"findings": [{"severity": "info", "issue": "x", "correction": "y"}], "overall_verdict": "pass"}
    )
    client = _FakeClient(content=content)
    with pytest.raises(llm_review.LLMError, match="evidence"):
        llm_review.review_draft({}, "Draft", client=client)


def test_finding_not_an_object_raises_llm_error():
    client = _FakeClient(content='{"findings": ["info"], "overall_verdict": "pass"}')
    with pytest.raises(llm_review.LLMError, match="kein JSON-Objekt"):
        llm_review.review_draft({}, "Draft", client=client)


def test_api_error_raises_llm_error():
    client = _FakeClient(error=RuntimeError("HTTP 500"))
    with pytest.raises(llm_review.LLMError, match="HTTP 500"):
        llm_review.review_draft({}, "Draft", client=client)


class TestCodeFenceParsing:
    """Markdown-Codefences um die JSON-Antwort werden vor dem Parsen entfernt —
    aber nur, wenn der gesamte Content exakt ein Fence ist. JSON-Extraktion
    aus Prosa oder aus Fences mit ungueltigem Inhalt bleibt fail-closed."""

    @staticmethod
    def _body() -> str:
        return _review_content("minor", "pass")

    @pytest.mark.parametrize(
        ("prefix", "suffix"),
        [
            # Fenced JSON mit json-Tag
            ("```json\n", "\n```"),
            # Bare fenced JSON ohne Tag
            ("```\n", "\n```"),
            # Umgebender Whitespace (inkl. Einrueckung) um den Fence
            ("\n\n  ```json\n", "\n```  \n\n"),
        ],
    )
    def test_whole_content_code_fence_accepted(self, prefix, suffix):
        client = _FakeClient(content=prefix + self._body() + suffix)
        result = llm_review.review_draft({}, "Draft", client=client)
        assert result["overall_verdict"] == "pass"
        assert client.chat.completions.calls == 1

    def test_prose_before_code_fence_raises_llm_error(self):
        """Prosa vor dem Fence: kein vollstaendiger Fence -> fail-closed,
        kein JSON wird aus Prosa extrahiert."""
        content = "Hier ist das Review:\n```json\n" + self._body() + "\n```"
        client = _FakeClient(content=content)
        with pytest.raises(llm_review.LLMError, match="kein gueltiges JSON"):
            llm_review.review_draft({}, "Draft", client=client)
        assert client.chat.completions.calls == 1

    def test_invalid_json_inside_fence_raises_llm_error(self):
        """Fence wird entfernt, aber der Inhalt ist ungueltiges JSON -> fail-closed."""
        client = _FakeClient(content="```json\ndas ist kein json {\n```")
        with pytest.raises(llm_review.LLMError, match="kein gueltiges JSON"):
            llm_review.review_draft({}, "Draft", client=client)
        assert client.chat.completions.calls == 1


class TestEvidenceValidation:
    """critical/major brauchen konkrete, belastbare Evidenz (fail-closed, kein Retry);
    info/minor bleiben weniger strikt."""

    @staticmethod
    def _content(severity: str, evidence: str, verdict: str = "block") -> str:
        return json.dumps(
            {
                "findings": [
                    {"severity": severity, "issue": "x", "evidence": evidence, "correction": "c"}
                ],
                "overall_verdict": verdict,
            }
        )

    @pytest.mark.parametrize("evidence", ["", "   "])
    def test_critical_without_evidence_raises_llm_error_no_retry(self, evidence):
        client = _FakeClient(content=self._content("critical", evidence))
        with pytest.raises(llm_review.LLMError, match="ohne Evidenz"):
            llm_review.review_draft({}, "Draft", client=client)
        assert client.chat.completions.calls == 1  # kein Retry/weiterer LLM-Call

    @pytest.mark.parametrize(
        "evidence",
        [
            "Stimmt nicht.",
            "Der Entwurf entspricht nicht dem Faktenpaket.",
            "Die Aussage ist falsch und unbegruendet.",
        ],
    )
    def test_critical_with_blanket_evidence_raises_llm_error(self, evidence):
        client = _FakeClient(content=self._content("critical", evidence))
        with pytest.raises(llm_review.LLMError, match="pauschaler Evidenz"):
            llm_review.review_draft({}, "Draft", client=client)
        assert client.chat.completions.calls == 1

    def test_major_with_blanket_evidence_raises_llm_error(self):
        evidence = "Entwurf weicht vom Faktenpaket ab."
        client = _FakeClient(content=self._content("major", evidence, verdict="revise"))
        with pytest.raises(llm_review.LLMError, match="pauschaler Evidenz"):
            llm_review.review_draft({}, "Draft", client=client)

    def test_critical_with_concrete_evidence_accepted(self):
        evidence = "Draft: 'Apple bei 25.3%' — Faktenpaket: 24.79%"
        client = _FakeClient(content=self._content("critical", evidence))
        result = llm_review.review_draft({}, "Draft", client=client)
        assert result["findings"][0]["severity"] == "critical"
        assert result["findings"][0]["evidence"] == evidence
        assert client.chat.completions.calls == 1

    @pytest.mark.parametrize("severity", ["minor", "info"])
    def test_minor_info_without_evidence_accepted(self, severity):
        """info/minor bleiben weniger strikt: leere Evidenz ist erlaubt."""
        client = _FakeClient(content=self._content(severity, "", verdict="pass"))
        result = llm_review.review_draft({}, "Draft", client=client)
        assert result["findings"][0]["severity"] == severity


def test_missing_api_key_raises_llm_error(monkeypatch):
    """Kein Client uebergeben + kein API-Key -> LLMError, kein API-Aufruf."""
    monkeypatch.setattr(llm_review, "_load_env", lambda: None)
    monkeypatch.setattr(llm_review.os, "getenv", lambda key, default=None: None)
    with pytest.raises(llm_review.LLMError, match="NEURALWATT_API_KEY"):
        llm_review.review_draft({}, "Draft")
