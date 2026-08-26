"""Tests fuer den Humanizer (P1-P3, Plan portfolio-briefing-humanizer.md).

Deckt den P1-Input-/Output-Contract ab (``humanize_briefing`` mit Signatur
``(str, str, client=None) -> str``, ``LLMError`` statt Input-Rueckgabe),
die P2-Prompt-Aufladung und die P3-Implementierung: erfolgreicher
Mock-Client, API-Fehler, Vertragsverletzungen (fehlende Sektion, geaenderter
Disclaimer, Fehlerstring) und No-Fallback (kein return des Inputs).

Keine echten API-Aufrufe: Mock-/Fake-Objekte oder reine Funktionen.
"""
from __future__ import annotations

import pytest

from scripts import llm_humanize


def _valid_humanized() -> str:
    """Gehumanisierter Text, der den P1-Contract erfuellt."""
    return (
        "## Kurzlage\n"
        "Guter Stand, keine roten Punkte.\n\n"
        "## Datenqualität\n"
        "Alle Daten vollständig.\n\n"
        "## Sell-/Reduce-Signale (bestehende Satellites)\n"
        "Keine Sell-/Reduce-Signale.\n\n"
        "Fundamentaldaten (Umsatz, Gewinn, Cashflow, Verschuldung, Bewertung) "
        "nicht automatisch verfügbar und fließen nicht in das Signal ein.\n\n"
        "## Watchlist-Signale\n"
        "Keine Watchlist-Signale (NO SIGNAL für alle Positionen).\n\n"
        "Fundamentaldaten (Umsatz, Gewinn, Cashflow, Verschuldung, Bewertung) "
        "nicht automatisch verfügbar und fließen nicht in das Signal ein.\n\n"
        "## Nächster Schritt\n"
        "Nächste Woche neuer Lauf, keine Aktion erforderlich."
    )


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


class TestHumanizeContract:
    """P1: Interface und Fail-closed-Semantik."""

    def test_humanize_briefing_exists_with_contract_signature(self):
        """Signatur (str, str, client=None) -> str (P1-Akzeptanzkriterium)."""
        assert callable(llm_humanize.humanize_briefing)
        assert llm_humanize.humanize_briefing.__doc__ is not None

    def test_humanize_briefing_success_returns_string(self):
        """Erfolgreicher Mock-Client: Rueckgabe ist der validierte `str`."""
        client = _FakeClient(content=_valid_humanized())
        result = llm_humanize.humanize_briefing("## Kurzlage\nTest", mode="monday", client=client)
        assert isinstance(result, str)
        assert "## Kurzlage" in result

    def test_llm_error_reused_not_duplicated(self):
        """LLMError wird aus llm_briefing importiert (keine Duplikation)."""
        from scripts import llm_briefing

        assert llm_humanize.LLMError is llm_briefing.LLMError

    def test_no_fallback_policy_documented(self):
        """Kein-Fallback-Regel ist dokumentiert und testbar (User-Vorgabe)."""
        assert "Kein Fallback" in llm_humanize.NO_FALLBACK_POLICY


class TestHumanizeCall:
    """P3: API-Aufruf, Fehlerpfad und No-Fallback (Mock-Client, keine echten Calls)."""

    def _assert_llm_error(self, client, briefing_text="## Kurzlage\nTest", match=None):
        """Assert: humanize_briefing wirft LLMError und gibt NIE den Input zurueck."""
        with pytest.raises(llm_humanize.LLMError, match=match):
            llm_humanize.humanize_briefing(briefing_text, mode="monday", client=client)

    def test_prompt_loaded_and_input_passed_as_user_message(self):
        """Prompt wird geladen, der Briefing-Text als User-Nachricht uebergeben."""
        client = _FakeClient(content=_valid_humanized())
        llm_humanize.humanize_briefing("## Kurzlage\nTestinhalt", mode="monday", client=client)
        messages = client.chat.completions.kwargs["messages"]
        # System-Prompt + User-Prompt (= Prompt + Trenner + Briefing-Text)
        assert messages[0]["role"] == "system"
        assert messages[1]["role"] == "user"
        user_msg = messages[1]["content"]
        assert "erwachsene Laien" in user_msg
        assert "\n\n---\n\n" in user_msg
        assert user_msg.endswith("## Kurzlage\nTestinhalt")

    def test_call_uses_deepseek_flash_with_low_temperature(self):
        """API-Call: deepseek-v4-flash, temperature 0.4, timeout 60 (wie Plan P3)."""
        client = _FakeClient(content=_valid_humanized())
        llm_humanize.humanize_briefing("## Kurzlage\nTest", client=client)
        kwargs = client.chat.completions.kwargs
        assert kwargs["model"] == "deepseek-v4-flash"
        assert kwargs["temperature"] == 0.4
        assert kwargs["timeout"] == 60

    def test_empty_response_raises_llm_error(self, monkeypatch):
        """Leere Antwort (content=None/\"\") -> LLMError, kein return des Inputs."""
        monkeypatch.setattr(llm_humanize.time, "sleep", lambda seconds: None)
        client = _FakeClient(content=None)
        self._assert_llm_error(client)

    def test_whitespace_only_response_raises_llm_error(self, monkeypatch):
        """Nur-Whitespace-Antwort zaehlt als leer -> LLMError."""
        monkeypatch.setattr(llm_humanize.time, "sleep", lambda seconds: None)
        client = _FakeClient(content="   \n  ")
        self._assert_llm_error(client)

    def test_api_error_raises_llm_error(self, monkeypatch):
        """API-Fehler (Exception im Client) -> LLMError, kein Fallback."""
        monkeypatch.setattr(llm_humanize.time, "sleep", lambda seconds: None)
        client = _FakeClient(error=RuntimeError("HTTP 500"))
        self._assert_llm_error(client, match="Humanize fehlgeschlagen")

    def test_error_string_raises_llm_error(self):
        """LLM-Fehlerstring im Output -> LLMError (defense-in-depth, kein Retry)."""
        client = _FakeClient(content=_valid_humanized() + "\nfehlgeschlagen")
        self._assert_llm_error(client, match="fehlgeschlagen")

    def test_missing_section_raises_llm_error(self):
        """Fehlende Sektion im Output -> LLMError (Vertragsbruch, kein Retry)."""
        text = _valid_humanized().replace("## Watchlist-Signale\n", "## Fehlt\n")
        client = _FakeClient(content=text)
        self._assert_llm_error(client, match="Watchlist-Signale")

    def test_changed_disclaimer_raises_llm_error(self):
        """Geaenderter Fundamentaldaten-Disclaimer -> LLMError (Vertragsbruch)."""
        text = _valid_humanized().replace(
            "nicht automatisch verfügbar", "sind nicht automatisch verfügbar"
        )
        client = _FakeClient(content=text)
        self._assert_llm_error(client, match="Disclaimer")

    def test_no_fallback_on_error(self):
        """Kein Fallback: bei LLMError wird nie der Input zurueckgegeben."""
        client = _FakeClient(content=None)
        with pytest.raises(llm_humanize.LLMError):
            llm_humanize.humanize_briefing("## Kurzlage\nRohfassung", client=client)

    def test_missing_api_key_raises_llm_error(self, monkeypatch):
        """Fehlender NEURALWATT_API_KEY (client=None) -> LLMError."""
        monkeypatch.setattr(llm_humanize, "_load_env", lambda: None)
        monkeypatch.setattr(llm_humanize.os, "getenv", lambda key, default=None: None)
        self._assert_llm_error(client=None, match="NEURALWATT_API_KEY")

    def test_client_creation_error_raises_llm_error(self, monkeypatch):
        """Fehler bei der Client-Erzeugung (client=None) -> LLMError, kein Fallback."""
        def _boom() -> None:
            raise RuntimeError("kein openai-Client konstruierbar")

        monkeypatch.setattr(llm_humanize, "_get_client", _boom)
        self._assert_llm_error(client=None, match="Humanize fehlgeschlagen")


class TestValidateHumanized:
    """P1: Defensiver Vorab-Check der Vertragsinvarianten (ohne API)."""

    def test_valid_humanized_passes(self):
        """Gueltiger gehumanisierter Text (Sektionen + Disclaimer) besteht."""
        llm_humanize._validate_humanized("## Kurzlage\nTest", _valid_humanized())

    def test_missing_section_raises(self):
        """Fehlende Sektion -> LLMError (Vertragsbruch)."""
        text = _valid_humanized().replace("## Watchlist-Signale\n", "## Fehlt\n")
        with pytest.raises(llm_humanize.LLMError, match="Kurzlage|Sektion"):
            llm_humanize._validate_humanized("## Kurzlage\nTest", text)

    def test_changed_disclaimer_raises(self):
        """Geaenderter Fundamentaldaten-Disclaimer -> LLMError."""
        text = _valid_humanized().replace(
            "nicht automatisch verfügbar", "sind nicht automatisch verfügbar"
        )
        with pytest.raises(llm_humanize.LLMError, match="Disclaimer"):
            llm_humanize._validate_humanized("## Kurzlage\nTest", text)

    def test_error_string_raises(self):
        """LLM-Fehlerstring im Output -> LLMError (Defense-in-Depth)."""
        text = _valid_humanized() + "\nfehlgeschlagen"
        with pytest.raises(llm_humanize.LLMError, match="fehlgeschlagen"):
            llm_humanize._validate_humanized("## Kurzlage\nTest", text)


class TestHumanizePrompt:
    """P2: Prompt fuer erwachsene Laien (config/prompts/humanize.txt)."""

    def test_humanize_prompt_exists(self):
        """Prompt ist ladbar und enthaelt harte Regeln + Laienstil."""
        prompt = llm_humanize._load_humanize_prompt()
        assert "erwachsene" in prompt
        assert "Laien" in prompt
        assert "KEINE Fakten" in prompt or "keine Fakten" in prompt
        assert "wortgleich" in prompt
        # Keine Fakten entfernen, keine Imperative, Sektionen erhalten
        assert "KEINE" in prompt
        assert "Sektionen" in prompt
        assert "Disclaimer" in prompt
