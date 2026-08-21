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
        "target_position_pct": 5.0,
        "warn_position_pct": 7.5,
        "max_position_pct": 5.0,
        "max_sector_pct": 15.0,
        "max_positions": 10,
        "max_turnover_annual_pct": 30.0,
        "max_trades_per_quarter": 5,
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
    assert "kein Bewertungswert (valuation null)" in result["issues"][0]
    assert "X" in result["issues"][0]


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
        {
            "portfolio": {"core_pct": 75.0, "satellite_pct": 25.0},
            "satellite_limits": {
                "target_position_pct": 5.0,
                "warn_position_pct": 7.5,
                "max_position_pct": 5.0,
                "max_sector_pct": 15.0,
                "max_positions": 10,
                "max_turnover_annual_pct": 30.0,
                "max_trades_per_quarter": 5,
            },
        }
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


def test_validate_strategy_unknown_fields_fail_closed():
    """Unbekannte Sektionen/Felder schlagen fehl (fail-closed statt permissive)."""
    strategy = {
        **REAL_STRATEGY,
        "future_section": {"x": 1},
        "portfolio": {**REAL_STRATEGY["portfolio"], "neues_feld": 123},
    }
    result = analyze.validate_strategy(strategy)
    assert result["valid"] is False
    assert "Unbekannte Sektion 'future_section'" in " ".join(result["errors"])
    assert "Unbekanntes Feld 'portfolio.neues_feld'" in " ".join(result["errors"])


# --- Sparplan-Trade-Limit (Plan Phase 4) --------------------------------------


def _txn(isin: str, txn_type: str, executed_at: str) -> dict:
    return {
        "isin": isin,
        "transaction_type": txn_type,
        "executed_at": executed_at,
        "quantity": 1.0,
        "amount_eur": 100.0,
        "side": "BUY",
    }


def test_trades_in_quarter_excludes_sparplan():
    """Nur manuelle Kaufe/Verkaufe zaehlen zum Quartals-Trade-Limit (Plan Phase 4)."""
    quarter = "2026-Q3"
    txns = [
        _txn("IE00BK5BQT80", "sparplan", "2026-08-01T08:00:00+00:00"),
        _txn("IE00BK5BQT80", "sparplan", "2026-08-15T08:00:00+00:00"),
        _txn("IE00BK5BQT80", "sparplan", "2026-09-01T08:00:00+00:00"),
        _txn("IE00BK5BQT80", "sparplan", "2026-09-15T08:00:00+00:00"),
        _txn("IE00BK5BQT80", "sparplan", "2026-09-30T08:00:00+00:00"),
    ]
    result = analyze.calculate_trades_in_quarter(txns, quarter=quarter, strategy=REAL_STRATEGY)
    assert result["trade_count"] == 0  # Sparplaene zaehlen NICHT
    assert result["status"] == "green"

    # 5 Sparplaene + 1 manueller Kauf -> genau 1 diskretionaerer Trade
    result_mixed = analyze.calculate_trades_in_quarter(
        txns + [_txn("US0378331005", "kauf", "2026-08-10T08:00:00+00:00")],
        quarter=quarter,
        strategy=REAL_STRATEGY,
    )
    assert result_mixed["trade_count"] == 1
    assert result_mixed["status"] == "green"


def test_is_trade_still_counts_sparplan_for_turnover():
    """Sparplaene bleiben Trades im Umschlag-Sinn (nur das Trade-Limit ist entlastet)."""
    sparplan = _txn("IE00BK5BQT80", "sparplan", "2026-08-01T08:00:00+00:00")
    assert analyze._is_trade(sparplan) is True
    assert analyze._is_discretionary_trade(sparplan) is False
    assert analyze._is_discretionary_trade(_txn("US0378331005", "kauf", "2026-08-01T08:00:00+00:00")) is True
    assert analyze._is_discretionary_trade(_txn("US0378331005", "verkauf", "2026-08-01T08:00:00+00:00")) is True


# --- Trade-Klassifikation (Plan Phase 3): Instrument-bewusst + transparent -----


def test_classify_trade_distinguishes_classes():
    """Klare Trennung: manuelle Trades vs. Sparplan vs. Cash vs. unklar."""
    assert analyze.classify_trade(_txn("US0378331005", "kauf", "2026-08-01T08:00:00+00:00")) == analyze.TRADE_CLASS_SATELLITE
    assert analyze.classify_trade(_txn("US0378331005", "verkauf", "2026-08-01T08:00:00+00:00")) == analyze.TRADE_CLASS_SATELLITE
    assert analyze.classify_trade(_txn("IE00BK5BQT80", "sparplan", "2026-08-01T08:00:00+00:00")) == analyze.TRADE_CLASS_SPARPLAN
    # Legacy-Formate bleiben manuelle Trades
    legacy_buy = {"date": "2026-08-01", "isin": "US0378331005", "type": "buy"}
    assert analyze.classify_trade(legacy_buy) == analyze.TRADE_CLASS_SATELLITE
    side_buy = {"isin": "US0378331005", "side": "BUY"}
    assert analyze.classify_trade(side_buy) == analyze.TRADE_CLASS_SATELLITE
    # Cash-Bewegung ist kein Trade
    assert analyze.classify_trade(_txn("US0378331005", "einzahlung", "2026-08-01T08:00:00+00:00")) == analyze.TRADE_CLASS_NON_TRADE
    # Unklar -> eigene Klasse (fail-closed)
    assert analyze.classify_trade({"isin": "US0378331005"}) == analyze.TRADE_CLASS_UNCLEAR


def test_sparplan_detection_from_sc_raw_savings_plan():
    """sc-Rohformat security_transaction_type=SAVINGS_PLAN -> Sparplan (nicht Trade)."""
    raw = {"isin": "IE00BK5BQT80", "security_transaction_type": "SAVINGS_PLAN", "side": "BUY"}
    assert analyze.classify_trade(raw) == analyze.TRADE_CLASS_SPARPLAN
    assert analyze._is_discretionary_trade(raw) is False


def test_etf_instrument_kind_and_category():
    """Instrument-Klasse: ETF vs. Aktie; ETF-Kategorie core/satellite aus etf_lookup."""
    core_etf = _txn("IE00BK5BQT80", "sparplan", "2026-08-01T08:00:00+00:00")
    assert analyze._txn_instrument_kind(core_etf) == "etf"
    assert analyze._txn_etf_category(core_etf) == "core"
    sat_etf = _txn("IE00B8GKDB10", "kauf", "2026-08-01T08:00:00+00:00")
    assert analyze._txn_instrument_kind(sat_etf) == "etf"
    assert analyze._txn_etf_category(sat_etf) == "satellite"
    stock = _txn("US0378331005", "kauf", "2026-08-01T08:00:00+00:00")
    assert analyze._txn_instrument_kind(stock) == "aktie"
    assert analyze._txn_etf_category(stock) == "unknown"


def test_trades_in_quarter_breaks_down_sparplan_and_unclear():
    """Transparente Klassifikation: Sparplan separat, unklar fail-closed + ausgewiesen."""
    quarter = "2026-Q3"
    txns = [
        _txn("IE00BK5BQT80", "sparplan", "2026-08-01T08:00:00+00:00"),
        _txn("IE00BK5BQT80", "sparplan", "2026-08-15T08:00:00+00:00"),
        _txn("US0378331005", "kauf", "2026-08-10T08:00:00+00:00"),
        _txn("US0378331005", "einzahlung", "2026-08-11T08:00:00+00:00"),
        {"isin": "DK0062498333", "executed_at": "2026-08-12T08:00:00+00:00"},  # unklar
    ]
    result = analyze.calculate_trades_in_quarter(txns, quarter=quarter, strategy=REAL_STRATEGY)
    assert result["trade_count"] == 2  # 1 manueller Kauf + 1 unklarer (fail-closed)
    assert result["sparplan_count"] == 2  # separat klassifiziert
    assert result["unclear_count"] == 1
    assert result["unclear"][0]["isin"] == "DK0062498333"
    assert result["status"] == "green"  # 2 < 5


def test_trades_in_quarter_all_sparplan_etf_renten_green():
    """Core-ETF-Sparplaene/Rentenanlage: kein Satellite-Trade-Limit-Verbrauch."""
    quarter = "2026-Q3"
    txns = [
        _txn("IE00BK5BQT80", "sparplan", "2026-08-01T08:00:00+00:00"),  # Core-ETF
        _txn("IE00B4L5Y983", "sparplan", "2026-08-15T08:00:00+00:00"),  # Core-ETF
        _txn("IE00B8GKDB10", "sparplan", "2026-09-01T08:00:00+00:00"),  # Satellite-ETF-Sparplan
        _txn("DE000A1EWWW0", "sparplan", "2026-09-15T08:00:00+00:00"),  # Rentenanlage (unbekannt im Lookup, trotzdem Sparplan)
        _txn("IE00BK5BQT80", "sparplan", "2026-09-30T08:00:00+00:00"),
    ]
    result = analyze.calculate_trades_in_quarter(txns, quarter=quarter, strategy=REAL_STRATEGY)
    assert result["trade_count"] == 0
    assert result["sparplan_count"] == 5
    assert result["status"] == "green"


def test_trades_in_quarter_boundary_yellow_at_limit_red_over():
    """Trade-Limit-Ampel: genau am Limit gelb, darueber rot (nur diskretionaere)."""
    quarter = "2026-Q3"
    txns = [_txn(f"US{i:09d}", "kauf", f"2026-08-0{i+1}T08:00:00+00:00") for i in range(5)]
    result = analyze.calculate_trades_in_quarter(txns, quarter=quarter, strategy=REAL_STRATEGY)
    assert result["trade_count"] == 5
    assert result["status"] == "yellow"  # Limit erreicht

    result_over = analyze.calculate_trades_in_quarter(
        txns + [_txn("US0378331005", "kauf", "2026-08-20T08:00:00+00:00")],
        quarter=quarter,
        strategy=REAL_STRATEGY,
    )
    assert result_over["trade_count"] == 6
    assert result_over["status"] == "red"  # Limit ueberschritten


def test_trades_in_quarter_manual_core_etf_kauf_still_counts():
    """Manueller Kauf eines Core-ETF bleibt ein Satellite-Trade (nur Sparplan ist entlastet)."""
    quarter = "2026-Q3"
    result = analyze.calculate_trades_in_quarter(
        [_txn("IE00BK5BQT80", "kauf", "2026-08-01T08:00:00+00:00")],  # Core-ETF, aber manuell
        quarter=quarter,
        strategy=REAL_STRATEGY,
    )
    assert result["trade_count"] == 1  # Entlastung gilt dem Sparplan-Mechanismus, nicht der Core-Kategorie
    assert result["status"] == "green"


def test_trades_in_quarter_only_counts_current_quarter():
    """Nur Transaktionen im Ziel-Quartal zaehlen (Sparplan-Zaehler ebenso)."""
    quarter = "2026-Q3"
    result = analyze.calculate_trades_in_quarter(
        [
            _txn("US0378331005", "kauf", "2026-06-15T08:00:00+00:00"),  # Q2 -> ignoriert
            _txn("IE00BK5BQT80", "sparplan", "2026-06-01T08:00:00+00:00"),  # Q2 -> ignoriert
            _txn("IE00BK5BQT80", "sparplan", "2026-08-01T08:00:00+00:00"),  # Q3 -> Sparplan
        ],
        quarter=quarter,
        strategy=REAL_STRATEGY,
    )
    assert result["trade_count"] == 0
    assert result["sparplan_count"] == 1


def test_trades_in_quarter_unclear_fail_closed_visible_at_boundary():
    """Unklare Trades zaehlen fail-closed zum Limit und sind transparent ausgewiesen."""
    quarter = "2026-Q3"
    txns = [_txn(f"US{i:09d}", "kauf", f"2026-08-0{i+1}T08:00:00+00:00") for i in range(5)]
    txns.append({"isin": "DK0062498333", "executed_at": "2026-08-20T08:00:00+00:00"})  # unklar
    result = analyze.calculate_trades_in_quarter(txns, quarter=quarter, strategy=REAL_STRATEGY)
    assert result["trade_count"] == 6  # 5 manuell + 1 unklar (fail-closed)
    assert result["unclear_count"] == 1
    assert result["unclear"][0]["isin"] == "DK0062498333"
    assert result["status"] == "red"  # unklar schiebt ueber das Limit


def test_trades_in_quarter_traffic_light_surfaces_classification():
    """Ampel 'Trades/Quartal' spiegelt trade_count + transparente Klassifikation."""
    quarter = "2026-Q3"
    txns = [
        _txn("IE00BK5BQT80", "sparplan", "2026-08-01T08:00:00+00:00"),
        _txn("US0378331005", "kauf", "2026-08-10T08:00:00+00:00"),
    ]
    checks = {"trades_per_quarter": analyze.calculate_trades_in_quarter(txns, quarter=quarter, strategy=REAL_STRATEGY)}
    lights = analyze.build_traffic_lights({"checks": checks}, REAL_STRATEGY)
    assert lights["trades_per_quarter"]["status"] == "green"
    assert "1 Trades im Quartal" in lights["trades_per_quarter"]["reason"]


# --- Watchlist-/Satellite-Signale (Plan Phase 3: 4 Dimensionen, deterministisch) ---
#
# Signal-Labels: BUY / SELL / AVOID / WATCH / NO SIGNAL. Fundamentaldaten
# werden nie verwendet (fundamentals_used: false). Harte Ausschlussregeln:
# Core-ETFs und SUSE/LU2722255754 (illiquide Legacy) -> NO SIGNAL.
# SELL nur fuer bestehende Satellite-Positionen.

_SIGNAL_STRATEGY = {
    **REAL_STRATEGY,
    "sectors": {"preferred": ["technology", "ai", "energy"], "excluded": ["fossil_fuels", "defense"]},
}

_SIGNAL_PORTFOLIO = {
    "total_value_eur": 10000.0,
    "holdings": [
        {"isin": "IE00BK5BQT80", "name": "Vanguard FTSE All-World", "category": "core", "value_eur": 7000.0},
        {"isin": "US0378331005", "name": "Apple Inc.", "category": "satellite", "value_eur": 1000.0, "sector": "technology"},
        {"isin": "US5949724083", "name": "NVIDIA Corp.", "category": "satellite", "value_eur": 2000.0, "sector": "technology"},
    ],
}


def _signal_analysis(positions: list | None = None) -> dict:
    if positions is None:
        positions = [
            {"isin": "US5949724083", "name": "NVIDIA Corp.", "category": "satellite", "value_eur": 2000.0, "weight": 0.2},
            {"isin": "US0378331005", "name": "Apple Inc.", "category": "satellite", "value_eur": 1000.0, "weight": 0.1},
        ]
    return {"checks": {"positions": {"positions": positions}, "sector_concentration": {}, "single_position": {}}}


def _nvidia_item(category: str = "satellite") -> dict:
    """Watchlist-Item NVIDIA (technologie-praeferiert, aus tests/mock_data/watchlist.json)."""
    return {
        "isin": "US5949724083",
        "name": "NVIDIA Corp.",
        "ticker": "NVDA",
        "category": category,
        "sector": "technology",
        "valuation": 1250.0,
        "valuation_currency": "EUR",
        "value_eur": 1250.0,
    }


def test_signal_buy_preferred_sector_positive_news():
    """BUY: Score >= +3, D1 >= 0 — praeferierter Sektor + positive News.

    Kandidat ist NICHT im Portfolio (kein Holding -> D2 +1), kleiner
    Positionswert (unter max_position_pct), Sektor unter max_sector_pct.
    """
    buy_portfolio = {
        "total_value_eur": 20000.0,
        "holdings": [
            {"isin": "IE00BK5BQT80", "name": "Vanguard FTSE All-World", "category": "core", "value_eur": 20000.0},
        ],
    }
    item = _nvidia_item()
    item["value_eur"] = 500.0  # 2.5% — unter max_position_pct 5%
    signals = analyze.compute_watchlist_signals(
        [item],
        buy_portfolio,
        _signal_analysis([]),
        news=[{"title": "NVIDIA meldet Rekord-Gewinn und starkes Wachstum", "summary": "", "source": "test"}],
        strategy=_SIGNAL_STRATEGY,
    )
    assert len(signals) == 1
    sig = signals[0]
    assert sig["signal"] == analyze.SIGNAL_BUY
    assert sig["score"] >= 3
    assert sig["fundamentals_used"] is False
    assert sig["dimensions"]["strategy_fit"] >= 0
    assert sig["dimensions"]["news_sentiment"] == 1


def test_signal_excluded_sector_is_avoid():
    """AVOID: Score <= -2 und D1 == -1 (Sektor ausgeschlossen)."""
    item = {
        "isin": "US1234567890",
        "name": "Fossil Co.",
        "category": "satellite",
        "sector": "fossil_fuels",
        "value_eur": 1000.0,
    }
    signals = analyze.compute_watchlist_signals(
        [item],
        {"total_value_eur": 10000.0, "holdings": []},
        _signal_analysis([]),
        news=[{"title": "Verlust und Absturz bei Fossil Co.", "summary": "", "source": "test"}],
        strategy=_SIGNAL_STRATEGY,
    )
    assert len(signals) == 1
    sig = signals[0]
    assert sig["signal"] == analyze.SIGNAL_AVOID
    assert sig["score"] <= -2
    assert sig["dimensions"]["strategy_fit"] == -1


def test_signal_separates_sell_from_watchlist():
    """SELL nur fuer bestehende Satellite-Holdings, nie fuer Watchlist-Neu.

    3M (Sektor industrials, neutral) als bestehende Satellite-Holding mit
    negativer News -> D1 0, D2 -1, D3 -1 -> Score -2 -> SELL. Als
    Watchlist-Neu (nicht gehalten) -> D2 +1 -> Score 0 -> WATCH, nie SELL.
    """
    item = {
        "isin": "US88579Y1010",
        "name": "3M Co.",
        "ticker": "MMM",
        "category": "satellite",
        "sector": "industrials",
        "value_eur": 1000.0,
    }
    news = [{"title": "MMM (3M Co.) verliert weiter — Absturz und Verlustwarnung", "summary": "", "source": "test"}]
    # Bestehende Satellite-Holding (3M im Portfolio) -> SELL moeglich.
    sell_portfolio = {
        "total_value_eur": 10000.0,
        "holdings": [
            {"isin": "IE00BK5BQT80", "name": "Vanguard FTSE All-World", "category": "core", "value_eur": 7000.0},
            {"isin": "US88579Y1010", "name": "3M Co.", "category": "satellite", "value_eur": 1000.0, "sector": "industrials"},
        ],
    }
    sell_signals = analyze.compute_watchlist_signals(
        [dict(item)],
        sell_portfolio,
        _signal_analysis(),
        news=news,
        strategy=_SIGNAL_STRATEGY,
    )
    assert any(s["signal"] == analyze.SIGNAL_SELL for s in sell_signals)

    # Watchlist-Kandidat (nicht im Portfolio) -> WATCH, nie SELL.
    watch_portfolio = {"total_value_eur": 10000.0, "holdings": []}
    watch_signals = analyze.compute_watchlist_signals(
        [dict(item)],
        watch_portfolio,
        _signal_analysis([]),
        news=news,
        strategy=_SIGNAL_STRATEGY,
    )
    sig = watch_signals[0]
    assert sig["signal"] == analyze.SIGNAL_WATCH
    assert sig["signal"] != analyze.SIGNAL_SELL


def test_signal_core_etf_no_signal():
    """Harte Ausschlussregel: Core-ETFs bekommen NO SIGNAL (kein Trade-Kandidat)."""
    core_etf = {
        "isin": "IE00BK5BQT80",
        "name": "Vanguard FTSE All-World UCITS ETF",
        "category": "core",
        "sector": "Diversified",
    }
    signals = analyze.compute_watchlist_signals(
        [core_etf],
        {"total_value_eur": 10000.0, "holdings": []},
        _signal_analysis([]),
        news=[],
        strategy=_SIGNAL_STRATEGY,
    )
    assert len(signals) == 1
    sig = signals[0]
    assert sig["signal"] == analyze.SIGNAL_NO_SIGNAL
    assert sig["excluded"] is True
    assert sig["score"] == 0
    assert sig["dimensions"] == {}


def test_signal_suse_legacy_no_signal():
    """SUSE/LU2722255754 (illiquide Legacy) -> NO SIGNAL, nie BUY/SELL."""
    suse = {"isin": "LU2722255754", "name": "SUSE", "category": "unknown", "sector": "software"}
    signals = analyze.compute_watchlist_signals(
        [suse],
        {"total_value_eur": 10000.0, "holdings": []},
        _signal_analysis([]),
        news=[],
        strategy=_SIGNAL_STRATEGY,
    )
    assert len(signals) == 1
    sig = signals[0]
    assert sig["signal"] == analyze.SIGNAL_NO_SIGNAL
    assert sig["excluded"] is True


def test_signal_no_signal_when_too_few_dimensions():
    """NO SIGNAL bei weniger als 3 nicht-None-Dimensionen (fail-closed)."""
    item = {"isin": "DE000A3E00M1", "name": "IONOS Group SE", "category": "unknown", "sector": "technology"}
    signals = analyze.compute_watchlist_signals(
        [item],
        {"total_value_eur": 10000.0, "holdings": []},
        _signal_analysis([]),
        news=[],
        strategy=_SIGNAL_STRATEGY,
        previous_snapshot=None,
        current_captured_at=None,
    )
    assert len(signals) == 1
    sig = signals[0]
    assert sig["signal"] == analyze.SIGNAL_NO_SIGNAL
    assert sig["dimensions"]["strategy_fit"] is None  # unknown-Kategorie -> fail-closed


def test_signal_mixed_news_is_neutral():
    """Gemischt positive+negative News -> D3 = 0 (kein klares Signal)."""
    item = _nvidia_item()
    signals = analyze.compute_watchlist_signals(
        [item],
        _SIGNAL_PORTFOLIO,
        _signal_analysis(),
        news=[{"title": "NVIDIA Gewinn-Rekord, aber Klage gegen Firma", "summary": "", "source": "test"}],
        strategy=_SIGNAL_STRATEGY,
    )
    assert signals[0]["dimensions"]["news_sentiment"] == 0


def test_signal_existing_holding_has_negative_portfolio_fit():
    """D2 Portfolio-Fit: bereits vorhandene Holding -> -1 (kein Add noetig)."""
    item = _nvidia_item()
    signals = analyze.compute_watchlist_signals(
        [item],
        _SIGNAL_PORTFOLIO,
        _signal_analysis(),
        news=[],
        strategy=_SIGNAL_STRATEGY,
    )
    assert signals[0]["dimensions"]["portfolio_fit"] == -1


def test_signal_etf_diversified_sector_is_neutral_not_unknown():
    """Satellite-ETF (Diversified) -> D1 = 0 statt None (kein Sektor-Signal)."""
    sat_etf = {
        "isin": "IE00B8GKDB10",
        "name": "Vanguard S&P 500 UCITS ETF",
        "category": "satellite",
        "sector": "Diversified",
    }
    signals = analyze.compute_watchlist_signals(
        [sat_etf],
        {"total_value_eur": 10000.0, "holdings": []},
        _signal_analysis([]),
        news=[],
        strategy=_SIGNAL_STRATEGY,
    )
    assert signals[0]["dimensions"]["strategy_fit"] == 0


def test_signal_news_sentiment_matches_ticker_and_isin():
    """D3-News-Matching via ISIN/Ticker/Name (deterministische Keywords)."""
    item = _nvidia_item()
    assert analyze._signal_item_news(
        item,
        [
            {"title": "NVIDIA kündigt neues Rechenzentrum an", "summary": ""},
            {"title": "US5949724083 Quartalszahlen", "summary": ""},
        ],
    )
    assert not analyze._signal_item_news(item, [{"title": "Apple gewinnt Marktanteile", "summary": ""}])


# --- D4: aktuelle sc-Kurs-/Kursentwicklung aus dem Vorgaenger-Snapshot ---

def _prev_snapshot(isin: str, value_eur: float) -> dict:
    """Vorgaenger-Snapshot mit genau einer Holding (nur Kurs-/Wertdaten)."""
    return {
        "captured_at": "2026-08-13T14:00:00+00:00",
        "portfolio": {"holdings": [{"isin": isin, "value_eur": value_eur, "quantity": 10}]},
    }


def test_signal_price_development_positive():
    """D4 = +1 bei Kursanstieg gegenueber Vorgaenger-Snapshot (sc-Kursdaten)."""
    item = _nvidia_item()
    item["value_eur"] = 1300.0
    assert analyze._dimension_price_development(item, _prev_snapshot("US5949724083", 1000.0), "2026-08-20T14:00:00+00:00") == 1


def test_signal_price_development_negative():
    """D4 = -1 bei Kursabfall gegenueber Vorgaenger-Snapshot (sc-Kursdaten)."""
    item = _nvidia_item()
    item["value_eur"] = 900.0
    assert analyze._dimension_price_development(item, _prev_snapshot("US5949724083", 1000.0), "2026-08-20T14:00:00+00:00") == -1


def test_signal_price_development_stagnant():
    """D4 = 0 bei stagnierendem Kurs (Aenderung innerhalb +-0.5%)."""
    item = _nvidia_item()
    item["value_eur"] = 1001.0
    assert analyze._dimension_price_development(item, _prev_snapshot("US5949724083", 1000.0), "2026-08-20T14:00:00+00:00") == 0


def test_signal_price_development_fail_closed_without_previous():
    """D4 = None ohne Vorgaenger-Snapshot oder ohne Kursdaten (fail-closed)."""
    item = _nvidia_item()
    item["value_eur"] = 1300.0
    assert analyze._dimension_price_development(item, None, "2026-08-20T14:00:00+00:00") is None
    assert analyze._dimension_price_development(item, _prev_snapshot("US5949724083", 1000.0), None) is None
    # Andere ISIN im Vorgaenger -> keine Vergleichsbasis -> None.
    assert analyze._dimension_price_development(item, _prev_snapshot("US0378331005", 1000.0), "2026-08-20T14:00:00+00:00") is None


def test_signal_price_development_feeds_sell():
    """D4 wirkt auf den Score: bestehende Satellite-Holding mit negativer
    Kursentwicklung und negativen News -> Score <= -2 -> SELL (D2 == -1)."""
    item = {
        "isin": "US5949724083",
        "name": "NVIDIA Corp.",
        "category": "satellite",
        "sector": "technology",
        "value_eur": 900.0,
    }
    sell_portfolio = {
        "total_value_eur": 10000.0,
        "holdings": [
            {"isin": "IE00BK5BQT80", "name": "Vanguard FTSE All-World", "category": "core", "value_eur": 7000.0},
            {"isin": "US5949724083", "name": "NVIDIA Corp.", "category": "satellite", "value_eur": 2000.0, "sector": "technology"},
        ],
    }
    signals = analyze.compute_watchlist_signals(
        [item],
        sell_portfolio,
        _signal_analysis(),
        news=[{"title": "NVIDIA Verlust und Absturz", "summary": "", "source": "test"}],
        strategy=_SIGNAL_STRATEGY,
        previous_snapshot=_prev_snapshot("US5949724083", 1000.0),
        current_captured_at="2026-08-20T14:00:00+00:00",
    )
    assert len(signals) == 1
    sig = signals[0]
    assert sig["dimensions"]["price_development"] == -1
    assert sig["signal"] == analyze.SIGNAL_SELL
