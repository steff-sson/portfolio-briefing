"""Humanizer — sprachliche Umschreibung des deterministischen Briefings (P1-P3).

``humanize_briefing(briefing_text, mode, client)`` laedt den Humanizer-
Prompt (config/prompts/humanize.txt), uebergibt den deterministisch
gerenderten Briefing-Text als User-Nachricht und validiert die Antwort vor
der Rueckgabe (``_validate_humanized``: Sektionen, Disclaimer, keine
Fehlerstrings). Die fachliche Quelle bleibt der deterministische Renderer
``final_briefing.render_final_briefing`` — der Humanizer schreibt
ausschliesslich die Sprache um, keine Fakten/Zahlen/Sektionen/Labels
aendern. Fail-closed ist der Vertrag: bei LLM-Fehlern (fehlender Key,
API-Fehler, leere Antwort, Vertragsbruch) wird ``LLMError`` geworfen, NIE
der Input zurueckgegeben (kein Fallback auf die deterministische
Rohfassung).

Modul ist reines Modul ohne Seiteneffekte ausser dem API-Call: kein IO
beim Import, keine Secrets.
"""
from __future__ import annotations

import os
import re
import time
from pathlib import Path

import openai
from dotenv import load_dotenv

from scripts.llm_briefing import LLMError  # keine Duplikation (Spiegel llm_review/llm_revise)

ENV_PATH = Path.home() / ".config" / "automation" / "config.env"
DEFAULT_BASE_URL = "https://api.neuralwatt.com/v1"
MODEL = "deepseek-v4-flash"
HUMANIZE_PROMPT_PATH = Path(__file__).resolve().parent.parent / "config" / "prompts" / "humanize.txt"

# Humanizer-Vertrag: Sektionen in exakt dieser Reihenfolge, zeilenverankert
# geprueft (gleiche Quelle wie verify.DRAFT_SECTIONS — via Import, keine
# Logik-Duplikation; siehe _validate_humanized).
HUMANIZE_SECTIONS = (
    "## Kurzlage",
    "## Datenqualität",
    "## Sell-/Reduce-Signale (bestehende Satellites)",
    "## Watchlist-Signale",
    "## Nächster Schritt",
)

# Kein Fallback auf die deterministische Rohfassung (User-Vorgabe): bei
# Humanizer-Vertragsbruch/Fehler wird fail-closed abgebrochen. Diese
# Konstante dokumentiert die Regel und macht sie testbar — der spätere
# Humanizer-Pfad (run_briefing) MUSS sie einhalten.
NO_FALLBACK_POLICY = (
    "Kein Fallback auf die deterministische Rohfassung: Bei Humanizer-Fehler "
    "wird fail-closed abgebrochen (kein Versand, keine Vault-Datei)."
)


def _load_humanize_prompt() -> str:
    """Lade den Humanizer-Prompt (config/prompts/humanize.txt).

    Existiert der Prompt nicht, wird Fail-closed beibehalten: kein
    Zurueckfallen auf einen Draft-Prompt, kein roher Text.
    """
    if not HUMANIZE_PROMPT_PATH.exists():
        raise LLMError("Humanize-Prompt fehlt (config/prompts/humanize.txt).")
    return HUMANIZE_PROMPT_PATH.read_text(encoding="utf-8")


def _load_env() -> None:
    if ENV_PATH.exists():
        load_dotenv(ENV_PATH, override=True)


def _get_client() -> openai.OpenAI:
    """Client fuer den Humanizer-Call (Spiegel llm_review/llm_revise).

    Gleicher NEURALWATT_API_KEY, gleiche Base-URL (default NeuralWatt,
    ueberschreibbar via NEURALWATT_BASE_URL). Fehlender Key -> LLMError
    (fail-closed).
    """
    _load_env()
    api_key = os.getenv("NEURALWATT_API_KEY")
    if not api_key:
        raise LLMError("Humanize fehlgeschlagen: NEURALWATT_API_KEY fehlt.")
    base_url = os.getenv("NEURALWATT_BASE_URL", DEFAULT_BASE_URL)
    return openai.OpenAI(api_key=api_key, base_url=base_url)


def _validate_humanized(briefing_text: str, content: str) -> None:
    """Defensiver Vorab-Check des gehumanisierten Texts (P1-Contract).

    Prueft, dass der gehumanisierte Text die Vertragsinvarianten erhaelt:
    - alle Sektionen vorhanden (zeilenverankert, gleicher Regex wie verify),
    - Fundamentaldaten-Disclaimer per Substring enthalten (wie verify),
    - keine LLM-Fehlerstrings (Defense-in-Depth, wie verify.ERROR_MARKERS).

    Wirft bei Verstoss LLMError (fail-closed). Nutzt die verify-Konstanten
    via Import — keine Logik-Duplikation, nur Anwendung.
    """
    from scripts.verify import ERROR_MARKERS, FUNDAMENTALS_DISCLAIMER

    for marker in ERROR_MARKERS:
        if marker in content:
            raise LLMError(f"Humanizer-Vertragsbruch: LLM-Fehlerstring-Marker '{marker}' im Output.")

    for section in HUMANIZE_SECTIONS:
        if not re.search(rf"^{re.escape(section)}$", content, re.MULTILINE):
            raise LLMError(f"Humanizer-Vertragsbruch: Sektion '{section}' fehlt im gehumanisierten Text.")

    if FUNDAMENTALS_DISCLAIMER.lower() not in content.lower():
        raise LLMError("Humanizer-Vertragsbruch: Fundamentaldaten-Disclaimer fehlt oder wurde geaendert.")


def humanize_briefing(briefing_text: str, mode: str = "monday", client=None) -> str:
    """Humanizer (P3): LLM schreibt die Sprache des deterministischen Texts um.

    Input: ``briefing_text`` = deterministisch gerendertes Markdown
    (``final_briefing.render_final_briefing`` — alle Sektionen, Disclaimer,
    Labels). Output: sprachlich umgeschriebenes Markdown mit gleichen
    Sektionen in gleicher Reihenfolge, gleichen Fakten (Zahlen, ISIN,
    Labels, Ampeln) und wortgleichem Fundamentaldaten-Disclaimer.

    Ablauf:
    1. Prompt laden (``_load_humanize_prompt``).
    2. User-Nachricht = Prompt + ``\\n\\n---\\n\\n`` + ``briefing_text``.
    3. API-Call: ``deepseek-v4-flash`` (gleicher wie Draft), temperature 0.4
       (Sprachvielfalt, aber deterministisch genug), timeout 60, max_tokens
       4096, ein Retry mit Backoff (Muster llm_briefing/llm_review).
    4. Leere Antwort -> ``LLMError``.
    5. ``_validate_humanized`` (defensiver Vorab-Check: Sektionen,
       Disclaimer, keine Fehlerstrings) — Vertragsbruch -> ``LLMError``.
    6. Return ``content``.

    Fail-closed: API-/Parsing-/Validierungsfehler -> ``LLMError``, NIE ein
    Return des Inputs (kein Fallback auf die deterministische Rohfassung).
    """
    prompt = _load_humanize_prompt()
    user_message = f"{prompt}\n\n---\n\n{briefing_text}"

    try:
        client = client or _get_client()
    except LLMError:
        raise
    except Exception as e:
        raise LLMError(f"Humanize fehlgeschlagen: {e}") from e

    last_error = None
    for attempt in range(2):
        try:
            response = client.chat.completions.create(
                model=MODEL,
                messages=[
                    {"role": "system", "content": "Du bist ein praeziser Editor fuer Portfolio-Briefings."},
                    {"role": "user", "content": user_message},
                ],
                temperature=0.4,
                max_tokens=4096,
                timeout=60,
            )
            content = response.choices[0].message.content
            if not content or not content.strip():
                raise ValueError("Empty response from LLM")
            _validate_humanized(briefing_text, content)
            return content
        except LLMError:
            # Vertragsbruch (fehlende Sektion, geaenderter Disclaimer,
            # Fehlerstring): fail-closed sofort — kein weiterer LLM-Call.
            raise
        except Exception as e:
            last_error = e
            if attempt < 1:
                time.sleep(2 ** attempt)
            continue
    raise LLMError(f"Humanize fehlgeschlagen: {last_error}") from last_error
