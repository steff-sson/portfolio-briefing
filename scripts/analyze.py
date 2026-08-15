"""Portfolio analysis: structure, drift, turnover, thesis deadlines, data quality."""
from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
THESIS_DIR = Path.home() / "docs" / "notizen" / "portfolio-theses"


Status = str


class PortfolioDataError(Exception):
    """Definierte Datenfehler-Exception: Holdings vorhanden, aber berechneter
    Gesamtwert ist 0/ungueltig — keine kuenstlichen Gewichte/Green-Checks."""


class StrategyValidationError(Exception):
    """Ungueltige strategy.yaml: fail-closed (kein Versand, kein Snapshot).

    Wird von ``load_strategy`` geworfen, wenn die geladene Strategie das
    feste Schema (``validate_strategy``) nicht erfuellt.
    """


def load_strategy() -> dict:
    """Strategie laden und schema-validieren (fail-closed bei ungueltig).

    ``strategy.yaml`` ist read-only im Pipeline-Kontext: wird weder erzeugt
    noch modifiziert. Fehlende Datei -> FileNotFoundError; ungueltiger Inhalt
    -> StrategyValidationError (beides fail-closed).
    """
    with open(CONFIG_DIR / "strategy.yaml", encoding="utf-8") as f:
        strategy = yaml.safe_load(f)
    validation = validate_strategy(strategy)
    if not validation["valid"]:
        raise StrategyValidationError(
            "ungueltige strategy.yaml: " + "; ".join(validation["errors"])
        )
    return strategy


def load_etf_lookup() -> dict:
    path = CONFIG_DIR / "etf_lookup.json"
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# --- Datenqualitaet vor Portfolio-Bewertung (Phase 1) ------------------------

# Pipeline-fixed Defaults (nicht in strategy.yaml, dokumentiert in
# config/pipeline.example.yaml). Die Plausibilitaetsschwelle (10x) ist ein
# Pipeline-Parameter, NICHT strategieabhaengig.
DATA_QUALITY_MAX_AGE_DAYS = 7
DATA_QUALITY_IMPLAUSIBLE_FACTOR = 10.0

# Status-Prioritaet bei mehreren Problemen (fest im Code).
_DATA_QUALITY_PRIORITY = {"ok": 0, "stale": 1, "incomplete": 2, "implausible": 3}


def _snapshot_age_days(captured_at: object) -> float | None:
    """Alter eines Snapshots in Tagen (naive ISO-Zeiten werden als UTC gelesen)."""
    if not isinstance(captured_at, str) or not captured_at:
        return None
    try:
        captured = datetime.fromisoformat(captured_at)
    except ValueError:
        return None
    if captured.tzinfo is None:
        captured = captured.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - captured).total_seconds() / 86400.0


def assess_data_quality(portfolio: dict, previous_snapshot: dict | None = None) -> dict:
    """Deterministische Datenqualitaetspruefung VOR den Portfolio-Checks.

    Prueft: Holdings vorhanden/nicht-leer, Gesamtwert > 0, Pflichtfelder je
    Holding (isin/name/value_eur|value/category), Plausibilitaet (negative
    Werte; Gesamtwert-Aenderung > 10x gegenueber Vorgaenger-Snapshot) und
    Snapshot-Alter (Vorgaenger aelter als 7 Tage -> stale).

    Status-Prioritaet bei mehreren Problemen: implausible > incomplete >
    stale > ok. Wirft nie — liefert stattdessen ``{"status", "issues"}``;
    der Orchestrator entscheidet ueber Abbruch bzw. Suppression roter Befunde.
    """
    issues: list[str] = []
    statuses = ["ok"]

    if not isinstance(portfolio, dict):
        return {"status": "incomplete", "issues": ["portfolio fehlt oder ist kein Dict"]}

    holdings = portfolio.get("holdings", [])
    if not isinstance(holdings, list) or not holdings:
        statuses.append("incomplete")
        issues.append("keine Holdings vorhanden")
        holdings = []

    total = _portfolio_total(portfolio, holdings)
    if not total or math.isnan(total) or total < 0:
        statuses.append("incomplete")
        issues.append(f"Gesamtwert ungueltig (nicht > 0): {total!r}")

    for index, holding in enumerate(holdings):
        if not isinstance(holding, dict):
            statuses.append("incomplete")
            issues.append(f"Holding[{index}] ist kein Dict")
            continue
        for field in ("isin", "name", "category"):
            if not holding.get(field):
                statuses.append("incomplete")
                issues.append(f"Holding[{index}].{field} fehlt")
        value = holding.get("value_eur", holding.get("value"))
        if value is None:
            statuses.append("incomplete")
            issues.append(f"Holding[{index}].value_eur/value fehlt")
        elif _holding_value(holding) < 0:
            statuses.append("implausible")
            issues.append(
                f"Holding[{index}] {holding.get('isin') or holding.get('name') or '?'}: negativer Wert"
            )

    if isinstance(previous_snapshot, dict):
        age_days = _snapshot_age_days(previous_snapshot.get("captured_at"))
        if age_days is not None and age_days > DATA_QUALITY_MAX_AGE_DAYS:
            statuses.append("stale")
            issues.append(
                f"Vorgaenger-Snapshot {age_days:.1f} Tage alt (> {DATA_QUALITY_MAX_AGE_DAYS} Tage)"
            )
        prev_portfolio = previous_snapshot.get("portfolio")
        if isinstance(prev_portfolio, dict) and total:
            prev_total = _portfolio_total(prev_portfolio, prev_portfolio.get("holdings", []))
            if prev_total:
                ratio = total / prev_total
                if ratio > DATA_QUALITY_IMPLAUSIBLE_FACTOR or ratio < 1.0 / DATA_QUALITY_IMPLAUSIBLE_FACTOR:
                    statuses.append("implausible")
                    issues.append(
                        f"Gesamtwert-Aenderung {ratio:.2f}x gegenueber Vorgaenger-Snapshot "
                        f"(erlaubt: max {DATA_QUALITY_IMPLAUSIBLE_FACTOR:.0f}x)"
                    )

    status = max(statuses, key=lambda s: _DATA_QUALITY_PRIORITY[s])
    return {"status": status, "issues": issues}


# --- Strategie-Validierung + kanonischer Hash (Phase 4) ----------------------

# Feste Schema-Definition (Pipeline-Logik, NICHT in strategy.yaml). Legt fest,
# welche Sektionen/Felder existieren duerfen und welche required sind — keine
# Werte. Unbekannte Felder werden ignoriert (permissive, Strategie-Evolution).
STRATEGY_SCHEMA = {
    "meta": {"required": False, "fields": {"version", "created", "last_reviewed", "next_review", "cooling_off_days"}},
    "investor": {"required": False, "fields": {"horizon", "income_source", "purpose", "risk_profile"}},
    "portfolio": {"required": True, "fields": {"core_pct", "satellite_pct", "core_description", "rebalancing"}},
    "satellite_limits": {
        "required": True,
        "fields": {"max_position_pct", "max_sector_pct", "max_positions", "max_turnover_annual_pct", "max_trades_per_quarter"},
    },
    "sectors": {"required": False, "fields": {"preferred", "excluded", "notes"}},
    "regions": {"required": False, "fields": {"core", "satellite_restriction"}},
    "thesis": {"required": False, "fields": {"required", "template"}},
    "alerts": {"required": False, "fields": set()},
    "review_schedule": {"required": False, "fields": {"quarterly_strategy_review", "annual_full_review", "triggers"}},
}

_PERCENT_STRATEGY_FIELDS = {
    "portfolio": ("core_pct", "satellite_pct"),
    "rebalancing": ("threshold_pct",),
    "satellite_limits": ("max_position_pct", "max_sector_pct", "max_turnover_annual_pct"),
}
_INT_STRATEGY_FIELDS = {
    "satellite_limits": ("max_positions", "max_trades_per_quarter"),
}
_LIST_STRATEGY_FIELDS = {
    "sectors": ("preferred", "excluded"),
    "review_schedule": ("triggers",),
}


def _strategy_section(strategy: dict, section: str) -> dict | None:
    """Sektions-Dict holen; "rebalancing" liegt verschachtelt unter portfolio."""
    if section == "rebalancing":
        portfolio = strategy.get("portfolio")
        if not isinstance(portfolio, dict):
            return None
        value = portfolio.get("rebalancing")
        return value if isinstance(value, dict) else None
    value = strategy.get(section)
    return value if isinstance(value, dict) else None


def _as_percent(value: object) -> float | None:
    """float-Wert wenn int|float (kein bool), sonst None (fehlend/ungueltig)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def validate_strategy(strategy: dict) -> dict:
    """Schema-Validierung der strategy.yaml (pipeline-fixed, permissive).

    Regeln: portfolio + satellite_limits sind Pflicht-Sektionen (Dicts);
    Prozentwerte sind int|float (kein bool); core_pct + satellite_pct == 100;
    max_position_pct <= max_sector_pct; max_positions >= 1 (int); Listen sind
    Listen. Unbekannte Felder werden ignoriert (permissive).

    Rueckgabe: {"valid": True, "errors": []} oder {"valid": False, "errors": [...]}.
    """
    errors: list[str] = []
    if not isinstance(strategy, dict):
        return {"valid": False, "errors": ["strategy ist kein Dict"]}

    for section, spec in STRATEGY_SCHEMA.items():
        value = strategy.get(section)
        if value is None:
            if spec["required"]:
                errors.append(f"Pflicht-Sektion '{section}' fehlt")
            continue
        if not isinstance(value, dict):
            errors.append(f"Sektion '{section}' ist kein Dict")

    for section, fields in _PERCENT_STRATEGY_FIELDS.items():
        value = _strategy_section(strategy, section)
        if value is None:
            continue
        for field in fields:
            field_value = value.get(field)
            if field_value is None:
                continue
            if isinstance(field_value, bool) or not isinstance(field_value, (int, float)):
                errors.append(f"{section}.{field} muss eine Zahl sein (int|float)")

    for section, fields in _INT_STRATEGY_FIELDS.items():
        value = _strategy_section(strategy, section)
        if value is None:
            continue
        for field in fields:
            field_value = value.get(field)
            if field_value is None:
                continue
            if isinstance(field_value, bool) or not isinstance(field_value, int):
                errors.append(f"{section}.{field} muss eine Ganzzahl sein (int)")

    for section, fields in _LIST_STRATEGY_FIELDS.items():
        value = _strategy_section(strategy, section)
        if value is None:
            continue
        for field in fields:
            field_value = value.get(field)
            if field_value is None:
                continue
            if not isinstance(field_value, list):
                errors.append(f"{section}.{field} muss eine Liste sein")

    portfolio = strategy.get("portfolio")
    if isinstance(portfolio, dict):
        core = _as_percent(portfolio.get("core_pct"))
        sat = _as_percent(portfolio.get("satellite_pct"))
        if core is not None and sat is not None and abs(core + sat - 100.0) > 0.01:
            errors.append(f"portfolio.core_pct + portfolio.satellite_pct == {core + sat}, erwartet 100")

    limits = strategy.get("satellite_limits")
    if isinstance(limits, dict):
        max_pos_pct = _as_percent(limits.get("max_position_pct"))
        max_sector_pct = _as_percent(limits.get("max_sector_pct"))
        if max_pos_pct is not None and max_sector_pct is not None and max_pos_pct > max_sector_pct:
            errors.append("satellite_limits.max_position_pct darf nicht groesser als max_sector_pct sein")
        max_positions = limits.get("max_positions")
        if isinstance(max_positions, int) and not isinstance(max_positions, bool) and max_positions < 1:
            errors.append("satellite_limits.max_positions muss >= 1 sein")

    return {"valid": not errors, "errors": errors}


def strategy_hash(strategy: dict) -> str:
    """SHA-256 des kanonisch serialisierten Strategy-Dicts (sortierte Keys).

    Der Hash ist nicht sensitiv (Einweg): erlaubt Aenderungserkennung, ohne
    Strategie-Inhalte zu speichern oder in LLM-Kontexte zu senden.
    """
    canonical = json.dumps(strategy, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _status(value: float, min_v: float, max_v: float) -> Status:
    if value < min_v or value > max_v:
        return "red"
    margin = (max_v - min_v) * 0.05
    if value < min_v + margin or value > max_v - margin:
        return "yellow"
    return "green"


def _holding_value(h: dict) -> float:
    raw = h.get("value_eur", h.get("value", 0)) or 0
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 0.0


def _portfolio_total(portfolio: dict, holdings: list) -> float:
    """Gesamtwert: autoritatives Summenfeld, sonst Summe der Holdings-Werte."""
    raw = portfolio.get("total_value_eur", portfolio.get("total_value"))
    if raw is not None:
        try:
            total = float(raw)
        except (TypeError, ValueError):
            total = 0.0
        if total:
            return total
    return sum(_holding_value(h) for h in holdings)


def _pct(value: object, default: float = 0.0) -> float:
    """Percent value from strategy config -> ratio (75.0 -> 0.75).

    Missing/invalid values default to 0.0: without a real threshold the
    related check turns red (fail-closed, unknown limits stay critical).
    """
    if not isinstance(value, (int, float, str)):
        return default
    try:
        return float(value) / 100.0
    except ValueError:
        return default


def _portfolio_cfg(strategy: dict) -> dict:
    return strategy.get("portfolio", {}) if isinstance(strategy, dict) else {}


def _rebalancing_cfg(strategy: dict) -> dict:
    portfolio = _portfolio_cfg(strategy)
    rebalancing = portfolio.get("rebalancing", {})
    return rebalancing if isinstance(rebalancing, dict) else {}


def _satellite_limits(strategy: dict) -> dict:
    limits = strategy.get("satellite_limits", {}) if isinstance(strategy, dict) else {}
    return limits if isinstance(limits, dict) else {}


def _alerts(strategy: dict) -> dict:
    alerts = strategy.get("alerts", {}) if isinstance(strategy, dict) else {}
    return alerts if isinstance(alerts, dict) else {}


def calculate_positions(portfolio: dict) -> dict:
    holdings = portfolio.get("holdings", [])
    if not isinstance(holdings, list):
        holdings = []
    total = _portfolio_total(portfolio, holdings)
    if holdings and (not total or math.isnan(total) or total < 0):
        raise PortfolioDataError(
            f"portfolio has {len(holdings)} holdings but computed total value is {total!r} — "
            "refusing to fabricate weights/checks"
        )
    positions = []
    for h in holdings:
        val = _holding_value(h)
        positions.append({
            "isin": h.get("isin"),
            "name": h.get("name"),
            "category": h.get("category") or "unknown",
            "value_eur": val,
            "weight": round(val / total, 4) if total else 0,
            "sector": h.get("sector"),
        })
    return {"total_value_eur": total, "positions": positions}


def calculate_core_satellite(positions: list, strategy: dict) -> dict:
    """Core/Satellite-Ratio gegen portfolio.core_pct (75.0 = 75%).

    Nur explizite category "core" zaehlt als Core, nur explizite category
    "satellite" als Satellite; ungemappte Positionen ("unknown") werden
    separat ausgewiesen und keiner Kategorie zugeschlagen. Toleranzband =
    core_pct ± rebalancing.threshold_pct. Fehlende Werte ergeben das Band
    0..0 -> jeder positive Ratio ist red (fail-closed).
    """
    total = sum(p["value_eur"] for p in positions)
    core_value = sum(p["value_eur"] for p in positions if p["category"] == "core")
    satellite_value = sum(p["value_eur"] for p in positions if p["category"] == "satellite")
    ratio = core_value / total if total else 0
    target = _pct(_portfolio_cfg(strategy).get("core_pct"))
    threshold = _pct(_rebalancing_cfg(strategy).get("threshold_pct"))
    return {
        "core_value_eur": core_value,
        "satellite_value_eur": satellite_value,
        "unknown_value_eur": round(total - core_value - satellite_value, 2),
        "core_ratio": round(ratio, 4),
        "target_ratio": target,
        "status": _status(ratio, target - threshold, target + threshold),
    }


def calculate_sector_concentration(positions: list, strategy: dict) -> dict:
    """Sektor-Konzentration gegen satellite_limits.max_sector_pct (15.0 = 15%).

    Sektor-Klassifikation: explizites ``sector``-Feld der Holding, sonst
    Fallback ueber config/etf_lookup.json (ISIN -> sector), sonst "Unknown".
    """
    etf_lookup = load_etf_lookup()
    total = sum(p["value_eur"] for p in positions)
    sector_values: dict[str, float] = {}
    for p in positions:
        sector = p.get("sector") or etf_lookup.get(p.get("isin"), {}).get("sector") or "Unknown"
        sector_values[sector] = sector_values.get(sector, 0) + p["value_eur"]
    sector_ratios = {s: round(v / total, 4) if total else 0 for s, v in sector_values.items()}
    max_sector = max(sector_ratios.items(), key=lambda kv: kv[1])[0] if sector_ratios else "Unknown"
    max_ratio = sector_ratios.get(max_sector, 0)
    threshold = _pct(_satellite_limits(strategy).get("max_sector_pct"))
    return {
        "sector_ratios": sector_ratios,
        "max_sector": max_sector,
        "max_ratio": max_ratio,
        "threshold": threshold,
        "status": "red" if max_ratio > threshold else "green" if max_ratio < threshold * 0.9 else "yellow",
    }


def calculate_single_position_max(positions: list, strategy: dict) -> dict:
    """Einzelposition-Limit gegen satellite_limits.max_position_pct (5.0 = 5%)."""
    threshold = _pct(_satellite_limits(strategy).get("max_position_pct"))
    max_pos = max(positions, key=lambda p: p["weight"]) if positions else None
    status = "green"
    if max_pos and max_pos["weight"] > threshold:
        status = "red"
    elif max_pos and max_pos["weight"] > threshold * 0.9:
        status = "yellow"
    return {
        "max_position": max_pos,
        "threshold": threshold,
        "status": status,
    }


def calculate_drift(positions: list, strategy: dict) -> dict:
    """Core-Drift gegen portfolio.core_pct und rebalancing.threshold_pct."""
    target = _pct(_portfolio_cfg(strategy).get("core_pct"))
    threshold = _pct(_rebalancing_cfg(strategy).get("threshold_pct"))
    actual = sum(p["weight"] for p in positions if p["category"] == "core")
    drift = abs(actual - target)
    return {
        "core_ratio_actual": round(actual, 4),
        "target": target,
        "drift": round(drift, 4),
        "threshold": threshold,
        "status": "red" if drift > threshold else "green" if drift < threshold * 0.6 else "yellow",
    }


def calculate_turnover(transactions: list, portfolio: dict, strategy: dict | None = None) -> dict:
    """Umschlag gegen satellite_limits.max_turnover_annual_pct (30.0 = 30%).

    Fehlende Strategie -> Grenzwert 0.0: jeder Umschlag > 0 ist red (fail-closed).
    """
    total = float(portfolio.get("total_value_eur", 0) or portfolio.get("total_value", 0))
    total_volume = sum(float(t.get("price_eur", 0) * t.get("quantity", 0)) for t in transactions)
    turnover = total_volume / total if total else 0
    threshold = _pct(_satellite_limits(strategy).get("max_turnover_annual_pct")) if strategy else 0.0
    return {
        "transaction_count": len(transactions),
        "total_volume_eur": round(total_volume, 2),
        "turnover_ratio": round(turnover, 4),
        "threshold": threshold,
        "status": "red" if turnover > threshold else "green" if turnover < threshold * 0.6 else "yellow",
    }


def _parse_thesis_date(md: Path) -> datetime | None:
    text = md.read_text(encoding="utf-8")
    match = re.search(r"^created:\s*(\d{4}-\d{2}-\d{2})", text, re.MULTILINE)
    if match:
        return datetime.strptime(match.group(1), "%Y-%m-%d")
    return None


def check_thesis_deadlines(strategy: dict) -> dict:
    """Thesen-Abgelaufen-Check gegen alerts.on_thesis_expiring_soon_days.

    Thesen aelter als der konfigurierte Zeitraum (Tage) gelten als
    veraltet. Fehlt der Wert, sind 0 Tage gesetzt: alle Thesen mit
    created-Datum werden als veraltet markiert (fail-closed).
    """
    alerts = _alerts(strategy)
    days_value = alerts.get("on_thesis_expiring_soon_days")
    days = float(days_value) if isinstance(days_value, (int, float)) else 0.0
    cutoff = datetime.now() - timedelta(days=days)
    outdated = []
    total = 0
    if THESIS_DIR.exists():
        for md in THESIS_DIR.glob("*.md"):
            total += 1
            created = _parse_thesis_date(md)
            if created and created < cutoff:
                outdated.append({"file": md.name, "created": created.strftime("%Y-%m-%d")})
    return {
        "total_theses": total,
        "outdated": outdated,
        "status": "red" if outdated else "green",
    }


def analyze_portfolio(portfolio: dict, transactions: list, strategy: dict | None = None) -> dict:
    if strategy is None:
        strategy = load_strategy()
    positions = calculate_positions(portfolio)
    pos_list = positions["positions"]
    checks = {
        "positions": positions,
        "core_satellite": calculate_core_satellite(pos_list, strategy),
        "sector_concentration": calculate_sector_concentration(pos_list, strategy),
        "single_position": calculate_single_position_max(pos_list, strategy),
        "drift": calculate_drift(pos_list, strategy),
        "turnover": calculate_turnover(transactions, portfolio, strategy),
        "thesis_deadlines": check_thesis_deadlines(strategy),
    }
    overall_status = max(
        (c["status"] for c in checks.values() if "status" in c),
        key=lambda s: {"green": 0, "yellow": 1, "red": 2}.get(s, 0),
    )
    return {
        "generated_at": datetime.now().isoformat(),
        "overall_status": overall_status,
        "checks": checks,
    }


if __name__ == "__main__":
    portfolio = json.loads((CONFIG_DIR / "portfolio.json").read_text())
    transactions = json.loads((CONFIG_DIR / "transactions.json").read_text())
    result = analyze_portfolio(portfolio, transactions)
    print(json.dumps(result, indent=2, ensure_ascii=False))
