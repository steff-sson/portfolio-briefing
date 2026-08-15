"""Regression tests: analyze.py uses the real config/strategy.yaml structure.

Der alte Code las falsche Pfade (strategy.strategy.* mit core_ratio_target,
single_position_max, ...). Diese Tests festschreiben die echten Pfade:

- portfolio.core_pct (75.0 = 75%) und portfolio.rebalancing.threshold_pct (5.0 = 5pp)
- satellite_limits.max_position_pct (5.0), max_sector_pct (15.0),
  max_turnover_annual_pct (30.0)
- alerts.on_thesis_expiring_soon_days (30)

Keine duplizierten Strategy-Bloecke: alle Funktionen lesen direkt aus der
vorhandenen Struktur. Fehlende Grenzwerte -> 0.0 -> Check rot (fail-closed).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from scripts import analyze

REAL_STRATEGY = {
    "portfolio": {
        "core_pct": 75.0,
        "satellite_pct": 25.0,
        "rebalancing": {"method": "threshold", "threshold_pct": 5.0},
    },
    "satellite_limits": {
        "max_position_pct": 5.0,
        "max_sector_pct": 15.0,
        "max_positions": 10,
        "max_turnover_annual_pct": 30.0,
    },
    "alerts": {"on_thesis_expiring_soon_days": 30},
}


def _positions(core_value_eur: float = 7500.0, sat_value_eur: float = 2500.0) -> list:
    total = core_value_eur + sat_value_eur
    return [
        {
            "isin": "IE00BK5BQT80",
            "name": "Vanguard FTSE All-World",
            "category": "core",
            "value_eur": core_value_eur,
            "weight": round(core_value_eur / total, 4),
        },
        {
            "isin": "US0378331005",
            "name": "Apple Inc.",
            "category": "satellite",
            "value_eur": sat_value_eur,
            "weight": round(sat_value_eur / total, 4),
        },
    ]


def test_core_satellite_reads_portfolio_core_pct():
    """Ziel-Ratio kommt aus portfolio.core_pct (75.0 -> 0.75)."""
    result = analyze.calculate_core_satellite(_positions(), REAL_STRATEGY)
    assert result["target_ratio"] == 0.75
    assert result["core_ratio"] == 0.75
    assert result["status"] == "green"


def test_core_satellite_band_uses_rebalancing_threshold_pct():
    """Toleranzband = core_pct ± rebalancing.threshold_pct (75 ± 5pp)."""
    # 72% Core liegt im Band -> green
    inside = analyze.calculate_core_satellite(_positions(7200.0, 2800.0), REAL_STRATEGY)
    assert inside["core_ratio"] == 0.72
    assert inside["status"] == "green"
    # 83% Core ausserhalb (75 + 5) -> red
    outside = analyze.calculate_core_satellite(_positions(8300.0, 1700.0), REAL_STRATEGY)
    assert outside["status"] == "red"


def test_drift_reads_core_pct_and_threshold_pct():
    """Drift-Ziel und Schwellwert aus portfolio.core_pct + rebalancing.threshold_pct."""
    result = analyze.calculate_drift(_positions(), REAL_STRATEGY)
    assert result["target"] == 0.75
    assert result["threshold"] == 0.05
    assert result["core_ratio_actual"] == 0.75
    assert result["drift"] == 0.0
    assert result["status"] == "green"
    # Core 90% -> Drift 0.15 > 0.05 -> red
    drifted = analyze.calculate_drift(_positions(9000.0, 1000.0), REAL_STRATEGY)
    assert drifted["status"] == "red"


def test_single_position_reads_satellite_limits_max_position_pct():
    """Einzelposition-Limit aus satellite_limits.max_position_pct (5.0 -> 0.05)."""
    result = analyze.calculate_single_position_max(_positions(), REAL_STRATEGY)
    assert result["threshold"] == 0.05
    assert result["max_position"]["name"] == "Vanguard FTSE All-World"
    assert result["status"] == "red"  # 75% >> 5%

    ok = analyze.calculate_single_position_max(
        [
            {
                "isin": "US0378331005",
                "name": "Apple Inc.",
                "category": "satellite",
                "value_eur": 400.0,
                "weight": 0.04,
            }
        ],
        REAL_STRATEGY,
    )
    assert ok["status"] == "green"


def test_sector_concentration_reads_max_sector_pct_with_etf_lookup():
    """Sektor-Grenzwert aus satellite_limits.max_sector_pct; Klassifikation via etf_lookup."""
    positions = [
        {
            "isin": "IE00BK5BQT80",
            "name": "Vanguard FTSE All-World",
            "category": "core",
            "value_eur": 9000.0,
            "weight": 0.9,
            "sector": None,
        },
        {
            "isin": "US0378331005",
            "name": "Apple Inc.",
            "category": "satellite",
            "value_eur": 1000.0,
            "weight": 0.1,
            "sector": None,
        },
    ]
    result = analyze.calculate_sector_concentration(positions, REAL_STRATEGY)
    assert result["threshold"] == 0.15
    assert result["max_sector"] == "Diversified"  # aus config/etf_lookup.json
    assert result["max_ratio"] == 0.9
    assert result["status"] == "red"


def test_sector_concentration_uses_holding_sector_field():
    """Explizites sector-Feld der Holding hat Vorrang vor dem ETF-Lookup."""
    positions = [
        {"isin": f"XX000000000{i}", "name": f"H{i}", "category": "satellite", "value_eur": 1250.0, "weight": 0.125, "sector": f"s{i}"}
        for i in range(8)
    ]
    result = analyze.calculate_sector_concentration(positions, REAL_STRATEGY)
    assert result["max_ratio"] == 0.125
    assert result["max_sector"] == "s0"
    assert result["status"] == "green"  # 12.5% < 90% von 15%


def test_turnover_reads_max_turnover_annual_pct():
    """Umschlag-Grenzwert aus satellite_limits.max_turnover_annual_pct (30.0 -> 0.30)."""
    portfolio = {"total_value_eur": 10000.0}
    high = [
        {"date": "2026-01-15", "isin": "US0378331005", "type": "buy", "quantity": 10, "price_eur": 400.0}
    ]
    result = analyze.calculate_turnover(high, portfolio, REAL_STRATEGY)
    assert result["turnover_ratio"] == 0.4
    assert result["threshold"] == 0.30
    assert result["status"] == "red"

    low = [
        {"date": "2026-01-15", "isin": "US0378331005", "type": "buy", "quantity": 5, "price_eur": 200.0}
    ]
    ok = analyze.calculate_turnover(low, portfolio, REAL_STRATEGY)
    assert ok["turnover_ratio"] == 0.1
    assert ok["status"] == "green"  # 10% < 60% von 30%


def test_thesis_deadlines_reads_alerts_days(monkeypatch, tmp_path):
    """Thesen-Abgelaufen-Zeitraum aus alerts.on_thesis_expiring_soon_days (Tage)."""
    monkeypatch.setattr(analyze, "THESIS_DIR", tmp_path)
    now = datetime.now(timezone.utc)
    old = tmp_path / "old.md"
    old.write_text(
        f"---\ncreated: {(now - timedelta(days=365)).strftime('%Y-%m-%d')}\n---\n",
        encoding="utf-8",
    )
    fresh = tmp_path / "fresh.md"
    fresh.write_text(
        f"---\ncreated: {(now - timedelta(days=1)).strftime('%Y-%m-%d')}\n---\n",
        encoding="utf-8",
    )

    result = analyze.check_thesis_deadlines(REAL_STRATEGY)
    assert result["total_theses"] == 2
    assert [t["file"] for t in result["outdated"]] == ["old.md"]
    assert result["status"] == "red"


def test_old_strategy_block_is_ignored():
    """Alter falscher Pfad (strategy.strategy.*) darf nicht mehr greifen."""
    old_style = {
        "strategy": {"single_position_max": 0.5, "core_ratio_target": 0.35, "drift_threshold": 0.2}
    }
    result = analyze.calculate_single_position_max(_positions(), old_style)
    assert result["threshold"] == 0.0  # echte Pfade fehlen -> 0.0, alte Keys ignoriert


def test_missing_thresholds_are_fail_closed():
    """Fehlende Grenzwerte -> 0.0 -> Check rot (fail-closed, keine Lockern)."""
    result = analyze.calculate_single_position_max(_positions(), {})
    assert result["threshold"] == 0.0
    assert result["status"] == "red"


# --- Gewichte + Fail-closed-Guard (Holdings vorhanden, Gesamtwert 0/ungueltig) ---


def test_positions_weights_from_holdings_values():
    """Gewichte korrekt aus value_eur der Holdings (ohne Summenfeld)."""
    portfolio = {
        "holdings": [
            {"isin": "A", "name": "A", "value_eur": 7500.0},
            {"isin": "B", "name": "B", "value_eur": 2500.0},
        ]
    }
    result = analyze.calculate_positions(portfolio)
    assert result["total_value_eur"] == 10000.0
    weights = {p["isin"]: p["weight"] for p in result["positions"]}
    assert weights["A"] == 0.75
    assert weights["B"] == 0.25


def test_positions_missing_category_is_unknown_not_satellite():
    """Fehlende category -> "unknown" statt stillschweigend "satellite"."""
    result = analyze.calculate_positions({"holdings": [{"isin": "A", "name": "A", "value_eur": 100.0}]})
    assert result["positions"][0]["category"] == "unknown"


def test_total_zero_with_holdings_raises_fail_closed():
    """Holdings vorhanden + Gesamtwert 0 (Summenfeld 0 und Holdings 0) -> Datenfehler."""
    portfolio = {"total_value_eur": 0.0, "holdings": [{"isin": "A", "name": "A", "value_eur": 0.0}]}
    with pytest.raises(analyze.PortfolioDataError):
        analyze.calculate_positions(portfolio)


def test_total_zero_with_holdings_without_values_raises():
    """Holdings vorhanden, aber ohne Werte -> berechneter Gesamtwert 0 -> Datenfehler."""
    portfolio = {"holdings": [{"isin": "A", "name": "A"}, {"isin": "B", "name": "B"}]}
    with pytest.raises(analyze.PortfolioDataError):
        analyze.calculate_positions(portfolio)


def test_total_negative_with_holdings_raises():
    """Negativer Gesamtwert ist ungueltig -> Datenfehler, keine kuenstlichen Gewichte."""
    portfolio = {"total_value_eur": -1.0, "holdings": [{"isin": "A", "name": "A", "value_eur": 100.0}]}
    with pytest.raises(analyze.PortfolioDataError):
        analyze.calculate_positions(portfolio)


def test_empty_holdings_zero_total_is_valid():
    """Leeres Portfolio (keine Holdings) ist kein Datenfehler."""
    result = analyze.calculate_positions({"holdings": []})
    assert result["total_value_eur"] == 0.0
    assert result["positions"] == []


def test_zero_sum_field_falls_back_to_holdings_sum():
    """Summenfeld 0 ist kein Datenfehler, wenn die Holdings-Werte eine Summe > 0 ergeben."""
    portfolio = {
        "total_value_eur": 0.0,
        "holdings": [
            {"isin": "A", "name": "A", "value_eur": 7500.0},
            {"isin": "B", "name": "B", "value_eur": 2500.0},
        ],
    }
    result = analyze.calculate_positions(portfolio)
    assert result["total_value_eur"] == 10000.0


def test_analyze_portfolio_real_live_shape_does_not_raise():
    """Echte Live-Daten NACH sc_bridge-Normalisierung (value_eur gesetzt,
    ungemappte Werte category unknown) werden korrekt verarbeitet."""
    portfolio = {
        "holdings": [
            {"isin": "IE00BK5BQT80", "name": "Vanguard FTSE All-World", "value_eur": 1850.0, "category": "core"},
            {"isin": "US0378331005", "name": "Apple Inc.", "value_eur": 2400.0, "category": "unknown"},
        ]
    }
    result = analyze.analyze_portfolio(portfolio, [], REAL_STRATEGY)
    positions = result["checks"]["positions"]
    assert positions["total_value_eur"] == 4250.0
    assert positions["positions"][0]["weight"] == 0.4353  # 1850/4250
    assert positions["positions"][1]["category"] == "unknown"


def test_core_satellite_unknown_not_counted_as_satellite():
    """Ungemappte Positionen werden weder Core noch Satellite zugeschlagen."""
    positions = [
        {"isin": "A", "category": "core", "value_eur": 6000.0, "weight": 0.6},
        {"isin": "B", "category": "unknown", "value_eur": 4000.0, "weight": 0.4},
    ]
    result = analyze.calculate_core_satellite(positions, REAL_STRATEGY)
    assert result["core_value_eur"] == 6000.0
    assert result["satellite_value_eur"] == 0.0
    assert result["unknown_value_eur"] == 4000.0
    assert result["core_ratio"] == 0.6
    assert result["status"] == "red"  # 60% < 70% (75 - 5)


# --- Datenqualitaet (assess_data_quality) ------------------------------------


def _dq_portfolio(total_value_eur: float = 10000.0) -> dict:
    return {
        "total_value_eur": total_value_eur,
        "holdings": [
            {"isin": "IE00BK5BQT80", "name": "Vanguard FTSE All-World", "category": "core", "value_eur": 7500.0},
            {"isin": "US0378331005", "name": "Apple Inc.", "category": "satellite", "value_eur": 2500.0},
        ],
    }


def _snapshot(captured_ago_days: int, prev_total: float = 10000.0) -> dict:
    captured_at = (datetime.now(timezone.utc) - timedelta(days=captured_ago_days)).isoformat()
    return {"captured_at": captured_at, "portfolio": {"total_value_eur": prev_total, "holdings": []}}


def test_data_quality_ok():
    """Vollstaendiges Portfolio + frischer Vorgaenger -> ok, keine Issues."""
    result = analyze.assess_data_quality(_dq_portfolio(), _snapshot(1))
    assert result["status"] == "ok"
    assert result["issues"] == []


def test_data_quality_ok_without_previous_snapshot():
    """Erstlauf (kein Vorgaenger) -> ok, solange die Daten vollstaendig sind."""
    result = analyze.assess_data_quality(_dq_portfolio())
    assert result["status"] == "ok"
    assert result["issues"] == []


def test_data_quality_incomplete_no_holdings():
    result = analyze.assess_data_quality({"total_value_eur": 0.0, "holdings": []})
    assert result["status"] == "incomplete"
    assert "keine Holdings vorhanden" in result["issues"]
    assert any("Gesamtwert ungueltig" in i for i in result["issues"])


def test_data_quality_incomplete_portfolio_not_a_dict():
    result = analyze.assess_data_quality(None)
    assert result["status"] == "incomplete"
    assert "portfolio fehlt oder ist kein Dict" in result["issues"]


def test_data_quality_incomplete_missing_required_fields():
    portfolio = {"total_value_eur": 1000.0, "holdings": [{"isin": "X", "value_eur": 1000.0}]}
    result = analyze.assess_data_quality(portfolio)
    assert result["status"] == "incomplete"
    assert "Holding[0].name fehlt" in result["issues"]
    assert "Holding[0].category fehlt" in result["issues"]


def test_data_quality_incomplete_missing_value():
    portfolio = {"total_value_eur": 1000.0, "holdings": [{"isin": "X", "name": "X", "category": "core"}]}
    result = analyze.assess_data_quality(portfolio)
    assert result["status"] == "incomplete"
    assert "Holding[0].value_eur/value fehlt" in result["issues"]


def test_data_quality_implausible_negative_holding_value():
    portfolio = {
        "total_value_eur": 1000.0,
        "holdings": [{"isin": "X", "name": "X", "category": "core", "value_eur": -100.0}],
    }
    result = analyze.assess_data_quality(portfolio)
    assert result["status"] == "implausible"
    assert any("negativer Wert" in i for i in result["issues"])


def test_data_quality_implausible_total_jump():
    """Gesamtwert-Sprung > 10x gegenueber Vorgaenger -> implausible."""
    result = analyze.assess_data_quality(_dq_portfolio(20000.0), _snapshot(1, prev_total=1000.0))
    assert result["status"] == "implausible"
    assert any("Gesamtwert-Aenderung" in i for i in result["issues"])


def test_data_quality_implausible_total_collapse():
    """Gesamtwert-Einbruch < 0.1x gegenueber Vorgaenger -> implausible."""
    result = analyze.assess_data_quality(_dq_portfolio(50.0), _snapshot(1, prev_total=1000.0))
    assert result["status"] == "implausible"


def test_data_quality_stale_previous_snapshot():
    """Vorgaenger aelter als 7 Tage -> stale."""
    result = analyze.assess_data_quality(_dq_portfolio(), _snapshot(10))
    assert result["status"] == "stale"
    assert any("Tage alt" in i for i in result["issues"])


def test_data_quality_priority_incomplete_beats_stale():
    """Prioritaet: incomplete (2) > stale (1)."""
    portfolio = {
        "total_value_eur": 10000.0,
        "holdings": [
            {"isin": "IE00BK5BQT80", "name": "Vanguard FTSE All-World", "category": "core", "value_eur": 7500.0},
            {"isin": None, "name": "Apple Inc.", "category": "satellite", "value_eur": 2500.0},
        ],
    }
    result = analyze.assess_data_quality(portfolio, _snapshot(10))
    assert result["status"] == "incomplete"
    assert "Holding[1].isin fehlt" in result["issues"]


def test_data_quality_priority_implausible_beats_stale():
    """Prioritaet: implausible (3) > stale (1)."""
    result = analyze.assess_data_quality(_dq_portfolio(20000.0), _snapshot(10, prev_total=1000.0))
    assert result["status"] == "implausible"


def test_data_quality_priority_implausible_beats_incomplete():
    """Prioritaet: implausible (3) > incomplete (2) — negativer Wert erzeugt beides."""
    portfolio = {
        "total_value_eur": 1000.0,
        "holdings": [
            {"isin": "X", "name": "X", "category": "core", "value_eur": -100.0},
            {"isin": "Y", "category": "satellite", "value_eur": 100.0},  # name fehlt -> incomplete
        ],
    }
    result = analyze.assess_data_quality(portfolio)
    assert result["status"] == "implausible"


# --- Strategie-Validierung (validate_strategy) -------------------------------


def test_validate_strategy_valid():
    result = analyze.validate_strategy(REAL_STRATEGY)
    assert result["valid"] is True
    assert result["errors"] == []


def test_validate_strategy_minimal_valid():
    """Pflicht-Sektionen vorhanden, Werte optional -> gueltig."""
    result = analyze.validate_strategy(
        {"portfolio": {"core_pct": 75.0, "satellite_pct": 25.0}, "satellite_limits": {}}
    )
    assert result["valid"] is True


def test_validate_strategy_missing_required_sections():
    result = analyze.validate_strategy({})
    assert result["valid"] is False
    assert "Pflicht-Sektion 'portfolio' fehlt" in result["errors"]
    assert "Pflicht-Sektion 'satellite_limits' fehlt" in result["errors"]


def test_validate_strategy_not_a_dict():
    for bad in (None, "strategy", [], 42):
        result = analyze.validate_strategy(bad)
        assert result["valid"] is False
        assert "strategy ist kein Dict" in result["errors"]


def test_validate_strategy_section_not_a_dict():
    result = analyze.validate_strategy({"portfolio": "x", "satellite_limits": []})
    assert "Sektion 'portfolio' ist kein Dict" in result["errors"]
    assert "Sektion 'satellite_limits' ist kein Dict" in result["errors"]


def test_validate_strategy_core_plus_satellite_must_be_100():
    strategy = {
        "portfolio": {"core_pct": 75.0, "satellite_pct": 20.0},
        "satellite_limits": {"max_position_pct": 5.0, "max_sector_pct": 15.0},
    }
    result = analyze.validate_strategy(strategy)
    assert result["valid"] is False
    assert any(e.startswith("portfolio.core_pct + portfolio.satellite_pct") for e in result["errors"])


def test_validate_strategy_percent_fields_must_be_numbers():
    strategy = {
        "portfolio": {"core_pct": True, "satellite_pct": 25.0},
        "satellite_limits": {"max_position_pct": 5.0, "max_sector_pct": 15.0},
    }
    result = analyze.validate_strategy(strategy)
    assert "portfolio.core_pct muss eine Zahl sein (int|float)" in result["errors"]

    nested = {
        "portfolio": {"core_pct": 75.0, "satellite_pct": 25.0, "rebalancing": {"threshold_pct": "5%"}},
        "satellite_limits": {"max_position_pct": 5.0, "max_sector_pct": 15.0},
    }
    result_nested = analyze.validate_strategy(nested)
    assert "rebalancing.threshold_pct muss eine Zahl sein (int|float)" in result_nested["errors"]


def test_validate_strategy_max_position_not_above_max_sector():
    strategy = {
        "portfolio": {"core_pct": 75.0, "satellite_pct": 25.0},
        "satellite_limits": {"max_position_pct": 20.0, "max_sector_pct": 15.0},
    }
    result = analyze.validate_strategy(strategy)
    assert "satellite_limits.max_position_pct darf nicht groesser als max_sector_pct sein" in result["errors"]


def test_validate_strategy_max_positions_ge_one():
    strategy = {
        "portfolio": {"core_pct": 75.0, "satellite_pct": 25.0},
        "satellite_limits": {"max_position_pct": 5.0, "max_sector_pct": 15.0, "max_positions": 0},
    }
    result = analyze.validate_strategy(strategy)
    assert "satellite_limits.max_positions muss >= 1 sein" in result["errors"]


def test_validate_strategy_max_positions_must_be_int():
    strategy = {
        "portfolio": {"core_pct": 75.0, "satellite_pct": 25.0},
        "satellite_limits": {"max_position_pct": 5.0, "max_sector_pct": 15.0, "max_positions": "10"},
    }
    result = analyze.validate_strategy(strategy)
    assert "satellite_limits.max_positions muss eine Ganzzahl sein (int)" in result["errors"]


def test_validate_strategy_list_fields_must_be_lists():
    strategy = {
        "portfolio": {"core_pct": 75.0, "satellite_pct": 25.0},
        "satellite_limits": {"max_position_pct": 5.0, "max_sector_pct": 15.0},
        "sectors": {"preferred": "technology"},
    }
    result = analyze.validate_strategy(strategy)
    assert "sectors.preferred muss eine Liste sein" in result["errors"]


def test_validate_strategy_unknown_fields_permissive():
    """Unbekannte Sektionen/Felder werden ignoriert (permissive, Strategie-Evolution)."""
    strategy = {
        **REAL_STRATEGY,
        "future_section": {"x": 1},
        "portfolio": {**REAL_STRATEGY["portfolio"], "neues_feld": 123},
    }
    result = analyze.validate_strategy(strategy)
    assert result["valid"] is True
    assert result["errors"] == []
