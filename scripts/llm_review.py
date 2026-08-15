"""Review a draft against the facts package via glm-5.2 (OpenAI-compatible API).

Stufe 4 der Two-Stage-Briefing-Pipeline: glm-5.2 liefert ein striktes
JSON-Review zu einem Draft. Fail-closed: jeder Fehler (fehlender API-Key,
API-Error, leere Antwort, ungueltiges JSON, fehlende Felder, unbekannte
severity, ungueltiges overall_verdict, fehlende/pauschale Evidenz bei
critical/major) wirft LLMError statt Fehlertext zurueckzugeben. Keine
Secrets werden geloggt oder in Fehlermeldungen aufgenommen.
"""
from __future__ import annotations

import json
import os
import re
import string
import time
from pathlib import Path

import openai
from dotenv import load_dotenv

from scripts.llm_briefing import LLMError

ENV_PATH = Path.home() / ".config" / "automation" / "config.env"
DEFAULT_BASE_URL = "https://api.neuralwatt.com/v1"
MODEL = "glm-5.2"
REVIEW_PROMPT_PATH = Path(__file__).resolve().parent.parent / "config" / "prompts" / "review.txt"

VALID_SEVERITIES = {"critical", "major", "minor", "info"}
VALID_VERDICTS = {"pass", "revise", "block"}
FINDING_FIELDS = ("severity", "issue", "evidence", "correction")

# Evidence-Anforderungen fuer critical/major Findings (fail-closed).
# Unter dieser Laenge ist eine Evidence nur ein Schlagwort, keine belastbare Angabe.
_MIN_EVIDENCE_CHARS = 20
# Konkrete Anker: Anfuehrungszeichen (Zitat) oder Ziffer (Zahl).
_EVIDENCE_ANCHOR_RE = re.compile(r'["\'„“”«»0-9]')
# Explizite Quellen-Zitation ("Draft: ...", "Faktenpaket: ...", "News: ...", "Zitat: ...").
_SOURCE_CITATION_RE = re.compile(r"(?i)(?:draft|faktenpaket|news|zitat)\s*:")
# Eine vollstaendige Markdown-Codefence (```json ... ``` bzw. ``` ... ```),
# die den gesamten Content ausmacht. Nur dann wird sie vor json.loads entfernt —
# niemals JSON-Extraktion aus Prosa (fail-closed).
_FENCE_RE = re.compile(r"```(?:json)?\s*([\s\S]*?)\s*```", re.DOTALL)


def _evidence_is_specific(evidence: str) -> bool:
    """Konkrete, belastbare Evidenz statt Pauschalbehauptung.

    Erforderlich: nicht leer, ausreichend lang und mindestens ein konkreter
    Anker — Zitat, Zahl oder Quellen-Zitation. Ohne Anker bleibt nur eine
    pauschale Behauptung ("stimmt nicht", "falsch", "inkonsistent"), die
    fail-closed abgelehnt wird.
    """
    text = evidence.strip()
    if not text or len(text) < _MIN_EVIDENCE_CHARS:
        return False
    return bool(_EVIDENCE_ANCHOR_RE.search(text) or _SOURCE_CITATION_RE.search(text))


def _validate_evidence(finding: dict) -> None:
    """Fail-closed Evidence-Check fuer critical/major Findings.

    Evidence muss ein nicht-leerer, ausreichend spezifischer String sein —
    kein bloßes Pauschalurteil. Wirft LLMError bei Verstoß. info/minor
    bleiben ungeprueft (weniger strikt).
    """
    severity = finding["severity"]
    if severity not in ("critical", "major"):
        return
    evidence = finding["evidence"]
    if not isinstance(evidence, str) or not evidence.strip():
        raise LLMError(
            f"Finding severity='{severity}' ohne Evidenz: 'evidence' fehlt oder ist leer."
        )
    if not _evidence_is_specific(evidence):
        raise LLMError(
            f"Finding severity='{severity}' mit pauschaler Evidenz: {evidence!r} — "
            "konkrete, belastbare Evidenz erforderlich (Zitat, Zahl oder Quellen-Angabe "
            "aus Draft/Faktenpaket/News)."
        )


def _load_env() -> None:
    if ENV_PATH.exists():
        load_dotenv(ENV_PATH, override=True)


def _get_client() -> openai.OpenAI:
    _load_env()
    api_key = os.getenv("NEURALWATT_API_KEY")
    if not api_key:
        raise LLMError("Review fehlgeschlagen: NEURALWATT_API_KEY fehlt.")
    base_url = os.getenv("NEURALWATT_BASE_URL", DEFAULT_BASE_URL)
    return openai.OpenAI(api_key=api_key, base_url=base_url)


def _load_prompt() -> str:
    """Load the review prompt template (string.Template: $facts, $draft)."""
    return REVIEW_PROMPT_PATH.read_text(encoding="utf-8")


def _review_context(facts_package: dict) -> dict:
    """Reduzierter Faktenkontext fuer den Review-Prompt (ohne Transaktionsdetails).

    GLM bewertet semantische Qualitaet gegen deterministische Fakten — es
    braucht keine Rohdaten: keine ``transactions`` (keine Kauf-/Verkaufs-
    zeitpunkte, Preise, Mengen), keine Positionswerte (``value_eur``/
    ``quantity``), kein vollstaendiges Analysis-/Strategie-JSON. Enthalten
    bleiben nur die fuer die semantische Pruefung noetigen deterministischen
    Anteile — ``deterministic_summary`` (Summary/Status-Listen, gleiche Quelle
    wie verify), ``strategy_thresholds_pct`` (Grenzwerte), reduzierte
    Holdings/News, die Trigger-Flags (``verify.compute_triggers``), der
    wertefreie ``strategy_diff`` (nur Feld-Pfade) und ``data_quality``
    (Status/Issues) sowie ``meta``. Ohne Transaktionsdaten kann GLM weder
    Transaktionszeitraeume noch Verhaltensaussagen zu Trades bewerten.
    """
    from scripts.verify import compute_triggers  # lazy: verify importiert llm_briefing

    holdings = facts_package.get("portfolio", {}).get("holdings", [])
    reduced_holdings = [
        {
            "isin": h.get("isin"),
            "name": h.get("name"),
            "ticker": h.get("ticker"),
            "category": h.get("category"),
        }
        for h in holdings
        if isinstance(h, dict)
    ]
    news = facts_package.get("news", [])
    reduced_news = [
        {
            "title": n.get("title"),
            "summary": n.get("summary") or n.get("description"),
        }
        for n in news
        if isinstance(n, dict)
    ]
    return {
        "meta": facts_package.get("meta", {}),
        "portfolio": reduced_holdings,
        "news": reduced_news,
        "deterministic_summary": facts_package.get("deterministic_summary", {}),
        "strategy_thresholds_pct": facts_package.get("strategy_thresholds_pct", {}),
        "triggers": compute_triggers(facts_package),
        "strategy_diff": facts_package.get("strategy_diff") or {},
        "data_quality": facts_package.get("data_quality") or {},
    }


def _strip_code_fence(content: str) -> str:
    """Entferne eine einzelne Markdown-Codefence, die den gesamten Content ausmacht.

    Das ``json``-Tag ist optional, umgebender Whitespace wird toleriert.
    Bildet der Content keinen vollstaendigen Fence (z. B. Prosa vor dem JSON),
    bleibt er unveraendert: JSON wird nie aus Prosa extrahiert (fail-closed).
    """
    match = _FENCE_RE.fullmatch(content.strip())
    if match is None:
        return content
    return match.group(1)


def _parse_review(content: str) -> dict:
    """Parse and validate the strict JSON review contract.

    Raises LLMError on any contract violation (fail-closed).
    """
    try:
        data = json.loads(_strip_code_fence(content))
    except json.JSONDecodeError as e:
        raise LLMError(f"Review-Antwort ist kein gueltiges JSON: {e}") from e

    if not isinstance(data, dict):
        raise LLMError("Review-Antwort ist kein JSON-Objekt.")
    if "findings" not in data:
        raise LLMError("Review-Antwort enthaelt keinen 'findings'-Schluessel.")
    findings = data["findings"]
    if not isinstance(findings, list):
        raise LLMError("'findings' ist keine Liste.")

    for finding in findings:
        if not isinstance(finding, dict):
            raise LLMError("Finding ist kein JSON-Objekt.")
        for field in FINDING_FIELDS:
            if field not in finding:
                raise LLMError(f"Finding enthaelt kein Feld '{field}'.")
        if finding["severity"] not in VALID_SEVERITIES:
            raise LLMError(
                f"Unbekannte severity '{finding['severity']}' "
                f"(erlaubt: {', '.join(sorted(VALID_SEVERITIES))})."
            )
        _validate_evidence(finding)

    if "overall_verdict" not in data:
        raise LLMError("Review-Antwort enthaelt keinen 'overall_verdict'-Schluessel.")
    verdict = data["overall_verdict"]
    if verdict not in VALID_VERDICTS:
        raise LLMError(
            f"Unbekanntes overall_verdict '{verdict}' "
            f"(erlaubt: {', '.join(sorted(VALID_VERDICTS))})."
        )
    return data


def review_draft(facts_package: dict, draft: str, client=None) -> dict:
    """Review a draft against the facts package via glm-5.2.

    The prompt receives a reduced facts context (``_review_context``): only
    deterministic summary/status/threshold values plus reduced holdings and
    news — no transactions and no raw transaction data, so GLM cannot
    evaluate transaction periods or behavioral statements about trades.
    Returns the validated review dict ({"findings": [...], "overall_verdict": "..."}).
    Raises LLMError on missing API key, API error, empty response or any
    contract violation. Leerer Content faellt auf reasoning_content zurueck
    (glm-5.2 Thinking). Sind content und reasoning_content beide leer, ist
    der Versuch retrybar (2 Versuche, wie im bestehenden Retry-Muster von
    llm_briefing), danach LLMError. Vertragsverletzungen in der Antwort
    (ungueltiges JSON, unbekannte severity, ungueltige Evidenz bei
    critical/major, ...) sind NICHT retrybar — sie werfen sofort LLMError
    ohne weiteren LLM-Call. Never returns error text as review.
    """
    client = client or _get_client()
    template = string.Template(_load_prompt())
    prompt = template.substitute(
        facts=json.dumps(_review_context(facts_package), ensure_ascii=False, indent=2),
        draft=draft,
    )
    last_error = None
    for attempt in range(2):
        try:
            response = client.chat.completions.create(
                model=MODEL,
                messages=[
                    {"role": "system", "content": "Du bist ein praeziser Fakten-Reviewer."},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.3,
                max_tokens=4096,
                timeout=60,
                # NeuralWatt-kompatibel: Reasoning fuer glm-5.2 explizit aus.
                reasoning_effort="none",
            )
            message = response.choices[0].message
            content = message.content
            if not content or not content.strip():
                # glm-5.2 liefert Thinking-Texte in reasoning_content: als Fallback nutzen.
                content = getattr(message, "reasoning_content", None)
            if not content or not content.strip():
                raise ValueError("Empty response from LLM")
            return _parse_review(content)
        except LLMError:
            # Vertragsverletzung (inkl. ungueltiger Evidenz): fail-closed, sofort —
            # ein weiterer LLM-Call kann das Modell nicht zu gueltigem Contract zwingen.
            raise
        except Exception as e:
            last_error = e
            if attempt < 1:
                time.sleep(2 ** attempt)
            continue
    raise LLMError(f"Review fehlgeschlagen: {last_error}") from last_error
