"""Tests für checks.py — Grenzwert-Mathematik (pur, ohne Netz)."""
from __future__ import annotations

import pytest

from scripts import checks


def test_calculate_ratios():
    holdings = checks_sample_holdings()
    total, core, sat = checks.calculate_ratios(holdings)
    # total = 5000+2000+600+700+800 = 9100
    assert total == 9100.0
    # core = 7000, sat = 2100
    assert core == pytest.approx(7000 / 9100 * 100)
    assert sat == pytest.approx(2100 / 9100 * 100)


def checks_sample_holdings():
    return [
        {"isin": "IE00BK5BQT80", "category": "core", "value_eur": 5000.0},
        {"isin": "IE00B4L5Y983", "category": "core", "value_eur": 2000.0},
        {"isin": "US1", "category": "satellite", "value_eur": 600.0, "sector": "technology"},
        {"isin": "US2", "category": "satellite", "value_eur": 700.0, "sector": "technology"},
        {"isin": "US3", "category": "satellite", "value_eur": 800.0, "sector": "healthcare"},
    ]


def test_no_position_limit_alarm_within_limit():
    # 3 Satellites mit 40/30/30 → keiner > 50% (Limit) → kein Signal.
    holdings = [
        {"isin": "US1", "category": "satellite", "value_eur": 40.0},
        {"isin": "US2", "category": "satellite", "value_eur": 30.0},
        {"isin": "US3", "category": "satellite", "value_eur": 30.0},
    ]
    assert checks.check_position_limits(holdings, 50.0) == []


def test_position_limit_alarm_over_limit():
    holdings = [
        {"isin": "US1", "category": "satellite", "value_eur": 1500.0},
        {"isin": "US2", "category": "satellite", "value_eur": 500.0},
    ]
    sigs = checks.check_position_limits(holdings, 50.0)
    assert len(sigs) == 1
    assert sigs[0].kind == "position"
    assert sigs[0].severity == "red"
    assert sigs[0].subject == "US1"


def test_position_limit_ignores_core():
    # User 2026-09-11: Einzelpositions-Limit ist Satellite-Regel. Eine große
    # Core-Position löst KEINE Einzelpositions-Verletzung aus (Core wird separat
    # über die Core-Konzentrations-Schwelle abgedeckt).
    holdings = [
        {"isin": "IE00BK5BQT80", "category": "core", "value_eur": 5000.0},
        {"isin": "US1", "category": "satellite", "value_eur": 100.0},
    ]
    sigs = checks.check_position_limits(holdings, 5.0)
    assert sigs == []  # Core ignoriert; Satellite US1 ~2% < 5%


def test_position_limit_basis_is_total_not_satellite():
    # 60% einer großen Satellite-Position in einem kleinen Gesamtportfolio.
    holdings = [
        {"isin": "US1", "category": "satellite", "value_eur": 600.0},
        {"isin": "US2", "category": "satellite", "value_eur": 400.0},
    ]
    sigs = checks.check_position_limits(holdings, 50.0)
    assert len(sigs) == 1
    assert sigs[0].subject == "US1"


def test_sector_concentration_alarm():
    holdings = [
        {"isin": "US1", "category": "satellite", "value_eur": 900.0, "sector": "technology"},
        {"isin": "US2", "category": "satellite", "value_eur": 100.0, "sector": "healthcare"},
    ]
    sigs = checks.check_sector_concentration(holdings, 15.0)
    assert len(sigs) == 1
    assert sigs[0].subject == "technology"
    assert sigs[0].severity == "red"


def test_sector_unknown_is_data_gap_not_alarm():
    holdings = [
        {"isin": "US1", "category": "satellite", "value_eur": 900.0, "sector": "unknown"},
        {"isin": "US2", "category": "satellite", "value_eur": 100.0, "sector": "unknown"},
    ]
    assert checks.check_sector_concentration(holdings, 15.0) == []


def test_position_count_alarm():
    holdings = [
        {"isin": f"U{i}", "category": "satellite", "value_eur": 100.0} for i in range(12)
    ]
    sigs = checks.check_position_count(holdings, 10)
    assert len(sigs) == 1
    assert "12" in sigs[0].message


def test_core_satellite_drift_alarm():
    holdings = [
        {"isin": "IE00BK5BQT80", "category": "core", "value_eur": 9000.0},
        {"isin": "US1", "category": "satellite", "value_eur": 1000.0},
    ]
    strategy = {
        "portfolio": {"core_pct": 70.0, "satellite_pct": 30.0,
                      "rebalancing": {"threshold_pct": 5.0}}
    }
    sigs = checks.check_core_satellite_drift(holdings, strategy)
    assert sigs, "Core 90% weicht 20pp ab → muss Alarm auslösen"
    assert all(s.kind == "drift" for s in sigs)


def test_run_checks_composes_all_thresholds():
    holdings = [
        {"isin": "IE00BK5BQT80", "category": "core", "value_eur": 5000.0},
        {"isin": "US1", "category": "satellite", "value_eur": 3000.0, "sector": "tech"},
    ]
    strategy = {
        "portfolio": {"core_pct": 70.0, "satellite_pct": 30.0,
                      "rebalancing": {"threshold_pct": 5.0}},
        "satellite_limits": {"max_position_pct": 5.0, "max_sector_pct": 15.0,
                             "max_positions": 10},
    }
    sigs = checks.run_checks(holdings, strategy)
    assert sigs
    kinds = {s.kind for s in sigs}
    assert "position" in kinds
    assert "sector" in kinds


def test_core_concentration_warn_above_threshold():
    # User 2026-09-11: einzelne Core-Position > Schwelle (25%) → warn (🟡),
    # gemessen relativ zum GESAMTPORTFOLIO. Kein Blocker.
    holdings = [
        {"isin": "IE00BK5BQT80", "category": "core", "value_eur": 9000.0},
        {"isin": "US1", "category": "satellite", "value_eur": 1000.0},
    ]
    sigs = checks.check_core_concentration(holdings, 25.0)
    assert len(sigs) == 1
    assert sigs[0].kind == "core_concentration"
    assert sigs[0].severity == "warn"
    assert sigs[0].subject == "IE00BK5BQT80"
    assert "Core-Konzentration" in sigs[0].message


def test_core_concentration_no_warn_below_threshold():
    holdings = [
        {"isin": "IE00BK5BQT80", "category": "core", "value_eur": 2000.0},
        {"isin": "US1", "category": "satellite", "value_eur": 8000.0},
    ]
    sigs = checks.check_core_concentration(holdings, 25.0)
    assert sigs == []  # Core 20% < 25% → kein Signal
