"""Diff-Tests: Erstlauf, identischer Stand, added/removed/changed, Schwellen, Txn-Diff.

Reine lokale Mockdaten (conftest-Fixtures + Inline-Varianten) — keine
API-/Telegram-/Broker-Aufrufe. Alle Vergleiche deterministisch, Inputs
werden nicht mutiert.
"""
from __future__ import annotations

import json

import pytest

from scripts import analyze, diff


def snap(portfolio, transactions, captured_at="2026-08-13T10:00:00+00:00") -> dict:
    return {
        "schema_version": 1,
        "captured_at": captured_at,
        "mode": "monday",
        "portfolio": portfolio,
        "transactions": transactions,
        "sc_meta": {"source": "test"},
    }


def _clone(obj):
    return json.loads(json.dumps(obj))


# --- Erstlauf ----------------------------------------------------------------


def test_first_run_no_fabricated_changes(portfolio, transactions):
    result = diff.diff_snapshots(None, snap(portfolio, transactions))

    assert result["has_previous"] is False
    assert result["positions"]["added"] == []
    assert result["positions"]["removed"] == []
    assert result["positions"]["changed"] == []
    assert result["totals"]["prev_total_value_eur"] is None
    assert result["totals"]["total_value_eur"] == 9680.0
    assert result["totals"]["delta_eur"] is None
    assert result["totals"]["delta_pct"] is None
    assert result["transactions"]["prev_count"] is None
    assert result["transactions"]["count"] == 4
    assert result["transactions"]["added"] == []
    assert result["transactions"]["removed"] == []
    assert result["transactions"]["added_count"] == 0
    assert result["transactions"]["removed_count"] == 0
    assert result["transactions"]["prev_volume_eur"] is None


# --- Identischer Stand -------------------------------------------------------


def test_identical_state_no_changes(portfolio, transactions):
    prev = snap(portfolio, transactions)
    result = diff.diff_snapshots(prev, snap(_clone(portfolio), _clone(transactions)))

    assert result["has_previous"] is True
    assert result["positions"]["added"] == []
    assert result["positions"]["removed"] == []
    assert result["positions"]["changed"] == []
    assert result["totals"]["delta_eur"] == 0.0
    assert result["totals"]["delta_pct"] == 0.0
    assert result["totals"]["prev_cash_eur"] == 320.0
    assert result["totals"]["cash_eur"] == 320.0
    assert result["transactions"]["added"] == []
    assert result["transactions"]["removed"] == []
    assert result["transactions"]["added_count"] == 0
    assert result["transactions"]["removed_count"] == 0
    assert result["transactions"]["delta_volume_eur"] == 0.0


# --- added / removed (immer, unabhaengig von Schwellen) ----------------------


def test_added_position_always_reported(portfolio, transactions):
    prev = snap(_clone(portfolio), _clone(transactions))
    cur_portfolio = _clone(portfolio)
    cur_portfolio["holdings"].append(
        {"isin": "IE00B3XXRP09", "name": "Vanguard FTSE EM", "quantity": 10, "value_eur": 100.0, "category": "core"}
    )
    cur_portfolio["total_value_eur"] = 9780.0
    result = diff.diff_snapshots(prev, snap(cur_portfolio, _clone(transactions)))

    added = result["positions"]["added"]
    assert len(added) == 1
    assert added[0]["isin"] == "IE00B3XXRP09"
    assert added[0]["value_eur"] == 100.0
    assert result["positions"]["removed"] == []
    assert result["positions"]["changed"] == []


def test_removed_position_always_reported():
    """Entfernte Position wird immer gemeldet (auch mit Wert 0, ohne Schwellen-Effekt)."""
    prev_portfolio = {"total_value_eur": 500.0, "holdings": [
        {"isin": "DE000A", "name": "A", "category": "core", "value_eur": 500.0},
        {"isin": "DE000B", "name": "B", "category": "satellite", "value_eur": 0.0},
    ]}
    cur_portfolio = {"total_value_eur": 500.0, "holdings": [
        {"isin": "DE000A", "name": "A", "category": "core", "value_eur": 500.0},
    ]}
    result = diff.diff_snapshots(snap(prev_portfolio, []), snap(cur_portfolio, []))

    removed = result["positions"]["removed"]
    assert len(removed) == 1
    assert removed[0]["isin"] == "DE000B"
    assert removed[0]["value_eur"] == 0.0
    assert result["positions"]["added"] == []
    assert result["positions"]["changed"] == []  # Gewichte unveraendert -> nichts zusaetzlich


def test_changed_above_weight_threshold():
    """A +10% / B -10% -> Gewichtsdifferenz +/-5pp ueber Schwelle, beide als changed."""
    prev_portfolio = {"total_value_eur": 1000.0, "holdings": [
        {"isin": "DE000A", "name": "A", "category": "core", "value_eur": 500.0},
        {"isin": "DE000B", "name": "B", "category": "satellite", "value_eur": 500.0},
    ]}
    cur_portfolio = {"total_value_eur": 1000.0, "holdings": [
        {"isin": "DE000A", "name": "A", "category": "core", "value_eur": 550.0},
        {"isin": "DE000B", "name": "B", "category": "satellite", "value_eur": 450.0},
    ]}
    result = diff.diff_snapshots(snap(prev_portfolio, []), snap(cur_portfolio, []))

    changed = result["positions"]["changed"]
    assert [c["isin"] for c in changed] == ["DE000A", "DE000B"]  # sortiert
    a = next(c for c in changed if c["isin"] == "DE000A")
    assert a["prev_value_eur"] == 500.0
    assert a["value_eur"] == 550.0
    assert a["value_delta_pct"] == pytest.approx(10.0, abs=0.01)
    assert a["weight_delta_pp"] == pytest.approx(5.0, abs=0.01)
    assert result["positions"]["added"] == []
    assert result["positions"]["removed"] == []


def test_changed_above_value_threshold_only(portfolio, transactions):
    """Gleichmaessige Skalierung: Gewichte stabil, Wert +2% -> changed via Wert-Schwelle."""
    prev = snap(_clone(portfolio), _clone(transactions))
    cur_portfolio = _clone(portfolio)
    for h in cur_portfolio["holdings"]:
        h["value_eur"] = round(h["value_eur"] * 1.02, 2)
    cur_portfolio["total_value_eur"] = 9873.6
    result = diff.diff_snapshots(prev, snap(cur_portfolio, _clone(transactions)))

    changed = result["positions"]["changed"]
    assert len(changed) == 6  # alle Positionen +2% >= 1.0%-Schwelle
    assert all(c["weight_delta_pp"] == pytest.approx(0.0, abs=0.0001) for c in changed)
    assert all(c["value_delta_pct"] == pytest.approx(2.0, abs=0.01) for c in changed)
    assert result["totals"]["delta_eur"] == pytest.approx(193.6, abs=0.01)
    assert result["totals"]["delta_pct"] == pytest.approx(2.0, abs=0.01)


def test_not_changed_below_thresholds(portfolio, transactions):
    prev = snap(_clone(portfolio), _clone(transactions))
    cur_portfolio = _clone(portfolio)
    for h in cur_portfolio["holdings"]:
        if h["isin"] == "US0378331005":  # AAPL +0.5%: Wert 2400 -> 2412
            h["value_eur"] = 2412.0
    cur_portfolio["total_value_eur"] = 9692.0
    result = diff.diff_snapshots(prev, snap(cur_portfolio, _clone(transactions)))

    assert result["positions"]["changed"] == []
    assert result["positions"]["added"] == []
    assert result["positions"]["removed"] == []


def test_changed_at_exact_threshold_uses_greater_equal():
    """Gewichtsdifferenz exakt ~0.5 Prozentpunkte -> changed (>= 0.5-Semantik).

    A (0.505) und B (0.495) liegen beide exakt 0.5pp vom Vorwert entfernt
    (Float-Rundung -> minimal ueber 0.5), beide werden als changed gemeldet.
    """
    prev_portfolio = {"total_value_eur": 1000.0, "holdings": [
        {"isin": "DE000A", "name": "A", "category": "core", "value_eur": 500.0},
        {"isin": "DE000B", "name": "B", "category": "satellite", "value_eur": 500.0},
    ]}
    cur_portfolio = {"total_value_eur": 1000.0, "holdings": [
        {"isin": "DE000A", "name": "A", "category": "core", "value_eur": 505.0},
        {"isin": "DE000B", "name": "B", "category": "satellite", "value_eur": 495.0},
    ]}
    result = diff.diff_snapshots(snap(prev_portfolio, []), snap(cur_portfolio, []))

    changed = result["positions"]["changed"]
    assert [c["isin"] for c in changed] == ["DE000A", "DE000B"]
    assert changed[0]["weight_delta_pp"] == pytest.approx(0.5, abs=0.001)
    assert changed[1]["weight_delta_pp"] == pytest.approx(-0.5, abs=0.001)


def test_below_threshold_not_changed():
    """Gewichtsdifferenz 0.4 Prozentpunkte + Wert +0.8% -> nicht changed."""
    prev_portfolio = {"total_value_eur": 1000.0, "holdings": [
        {"isin": "DE000A", "name": "A", "category": "core", "value_eur": 500.0},
        {"isin": "DE000B", "name": "B", "category": "satellite", "value_eur": 500.0},
    ]}
    cur_portfolio = {"total_value_eur": 1000.0, "holdings": [
        {"isin": "DE000A", "name": "A", "category": "core", "value_eur": 504.0},
        {"isin": "DE000B", "name": "B", "category": "satellite", "value_eur": 496.0},
    ]}
    result = diff.diff_snapshots(snap(prev_portfolio, []), snap(cur_portfolio, []))

    assert result["positions"]["changed"] == []


# --- Transaktions-Diff -------------------------------------------------------


def test_transaction_added_removed(portfolio, transactions):
    prev_txns = _clone(transactions)[:3]
    cur_txns = _clone(transactions)[:2]  # SAP-Kauf (2026-06-10) entfaellt
    cur_txns.append({"date": "2026-08-01", "isin": "IE00BK5BQT80", "type": "buy", "quantity": 2, "price_eur": 120.0})

    result = diff.diff_snapshots(snap(_clone(portfolio), prev_txns), snap(_clone(portfolio), cur_txns))

    txn_diff = result["transactions"]
    assert txn_diff["prev_count"] == 3
    assert txn_diff["count"] == 3
    assert txn_diff["added_count"] == 1
    assert txn_diff["removed_count"] == 1
    assert txn_diff["added"][0] == {
        "date": "2026-08-01",
        "isin": "IE00BK5BQT80",
        "type": "buy",
        "quantity": 2.0,
        "price_eur": 120.0,
    }
    assert txn_diff["removed"][0]["date"] == "2026-06-10"


def test_transaction_volume_delta(portfolio, transactions):
    prev_txns = [{"date": "2026-01-15", "isin": "US0378331005", "type": "buy", "quantity": 5, "price_eur": 195.0}]
    cur_txns = [{"date": "2026-01-15", "isin": "US0378331005", "type": "buy", "quantity": 5, "price_eur": 195.0},
                {"date": "2026-08-01", "isin": "IE00BK5BQT80", "type": "buy", "quantity": 2, "price_eur": 120.0}]

    result = diff.diff_snapshots(snap(_clone(portfolio), prev_txns), snap(_clone(portfolio), cur_txns))

    assert result["transactions"]["prev_volume_eur"] == 975.0
    assert result["transactions"]["volume_eur"] == 1215.0
    assert result["transactions"]["delta_volume_eur"] == 240.0


def test_transaction_match_key_ignores_extra_fields(portfolio, transactions):
    """Gleicher Key (date+isin+type+quantity+price), andere Zusatzfelder -> kein Diff."""
    prev_txns = [{"date": "2026-01-15", "isin": "US0378331005", "type": "buy", "quantity": 5, "price_eur": 195.0}]
    cur_txns = [{"date": "2026-01-15", "isin": "US0378331005", "type": "buy", "quantity": 5, "price_eur": 195.0,
                 "note": "Limit-Order", "fee_eur": 1.0}]

    result = diff.diff_snapshots(snap(_clone(portfolio), prev_txns), snap(_clone(portfolio), cur_txns))

    assert result["transactions"]["added"] == []
    assert result["transactions"]["removed"] == []
    assert result["transactions"]["added_count"] == 0
    assert result["transactions"]["removed_count"] == 0


def test_transaction_key_change_is_added_and_removed(portfolio, transactions):
    """Andere quantity -> anderer Key -> removed + added."""
    prev_txns = [{"date": "2026-01-15", "isin": "US0378331005", "type": "buy", "quantity": 5, "price_eur": 195.0}]
    cur_txns = [{"date": "2026-01-15", "isin": "US0378331005", "type": "buy", "quantity": 6, "price_eur": 195.0}]

    result = diff.diff_snapshots(snap(_clone(portfolio), prev_txns), snap(_clone(portfolio), cur_txns))

    assert result["transactions"]["added_count"] == 1
    assert result["transactions"]["removed_count"] == 1
    assert result["transactions"]["added"][0]["quantity"] == 6.0
    assert result["transactions"]["removed"][0]["quantity"] == 5.0


# --- Robustheit / Determinismus ----------------------------------------------


def test_no_mutation(portfolio, transactions):
    prev = snap(_clone(portfolio), _clone(transactions))
    cur = snap(_clone(portfolio), _clone(transactions))
    prev_copy = _clone(prev)
    cur_copy = _clone(cur)
    # cur mit Aenderung
    cur["portfolio"]["holdings"][0]["value_eur"] = 9999.0
    cur["transactions"].append({"date": "2026-08-01", "isin": "X", "type": "sell", "quantity": 1, "price_eur": 10.0})
    cur_copy["portfolio"]["holdings"][0]["value_eur"] = 9999.0
    cur_copy["transactions"].append({"date": "2026-08-01", "isin": "X", "type": "sell", "quantity": 1, "price_eur": 10.0})

    diff.diff_snapshots(prev, cur)

    assert prev == prev_copy
    assert cur == cur_copy


def test_deterministic_sorted_output(portfolio, transactions):
    prev = snap(_clone(portfolio), _clone(transactions))
    cur_portfolio = _clone(portfolio)
    cur_portfolio["holdings"].append(
        {"isin": "IE00B3XXRP09", "name": "Vanguard FTSE EM", "quantity": 10, "value_eur": 100.0, "category": "core"}
    )
    cur_portfolio["total_value_eur"] = 9780.0
    cur = snap(cur_portfolio, _clone(transactions))

    r1 = diff.diff_snapshots(prev, cur)
    r2 = diff.diff_snapshots(_clone(prev), _clone(cur))

    assert r1 == r2  # deterministisch
    isins = [p["isin"] for p in r1["positions"]["added"]]
    assert isins == sorted(isins)  # sortiert
    keys = [t["date"] for t in r1["transactions"]["added"]]
    assert keys == sorted(keys)
    json.dumps(r1)  # JSON-serialisierbar


def test_missing_optional_fields_defensive(portfolio, transactions):
    """Portfolio ohne total_value (aus Holdings berechnet), Holding ohne value -> 0."""
    prev_portfolio = {"holdings": [
        {"isin": "DE000A", "name": "A", "category": "core", "value_eur": 100.0},
        {"isin": "DE000B", "name": "B", "category": "satellite"},  # kein Wert
    ]}
    cur_portfolio = {"holdings": [
        {"isin": "DE000A", "name": "A", "category": "core", "value_eur": 100.0},
        {"isin": "DE000B", "name": "B", "category": "satellite"},
    ]}
    result = diff.diff_snapshots(snap(prev_portfolio, []), snap(cur_portfolio, []))

    assert result["totals"]["total_value_eur"] == 100.0  # summiert aus Holdings
    assert result["transactions"]["count"] == 0  # fehlende Transaktionen -> leer
    assert result["positions"]["changed"] == []


def test_empty_portfolios_first_run(portfolio, transactions):
    result = diff.diff_snapshots(None, snap({}, []))
    assert result["has_previous"] is False
    assert result["totals"]["total_value_eur"] == 0.0
    assert result["transactions"]["count"] == 0


# --- strategy_hash / diff_strategy -------------------------------------------


def _strategy_v1() -> dict:
    return {
        "portfolio": {"core_pct": 75.0, "satellite_pct": 25.0, "core_description": "FTSE All-World (Acc)"},
        "satellite_limits": {"max_position_pct": 5.0, "max_sector_pct": 15.0, "max_positions": 10},
    }


def test_strategy_hash_deterministic_and_order_independent():
    """Gleicher Inhalt -> gleicher Hash, unabhaengig von der Key-Reihenfolge."""
    s = _strategy_v1()
    h1 = analyze.strategy_hash(s)
    h2 = analyze.strategy_hash({k: s[k] for k in reversed(s)})
    assert h1 == h2
    assert len(h1) == 64  # SHA-256 hex


def test_strategy_hash_changes_with_content():
    s = _strategy_v1()
    changed = _strategy_v1()
    changed["portfolio"]["core_pct"] = 80.0
    assert analyze.strategy_hash(changed) != analyze.strategy_hash(s)


def test_diff_strategy_no_previous():
    """Erstlauf (prev None) -> has_previous False, keine erfundenen Aenderungen."""
    cur = _strategy_v1()
    result = diff.diff_strategy(None, cur)
    assert result["has_previous"] is False
    assert result["has_changed"] is False
    assert result["changed_keys"] == []
    assert result["added_keys"] == []
    assert result["removed_keys"] == []
    assert result["hash_previous"] is None
    assert result["hash_current"] == analyze.strategy_hash(cur)


def test_diff_strategy_previous_not_a_dict_treated_as_none():
    result = diff.diff_strategy("not-a-dict", _strategy_v1())
    assert result["has_previous"] is False
    assert result["has_changed"] is False
    assert result["hash_previous"] is None


def test_diff_strategy_no_change():
    s = _strategy_v1()
    result = diff.diff_strategy(s, json.loads(json.dumps(s)))
    assert result["has_previous"] is True
    assert result["has_changed"] is False
    assert result["changed_keys"] == []
    assert result["added_keys"] == []
    assert result["removed_keys"] == []
    assert result["hash_previous"] == result["hash_current"]


def test_diff_strategy_changed_value():
    prev = _strategy_v1()
    cur = _strategy_v1()
    cur["portfolio"]["core_pct"] = 80.0
    result = diff.diff_strategy(prev, cur)
    assert result["has_changed"] is True
    assert result["changed_keys"] == ["portfolio.core_pct"]
    assert result["added_keys"] == []
    assert result["removed_keys"] == []
    assert result["hash_previous"] != result["hash_current"]


def test_diff_strategy_nested_change():
    """Verschachteltes Feld (portfolio.rebalancing.threshold_pct) wird flach gemeldet."""
    prev = _strategy_v1()
    cur = _strategy_v1()
    cur["portfolio"]["rebalancing"] = {"method": "threshold", "threshold_pct": 7.5}
    result = diff.diff_strategy(prev, cur)
    assert "portfolio.rebalancing.threshold_pct" in result["added_keys"]
    assert result["has_changed"] is True


def test_diff_strategy_added_and_removed_keys():
    prev = _strategy_v1()
    cur = _strategy_v1()
    cur["alerts"] = {"on_thesis_expiring_soon_days": 30}  # neu
    del cur["satellite_limits"]["max_positions"]  # entfernt
    result = diff.diff_strategy(prev, cur)
    assert result["added_keys"] == ["alerts.on_thesis_expiring_soon_days"]
    assert result["removed_keys"] == ["satellite_limits.max_positions"]
    assert result["has_changed"] is True


def test_diff_strategy_current_none_removes_all():
    prev = _strategy_v1()
    result = diff.diff_strategy(prev, None)
    assert result["has_changed"] is True
    assert result["removed_keys"] == [
        "portfolio.core_description",
        "portfolio.core_pct",
        "portfolio.satellite_pct",
        "satellite_limits.max_position_pct",
        "satellite_limits.max_positions",
        "satellite_limits.max_sector_pct",
    ]
    assert result["added_keys"] == []
    assert result["hash_current"] is None


def test_diff_strategy_never_leaks_values():
    """Ausgabe enthaelt nur Feld-Pfade + Hashes, NIE Strategiewerte."""
    prev = {"portfolio": {"core_pct": 75.0, "core_description": "geheimwert-abc-123"}}
    cur = {"portfolio": {"core_pct": 80.0, "core_description": "geheimwert-xyz-789"}}
    result = diff.diff_strategy(prev, cur)
    dumped = json.dumps(result)
    assert "geheimwert-abc-123" not in dumped
    assert "geheimwert-xyz-789" not in dumped
    assert "75.0" not in dumped
    assert "80.0" not in dumped
    assert result["changed_keys"] == ["portfolio.core_description", "portfolio.core_pct"]


def test_diff_strategy_deterministic_and_json_serializable():
    prev = _strategy_v1()
    cur = _strategy_v1()
    cur["portfolio"]["core_pct"] = 80.0
    cur["alerts"] = {"on_thesis_expiring_soon_days": 30}
    r1 = diff.diff_strategy(prev, cur)
    r2 = diff.diff_strategy(json.loads(json.dumps(prev)), json.loads(json.dumps(cur)))
    assert r1 == r2  # deterministisch
    assert json.loads(json.dumps(r1)) == r1  # JSON-serialisierbar
