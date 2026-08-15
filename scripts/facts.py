"""Deterministic facts package builder — LLM-free, serializable input contract.

Stufe 1 der Two-Stage-Briefing-Pipeline (Plan §3.1/§4.1): baut aus bereits
berechneten Portfolio-/Analyse-/News-Daten ein stabiles, explizit strukturiertes
Faktenpaket. `deterministic_summary` wird aus denselben `analysis["checks"]`
abgeleitet (nie neu berechnet) und enthaelt die Schluesselzahlen, die das LLM
nur referenzieren darf. Kein LLM-Aufruf, keine Secrets.
"""
from __future__ import annotations

from datetime import datetime, timezone

PIPELINE_VERSION = "2.0"

# Reihenfolge der Checks mit Status-Feld, wie von analyze.analyze_portfolio erzeugt.
_STATUS_CHECKS = [
    "core_satellite",
    "sector_concentration",
    "single_position",
    "drift",
    "turnover",
    "thesis_deadlines",
]

# Prozent-Grenzwerte aus config/strategy.yaml, die das LLM referenzieren darf.
_STRATEGY_THRESHOLD_KEYS = (
    "core_pct",
    "satellite_pct",
    "threshold_pct",
    "max_position_pct",
    "max_sector_pct",
    "max_turnover_annual_pct",
)


def _get_check(analysis: dict, check_name: str, key: str, default: object = None) -> object:
    """Return analysis["checks"][check_name][key] or default (never recomputed)."""
    check = analysis.get("checks", {}).get(check_name)
    if not isinstance(check, dict):
        return default
    return check.get(key, default)


def _float(value: object) -> float:
    """Coerce a numeric value from analysis to float (None/invalid -> 0.0)."""
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return 0.0
    return 0.0


def _str(value: object) -> str:
    """Coerce a value to str (None -> empty string)."""
    return str(value) if value is not None else ""


def _status_lists(analysis: dict) -> tuple[list[str], list[str], list[str]]:
    """Split status-bearing checks into red/yellow/green lists (sorted, deterministic)."""
    red: list[str] = []
    yellow: list[str] = []
    green: list[str] = []
    for name in sorted(_STATUS_CHECKS):
        status = _get_check(analysis, name, "status")
        if status == "red":
            red.append(name)
        elif status == "yellow":
            yellow.append(name)
        elif status == "green":
            green.append(name)
    return red, yellow, green


def _deterministic_summary(
    portfolio: dict, analysis: dict, data_quality: dict | None = None
) -> dict:
    """Extract the numbers the LLM may reference — all from analysis["checks"].

    ``data_quality`` (aus analyze.assess_data_quality) steuert die
    Suppression: bei Status != "ok" werden KEINE Portfolio-Befunde als
    red/yellow markiert — stattdessen nur ``data_quality`` als einziger
    roter Punkt. green_checks bleiben unveraendert (kein Handlungsbedarf).
    """
    max_position = _get_check(analysis, "single_position", "max_position")
    if isinstance(max_position, dict):
        max_position_weight = _float(max_position.get("weight"))
        max_position_name = _str(max_position.get("name"))
    else:
        max_position_weight = 0.0
        max_position_name = ""

    positions = _get_check(analysis, "positions", "positions", [])
    if not isinstance(positions, list):
        positions = []
    position_count = len(positions) if positions else len(portfolio.get("holdings", []))

    red_checks, yellow_checks, green_checks = _status_lists(analysis)

    dq = data_quality if isinstance(data_quality, dict) else {}
    dq_status = dq.get("status")
    dq_issues = dq.get("issues", [])
    if not isinstance(dq_issues, list):
        dq_issues = []
    if dq_status is not None and dq_status != "ok":
        red_checks = ["data_quality"]
        yellow_checks = []

    return {
        "total_value_eur": _float(
            _get_check(analysis, "positions", "total_value_eur", portfolio.get("total_value_eur"))
        ),
        "position_count": position_count,
        "core_ratio": _float(_get_check(analysis, "core_satellite", "core_ratio")),
        "max_position_weight": max_position_weight,
        "max_position_name": max_position_name,
        "max_sector": _str(_get_check(analysis, "sector_concentration", "max_sector")),
        "max_sector_ratio": _float(_get_check(analysis, "sector_concentration", "max_ratio")),
        "drift": _float(_get_check(analysis, "drift", "drift")),
        "turnover_ratio": _float(_get_check(analysis, "turnover", "turnover_ratio")),
        "outdated_theses": _get_check(analysis, "thesis_deadlines", "outdated", []),
        "red_checks": red_checks,
        "yellow_checks": yellow_checks,
        "green_checks": green_checks,
        "data_quality_status": dq_status,
        "data_quality_issues": dq_issues,
    }


def _strategy_thresholds_pct(strategy: dict) -> dict:
    """Extract the real percentage limits from the strategy config (never recomputed).

    Values are already in percent (e.g. core_pct 75.0 = 75%) and read directly
    from the strategy structure (portfolio.core_pct, portfolio.satellite_pct,
    portfolio.rebalancing.threshold_pct, satellite_limits.*). Missing/invalid
    values default to 0.0 — verify skips non-positive thresholds, so without
    real limits unknown numbers stay critical (fail-closed).
    """
    portfolio = strategy.get("portfolio", {}) if isinstance(strategy, dict) else {}
    rebalancing = portfolio.get("rebalancing", {}) if isinstance(portfolio, dict) else {}
    satellite_limits = strategy.get("satellite_limits", {}) if isinstance(strategy, dict) else {}
    return {
        "core_pct": _float(portfolio.get("core_pct")),
        "satellite_pct": _float(portfolio.get("satellite_pct")),
        "threshold_pct": _float(rebalancing.get("threshold_pct")),
        "max_position_pct": _float(satellite_limits.get("max_position_pct")),
        "max_sector_pct": _float(satellite_limits.get("max_sector_pct")),
        "max_turnover_annual_pct": _float(satellite_limits.get("max_turnover_annual_pct")),
    }


def _has_relevant_changes(changes: dict | None) -> bool:
    """Relevante Portfolio-Veränderungen: Positionen added/removed/changed oder
    Transaktionen added/removed. Reine Werte-Bewegungen unter den Diff-Schwellen
    zaehlen nicht (kein Trigger). Erstlauf (has_previous False) -> False.
    """
    if not isinstance(changes, dict) or changes.get("has_previous") is not True:
        return False
    positions = changes.get("positions")
    if isinstance(positions, dict) and (positions.get("added") or positions.get("removed") or positions.get("changed")):
        return True
    txns = changes.get("transactions")
    if isinstance(txns, dict) and (txns.get("added_count") or txns.get("removed_count")):
        return True
    return False


def compute_triggers(facts_package: dict) -> dict:
    """Deterministische Trigger-Flags + geordnete Trigger-Liste (Phase 3).

    Reihenfolge ist fest im Code (Pipeline-Logik, nicht konfigurierbar):
    1. data_quality (falls nicht ok — hat Vorrang, blockiert Portfolio-Trigger)
    2. strategy_change
    3. boundary_violation
    4. relevant_changes
    5. thesis_news

    Der "data_quality"-Marker in red_checks zaehlt nicht als Grenzverletzung.
    """
    summary = facts_package.get("deterministic_summary", {})
    data_quality = facts_package.get("data_quality")
    data_quality_status = data_quality.get("status") if isinstance(data_quality, dict) else None
    changes = facts_package.get("changes")
    strategy_diff = facts_package.get("strategy_diff")
    news = facts_package.get("news", [])

    red = summary.get("red_checks") or []
    yellow = summary.get("yellow_checks") or []
    boundary_checks = [c for c in list(red) + list(yellow) if c != "data_quality"]
    has_boundary = bool(boundary_checks)
    has_changes = _has_relevant_changes(changes)
    has_news = any(
        isinstance(n, dict) and n.get("thesis_relevant") for n in (news if isinstance(news, list) else [])
    )
    has_strategy = strategy_diff is not None and bool(strategy_diff.get("has_changed", False))
    dq_not_ok = data_quality_status is not None and data_quality_status != "ok"

    ordered: list[str] = []
    if dq_not_ok:
        ordered.append("data_quality")
    if has_strategy:
        ordered.append("strategy_change")
    if has_boundary:
        ordered.append("boundary_violation")
    if has_changes:
        ordered.append("relevant_changes")
    if has_news:
        ordered.append("thesis_news")

    return {
        "has_boundary_violation": has_boundary,
        "has_relevant_changes": has_changes,
        "has_thesis_news": has_news,
        "has_strategy_change": has_strategy,
        "ordered": ordered,
        "has_any_trigger": bool(ordered),
    }


def build_facts_package(
    portfolio: dict,
    transactions: list,
    analysis: dict,
    news: list,
    strategy: dict,
    mode: str,
    changes: dict | None = None,
    data_quality: dict | None = None,
) -> dict:
    """Build the deterministic, JSON-serializable facts package.

    Pure function of its inputs (no LLM, no secrets): portfolio/analysis/news/
    strategy/transactions/changes are passed through unchanged,
    `deterministic_summary` is extracted from the precomputed
    `analysis["checks"]` and `strategy_thresholds_pct` from the strategy
    config's percentage limits.

    ``changes`` ist das deterministische Diff-Ergebnis (diff.diff_snapshots)
    gegen den letzten Snapshot — None im Erstlauf/Dry-Run. LLM-Kontexte
    reduzieren es separat (``reduce_changes_for_llm``): Roh-Transaktions-
    Records bleiben aus den Prompts.

    ``data_quality`` kommt aus analyze.assess_data_quality (vom Orchestrator
    berechnet, inkl. Vorgaenger-Snapshot); None bei fehlender Bewertung
    (Dry-Run/Erstlauf) -> keine Suppression roter Befunde.

    ``strategy_diff`` stammt aus ``changes["strategy"]`` (diff.diff_strategy)
    und enthaelt nur Feld-Pfade + Hashes — nie Strategiewerte. ``triggers``
    (compute_triggers) ist die deterministische Trigger-Basis fuer die
    Optionen-Generierung.
    """
    summary = _deterministic_summary(portfolio, analysis, data_quality)
    strategy_diff = changes.get("strategy") if isinstance(changes, dict) else None
    package = {
        "meta": {
            "mode": mode,
            "generated_at": datetime.now(tz=timezone.utc).astimezone().isoformat(timespec="seconds"),
            "pipeline_version": PIPELINE_VERSION,
        },
        "portfolio": portfolio,
        "analysis": analysis,
        "news": news,
        "strategy": strategy,
        "transactions": transactions,
        "changes": changes,
        "data_quality": data_quality,
        "strategy_diff": strategy_diff,
        "deterministic_summary": summary,
        "strategy_thresholds_pct": _strategy_thresholds_pct(strategy),
    }
    triggers = compute_triggers(package)
    package["triggers"] = triggers
    summary["has_triggers"] = triggers["has_any_trigger"]
    return package


def reduce_changes_for_llm(changes: dict | None) -> dict | None:
    """Reduzierte Aenderungsdaten fuer LLM-Kontexte (ohne Roh-Transaktions-Records).

    Entfernt aus dem Diff-Ergebnis die Transaktions-Datensaetze ``added``/
    ``removed`` (Zeitpunkt/ISIN/Typ/Menge/Preis) — die MVP-GRENZE verbietet
    die Behandlung einzelner Trades. Aggregierte Diff-Felder (Counts,
    Volumen-Delta, Positions-Diff) bleiben erhalten. None (Erstlauf/Dry-Run)
    bleibt None. Mutiert den Input nicht.
    """
    if not isinstance(changes, dict):
        return None
    reduced = {key: value for key, value in changes.items() if key != "transactions"}
    txns = changes.get("transactions")
    if isinstance(txns, dict):
        reduced["transactions"] = {
            key: value for key, value in txns.items() if key not in ("added", "removed")
        }
    return reduced


if __name__ == "__main__":
    import json

    from scripts import analyze, sc_bridge

    portfolio = sc_bridge.get_portfolio()
    transactions = sc_bridge.get_transactions()
    strategy = analyze.load_strategy()
    analysis = analyze.analyze_portfolio(portfolio, transactions, strategy)
    news: list = []
    package = build_facts_package(portfolio, transactions, analysis, news, strategy, mode="monday")
    print(json.dumps(package, indent=2, ensure_ascii=False))
