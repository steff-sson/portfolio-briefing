"""Revise a draft against review findings via deepseek-v4-flash (OpenAI-compatible API).

Stufe 5 der Two-Stage-Briefing-Pipeline: deepseek-v4-flash ueberarbeitet
den Draft unter Behebung der (nicht-kritischen) Review-Findings. Fail-closed:
jeder Fehler (fehlender API-Key, API-Error, leere Antwort) wirft LLMError
statt Fehlertext zurueckzugeben. Keine Secrets werden geloggt oder in
Fehlermeldungen aufgenommen.
"""
from __future__ import annotations

import json
import os
import string
from pathlib import Path

import openai
from dotenv import load_dotenv

from scripts.llm_briefing import LLMError

ENV_PATH = Path.home() / ".config" / "automation" / "config.env"
DEFAULT_BASE_URL = "https://api.neuralwatt.com/v1"
MODEL = "deepseek-v4-flash"
REVISE_PROMPT_PATH = Path(__file__).resolve().parent.parent / "config" / "prompts" / "revise.txt"


def _load_env() -> None:
    if ENV_PATH.exists():
        load_dotenv(ENV_PATH, override=True)


def _get_client() -> openai.OpenAI:
    _load_env()
    api_key = os.getenv("NEURALWATT_API_KEY")
    if not api_key:
        raise LLMError("Revision fehlgeschlagen: NEURALWATT_API_KEY fehlt.")
    base_url = os.getenv("NEURALWATT_BASE_URL", DEFAULT_BASE_URL)
    return openai.OpenAI(api_key=api_key, base_url=base_url)


def _load_prompt() -> str:
    """Load the revise prompt template (string.Template: $facts, $draft, $review)."""
    return REVISE_PROMPT_PATH.read_text(encoding="utf-8")


def _revise_facts_context(facts_package: dict) -> dict:
    """Reduzierter Faktenkontext fuer den Revise-Prompt (wie Draft/Review).

    Die Revision darf keine Rohdaten sehen: keine Top-Level-``transactions``,
    keine Positionswerte (``value_eur``/``quantity``), keine Roh-Checks aus
    ``analysis`` und kein vollstaendiges Strategie-JSON. Die MVP-GRENZE
    verbietet zusaetzlich die Behandlung einzelner Trades: die
    Transaktions-Datensaetze im ``changes``-Diff (Zeitpunkt/Menge/Preis)
    bleiben aus dem Revise-Prompt; aggregierte Diff-Werte bleiben erhalten.

    Enthalten bleiben nur deterministische Anteile: ``deterministic_summary``
    (einzige erlaubte Zahlenquelle), ``strategy_thresholds_pct`` (Grenzwerte),
    reduzierte ``changes``-Aggregate, reduzierte Holdings/News, die
    Trigger-Flags (``verify.compute_triggers``), der wertefreie
    ``strategy_diff`` (nur Feld-Pfade), ``data_quality`` und ``meta``.
    """
    from scripts import facts as facts_module
    from scripts.verify import compute_triggers  # lazy: verify importiert llm_briefing

    if not isinstance(facts_package, dict):
        return facts_package
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
        "changes": facts_module.reduce_changes_for_llm(facts_package.get("changes")),
        "deterministic_summary": facts_package.get("deterministic_summary", {}),
        "strategy_thresholds_pct": facts_package.get("strategy_thresholds_pct", {}),
        "triggers": compute_triggers(facts_package),
        "strategy_diff": facts_package.get("strategy_diff") or {},
        "data_quality": facts_package.get("data_quality") or {},
    }


def revise_draft(facts_package: dict, draft: str, review: dict, client=None) -> str:
    """Revise a draft addressing all major/minor review findings via deepseek-v4-flash.

    Returns the revised draft markdown (text contract: only the markdown,
    no intro). Raises LLMError on missing API key, API error or empty
    response — never returns error text as revised draft.
    """
    client = client or _get_client()
    template = string.Template(_load_prompt())
    prompt = template.substitute(
        facts=json.dumps(_revise_facts_context(facts_package), ensure_ascii=False, indent=2),
        draft=draft,
        review=json.dumps(review, ensure_ascii=False, indent=2),
    )
    try:
        response = client.chat.completions.create(
            model=MODEL,
            messages=[
                {"role": "system", "content": "Du bist ein praeziser Revisor fuer Portfolio-Briefings."},
                {"role": "user", "content": prompt},
            ],
            temperature=0.2,
            max_tokens=4096,
            timeout=60,
        )
        content = response.choices[0].message.content
    except Exception as e:
        raise LLMError(f"Revision fehlgeschlagen: {e}") from e

    if not content or not content.strip():
        raise LLMError("Revision fehlgeschlagen: leere Antwort vom LLM.")
    return content
