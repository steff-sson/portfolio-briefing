"""Portfolio analysis: structure, drift, turnover, thesis deadlines, data quality."""
from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from scripts import filter_news

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
            isin = holding.get("isin") or "?"
            issues.append(
                f"Holding <{isin}>: kein Bewertungswert (valuation null) — Wert unvollständig, "
                "Position bleibt mit Kategorie unknown und Gewicht 0 im Positionsreport, "
                'wird aber explizit als "unbewertet" ausgewiesen (Datenqualitätsregel §8).'
            )
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

# Single Source of Truth: config/strategy.schema.yaml (eingecheckt, NICHT
# gitignored). Unbekannte Felder schlagen fehl (fail-closed) — es gibt kein
# stilles Ignorieren mehr. STRATEGY_SCHEMA (alter, permissiver Block) ist
# entfernt; das Schema wird ausschliesslich aus der YAML-Datei geladen.
STRATEGY_SCHEMA_PATH = CONFIG_DIR / "strategy.schema.yaml"

# Feld-Typen im Schema: value-Typ -> Name fuer Fehlermeldungen.
_SCHEMA_TYPE_NAMES = {
    "str": "str",
    "int": "int",
    "float": "float",
    "bool": "bool",
    "enum": "enum",
    "list": "list",
    "dict": "dict",
    "date": "date (YYYY-MM-DD)",
}

# Fachliche Integritaetsregeln (gelten fuer die Wurzel-Strategie).
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


def load_strategy_schema() -> dict:
    """Kanonisches Strategie-Schema aus config/strategy.schema.yaml laden (SSoT).

    Fail-closed: fehlende/korrupte Schema-Datei -> StrategyValidationError
    (ohne Schema ist keine Validierung moeglich).
    """
    path = STRATEGY_SCHEMA_PATH
    if not path.exists():
        raise StrategyValidationError(
            f"Strategie-Schema fehlt: {path} (Single Source of Truth, eingecheckt)"
        )
    with open(path, encoding="utf-8") as f:
        schema = yaml.safe_load(f)
    if not isinstance(schema, dict) or not isinstance(schema.get("sections"), dict):
        raise StrategyValidationError(f"Strategie-Schema ungueltig: {path}")
    return schema


def _schema_sections(schema: dict) -> dict:
    sections = schema.get("sections", {}) if isinstance(schema, dict) else {}
    return sections if isinstance(sections, dict) else {}


def _schema_field_spec(schema: dict, section: str, field: str) -> dict | None:
    """Feldspezifikation fuer section.field aus dem Schema (None wenn unbekannt)."""
    sections = _schema_sections(schema)
    section_spec = sections.get(section)
    if not isinstance(section_spec, dict):
        return None
    fields = section_spec.get("fields")
    if not isinstance(fields, dict):
        return None
    spec = fields.get(field)
    return spec if isinstance(spec, dict) else None


def _known_schema_sections(schema: dict) -> set[str]:
    return set(_schema_sections(schema).keys())


def _known_schema_fields(schema: dict, section: str) -> set[str]:
    sections = _schema_sections(schema)
    section_spec = sections.get(section)
    if not isinstance(section_spec, dict):
        return set()
    fields = section_spec.get("fields")
    if not isinstance(fields, dict):
        return set()
    return set(fields.keys())


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


def _validate_date(value: object) -> bool:
    """Datumsformat YYYY-MM-DD (fachliche Anforderung, keine Zeitzonen)."""
    if not isinstance(value, str) or not value:
        return False
    try:
        datetime.fromisoformat(value)
    except ValueError:
        return False
    return True


def _validate_schema_value(spec: dict, value: object, errors: list[str], path: str) -> None:
    """Typ-/Werte-Validierung eines Feldes gegen die Schema-Feldspezifikation.

    Wandelt das Feld inkl. Pfadangabe. ``spec`` muss eine Schema-Feldspez
    sein (dict mit "type"); ``path`` ist der Fehler-Pfad (z.B. "portfolio.core_pct").

    Dict-Felder werden je nach Spezifikation validiert:
    - ``fields`` (feste Struktur): bekannte Schlüssel typgeprüft, unbekannte
      Schlüssel schlagen fehl (fail-closed), required-Felder müssen da sein.
    - ``mapping: analyserelevant`` + ``fields`` (dynamische Schlüssel, z.B.
      ISINs in holdings_classification.isins): jeder Schlüssel wird als
      dynamisches Element gegen das Subschema validiert (fail-closed bei
      unbekannten Feldnamen, Typ-/Enum-Prüfung der Werte).
    """
    field_type = spec.get("type")
    if field_type == "str":
        if not isinstance(value, str):
            errors.append(f"{path} muss ein String sein (str)")
    elif field_type in ("int", "float"):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            errors.append(f"{path} muss eine Zahl sein (int|float)")
    elif field_type == "bool":
        if not isinstance(value, bool):
            errors.append(f"{path} muss ein Bool sein (true|false)")
    elif field_type == "enum":
        allowed = spec.get("enum")
        if not isinstance(allowed, list) or value not in allowed:
            errors.append(f"{path} muss einer der Werte sein: {allowed}")
    elif field_type == "list":
        if not isinstance(value, list):
            errors.append(f"{path} muss eine Liste sein")
    elif field_type == "date":
        if not _validate_date(value):
            errors.append(f"{path} muss ein Datum sein (YYYY-MM-DD)")
    elif field_type == "dict":
        if not isinstance(value, dict):
            errors.append(f"{path} muss ein Dict sein")
            return
        nested = spec.get("fields")
        if isinstance(nested, dict):
            if spec.get("dynamic") is True:
                # Dynamische Schlüssel (z.B. ISINs): jedes Element gegen das
                # Subschema validieren, unbekannte Feldnamen fail-closed.
                for key, item in value.items():
                    if not isinstance(item, dict):
                        errors.append(f"{path}.{key} muss ein Dict sein")
                        continue
                    _validate_schema_dict(item, nested, errors, f"{path}.{key}")
            else:
                _validate_schema_dict(value, nested, errors, path)


def _validate_schema_dict(value: dict, nested: dict, errors: list[str], path: str) -> None:
    """Verschachteltes Dict gegen Schema-Feldspezifikationen validieren.

    Unbekannte Felder innerhalb bekannter Dict-Sektionen schlagen fehl
    (fail-closed). Required-Felder mit Mapping analyserelevant muessen
    vorhanden sein.
    """
    for key, field_value in value.items():
        spec = nested.get(key)
        if spec is None or not isinstance(spec, dict):
            errors.append(f"Unbekanntes Feld '{path}.{key}' (nicht im Strategie-Schema)")
            continue
        _validate_schema_value(spec, field_value, errors, f"{path}.{key}")
    for key, spec in nested.items():
        if not isinstance(spec, dict):
            continue
        if spec.get("required") and key not in value:
            errors.append(f"Pflichtfeld '{path}.{key}' fehlt")


def validate_strategy(strategy: dict) -> dict:
    """Schema-Validierung der strategy.yaml (SSoT: config/strategy.schema.yaml).

    Fail-closed fuer unbekannte Felder: jedes Feld, das nicht im Schema steht,
    erzeugt einen Fehler (kein stilles Ignorieren). Typ-/Werte-Validierung
    wird vollstaendig vom Schema abgeleitet. Fachliche Regeln:
    portfolio + satellite_limits sind Pflicht-Sektionen; core_pct +
    satellite_pct == 100 (Toleranz 0.01); max_position_pct <= max_sector_pct;
    max_positions >= 1; max_trades_per_quarter >= 1; Datumsfelder YYYY-MM-DD.

    Rueckgabe: {"valid": True, "errors": []} oder {"valid": False, "errors": [...]}.
    """
    errors: list[str] = []
    if not isinstance(strategy, dict):
        return {"valid": False, "errors": ["strategy ist kein Dict"]}

    schema = load_strategy_schema()
    sections = _schema_sections(schema)

    # 1. Unbekannte Sektionen -> fail-closed; bekannte Sektionen typ- und feldgeprueft.
    for section, value in strategy.items():
        section_spec = sections.get(section)
        if section_spec is None:
            errors.append(f"Unbekannte Sektion '{section}' (nicht im Strategie-Schema)")
            continue
        if not isinstance(value, dict):
            errors.append(f"Sektion '{section}' ist kein Dict")
            continue
        nested = section_spec.get("fields")
        if isinstance(nested, dict):
            _validate_schema_dict(value, nested, errors, section)
        if section_spec.get("required") and not value:
            errors.append(f"Pflicht-Sektion '{section}' fehlt")

    # 2. Pflicht-Sektionen vorhanden.
    for section, spec in sections.items():
        if spec.get("required") and not isinstance(strategy.get(section), dict):
            errors.append(f"Pflicht-Sektion '{section}' fehlt")

    # 3. Fachliche Regeln (aus dem Schema abgeleitete Struktur, Werte aus der Strategie).
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
        max_trades = limits.get("max_trades_per_quarter")
        if isinstance(max_trades, int) and not isinstance(max_trades, bool) and max_trades < 1:
            errors.append("satellite_limits.max_trades_per_quarter muss >= 1 sein")

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


def _holding_category(holding: dict, strategy: dict) -> str:
    """Kategorie einer Holding: Strategie-Intent (confirmed) -> Rohdaten -> etf_lookup.

    Fallback-Kette (Plan strategy-assistant-feedback.md, P2):
    1. ``strategy["holdings_classification"]["isins"][<ISIN>]`` mit
       ``confirmed: true`` -> die dort hinterlegte Kategorie (Strategie-Intent).
    2. Sonst Roh-``category``-Feld der Holding (bereits von sc_bridge mit
       etf_lookup angereichert; fehlende Rohdaten -> "unknown").
    3. Sonst ``etf_lookup[<ISIN>]["category"]`` (Abwärtskompatibilität).
    4. Sonst ``"unknown"``.

    Fail-closed: unbestaetigte (``confirmed`` fehlt/false) oder unbekannte
    Klassifikationen werden NICHT erfunden — sie bleiben bei der
    Rohdaten-Kategorie bzw. "unknown". SUSE/Legacy wird hier nicht
    automatisch klassifiziert.
    """
    classification = _strategy_isin_classification(holding, strategy)
    if classification is not None:
        return classification
    category = holding.get("category")
    if isinstance(category, str) and category:
        return category
    lookup_entry = load_etf_lookup().get(str(holding.get("isin") or ""))
    if isinstance(lookup_entry, dict):
        lookup_category = lookup_entry.get("category")
        if isinstance(lookup_category, str) and lookup_category:
            return lookup_category
    return "unknown"


def _strategy_isin_classification(holding: dict, strategy: dict) -> str | None:
    """Strategie-Klassifikation einer ISIN (nur confirmed: true), sonst None.

    Liest ``strategy["holdings_classification"]["isins"][<ISIN>]["category"]``
    und liefert sie nur, wenn das Feld ``confirmed: true`` ist. Unbestaetigte,
    fehlende oder ungueltige Eintraege -> None (fail-closed, keine Heuristik).
    """
    if not isinstance(strategy, dict):
        return None
    section = strategy.get("holdings_classification")
    if not isinstance(section, dict):
        return None
    isins = section.get("isins")
    if not isinstance(isins, dict):
        return None
    entry = isins.get(str(holding.get("isin") or ""))
    if not isinstance(entry, dict):
        return None
    if entry.get("confirmed") is not True:
        return None
    category = entry.get("category")
    if isinstance(category, str) and category:
        return category
    return None


def calculate_positions(portfolio: dict, strategy: dict | None = None) -> dict:
    """Positionsobjekte aus den Holdings (Gewichte, Kategorie, Sektor).

    Kategorie je Position: Strategie-Klassifikation (confirmed) -> Rohdaten-
    category -> etf_lookup -> "unknown" (siehe _holding_category). Ohne
    Strategie (None) bleibt die Rohdaten-/Lookup-Kategorie erhalten
    (Abwaertskompatibilitaet: bestehende Aufrufer ohne Strategie-Kontext
    verhalten sich unveraendert).
    """
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
            "category": _holding_category(h, strategy) if strategy is not None else (h.get("category") or "unknown"),
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

    Kategorie je Position: Strategie-Klassifikation (confirmed) -> Rohdaten-
    category -> etf_lookup (siehe _holding_category).
    """
    total = sum(p["value_eur"] for p in positions)
    core_value = sum(p["value_eur"] for p in positions if _holding_category(p, strategy) == "core")
    satellite_value = sum(p["value_eur"] for p in positions if _holding_category(p, strategy) == "satellite")
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
    actual = sum(p["weight"] for p in positions if _holding_category(p, strategy) == "core")
    drift = abs(actual - target)
    return {
        "core_ratio_actual": round(actual, 4),
        "target": target,
        "drift": round(drift, 4),
        "threshold": threshold,
        "status": "red" if drift > threshold else "green" if drift < threshold * 0.6 else "yellow",
    }


def _txn_price(txn: dict) -> float:
    """Preis je Einheit: price_eur > price; bei 0/fehlend (z.B. Cash) -> 0."""
    for key in ("price_eur", "price"):
        raw = txn.get(key)
        if isinstance(raw, (int, float)) and not isinstance(raw, bool):
            return float(raw)
    return 0.0


def _txn_quantity(txn: dict) -> float:
    raw = txn.get("quantity")
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return float(raw)
    return 0.0


def calculate_turnover(transactions: list, portfolio: dict, strategy: dict | None = None) -> dict:
    """Umschlag gegen satellite_limits.max_turnover_annual_pct (30.0 = 30%).

    Rechnet auf dem normalisierten Transaktionsschema (Plan §9): pro Ereignis
    amount_eur (absolut). Legacy-Eintraege (date/type/price_eur) werden als
    Kauf-/Verkaufs-Volumen (price_eur * quantity) gewertet. Fehlende Strategie
    -> Grenzwert 0.0: jeder Umschlag > 0 ist red (fail-closed).
    """
    total = float(portfolio.get("total_value_eur", 0) or portfolio.get("total_value", 0))
    total_volume = 0.0
    for t in transactions:
        if not isinstance(t, dict):
            continue
        amount = t.get("amount_eur")
        if isinstance(amount, (int, float)) and not isinstance(amount, bool):
            total_volume += abs(float(amount))
            continue
        total_volume += _txn_price(t) * _txn_quantity(t)
    turnover = total_volume / total if total else 0
    threshold = _pct(_satellite_limits(strategy).get("max_turnover_annual_pct")) if strategy else 0.0
    return {
        "transaction_count": len(transactions),
        "total_volume_eur": round(total_volume, 2),
        "turnover_ratio": round(turnover, 4),
        "threshold": threshold,
        "status": "red" if turnover > threshold else "green" if turnover < threshold * 0.6 else "yellow",
    }


def _txn_executed_at(txn: dict) -> str:
    """Zeitpunkt: executed_at (normalisiert) > date (Legacy)."""
    value = txn.get("executed_at") or txn.get("date")
    return str(value) if value else ""

def _quarter_key(iso: str) -> str | None:
    """Kalender-Quartal (YYYY-Qn) aus ISO-Zeitpunkt; unparsbar -> None."""
    dt = _parse_iso(iso)
    if dt is None:
        return None
    return f"{dt.year}-Q{(dt.month - 1) // 3 + 1}"


def _parse_iso(value: str) -> datetime | None:
    """ISO-Zeitpunkt parsen (Zeitzone bewusst); unparsbar -> None."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


# Trade-Klassifikation (Plan Phase 3/4): welcher Trade zaehlt zum
# Satellite-Trade-Limit?
# - ``satellite_trade``: diskretionaere Kauf-/Verkaufs-Ereignisse (Einzelaktien
#   und manuelle Trades) — zaehlen zum Limit.
# - ``sparplan``: automatischer Bestandsaufbau (Core-ETF-Sparplaene,
#   Rentenanlage) — zaehlen NICHT zum Limit, werden separat klassifiziert.
# - ``non_trade``: Cash-Bewegungen (einzahlung/ausschuettung/sonstiges) —
#   irrelevant fuer das Limit.
# - ``unclear``: Typ nicht bestimmbar — fail-closed (zaehlt zum Limit,
#   wird transparent separat ausgewiesen).
TRADE_CLASS_SATELLITE = "satellite_trade"
TRADE_CLASS_SPARPLAN = "sparplan"
TRADE_CLASS_NON_TRADE = "non_trade"
TRADE_CLASS_UNCLEAR = "unclear"


def classify_trade(txn: dict) -> str:
    """Transaktion deterministisch einer Trade-Klasse zuordnen.

    - ``satellite_trade``: manueller Kauf/Verkauf (transaction_type
      kauf/verkauf, Legacy type buy/sell oder side BUY/SELL). Einzelaktien-
      Trades und manuelle ETF-Kaeufe zaehlen zum Satellite-Trade-Limit.
    - ``sparplan``: automatischer Sparplan (transaction_type sparplan oder
      security_transaction_type SAVINGS_PLAN). Core-ETF-Sparplaene und
      Rentenanlage sind Bestandsaufbau, kein diskretionaerer Satellite-Trade.
    - ``non_trade``: Cash-Bewegung (einzahlung/ausschuettung/sonstiges).
    - ``unclear``: Typ nicht bestimmbar — fail-closed (zaehlt zum Limit,
      wird transparent ausgewiesen).
    """
    txn_type = txn.get("transaction_type")
    if txn_type == "sparplan":
        return TRADE_CLASS_SPARPLAN
    if txn_type in ("kauf", "verkauf"):
        return TRADE_CLASS_SATELLITE
    if txn_type:
        return TRADE_CLASS_NON_TRADE
    if txn.get("security_transaction_type") == "SAVINGS_PLAN":
        return TRADE_CLASS_SPARPLAN
    legacy_type = txn.get("type")
    if legacy_type in ("buy", "sell"):
        return TRADE_CLASS_SATELLITE
    if txn.get("side") in ("BUY", "SELL"):
        return TRADE_CLASS_SATELLITE
    return TRADE_CLASS_UNCLEAR


def _txn_instrument_kind(txn: dict, lookup: dict | None = None) -> str:
    """Instrument-Klasse: 'etf' (in config/etf_lookup.json) / 'aktie' / 'unknown'."""
    isin = str(txn.get("isin") or "")
    if not isin:
        return "unknown"
    if lookup is None:
        lookup = load_etf_lookup()
    return "etf" if isin in lookup else "aktie"


def _txn_etf_category(txn: dict, lookup: dict | None = None) -> str:
    """ETF-Kategorie (core/satellite) aus etf_lookup, sonst 'unknown'."""
    isin = str(txn.get("isin") or "")
    if not isin:
        return "unknown"
    if lookup is None:
        lookup = load_etf_lookup()
    entry = lookup.get(isin)
    return entry.get("category", "unknown") if isinstance(entry, dict) else "unknown"


def _is_trade(txn: dict) -> bool:
    """Kauf-/Verkaufs-Transaktion (inkl. Sparplan), keine Cash-Bewegung.

    Normalisiert: transaction_type in {kauf, verkauf, sparplan}. Legacy:
    type in {buy, sell} (Mock-Format) bzw. side BUY/SELL.
    """
    return classify_trade(txn) in (TRADE_CLASS_SATELLITE, TRADE_CLASS_SPARPLAN)


def _is_discretionary_trade(txn: dict) -> bool:
    """Diskretionaerer Kauf-/Verkauf — Sparplaene zaehlen NICHT dazu (Plan Phase 4).

    Nur manuelle Kaufe/Verkaufe (``kauf``/``verkauf`` bzw. Legacy ``buy``/``sell``)
    sind diskretionaere Trades; ``sparplan`` ist automatischer Bestandsaufbau
    und gehoert nicht zum Satellite-Trade-Limit (Core-ETF-Sparplaene).
    """
    return classify_trade(txn) == TRADE_CLASS_SATELLITE


def calculate_trades_in_quarter(transactions: list, quarter: str | None = None, strategy: dict | None = None) -> dict:
    """Trades im aktuellen Kalenderquartal gegen max_trades_per_quarter (5).

    Deterministische Basis fuer die Ampel-Kategorie Trades/Quartal (§6a):
    zaehlt nur DISKRETIONAERE Kauf-/Verkaufs-Ereignisse (keine Cash-Bewegungen,
    keine Sparplaene — Plan Phase 4) im Quartal (normalisiertes
    Transaktionsschema). Ohne quarter wird das aktuelle Quartal verwendet.
    Grenzwert fehlt -> 0: jeder Trade > 0 ist red (fail-closed).

    Unklare Fälle (Typ nicht bestimmbar) werden transparent ausgewiesen
    (``unclear_count``/``unclear``) und fail-closed wie diskretionaere Trades
    zum Limit gezaehlt — kein stilles Ignorieren.
    """
    if quarter is None:
        now = datetime.now(timezone.utc)
        quarter = f"{now.year}-Q{(now.month - 1) // 3 + 1}"
    trades: list[dict] = []
    unclear: list[dict] = []
    sparplan_count = 0
    for txn in transactions:
        if not isinstance(txn, dict):
            continue
        trade_class = classify_trade(txn)
        if _quarter_key(_txn_executed_at(txn)) != quarter:
            continue
        if trade_class == TRADE_CLASS_SPARPLAN:
            sparplan_count += 1
            continue
        if trade_class == TRADE_CLASS_NON_TRADE:
            continue
        entry = {
            "isin": txn.get("isin", ""),
            "executed_at": _txn_executed_at(txn),
            "transaction_type": txn.get("transaction_type", txn.get("type", "")),
        }
        if trade_class == TRADE_CLASS_UNCLEAR:
            unclear.append(entry)
            trades.append({**entry, "classification": "unclear"})
        else:
            trades.append(entry)
    threshold = _satellite_limits(strategy).get("max_trades_per_quarter") if strategy else None
    count = len(trades)
    status = "red"
    if isinstance(threshold, int) and not isinstance(threshold, bool):
        status = "green" if count < threshold else "yellow" if count == threshold else "red"
    return {
        "quarter": quarter,
        "trade_count": count,
        "trades": trades,
        "max_trades_per_quarter": threshold,
        "status": status,
        # Transparente Klassifikation (Plan Phase 3/4):
        "sparplan_count": sparplan_count,
        "unclear_count": len(unclear),
        "unclear": unclear,
    }


def calculate_weekly_trades(transactions: list, year: int | None = None, week: int | None = None) -> dict:
    """Wochenvergleich: Kauf-/Verkaufs-Ereignisse einer Kalenderwoche (ISO).

    Basis: normalisiertes Transaktionsschema (Plan §9). Ohne year/week wird
    die aktuelle ISO-Woche verwendet. Liefert Summen nach side (BUY/SELL).
    """
    if year is None or week is None:
        today = datetime.now(timezone.utc)
        year, week, _ = today.isocalendar()
    trades: list[dict] = []
    for txn in transactions:
        if not isinstance(txn, dict) or not _is_trade(txn):
            continue
        dt = _parse_iso(_txn_executed_at(txn))
        if dt is None:
            continue
        iso = dt.isocalendar()
        if iso[0] == year and iso[1] == week:
            trades.append({
                "isin": txn.get("isin", ""),
                "side": txn.get("side", "other"),
                "transaction_type": txn.get("transaction_type", txn.get("type", "")),
                "executed_at": _txn_executed_at(txn),
                "amount_eur": _txn_abs_amount(txn),
            })
    buys = sum(1 for t in trades if t["side"] == "BUY")
    sells = sum(1 for t in trades if t["side"] == "SELL")
    return {
        "year": year,
        "week": week,
        "trade_count": len(trades),
        "buy_count": buys,
        "sell_count": sells,
        "total_amount_eur": round(sum(t["amount_eur"] for t in trades), 2),
        "trades": trades,
    }


def _txn_abs_amount(txn: dict) -> float:
    """Absolutes Volumen einer Transaktion (amount_eur > amount > abgeleitet)."""
    for key in ("amount_eur", "amount"):
        raw = txn.get(key)
        if isinstance(raw, (int, float)) and not isinstance(raw, bool):
            return abs(float(raw))
    return _txn_price(txn) * _txn_quantity(txn)


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


# --- Briefing-Schnittstelle (Plan §6a): Ampel, Empfehlung, Positionsvorschlaege ----

# Die sieben verbindlichen Briefing-Kategorien (deterministische Ampel).
TRAFFIC_LIGHT_CATEGORIES = (
    "core_satellite",
    "sector_concentration",
    "single_position",
    "thesis_deadlines",
    "turnover",
    "trades_per_quarter",
    "data_quality",
)

_CATEGORY_LABELS = {
    "core_satellite": "Core-/Satelliten-Aufteilung",
    "sector_concentration": "Sektorkonzentration",
    "single_position": "Einzelposition",
    "thesis_deadlines": "Thesen-Fristen",
    "turnover": "Umschlag",
    "trades_per_quarter": "Trades/Quartal",
    "data_quality": "Datenqualität",
}

# Erlaubte Aktionen der Top-3-Positionsvorschlaege (nur Aenderungen).
POSITION_ACTION_TYPES = ("aufstocken", "reduzieren", "verkaufen")

# Prioritaet der Top-3-Rangfolge (Plan §6a): 1 = hoechste.
_ACTION_PRIORITY = {
    "single_position_red": 1,
    "sector_red": 2,
    "thesis_red": 3,
    "perf_negative": 4,
    "turnover_red": 5,
    "single_position_yellow": 6,
    "underweight": 7,
}

# Deterministische, nummernfreie Gegenargumente/Risiken pro Aktionstyp
# (Plan §6a: jede Positionsaktion muss Begruendung UND Gegenargument/Risiko
# enthalten). Bewusst ohne Zahlen: Positionsgewichte sind nicht Teil der
# verify-Allowlist (Zahlen duerfen nur 1:1 aus deterministic_summary kommen).
_ACTION_COUNTER_ARGUMENTS = {
    "aufstocken": "Bei fallenden Kursen vergrößert sich die Position vorübergehend.",
    "reduzieren": "Eine Kurserholung kann das Aufwärtspotenzial der reduzierten Position erhöhen.",
    "verkaufen": "Ein späterer Wiedereinstieg kann höhere Kosten verursachen.",
}


def _position_perf_6m_pct(position: dict) -> float | None:
    """6-Monats-Performance einer Position (position_perf_6m_pct).

    Kein Benchmark, absolute Performance. Fehlt das Datenfeld -> None
    (fail-closed: daraus wird kein SELL abgeleitet).
    """
    raw = position.get("position_perf_6m_pct")
    if raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if math.isnan(value):
        return None
    return value


# 6-Monats-Fenster: 180 Tage +/- Toleranz (Plan §6a: „Kurs vor 6 Monaten").
_PERF_6M_WINDOW_DAYS = 180
_PERF_6M_TOLERANCE_DAYS = 30


def _holding_unit_price(holding: dict) -> float | None:
    """Kurs je Einheit einer Holding: quote_mid_price > abgeleitet aus value/quantity."""
    raw = holding.get("quote_mid_price")
    if isinstance(raw, (int, float)) and not isinstance(raw, bool) and raw > 0:
        return float(raw)
    quantity = holding.get("quantity")
    value = holding.get("value_eur", holding.get("value"))
    if (
        isinstance(quantity, (int, float)) and not isinstance(quantity, bool) and quantity > 0
        and isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0
    ):
        return float(value) / float(quantity)
    return None


def _holding_value_eur(holding: dict) -> float | None:
    """Wert einer Holding in EUR (value_eur > valuation mit EUR-Currency)."""
    value = holding.get("value_eur")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    valuation = holding.get("valuation")
    if (
        isinstance(valuation, (int, float)) and not isinstance(valuation, bool)
        and holding.get("valuation_currency") in (None, "EUR")
    ):
        return float(valuation)
    return None


def _perf_6m_from_holdings(current: dict, previous: dict) -> float | None:
    """6-Monats-Performance einer Position aus zwei Holdings (absolut, ohne Benchmark).

    Beide Kurs-/Wertdaten muessen vorhanden sein, sonst None (fail-closed).
    Performance = (aktueller Wert - Wert vor 6 Monaten) / Wert vor 6 Monaten.
    """
    prev_value = _holding_value_eur(previous)
    cur_value = _holding_value_eur(current)
    if prev_value is None or cur_value is None or prev_value <= 0:
        return None
    return round((cur_value - prev_value) / prev_value, 6)


def compute_position_perf_6m(
    current_portfolio: dict,
    previous_snapshot: dict | None = None,
    current_captured_at: str | None = None,
) -> dict:
    """6-Monats-Performance pro ISIN aus dem Vorgaenger-Snapshot (Plan §6a).

    Nutzt den Archiv-/Vorgaenger-Snapshot, dessen captured_at im 6-Monats-
    Fenster (180 +/- 30 Tage vor ``current_captured_at``) liegt. Fehlen die
    Kurs-/Wertdaten einer Position, der passende Snapshot oder der aktuelle
    Zeitpunkt, bleibt die ISIN aus dem Ergebnis (fail-closed: kein SELL aus
    der Performance-Regel).

    Rueckgabe: {isin: position_perf_6m_pct} — nur Positionen mit Daten.
    """
    if not isinstance(current_portfolio, dict) or not isinstance(previous_snapshot, dict):
        return {}
    cur_captured = current_captured_at
    cur_holdings = current_portfolio.get("holdings", [])
    prev_portfolio = previous_snapshot.get("portfolio")
    if not isinstance(prev_portfolio, dict):
        return {}
    prev_holdings = prev_portfolio.get("holdings", [])
    if not isinstance(cur_holdings, list) or not isinstance(prev_holdings, list):
        return {}

    # Zeitfenster pruefen: previous muss ~6 Monate vor dem aktuellen Stand liegen.
    cur_cap = _parse_iso(str(cur_captured)) if cur_captured else None
    prev_cap = _parse_iso(str(previous_snapshot.get("captured_at")))
    if cur_cap is None or prev_cap is None:
        return {}
    delta_days = abs((cur_cap - prev_cap).total_seconds()) / 86400.0
    if not (_PERF_6M_WINDOW_DAYS - _PERF_6M_TOLERANCE_DAYS
            <= delta_days <= _PERF_6M_WINDOW_DAYS + _PERF_6M_TOLERANCE_DAYS):
        return {}

    prev_by_isin = {str(h.get("isin")): h for h in prev_holdings if isinstance(h, dict) and h.get("isin")}
    perf: dict[str, float] = {}
    for holding in cur_holdings:
        if not isinstance(holding, dict):
            continue
        isin = str(holding.get("isin", ""))
        if not isin or isin not in prev_by_isin:
            continue
        value = _perf_6m_from_holdings(holding, prev_by_isin[isin])
        if value is not None:
            perf[isin] = value
    return perf


def _find_position(positions: list, isin: str) -> dict | None:
    for p in positions:
        if p.get("isin") == isin:
            return p
    return None


def _weight_of(positions: list, isin: str) -> float:
    pos = _find_position(positions, isin)
    return pos.get("weight", 0.0) if pos else 0.0


# Normierter Dateiname einer Thesis: alles ausser [a-z0-9] wird entfernt.
_THESIS_STEM_NORMALIZE_RE = re.compile(r"[^a-z0-9]+")


def _normalize_token(token: str) -> str:
    """Normierter Vergleichs-Token: lowercase, nur [a-z0-9]."""
    return _THESIS_STEM_NORMALIZE_RE.sub("", str(token).lower())


def _position_identifiers(position: dict) -> list[str]:
    """Normierte Identifier einer Position: ISIN, Ticker, Name(woerter)."""
    identifiers = [
        _normalize_token(position.get("isin", "")),
        _normalize_token(position.get("ticker", "")),
        _normalize_token(position.get("name", "")),
    ]
    name = position.get("name")
    if isinstance(name, str):
        identifiers.extend(_normalize_token(part) for part in name.split() if len(part) >= 3)
    return [i for i in identifiers if i]


# --- Watchlist-/Satellite-Signale (Plan Phase 3): 4 Dimensionen, deterministisch ---
#
# Das deterministische Signalmodell fuer Watchlist-Kandidaten UND bestehende
# Satellite-Positionen. Labels: BUY / SELL / AVOID / WATCH / NO SIGNAL.
# Fundamentaldaten (KGV, Gewinnwachstum, Umsatz, Dividendenrendite) werden
# NIE gelesen — nur die 4 Dimensionen. Jede Dimension ist -1/0/+1, fail-closed
# None bei fehlenden Daten (kein erfundener Score). Das Signal-Datenmodell
# bildet den Disclaimer ab: ``fundamentals_used: false`` und pro Dimension
# der Teil-Score, damit der Renderer den Hinweis auf fehlende Fundamentaldaten
# verankern kann.
#
# Harte Ausschlussregeln (vor der Score-Berechnung, kein BUY/SELL moeglich):
# - Core-ETFs (category "core" aus etf_lookup) bekommen KEIN Signal
#   (Strategie-Setup, keine Trade-Kandidaten) -> NO SIGNAL.
# - SUSE / LU2722255754 ist illiquide Legacy (Datenqualitaetsregel §8):
#   Kategorie "unknown", Gewicht 0, Kennzahlen fliessen nie in den Score ->
#   NO SIGNAL.
# - SELL NUR fuer bestehende Satellite-Positionen (nicht Core-ETFs, nicht
#   SUSE/unknown-Kategorien).
# - BUY/AVOID/WATCH/NO SIGNAL fuer Watchlist-Kandidaten.
# - ETFs (category "satellite", sector Diversified) erhalten D1 = 0 statt
#   None: kein Sektor-Signal, aber bekannte Kategorie.
#
# Schwellen (fest im Code, transparent):
#   BUY      Score >= +3 und D1 >= 0
#   SELL     Score <= -2 und D2 == -1 (nur bestehende Satellites)
#   AVOID    Score <= -2 und D1 == -1 (Sektor ausgeschlossen)
#   WATCH    sonst (mindestens 3 nicht-None-Dimensionen)
#   NO SIGNAL weniger als 3 nicht-None-Dimensionen

# ISINs ohne Signal (Legacy/illiquide oder Strategie-Setup).
ILLIQUID_LEGACY_ISINS = {"LU2722255754"}  # SUSE — illiquide Legacy

# Label-Konstanten des deterministischen Signalmodells.
SIGNAL_BUY = "BUY"
SIGNAL_SELL = "SELL"
SIGNAL_AVOID = "AVOID"
SIGNAL_WATCH = "WATCH"
SIGNAL_NO_SIGNAL = "NO SIGNAL"

# Score-Schwellen (fest im Code, Teil der Signal-Transparenz).
_SIGNAL_BUY_MIN_SCORE = 3
_SIGNAL_SELL_MAX_SCORE = -2
_SIGNAL_AVOID_MAX_SCORE = -2
_SIGNAL_MIN_DIMENSIONS = 3

# Keyword-Signale fuer D3 (7-Tage-News-Sentiment). Deterministisch, kein LLM.
# Negative Begriffe haben Vorrang vor positiven (Sicherheits-Bias).
_SIGNAL_POSITIVE_KEYWORDS = (
    "gewinn", "rekord", "wachstum", "umsatzplus", "aufwaerts", "rally",
    "steigt", "steigend", "erholt", "kaufen", "outperform", "stark",
)
_SIGNAL_NEGATIVE_KEYWORDS = (
    "verlust", "rueckgang", "ruecklaeufig", "fällt", "faellt", "absturz",
    "klage", "strafe", "buße", "busse", "entlassung", "warnung", "verliert",
    "corona", "krise", "pleite", "insolvenz", "kursrutsch", "einbruch",
)


def _signal_sector(item: dict) -> str:
    """Sektor eines Items: explizites Feld, sonst etf_lookup, sonst 'unknown'."""
    sector = item.get("sector")
    if isinstance(sector, str) and sector:
        return sector
    lookup = load_etf_lookup()
    entry = lookup.get(str(item.get("isin", "")))
    if isinstance(entry, dict):
        sector = entry.get("sector")
        if isinstance(sector, str) and sector:
            return sector
    return "unknown"


def _signal_is_etf(item: dict) -> bool:
    """True, wenn das Item ein ETF ist (etf_lookup-ISIN)."""
    return str(item.get("isin", "")) in load_etf_lookup()


def _dimension_strategy_fit(item: dict, strategy: dict) -> int | None:
    """D1 Strategie-Fit: Core/Satellite-Kategorie + Sektor-Praeferenz/-Ausschluss.

    - category "core" -> +1 (Basis-Anlage), category "satellite" -> 0,
      category "unknown" -> None (fail-closed, kein erfundener Fit).
    - ETFs (Kategorie "satellite" mit Sektor "Diversified") -> 0 statt None:
      kein Sektor-Signal, aber bekannte Kategorie.
    - Sektor in ``sectors.preferred`` -> +1, in ``sectors.excluded`` -> -1,
      sonst 0.

    Kategorie je Item: Strategie-Klassifikation (confirmed) -> Rohdaten-
    category -> etf_lookup (siehe _holding_category).
    """
    category = _holding_category(item, strategy)
    if category == "core":
        base = 1
    elif category == "satellite":
        base = 0
    else:
        return None
    sector = _signal_sector(item)
    if _signal_is_etf(item) or sector in ("Diversified", "unknown"):
        return base
    preferred = strategy.get("sectors", {}).get("preferred", [])
    excluded = strategy.get("sectors", {}).get("excluded", [])
    if isinstance(preferred, list) and sector in preferred:
        return base + 1
    if isinstance(excluded, list) and sector in excluded:
        return base - 1
    return base


def _signal_holding_isin(item: dict) -> str:
    """ISIN eines Holdings/Items (string, leer wenn fehlt)."""
    return str(item.get("isin") or "")


def _dimension_portfolio_fit(
    item: dict,
    portfolio: dict,
    strategy: dict,
    analysis: dict | None = None,
) -> int | None:
    """D2 Portfolio-Fit: Platz im Satellite-Budget.

    - Schon vorhanden als Holding -> -1 (kein Add noetig, ggf. reduzieren).
    - Sektor-Konzentration wuerde ``max_sector_pct`` bei Add ueberschreiten -> -1.
    - Gewicht wuerde ``max_position_pct`` ueberschreiten -> -1.
    - Sonst +1 (Platz im Satellite-Budget).
    """
    holdings = portfolio.get("holdings", [])
    if not isinstance(holdings, list):
        holdings = []
    isin = _signal_holding_isin(item)
    if any(str(h.get("isin")) == isin for h in holdings if isinstance(h, dict)):
        return -1
    if _signal_is_etf(item):
        return 1
    limits = _satellite_limits(strategy)
    max_sector = _pct(limits.get("max_sector_pct"))
    max_position = _pct(limits.get("max_position_pct"))
    total = _portfolio_total(portfolio, holdings)
    sector = _signal_sector(item)
    if max_sector and total > 0:
        sector_total = 0.0
        for h in holdings:
            if not isinstance(h, dict):
                continue
            h_sector = h.get("sector") or load_etf_lookup().get(str(h.get("isin")), {}).get("sector") or "unknown"
            if h_sector == sector:
                sector_total += _holding_value(h)
        if (sector_total + _holding_value(item)) / total > max_sector:
            return -1
    item_value = _holding_value(item)
    if max_position and total > 0 and item_value > 0:
        if item_value / total > max_position:
            return -1
    return 1


def _signal_item_news(item: dict, news: list) -> list[dict]:
    """News-Eintraege zu einem Item (ISIN/Ticker/Name-Matching, deterministisch)."""
    if not isinstance(news, list):
        return []
    keywords = _position_identifiers(item)
    if not keywords:
        return []
    matched = []
    for n in news:
        if not isinstance(n, dict):
            continue
        text = " ".join([
            str(n.get("title", "")),
            str(n.get("summary", "")),
            str(n.get("description", "")),
        ]).lower()
        if any(kw in text for kw in keywords):
            matched.append(n)
    return matched


def _dimension_news_sentiment(item: dict, news: list) -> int | None:
    """D3 7-Tage-News-Sentiment: +1 positiv, -1 negativ, 0 gemischt/keine.

    Deterministische Keyword-Signale (kein LLM). Negative Begriffe haben
    Vorrang (Sicherheits-Bias); sowohl positive als auch negative Treffer
    -> 0 (gemischt, kein klares Signal). Keine News -> 0 (kein Signal).
    """
    matched = _signal_item_news(item, news)
    if not matched:
        return 0
    text = " ".join(
        str(entry.get("title", ""))
        + " "
        + str(entry.get("summary", ""))
        + " "
        + str(entry.get("description", ""))
        for entry in matched
    ).lower()
    negative_hits = [kw for kw in _SIGNAL_NEGATIVE_KEYWORDS if kw in text]
    positive_hits = [kw for kw in _SIGNAL_POSITIVE_KEYWORDS if kw in text]
    if negative_hits and not positive_hits:
        return -1
    if positive_hits and not negative_hits:
        return 1
    return 0


def _dimension_price_development(
    item: dict,
    previous_snapshot: dict | None,
    current_captured_at: str | None,
) -> int | None:
    """D4 sc-Kurs-/Kursentwicklung: +1/-1/0 aus Vorgaenger-Snapshot, sonst None.

    Nur wenn ein Vorgaenger-Snapshot mit Kurs-/Wertdaten derselben ISIN
    vorhanden ist: beobachtete Entwicklung positiv/negativ/stagnierend.
    Fehlt der Kurs oder der Vergleichszeitpunkt -> None (fail-closed,
    kein erfundener Kurs-Trend).
    """
    if not isinstance(previous_snapshot, dict) or not current_captured_at:
        return None
    prev_portfolio = previous_snapshot.get("portfolio")
    if not isinstance(prev_portfolio, dict):
        return None
    isin = _signal_holding_isin(item)
    if not isin:
        return None
    prev_holdings = prev_portfolio.get("holdings", [])
    if not isinstance(prev_holdings, list):
        return None
    prev_holding = next((h for h in prev_holdings if isinstance(h, dict) and str(h.get("isin")) == isin), None)
    if prev_holding is None:
        return None
    prev_value = _holding_value_eur(prev_holding)
    cur_value = _holding_value_eur(item)
    if prev_value is None or cur_value is None or prev_value <= 0:
        return None
    change = (cur_value - prev_value) / prev_value
    if change > 0.005:
        return 1
    if change < -0.005:
        return -1
    return 0


def _signal_item_reason(item: dict, dimensions: dict) -> str:
    """Deterministische, nummernfreie Signal-Begruendung (keine Fundamentaldaten)."""
    parts = []
    d1 = dimensions.get("strategy_fit")
    d2 = dimensions.get("portfolio_fit")
    d3 = dimensions.get("news_sentiment")
    d4 = dimensions.get("price_development")
    if d1 is not None:
        sector = _signal_sector(item)
        if d1 >= 1:
            parts.append("Strategie-Fit positiv")
        elif d1 == 0:
            parts.append("Strategie-Fit neutral")
        else:
            parts.append("Sektor ausgeschlossen")
    if d2 is not None:
        parts.append("Portfolio-Fit positiv" if d2 > 0 else "Portfolio-Fit negativ")
    if d3 is not None:
        if d3 > 0:
            parts.append("7-Tage-News positiv")
        elif d3 < 0:
            parts.append("7-Tage-News negativ")
        else:
            parts.append("7-Tage-News neutral")
    if d4 is not None:
        if d4 > 0:
            parts.append("Kursentwicklung positiv")
        elif d4 < 0:
            parts.append("Kursentwicklung negativ")
        else:
            parts.append("Kursentwicklung stagnierend")
    return "; ".join(parts) if parts else "Keine ausreichenden Daten."


def compute_watchlist_signals(
    watchlist: list,
    portfolio: dict,
    analysis: dict,
    news: list,
    strategy: dict,
    previous_snapshot: dict | None = None,
    current_captured_at: str | None = None,
) -> list[dict]:
    """Deterministische Signale fuer Watchlist-Kandidaten UND bestehende Satellites.

    Pro Item ein Signal-Objekt: {isin, name, signal, score, dimensions, reason,
    fundamentals_used: false}. Kategorien und Gewichte der bestehenden
    Satellite-Holdings werden aus ``analysis["checks"]["positions"]``
    (deterministisch) gelesen — nie neu berechnet. Sektor-Konzentration/
    Einzelpositions-Limits kommen aus der Analyse (Quelle: strategy.yaml).

    Harte Ausschlussregeln: Core-ETFs und SUSE/LU2722255754 (illiquide
    Legacy) bekommen NO SIGNAL (kein BUY/SELL). SELL nur fuer bestehende
    Satellite-Positionen; BUY/AVOID/WATCH/NO SIGNAL fuer Watchlist-Items.

    Das Signal-Objekt traegt ``fundamentals_used: false`` und die Teil-Scores
    je Dimension — das Datenmodell bildet den Fundamentaldaten-Disclaimer ab
    (der Renderer zeigt ihn in der Signal-Sektion an).
    """
    if not isinstance(watchlist, list):
        return []
    holdings = portfolio.get("holdings", []) if isinstance(portfolio, dict) else []
    if not isinstance(holdings, list):
        holdings = []
    holding_isins = {str(h.get("isin")) for h in holdings if isinstance(h, dict) and h.get("isin")}
    positions = analysis.get("checks", {}).get("positions", {}).get("positions", []) if isinstance(analysis, dict) else []
    if not isinstance(positions, list):
        positions = []
    sector_check = analysis.get("checks", {}).get("sector_concentration", {}) if isinstance(analysis, dict) else {}
    single_pos_check = analysis.get("checks", {}).get("single_position", {}) if isinstance(analysis, dict) else {}
    max_sector = _pct(_satellite_limits(strategy).get("max_sector_pct"))
    max_position = _pct(_satellite_limits(strategy).get("max_position_pct"))

    signals: list[dict] = []
    for item in watchlist:
        if not isinstance(item, dict):
            continue
        item_isin = str(item.get("isin") or "")
        if not item_isin:
            continue
        category = _holding_category(item, strategy)
        name = str(item.get("name") or item_isin)

        # Harte Ausschlussregeln: kein Signal fuer Core-ETFs und Legacy/illiquide.
        # SUSE/LU2722255754 bleibt ueber ILLIQUID_LEGACY_ISINS abgedeckt; eine
        # Strategie-Klassifikation "legacy" (confirmed) blockt ebenfalls Signale.
        if category == "core" or item_isin in ILLIQUID_LEGACY_ISINS or category == "legacy":
            signals.append({
                "isin": item_isin,
                "name": name,
                "signal": SIGNAL_NO_SIGNAL,
                "score": 0,
                "dimensions": {},
                "reason": "Ausschlussregel: Strategie-Setup oder illiquide Legacy (keine Fundamentaldaten-Basis).",
                "fundamentals_used": False,
                "excluded": True,
            })
            continue

        dimensions = {
            "strategy_fit": _dimension_strategy_fit(item, strategy),
            "portfolio_fit": _dimension_portfolio_fit(item, portfolio, strategy, analysis),
            "news_sentiment": _dimension_news_sentiment(item, news),
            "price_development": _dimension_price_development(item, previous_snapshot, current_captured_at),
        }
        scores = [d for d in dimensions.values() if d is not None]
        score = sum(scores)
        present = len(scores)

        is_holding = item_isin in holding_isins
        is_satellite_holding = is_holding and category == "satellite"
        if present < _SIGNAL_MIN_DIMENSIONS:
            signal = SIGNAL_NO_SIGNAL
        elif score >= _SIGNAL_BUY_MIN_SCORE and dimensions["strategy_fit"] is not None and dimensions["strategy_fit"] >= 0:
            signal = SIGNAL_BUY
        elif score <= _SIGNAL_SELL_MAX_SCORE and is_satellite_holding and dimensions["portfolio_fit"] == -1:
            signal = SIGNAL_SELL
        elif score <= _SIGNAL_AVOID_MAX_SCORE and dimensions["strategy_fit"] == -1:
            signal = SIGNAL_AVOID
        elif score <= _SIGNAL_SELL_MAX_SCORE and not is_satellite_holding:
            signal = SIGNAL_WATCH  # negativer Watchlist-Kandidat: kein SELL, WATCH
        else:
            signal = SIGNAL_WATCH

        signals.append({
            "isin": item_isin,
            "name": name,
            "signal": signal,
            "score": score,
            "dimensions": dimensions,
            "reason": _signal_item_reason(item, dimensions),
            "fundamentals_used": False,
            "excluded": False,
        })
    return signals


def _match_thesis_to_position(thesis_file: str, positions: list) -> dict | None:
    """Ordnet eine Thesis-Datei einer Position zu (ISIN/Ticker/Name-Matching).

    Deterministische Reihenfolge:
    1. Exakte ISIN-Uebereinstimmung: Dateiname enthaelt die ISIN einer Position.
    2. Exakter Ticker-/Name-Token: normierter Dateiname == normierter
       Ticker/Name einer Position (oder Token im Dateinamen enthalten).
    3. Robustes Wort-Token-Matching: normierter Dateiname enthaelt einen
       normierten Namensteil (>= 4 Zeichen) einer Position.

    Liefert die erste passende Position oder None (keine erfundene Zuordnung).
    """
    stem = _normalize_token(Path(thesis_file).stem)

    # 1. Exakte ISIN im Dateinamen.
    for p in positions:
        isin = _normalize_token(p.get("isin", ""))
        if isin and len(isin) >= 10 and isin in stem:
            return p

    # 2. Exakter Ticker-/Name-Token-Match (Dateiname == Identifier).
    for p in positions:
        for identifier in _position_identifiers(p):
            if identifier and len(identifier) >= 2 and identifier == stem:
                return p

    # 3. Wort-Token im Dateinamen (Name-Bestandteile >= 4 Zeichen).
    for p in positions:
        name = _normalize_token(p.get("name", ""))
        if not name:
            continue
        name_parts = [t for t in name.split() if len(t) >= 4]
        if name_parts and all(part in stem for part in name_parts):
            return p
        for part in name_parts:
            if part in stem:
                return p

    return None


def build_traffic_lights(analysis: dict, strategy: dict, data_quality: dict | None = None) -> dict:
    """Deterministische Ampel fuer die sieben Briefing-Kategorien (Plan §6a).

    Jede Kategorie erhaelt {status: green|yellow|red, reason: <str>}. Das LLM
    uebernimmt Status + Begruendung 1:1 und darf die Ampel weder erfinden
    noch verschieben. Die sieben Kategorien sind verbindlich; es gibt keine
    achte Kategorie.
    """
    checks = analysis.get("checks", {})
    dq = data_quality if isinstance(data_quality, dict) else {}
    dq_status = dq.get("status")
    dq_issues = dq.get("issues", [])
    if not isinstance(dq_issues, list):
        dq_issues = []

    limits = _satellite_limits(strategy)
    portfolio_cfg = _portfolio_cfg(strategy)
    core_pct = _pct(portfolio_cfg.get("core_pct"))
    threshold_pct = _pct(_rebalancing_cfg(strategy).get("threshold_pct"))
    target_pos = _pct(limits.get("target_position_pct"))
    max_pos = _pct(limits.get("max_position_pct"))
    max_sector = _pct(limits.get("max_sector_pct"))
    max_turnover = _pct(limits.get("max_turnover_annual_pct"))

    lights: dict[str, dict] = {}

    # 1. Core-/Satelliten-Aufteilung
    core_check = checks.get("core_satellite", {})
    core_ratio = _as_percent(core_check.get("core_ratio"))
    if core_ratio is None or not core_pct:
        lights["core_satellite"] = {
            "status": "red",
            "reason": "Core-/Satelliten-Aufteilung nicht bestimmbar (Zielquote oder Ist-Ratio fehlt).",
        }
    elif abs(core_ratio - core_pct) <= threshold_pct:
        lights["core_satellite"] = {
            "status": "green",
            "reason": f"Core-Ratio {core_ratio:.1%} innerhalb der Toleranz von {threshold_pct:.1%} um das Ziel {core_pct:.1%}.",
        }
    elif abs(core_ratio - core_pct) <= threshold_pct * 2:
        lights["core_satellite"] = {
            "status": "yellow",
            "reason": f"Core-Ratio {core_ratio:.1%} driftet, aber noch innerhalb der Warn-Stufe.",
        }
    else:
        lights["core_satellite"] = {
            "status": "red",
            "reason": f"Core-Ratio {core_ratio:.1%} driftet deutlich vom Ziel {core_pct:.1%} ab (Toleranz {threshold_pct:.1%}).",
        }

    # 2. Sektorkonzentration
    sector_check = checks.get("sector_concentration", {})
    max_ratio = _as_percent(sector_check.get("max_ratio"))
    max_sector_name = _str_or(sector_check.get("max_sector"))
    if max_ratio is None or not max_sector:
        lights["sector_concentration"] = {
            "status": "red",
            "reason": "Sektorkonzentration nicht bestimmbar (Grenzwert oder Ist-Wert fehlt).",
        }
    elif max_ratio <= max_sector:
        lights["sector_concentration"] = {
            "status": "green",
            "reason": f"Stärkster Sektor {max_sector_name} bei {max_ratio:.1%} innerhalb der Grenze von {max_sector:.1%}.",
        }
    else:
        lights["sector_concentration"] = {
            "status": "red",
            "reason": f"Stärkster Sektor {max_sector_name} bei {max_ratio:.1%} über der Grenze von {max_sector:.1%}.",
        }

    # 3. Einzelposition
    sp_check = checks.get("single_position", {})
    max_position = sp_check.get("max_position")
    max_position_weight = _as_percent(max_position.get("weight")) if isinstance(max_position, dict) else None
    max_position_name = _str_or(max_position.get("name")) if isinstance(max_position, dict) else ""
    if max_position_weight is None or not max_pos:
        lights["single_position"] = {
            "status": "red",
            "reason": "Einzelpositions-Limit nicht bestimmbar (Grenzwert oder Position fehlt).",
        }
    elif max_position_weight > max_pos:
        lights["single_position"] = {
            "status": "red",
            "reason": f"Position {max_position_name} bei {max_position_weight:.1%} über dem Maximum von {max_pos:.1%}.",
        }
    elif target_pos and max_position_weight > target_pos:
        lights["single_position"] = {
            "status": "yellow",
            "reason": f"Position {max_position_name} bei {max_position_weight:.1%} zwischen Ziel {target_pos:.1%} und Maximum {max_pos:.1%}.",
        }
    else:
        lights["single_position"] = {
            "status": "green",
            "reason": f"Größte Position {max_position_name} bei {max_position_weight:.1%} innerhalb des Ziels {target_pos:.1%}.",
        }

    # 4. Thesen-Fristen
    thesis_check = checks.get("thesis_deadlines", {})
    outdated = thesis_check.get("outdated", [])
    if not isinstance(outdated, list):
        outdated = []
    if outdated:
        names = ", ".join(o.get("file", "?") for o in outdated[:3])
        lights["thesis_deadlines"] = {
            "status": "red",
            "reason": f"Abgelaufene Thesen: {names}.",
        }
    else:
        lights["thesis_deadlines"] = {
            "status": "green",
            "reason": "Keine abgelaufenen Thesen.",
        }

    # 5. Umschlag
    turnover_check = checks.get("turnover", {})
    turnover_ratio = _as_percent(turnover_check.get("turnover_ratio"))
    if turnover_ratio is None or not max_turnover:
        lights["turnover"] = {
            "status": "red",
            "reason": "Umschlag nicht bestimmbar (Grenzwert oder Ist-Wert fehlt).",
        }
    elif turnover_ratio <= max_turnover:
        lights["turnover"] = {
            "status": "green",
            "reason": f"Umschlag {turnover_ratio:.1%} innerhalb der Grenze von {max_turnover:.1%}.",
        }
    else:
        lights["turnover"] = {
            "status": "red",
            "reason": f"Umschlag {turnover_ratio:.1%} über der Grenze von {max_turnover:.1%}.",
        }

    # 6. Trades/Quartal (aus analyse: trades_per_quarter)
    trades_check = checks.get("trades_per_quarter", {})
    trade_count = trades_check.get("trade_count")
    max_trades = trades_check.get("max_trades_per_quarter")
    if trade_count is None or not isinstance(max_trades, int) or isinstance(max_trades, bool):
        lights["trades_per_quarter"] = {
            "status": "red",
            "reason": "Trades/Quartal nicht bestimmbar (Grenzwert fehlt).",
        }
    elif trade_count < max_trades:
        lights["trades_per_quarter"] = {
            "status": "green",
            "reason": f"{trade_count} Trades im Quartal, unter dem Limit von {max_trades}.",
        }
    elif trade_count == max_trades:
        lights["trades_per_quarter"] = {
            "status": "yellow",
            "reason": f"{trade_count} Trades im Quartal, Limit {max_trades} erreicht.",
        }
    else:
        lights["trades_per_quarter"] = {
            "status": "red",
            "reason": f"{trade_count} Trades im Quartal, über dem Limit von {max_trades}.",
        }

    # 7. Datenqualität (aggregiert SUSE-Regel + fehlende 6-Monats-Performance)
    if dq_status in (None, "ok"):
        lights["data_quality"] = {
            "status": "green",
            "reason": "Datenqualität: ok.",
        }
    elif dq_status in ("incomplete", "stale"):
        lights["data_quality"] = {
            "status": "yellow",
            "reason": "Datenqualität unvollständig: " + "; ".join(dq_issues[:3]),
        }
    else:
        lights["data_quality"] = {
            "status": "red",
            "reason": "Datenqualitätsfehler: " + "; ".join(dq_issues[:3]),
        }

    return lights


def _str_or(value: object) -> str:
    return str(value) if value else ""


def build_recommendation(traffic_lights: dict, strategy: dict) -> dict:
    """Deterministische Gesamt-Empfehlung (Plan §6a).

    - SELL, wenn mindestens 4 von 7 Kategorien rot sind.
    - BUY, wenn mindestens 4 von 7 Kategorien gruen sind UND Sparrate > 0.
    - WATCH in allen anderen Faellen.

    Label + Begruendung werden 1:1 vom LLM uebernommen (verify prueft).
    """
    statuses = [t.get("status") for t in traffic_lights.values() if isinstance(t, dict)]
    red = sum(1 for s in statuses if s == "red")
    green = sum(1 for s in statuses if s == "green")
    total = len(statuses)
    # Sparrate: analyserelevantes Investor-Feld (Plan §6a: BUY nur bei Sparrate > 0).
    investor = strategy.get("investor", {}) if isinstance(strategy, dict) else {}
    savings = investor.get("monthly_savings_eur") if isinstance(investor, dict) else None
    savings_positive = isinstance(savings, (int, float)) and not isinstance(savings, bool) and savings > 0

    if total and red >= 4:
        label = "SELL"
        reason = f"{red} von {total} Kategorien rot (Mehrheit rot) — Gesamt-Empfehlung SELL."
    elif total and green >= 4 and savings_positive:
        label = "BUY"
        reason = f"{green} von {total} Kategorien grün und Sparrate > 0 — Gesamt-Empfehlung BUY."
    else:
        label = "WATCH"
        if not savings_positive:
            reason = f"{green} von {total} Kategorien grün, aber Sparrate 0 — kein BUY; Gesamt-Empfehlung WATCH."
        else:
            reason = f"{red} rot, {green} grün von {total} Kategorien — Gesamt-Empfehlung WATCH."
    return {"label": label, "reason": reason}


def build_position_actions(
    positions: list,
    traffic_lights: dict,
    strategy: dict,
    transactions: list | None = None,
    data_quality: dict | None = None,
    analysis: dict | None = None,
) -> list[dict]:
    """Deterministische Top-3-Positionsvorschlaege (Plan §6a).

    Nur Aenderungen (aufstocken/reduzieren/verkaufen) mit konkreter ISIN,
    keine bloesse HOLD-Ausgabe. Priorisierung nach Rangfolge 1-7, bei
    gleichem Rang nach Groesse der Abweichung vom Strategie-Grenzwert.
    Maximal 3 Vorschlaege. 6-Monats-Performance: negativ -> Reduktion NUR
    wenn das Datenfeld vorhanden ist (fail-closed, kein SELL ohne Daten).

    Jeder Vorschlag enthaelt ``gegenargument``: eine deterministische,
    nummernfreie Risiko-/Gegenargument-Formulierung passend zum Aktionstyp
    (``_ACTION_COUNTER_ARGUMENTS``). Reasons sind bewusst nummernfrei —
    Positionsgewichte gehoeren nicht zur verify-Allowlist (Zahlen nur 1:1
    aus deterministic_summary, verify.verify_draft blockt Abweichungen).

    ``analysis`` (optional): liefert die strukturierten thesis_deadlines/
    outdated-Daten (file + created) fuer die deterministische Zuordnung
    abgelaufener Thesen zu Positionen (ISIN/Ticker/Dateinamen-Matching).
    Ohne analysis wird auf die traffic_lights-reason zurueckgegriffen
    (keine Zuordnung moeglich -> keine erfundenen Vorschlaege).
    """
    limits = _satellite_limits(strategy)
    target_pos = _pct(limits.get("target_position_pct"))
    max_pos = _pct(limits.get("max_position_pct"))
    max_sector = _pct(limits.get("max_sector_pct"))

    candidates: list[dict] = []

    # 1. Rote Ampel Einzelposition -> Reduzierung der betreffenden ISIN.
    sp_light = traffic_lights.get("single_position", {})
    if sp_light.get("status") == "red":
        for p in positions:
            if p.get("weight", 0) > max_pos:
                candidates.append({
                    "action": "reduzieren",
                    "isin": p.get("isin", ""),
                    "name": p.get("name", ""),
                    "reason": "Einzelposition über dem Maximum.",
                    "gegenargument": _ACTION_COUNTER_ARGUMENTS["reduzieren"],
                    "priority": _ACTION_PRIORITY["single_position_red"],
                    "deviation": p.get("weight", 0) - max_pos,
                })

    # 2. Rote Ampel Sektorkonzentration -> Reduzierung der uebergewichtigen Positionen.
    sector_light = traffic_lights.get("sector_concentration", {})
    if sector_light.get("status") == "red":
        sector_check_value = _sector_overweight_positions(positions, max_sector)
        for p, _weight in sector_check_value:
            candidates.append({
                "action": "reduzieren",
                "isin": p.get("isin", ""),
                "name": p.get("name", ""),
                "reason": "Position im übergewichteten Sektor.",
                "gegenargument": _ACTION_COUNTER_ARGUMENTS["reduzieren"],
                "priority": _ACTION_PRIORITY["sector_red"],
                "deviation": _weight - max_sector,
            })

    # 3. Rote Ampel Thesen-Fristen -> Verkauf/Reduktion der betreffenden ISIN.
    #    Nutzt die strukturierten thesis_deadlines/outdated-Daten (file + created)
    #    und matcht per ISIN/Ticker/robustem Thesis-Dateinamen (kein reason-String-
    #    Substring-Match).
    thesis_light = traffic_lights.get("thesis_deadlines", {})
    if thesis_light.get("status") == "red":
        thesis_check = {}
        if isinstance(analysis, dict):
            thesis_check = analysis.get("checks", {}).get("thesis_deadlines", {})
        if not isinstance(thesis_check, dict):
            thesis_check = {}
        outdated = thesis_check.get("outdated", [])
        if not isinstance(outdated, list):
            outdated = []
        for thesis in outdated:
            if not isinstance(thesis, dict):
                continue
            thesis_file = thesis.get("file", "")
            matched = _match_thesis_to_position(str(thesis_file), positions)
            if matched is None:
                continue
            candidates.append({
                "action": "verkaufen",
                "isin": matched.get("isin", ""),
                "name": matched.get("name", ""),
                "reason": "Abgelaufene Thesis.",
                "gegenargument": _ACTION_COUNTER_ARGUMENTS["verkaufen"],
                "priority": _ACTION_PRIORITY["thesis_red"],
                "deviation": 0.0,
            })

    # 4. 6-Monats-Performance negativ (Datenfeld vorhanden) -> Reduktion/Verkauf.
    for p in positions:
        perf = _position_perf_6m_pct(p)
        if perf is not None and perf < 0:
            candidates.append({
                "action": "reduzieren",
                "isin": p.get("isin", ""),
                "name": p.get("name", ""),
                "reason": "6-Monats-Performance negativ.",
                "gegenargument": _ACTION_COUNTER_ARGUMENTS["reduzieren"],
                "priority": _ACTION_PRIORITY["perf_negative"],
                "deviation": abs(perf),
            })

    # 5. Rote Ampel Umschlag/Trades -> Reduzierung des staerksten Umschlag-Beitragers.
    turnover_light = traffic_lights.get("turnover", {})
    if turnover_light.get("status") == "red" and transactions:
        contributor = _max_turnover_contributor(transactions, positions)
        if contributor:
            candidates.append({
                "action": "reduzieren",
                "isin": contributor.get("isin", ""),
                "name": contributor.get("name", ""),
                "reason": "Stärkster Beitrag zum überhöhten Umschlag.",
                "gegenargument": _ACTION_COUNTER_ARGUMENTS["reduzieren"],
                "priority": _ACTION_PRIORITY["turnover_red"],
                "deviation": 0.0,
            })

    # 6. Gelbe Ampel Einzelposition (zwischen Ziel und Maximum) -> Aufstockung/Reduzierung.
    if sp_light.get("status") == "yellow" and target_pos:
        for p in positions:
            if target_pos < p.get("weight", 0) <= max_pos:
                candidates.append({
                    "action": "reduzieren",
                    "isin": p.get("isin", ""),
                    "name": p.get("name", ""),
                    "reason": "Einzelposition zwischen Ziel und Maximum.",
                    "gegenargument": _ACTION_COUNTER_ARGUMENTS["reduzieren"],
                    "priority": _ACTION_PRIORITY["single_position_yellow"],
                    "deviation": p.get("weight", 0) - target_pos,
                })

    # 7. Untergewichtung (Gewicht < Ziel) -> Aufstockung (bei positiver/aktiver These).
    if target_pos:
        for p in positions:
            if p.get("weight", 0) < target_pos:
                candidates.append({
                    "action": "aufstocken",
                    "isin": p.get("isin", ""),
                    "name": p.get("name", ""),
                    "reason": "Position unter dem Ziel.",
                    "gegenargument": _ACTION_COUNTER_ARGUMENTS["aufstocken"],
                    "priority": _ACTION_PRIORITY["underweight"],
                    "deviation": target_pos - p.get("weight", 0),
                })

    # Deduplizieren (gleiche ISIN + Aktion), deterministisch sortieren, Top-3.
    seen: set[tuple] = set()
    unique: list[dict] = []
    for c in candidates:
        key = (c["action"], c["isin"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(c)
    unique.sort(key=lambda c: (c["priority"], -c["deviation"], c["isin"]))
    return unique[:3]


def _sector_overweight_positions(positions: list, max_sector: float) -> list[tuple[dict, float]]:
    """Positionen in Sektoren ueber der Grenze (mit Sektor-Gesamtgewicht)."""
    etf_lookup = load_etf_lookup()
    sector_total: dict[str, float] = {}
    for p in positions:
        sector = p.get("sector") or etf_lookup.get(p.get("isin", ""), {}).get("sector") or "Unknown"
        sector_total[sector] = sector_total.get(sector, 0) + p.get("weight", 0)
    result: list[tuple[dict, float]] = []
    for p in positions:
        sector = p.get("sector") or etf_lookup.get(p.get("isin", ""), {}).get("sector") or "Unknown"
        if sector_total.get(sector, 0) > max_sector:
            result.append((p, sector_total[sector]))
    return result


def _max_turnover_contributor(transactions: list, positions: list) -> dict | None:
    """ISIN mit dem groessten absoluten Umschlag-Beitrag (amount_eur-Summe).

    Nur diskretionaere Trades (keine Sparplaene): Sparplaene sind automatischer
    Bestandsaufbau und kein Kandidat fuer eine Reduzierung (Plan Phase 4).
    """
    by_isin: dict[str, float] = {}
    for t in transactions:
        if not isinstance(t, dict):
            continue
        if not _is_discretionary_trade(t):
            continue
        isin = t.get("isin", "")
        if not isin:
            continue
        by_isin[isin] = by_isin.get(isin, 0.0) + _txn_abs_amount(t)
    if not by_isin:
        return None
    top_isin = max(by_isin, key=lambda i: (by_isin[i], i))
    pos = _find_position(positions, top_isin)
    return {
        "isin": top_isin,
        "name": pos.get("name", top_isin) if pos else top_isin,
        "amount_eur": by_isin[top_isin],
    }


def build_briefing_decisions(
    analysis: dict,
    strategy: dict,
    transactions: list,
    data_quality: dict | None = None,
    position_perf: dict | None = None,
) -> dict:
    """Buenelt Ampel, Gesamt-Empfehlung und Positionsvorschlaege (deterministisch).

    ``position_perf`` (optional): ISIN -> position_perf_6m_pct (aus Kursdaten
    der Briefing-Pipeline). Fehlt eine ISIN -> fail-closed (kein SELL aus
    der Performance-Regel).
    """
    traffic_lights = build_traffic_lights(analysis, strategy, data_quality)
    recommendation = build_recommendation(traffic_lights, strategy)
    positions = analysis.get("checks", {}).get("positions", {}).get("positions", [])
    if not isinstance(positions, list):
        positions = []
    if isinstance(position_perf, dict):
        positions = [
            {**p, "position_perf_6m_pct": position_perf.get(p.get("isin"))}
            if position_perf.get(p.get("isin")) is not None else p
            for p in positions
        ]
    position_actions = build_position_actions(
        positions, traffic_lights, strategy, transactions, data_quality, analysis=analysis
    )
    return {
        "traffic_lights": traffic_lights,
        "recommendation": recommendation,
        "position_actions": position_actions,
    }


def analyze_portfolio(portfolio: dict, transactions: list, strategy: dict | None = None) -> dict:
    if strategy is None:
        strategy = load_strategy()
    positions = calculate_positions(portfolio, strategy)
    pos_list = positions["positions"]
    checks = {
        "positions": positions,
        "core_satellite": calculate_core_satellite(pos_list, strategy),
        "sector_concentration": calculate_sector_concentration(pos_list, strategy),
        "single_position": calculate_single_position_max(pos_list, strategy),
        "drift": calculate_drift(pos_list, strategy),
        "turnover": calculate_turnover(transactions, portfolio, strategy),
        "thesis_deadlines": check_thesis_deadlines(strategy),
        "trades_per_quarter": calculate_trades_in_quarter(transactions, strategy=strategy),
        "weekly_trades": calculate_weekly_trades(transactions),
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
