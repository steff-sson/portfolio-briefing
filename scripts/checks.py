"""Grenzwert-Prüfungen des Portfolios gegen strategy.yaml.

Quellenneutral: bekommt das normalisierte Snapshot-Modell (data.Snapshot) und
die Strategie-Datei und liefert eine Liste von Handlungs-Signalen. Jede Prüfung
ist pur/testbar ohne Netz oder LLM.

Thresholds (aus strategy.yaml):
- Einzelposition > ``satellite_limits.max_position_pct`` %
- Sektor-Konzentration > ``satellite_limits.max_sector_pct`` %
- Anzahl Satellite-Positionen > ``satellite_limits.max_positions``
- Core/Satellite-Drift über ``portfolio.rebalancing.threshold_pct`` pp
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import yaml


@dataclass
class Signal:
    """Ein Handlungs-Signal aus der deterministischen Prüfung."""

    kind: str  # position|sector|count|drift
    severity: str  # red|yellow|green
    subject: str  # ISIN / Sektor-Name / "positions" / "drift"
    message: str
    # Aktuelle Position von core_ratio/satellite_ratio für Render/Brief.
    core_ratio: float | None = None
    satellite_ratio: float | None = None


def load_strategy(path: str | None = None) -> dict[str, Any]:
    """Lädt strategy.yaml (Default config/strategy.yaml) aus dem Repo-Root."""
    if path is None:
        path = "config/strategy.yaml"
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def _position_value(holding: dict[str, Any]) -> float:
    return float(holding.get("value_eur") or 0.0)


def _isin_category(holding: dict[str, Any]) -> str:
    return str(holding.get("category") or "unknown").lower()


def calculate_ratios(holdings: list[dict[str, Any]]) -> tuple[float, float, float]:
    """Berechnet (total, core_ratio, satellite_ratio) aus normalisierten Daten.

    Ratio = Wert der jeweiligen Kategorie / Gesamtwert. Core- und
    Satellite-Anteile sind relativ zum Portfolio; "unknown" zählt zu keinem
    (Datenlücke), legacy wird dem Satellite zugerechnet (Bestandsinstrument).
    """
    total = sum(_position_value(h) for h in holdings)
    core = sum(_position_value(h) for h in holdings if _isin_category(h) == "core")
    sat = sum(
        _position_value(h)
        for h in holdings
        if _isin_category(h) in ("satellite", "legacy")
    )
    if total <= 0:
        return 0.0, 0.0, 0.0
    return total, core / total * 100.0, sat / total * 100.0


def check_position_limits(holdings: list[dict[str, Any]], max_position_pct: float) -> list[Signal]:
    """>max_position_pct% pro Position (Satellite-Regel), relativ zum GESAMTPORTFOLIO.

    User-Entscheidung 2026-09-11: Einzelpositions-Limit ist eine Satellite-Regel.
    Core-Positionen lösen KEINE Einzelpositions-Verletzung aus (Core-Konzentration
    wird separat per Core-Schwelle abgedeckt, siehe check_core_concentration).
    Basis bleibt das Gesamtportfolio; die Sektor-/Anzahl-Regeln bleiben auf dem
    Satellite-Sleeve.
    """
    signals: list[Signal] = []
    total = sum(_position_value(h) for h in holdings)
    if total <= 0:
        return signals
    for h in holdings:
        if _isin_category(h) not in ("satellite", "legacy"):
            continue  # Core: keine Einzelpositions-Verletzung
        pct = _position_value(h) / total * 100.0
        if pct > max_position_pct:
            signals.append(
                Signal(
                    kind="position",
                    severity="red",
                    subject=str(h.get("isin") or h.get("name")),
                    message=(
                        f"{h.get('name')} ({h.get('isin')}) übersteigt "
                        f"{max_position_pct:.0f}% Einzelposition: {pct:.1f}% (des Gesamtportfolios)"
                    ),
                )
            )
    return signals


def _sector_of(holding: dict[str, Any]) -> str:
    # Sektor ist in der Holding optional; wir nutzen ihn, wenn present.
    return str(holding.get("sector") or "").strip().lower()


def check_sector_concentration(holdings: list[dict[str, Any]], max_sector_pct: float) -> list[Signal]:
    """Sektor-Anteile relativ zum Satellite-Umfang (Plan: Sektoren = Satellite)."""
    satellites = [h for h in holdings if _isin_category(h) in ("satellite", "legacy")]
    if not satellites:
        return []
    satellite_total = sum(_position_value(h) for h in satellites)
    if satellite_total <= 0:
        return []
    by_sector: dict[str, float] = {}
    for h in satellites:
        sector = _sector_of(h) or "unknown"
        by_sector[sector] = by_sector.get(sector, 0.0) + _position_value(h)
    signals: list[Signal] = []
    for sector, value in sorted(by_sector.items(), key=lambda kv: -kv[1]):
        pct = value / satellite_total * 100.0
        # "unknown" ist eine Datenlücke, kein Konzentrations-Alarm.
        if sector == "unknown":
            continue
        if pct > max_sector_pct:
            signals.append(
                Signal(
                    kind="sector",
                    severity="red",
                    subject=sector,
                    message=(
                        f"Sektor '{sector}' übersteigt {max_sector_pct:.0f}% "
                        f"im Satellite-Umfang: {pct:.1f}%"
                    ),
                )
            )
    return signals


def check_position_count(holdings: list[dict[str, Any]], max_positions: int) -> list[Signal]:
    satellites = [h for h in holdings if _isin_category(h) in ("satellite", "legacy")]
    n = len(satellites)
    if n > max_positions:
        return [
            Signal(
                kind="count",
                severity="red",
                subject="positions",
                message=(
                    f"{n} Satellite-Positionen überschreiten das Limit von {max_positions}"
                ),
            )
        ]
    return []


def check_core_satellite_drift(holdings: list[dict[str, Any]], strategy: dict[str, Any]) -> list[Signal]:
    _, core_ratio, sat_ratio = calculate_ratios(holdings)
    portfolio = strategy.get("portfolio", {})
    core_target = float(portfolio.get("core_pct", 70.0))
    sat_target = float(portfolio.get("satellite_pct", 30.0))
    threshold = float((portfolio.get("rebalancing") or {}).get("threshold_pct", 5.0))
    signals: list[Signal] = []
    if abs(core_ratio - core_target) > threshold:
        signals.append(
            Signal(
                kind="drift",
                severity="red",
                subject="drift",
                message=(
                    f"Core-Anteil {core_ratio:.1f}% weicht mehr als {threshold:.0f}pp "
                    f"vom Ziel {core_target:.0f}% ab"
                ),
                core_ratio=core_ratio,
                satellite_ratio=sat_ratio,
            )
        )
    if abs(sat_ratio - sat_target) > threshold:
        signals.append(
            Signal(
                kind="drift",
                severity="red",
                subject="drift",
                message=(
                    f"Satellite-Anteil {sat_ratio:.1f}% weicht mehr als {threshold:.0f}pp "
                    f"vom Ziel {sat_target:.0f}% ab"
                ),
                core_ratio=core_ratio,
                satellite_ratio=sat_ratio,
            )
        )
    return signals


def check_core_concentration(holdings: list[dict[str, Any]], max_position_pct: float) -> list[Signal]:
    """Core-Konzentration: einzelne CORE-Position > Schwelle → warn (🟡).

    User-Entscheidung 2026-09-11: eine einzelne Core-Position darf nicht zu stark
    konzentrieren; Schwelle aus ``core_limits.max_position_pct`` (Default 25%).
    Messung position/total relativ zum GESAMTPORTFOLIO. Warn (kein Blocker, kein
    fail-closed auf dieser Stufe). Core-Positionen sind hier NICHT von der
    Einzelpositions-Regel (Satellite) betroffen — diese Prüfung deckt Core ab.
    """
    signals: list[Signal] = []
    total = sum(_position_value(h) for h in holdings)
    if total <= 0:
        return signals
    for h in holdings:
        if _isin_category(h) != "core":
            continue
        pct = _position_value(h) / total * 100.0
        if pct > max_position_pct:
            signals.append(
                Signal(
                    kind="core_concentration",
                    severity="warn",
                    subject=str(h.get("isin") or h.get("name")),
                    message=(
                        f"Core-Konzentration: {h.get('name')} ({h.get('isin')}) bei "
                        f"{pct:.1f}% des Gesamtportfolios (Schwelle {max_position_pct:.0f}%)"
                    ),
                )
            )
    return signals


def run_checks(
    holdings: list[dict[str, Any]],
    strategy: dict[str, Any] | None = None,
) -> list[Signal]:
    """Führt alle Grenzwert-Prüfungen aus und liefert die kombinierten Signale."""
    if strategy is None:
        strategy = load_strategy()
    limits = strategy.get("satellite_limits", {})
    max_pos = float(limits.get("max_position_pct", 5.0))
    max_sector = float(limits.get("max_sector_pct", 15.0))
    max_count = int(limits.get("max_positions", 10))
    core_max = float((strategy.get("core_limits") or {}).get("max_position_pct", 25.0))

    signals: list[Signal] = []
    signals.extend(check_position_limits(holdings, max_pos))
    signals.extend(check_core_concentration(holdings, core_max))
    signals.extend(check_sector_concentration(holdings, max_sector))
    signals.extend(check_position_count(holdings, max_count))
    signals.extend(check_core_satellite_drift(holdings, strategy))
    return signals
