"""Generate LLM briefing via NeuralWatt API (OpenAI-compatible)."""
from __future__ import annotations

import json
import os
import time
from datetime import datetime
from pathlib import Path

import openai
from dotenv import load_dotenv

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"
PROMPTS_DIR = CONFIG_DIR / "prompts"
ENV_PATH = Path.home() / ".config" / "automation" / "config.env"


class LLMError(Exception):
    """LLM call failed (missing key, API error, empty response).

    Raised instead of returning error text, so callers can fail closed.
    """


def _load_env() -> None:
    if ENV_PATH.exists():
        load_dotenv(ENV_PATH, override=True)


def _load_prompt(mode: str, context: dict) -> str:
    """Load the briefing prompt template and fill it from a context dict.

    Mode-independent: the single ``briefing.txt`` template replaces the old
    per-mode prompts (monday/friday/monthly/humanize). ``context`` must
    provide the placeholder ``facts`` (raw dict — the values are
    JSON-serialized here). Nach der Fakten-Serialisierung wird die
    deterministische ZULÄSSIGE-ZAHLEN-Allowlist angehängt (gleiche Quelle
    wie das verify-Gate); der q4_tax_context-Anhang bleibt danach.
    """
    prompt_file = PROMPTS_DIR / "briefing.txt"
    template = prompt_file.read_text(encoding="utf-8")
    prompt = template.format(
        facts=json.dumps(context.get("facts") or {}, ensure_ascii=False, indent=2),
    )
    prompt += _zulaessige_zahlen_block(context.get("facts") or {})
    if 10 <= datetime.now().month <= 12:
        tax_file = PROMPTS_DIR / "q4_tax_context.txt"
        if tax_file.exists():
            prompt = prompt + "\n\n" + tax_file.read_text(encoding="utf-8")
    return prompt


def _zulaessige_zahlen_block(facts_package: dict) -> str:
    """Deterministische ``ZULÄSSIGE ZAHLEN``-Allowlist am Prompt-Ende.

    Vereinigt summary-Prozente, Strategie-Grenzwerte, Holdings-Gewichte und
    ETF-TERs — alle direkt aus verify (gleiche Quelle wie das Gate, kann nie
    divergieren): dedupliziert, absteigend sortiert. Werte >= 1.0 mit 1
    Dezimalstelle (wie bisher), TERs (Werte < 1.0) mit 2 Dezimalstellen,
    damit 0.08% in Prompt-Liste und Gate-Liste identisch sind.
    """
    from scripts.verify import (
        _etf_ters_pct,
        _holdings_weights_pct,
        _strategy_thresholds_pct,
        _summary_numbers_pct,
    )

    summary = facts_package.get("deterministic_summary", {})
    thresholds = facts_package.get("strategy_thresholds_pct", {})
    values = sorted(
        set(
            _summary_numbers_pct(summary)
            + _strategy_thresholds_pct(thresholds)
            + _holdings_weights_pct(facts_package)
            + _etf_ters_pct(facts_package)
        ),
        reverse=True,
    )
    numbers = ", ".join(
        f"{value:.2f}" if value < 1.0 else f"{value:.1f}" for value in values
    )
    if not numbers:
        numbers = "(keine — keine Prozentwerte zulässig)"
    return (
        "\n\n## ZULÄSSIGE ZAHLEN (nur diese dürfen im Briefing vorkommen)\n"
        f"{numbers}\n"
    )


def _draft_context(facts_package: dict) -> dict:
    """Build the reduced, deterministic context the draft LLM may see.

    The draft must not receive raw data with uncontrolled percentages: no
    raw transactions, no full analysis/strategy JSON and no holdings with
    ``value_eur``/``quantity`` (Positionsgewicht-Berechnungsgrundlagen).
    The context only contains:

    - ``portfolio``: reduced holdings metadata (isin/name/ticker/category)
      so the Ticker/ISIN-Pruefung remains possible;
    - ``analysis``: the deterministically computed findings
      (red/yellow/green check lists + outdated theses);
    - ``news``: only titles and summaries;
    - ``strategy``: the already extracted percentage limits
      (``strategy_thresholds_pct``);
    - ``triggers``: deterministische Trigger-Flags (verify.compute_triggers)
      fuer die Optionen-Regel;
    - ``strategy_diff``: nur Feld-Pfade der Strategieaenderung (keine Werte,
      keine sensitiven Strategieinhalte);
    - ``data_quality``: Status/Issues der deterministischen
      Datenqualitaetspruefung (hat Vorrang vor Portfolio-Bewertung);
    - ``changes``: reduziertes Diff (``facts.reduce_changes_for_llm``) ohne
      Roh-Transaktions-Records — aggregierte Diff-Werte und der wertefreie
      Strategie-Diff (nur Feld-Pfade/Hashes) bleiben erhalten.

    Numbers may only come from ``deterministic_summary``, which is appended
    separately by the caller (see ``generate_draft``).
    """
    from scripts import facts as facts_module  # facts ist LLM-frei, kein Import-Zyklus
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
    summary = facts_package.get("deterministic_summary", {})
    return {
        "portfolio": reduced_holdings,
        "analysis": {
            "red_checks": summary.get("red_checks", []),
            "yellow_checks": summary.get("yellow_checks", []),
            "green_checks": summary.get("green_checks", []),
            "outdated_theses": summary.get("outdated_theses", []),
        },
        "news": reduced_news,
        "strategy": facts_package.get("strategy_thresholds_pct", {}),
        "changes": facts_module.reduce_changes_for_llm(facts_package.get("changes")),
        "triggers": compute_triggers(facts_package),
        "strategy_diff": facts_package.get("strategy_diff") or {},
        "data_quality": facts_package.get("data_quality") or {},
        # Briefing-Schnittstelle (Plan §6a): Ampeln, Gesamt-Empfehlung und
        # Positionsvorschlaege sind deterministisch — das LLM uebernimmt sie 1:1.
        "traffic_lights": summary.get("traffic_lights", {}),
        "recommendation": summary.get("recommendation", {}),
        "position_actions": summary.get("position_actions", []),
    }


def _allowed_pct_values(facts_package: dict) -> list[float]:
    """Prozentwerte, die der Draft verwenden darf — gleiche Quelle wie verify.

    Reuses verify's extraction unchanged (deterministic_summary ratios -> %,
    positive strategy thresholds as-is, Holdings-Gewichte aus value_eur), so
    the prompt allowlist can never diverge from the verify gate. No new
    tolerance/allowlist logic here.
    """
    from scripts.verify import (
        _holdings_weights_pct,
        _strategy_thresholds_pct,
        _summary_numbers_pct,
    )

    summary = facts_package.get("deterministic_summary", {})
    thresholds = facts_package.get("strategy_thresholds_pct", {})
    return (
        _summary_numbers_pct(summary)
        + _strategy_thresholds_pct(thresholds)
        + _holdings_weights_pct(facts_package)
    )


def _format_allowed_pct_list(facts_package: dict) -> str:
    """Deterministic ``ZULÄSSIGE ZAHLEN`` line: sorted, deduplicated, 1 decimal."""
    values = sorted(set(_allowed_pct_values(facts_package)))
    if not values:
        return "ZULÄSSIGE ZAHLEN: (keine — keine Prozentwerte zulässig)"
    return "ZULÄSSIGE ZAHLEN: " + ", ".join(f"{v:.1f}%" for v in values)


def _get_client() -> openai.OpenAI:
    _load_env()
    api_key = os.getenv("NEURALWATT_API_KEY")
    if not api_key:
        raise RuntimeError("NEURALWATT_API_KEY not set")
    return openai.OpenAI(api_key=api_key, base_url="https://api.neuralwatt.com/v1")


def generate_briefing(
    portfolio: dict,
    analysis: dict,
    news: list,
    strategy: dict,
    mode: str = "monday",
) -> str:
    """Single-stage LLM filter+composition (legacy path).

    Fail-closed: raises LLMError instead of returning error text as briefing.
    Deprecated: the pipeline uses ``generate_draft``; this legacy entry
    point still passes the full raw context via ``briefing.txt``.
    """
    prompt = _load_prompt(
        mode,
        {"facts": {"portfolio": portfolio, "analysis": analysis, "news": news, "strategy": strategy}},
    )
    try:
        client = _get_client()
    except RuntimeError:
        raise LLMError("Briefing-Generierung fehlgeschlagen: API-Key fehlt.") from None

    last_error = None
    for attempt in range(2):
        try:
            response = client.chat.completions.create(
                model="glm-5.2",
                messages=[
                    {"role": "system", "content": "Du bist ein präziser Portfoliobeobachter."},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.3,
                max_tokens=4096,
                timeout=60,
            )
            content = response.choices[0].message.content
            if not content:
                raise ValueError("Empty response from LLM")
            return content
        except Exception as e:
            last_error = e
            if attempt < 1:
                time.sleep(2 ** attempt)
            continue
    raise LLMError(f"Briefing-Generierung fehlgeschlagen: {last_error}") from last_error


def generate_draft(facts_package: dict, mode: str = "monday", client=None) -> str:
    """Two-stage pipeline stage 2: structured draft via deepseek-v4-flash.

    Builds the prompt from the deterministic facts package only: the draft
    sees reduced holdings metadata, deterministic findings, news
    titles/summaries, ``strategy_thresholds_pct`` and ``deterministic_summary``
    as the only allowed numbers — never raw transactions, positions values
    or the full analysis/strategy JSON. Returns the draft markdown.
    Fail-closed: raises LLMError on missing key, API error or empty response
    — never returns error text as draft.
    """
    context = _draft_context(facts_package)
    # 1-Call-Architektur (Plan-1-call-briefing): der einzige Prompt briefing.txt
    # erhaelt das volle deterministische Faktenpaket ($facts) — Zahlen/Labels/
    # Signale stammen ausschliesslich daraus. Die reduzierte Sicht (nur
    # deterministic_summary + allowlist) entfaellt zugunsten des Pakets.
    prompt = _load_prompt(mode, {"facts": facts_package})

    try:
        client = client or _get_client()
    except RuntimeError:
        raise LLMError("Draft-Generierung fehlgeschlagen: API-Key fehlt.") from None

    last_error = None
    for attempt in range(2):
        try:
            response = client.chat.completions.create(
                model="deepseek-v4-flash",
                messages=[
                    {"role": "system", "content": "Du bist ein präziser Portfoliobeobachter."},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.3,
                max_tokens=4096,
                timeout=60,
            )
            content = response.choices[0].message.content
            if not content:
                raise ValueError("Empty response from LLM")
            return content
        except Exception as e:
            last_error = e
            if attempt < 1:
                time.sleep(2 ** attempt)
            continue
    raise LLMError(f"Draft-Generierung fehlgeschlagen: {last_error}") from last_error
