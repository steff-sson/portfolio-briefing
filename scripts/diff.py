"""Deterministischer Snapshot-Vergleich prev -> current (Phase 1 + Phase 4).

Reine Funktionen ohne Disk-/Netz-Zugriff: ``diff_snapshots(previous, current)``.
- Positions-Match: ISIN. Schwellen: ``WEIGHT_DELTA_PP`` (Prozentpunkte) und
  ``VALUE_DELTA_PCT`` (Prozent); added/removed werden immer gemeldet.
- Transaktions-Match-Key: date+isin+type+quantity+price (exakte Gleichheit).
- ``diff_strategy``: Feld-Level-Diff der Strategie OHNE Werte (nur Pfade +
  Hashes) — sensitive Strategieinhalte bleiben aus LLM-Kontexten heraus.
- Erstlauf (previous=None) meldet keine erfundenen Aenderungen: has_previous
  False, keine added/removed/changed, prev-*-Felder None.
- Ausgabe JSON-serialisierbar, sortiert, Inputs werden nie mutiert.
"""
from __future__ import annotations

import json
from pathlib import Path

from scripts import analyze

WEIGHT_DELTA_PP = 0.5  # Schwellwert Gewichtsdifferenz in Prozentpunkten
VALUE_DELTA_PCT = 1.0  # Schwellwert Wertdifferenz in Prozent

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
CURRENT_PATH = CONFIG_DIR / "snapshot.current.json"


def _portfolio(snapshot: dict | None) -> dict:
    if not isinstance(snapshot, dict):
        return {}
    p = snapshot.get("portfolio")
    return p if isinstance(p, dict) else {}


def _holdings(portfolio: dict) -> list:
    holdings = portfolio.get("holdings", [])
    return holdings if isinstance(holdings, list) else []


def _transactions(snapshot: dict | None) -> list:
    if not isinstance(snapshot, dict):
        return []
    txns = snapshot.get("transactions")
    return txns if isinstance(txns, list) else []


def _value(holding: dict) -> float:
    try:
        return float(holding.get("value_eur", holding.get("value", 0)) or 0)
    except (TypeError, ValueError):
        return 0.0


def _total_value(portfolio: dict, holdings: list) -> float:
    total = portfolio.get("total_value_eur", portfolio.get("total_value"))
    if not total:
        return sum(_value(h) for h in holdings)
    try:
        return float(total)
    except (TypeError, ValueError):
        return sum(_value(h) for h in holdings)


def _cash(portfolio: dict) -> float:
    try:
        return round(float(portfolio.get("cash_eur", portfolio.get("cash", 0)) or 0), 2)
    except (TypeError, ValueError):
        return 0.0


def _normalize_position(holding: dict, total: float) -> dict:
    value = _value(holding)
    weight = round(value / total, 4) if total else 0.0
    return {
        "isin": holding.get("isin"),
        "name": holding.get("name"),
        "category": holding.get("category"),
        "value_eur": value,
        "weight": weight,
    }


def _diff_positions(
    prev_holdings: list, cur_holdings: list, prev_total: float, cur_total: float
) -> tuple[list, list, list]:
    prev_by_isin = {str(h.get("isin")): _normalize_position(h, prev_total) for h in prev_holdings}
    cur_by_isin = {str(h.get("isin")): _normalize_position(h, cur_total) for h in cur_holdings}
    prev_isins = set(prev_by_isin)
    cur_isins = set(cur_by_isin)

    added = [cur_by_isin[i] for i in sorted(cur_isins - prev_isins, key=str)]
    removed = [prev_by_isin[i] for i in sorted(prev_isins - cur_isins, key=str)]

    changed = []
    for isin in sorted(prev_isins & cur_isins, key=str):
        prev = prev_by_isin[isin]
        cur = cur_by_isin[isin]
        weight_delta_pp = (cur["weight"] - prev["weight"]) * 100
        if prev["value_eur"]:
            value_delta_pct = (cur["value_eur"] - prev["value_eur"]) / prev["value_eur"] * 100
        else:
            value_delta_pct = None
        if abs(weight_delta_pp) >= WEIGHT_DELTA_PP or (
            value_delta_pct is not None and abs(value_delta_pct) >= VALUE_DELTA_PCT
        ):
            changed.append({
                "isin": isin,
                "name": cur["name"],
                "category": cur["category"],
                "prev_value_eur": prev["value_eur"],
                "value_eur": cur["value_eur"],
                "value_delta_pct": round(value_delta_pct, 2) if value_delta_pct is not None else None,
                "prev_weight": prev["weight"],
                "weight": cur["weight"],
                "weight_delta_pp": round(weight_delta_pp, 2),
            })
    return added, removed, changed


def _txn_key(txn: dict) -> tuple:
    """Match-Key: date+isin+type+quantity+price (exakte Gleichheit)."""
    return (
        str(txn.get("date", "")),
        str(txn.get("isin", "")),
        str(txn.get("type", "")),
        float(txn.get("quantity", 0) or 0),
        float(txn.get("price_eur", txn.get("price", 0)) or 0),
    )


def _txn_volume(txn: dict) -> float:
    return float(txn.get("quantity", 0) or 0) * float(txn.get("price_eur", txn.get("price", 0)) or 0)


def _txn_record(key: tuple) -> dict:
    """Kanonischer Transaktions-Eintrag aus dem Match-Key (deterministisch)."""
    return {
        "date": key[0],
        "isin": key[1],
        "type": key[2],
        "quantity": key[3],
        "price_eur": key[4],
    }


def _diff_transactions(prev_txns: list, cur_txns: list, has_previous: bool) -> dict:
    prev_keys = {_txn_key(t) for t in prev_txns}
    cur_keys = {_txn_key(t) for t in cur_txns}
    added_keys = sorted(cur_keys - prev_keys, key=str) if has_previous else []
    removed_keys = sorted(prev_keys - cur_keys, key=str) if has_previous else []
    prev_volume = sum(_txn_volume(t) for t in prev_txns)
    cur_volume = sum(_txn_volume(t) for t in cur_txns)
    return {
        "prev_count": len(prev_txns) if has_previous else None,
        "count": len(cur_txns),
        "added_count": len(added_keys),
        "removed_count": len(removed_keys),
        "added": [_txn_record(k) for k in added_keys],
        "removed": [_txn_record(k) for k in removed_keys],
        "prev_volume_eur": round(prev_volume, 2) if has_previous else None,
        "volume_eur": round(cur_volume, 2),
        "delta_volume_eur": round(cur_volume - prev_volume, 2) if has_previous else None,
    }


def _diff_totals(
    prev_total: float, cur_total: float, prev_portfolio: dict, cur_portfolio: dict, has_previous: bool
) -> dict:
    prev_cash = _cash(prev_portfolio) if has_previous else None
    delta_eur = round(cur_total - prev_total, 2) if has_previous else None
    delta_pct = round((cur_total - prev_total) / prev_total * 100, 2) if has_previous and prev_total else None
    return {
        "prev_total_value_eur": round(prev_total, 2) if has_previous else None,
        "total_value_eur": round(cur_total, 2),
        "delta_eur": delta_eur,
        "delta_pct": delta_pct,
        "prev_cash_eur": prev_cash,
        "cash_eur": _cash(cur_portfolio),
    }


def _flatten(value: dict, prefix: str = "") -> dict:
    """Verschachteltes Dict -> {"pfad.zum.feld": wert} (sortiert, deterministisch)."""
    paths: dict = {}
    if not isinstance(value, dict):
        return paths
    for key in sorted(value.keys()):
        path = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(value[key], dict):
            paths.update(_flatten(value[key], path))
        else:
            paths[path] = value[key]
    return paths


def diff_strategy(prev_strategy: dict | None, cur_strategy: dict | None) -> dict:
    """Deterministischer Feld-Level-Diff der Strategie — OHNE Werte in der Ausgabe.

    Nur Feld-Pfade ("portfolio.core_pct") + Hashes, nie Werte: sensitive
    Strategieinhalte bleiben aus LLM-Kontexten heraus. Erstlauf / alter
    Snapshot ohne ``strategy_content`` (prev_strategy=None) -> has_previous
    False, has_changed False (keine erfundenen Aenderungen).
    """
    prev = prev_strategy if isinstance(prev_strategy, dict) else None
    cur = cur_strategy if isinstance(cur_strategy, dict) else None

    if prev is None:
        return {
            "has_changed": False,
            "has_previous": False,
            "changed_keys": [],
            "added_keys": [],
            "removed_keys": [],
            "hash_previous": None,
            "hash_current": analyze.strategy_hash(cur) if cur is not None else None,
        }

    prev_paths = _flatten(prev)
    cur_paths = _flatten(cur) if cur is not None else {}
    changed_keys = sorted(
        p for p in prev_paths.keys() & cur_paths.keys() if prev_paths[p] != cur_paths[p]
    )
    added_keys = sorted(cur_paths.keys() - prev_paths.keys())
    removed_keys = sorted(prev_paths.keys() - cur_paths.keys())
    return {
        "has_changed": bool(changed_keys or added_keys or removed_keys),
        "has_previous": True,
        "changed_keys": changed_keys,
        "added_keys": added_keys,
        "removed_keys": removed_keys,
        "hash_previous": analyze.strategy_hash(prev),
        "hash_current": analyze.strategy_hash(cur) if cur is not None else None,
    }


def diff_snapshots(previous: dict | None, current: dict) -> dict:
    """Deterministischer Vergleich prev -> current. Mutiert keine Inputs."""
    prev_portfolio = _portfolio(previous)
    cur_portfolio = _portfolio(current)
    prev_holdings = _holdings(prev_portfolio)
    cur_holdings = _holdings(cur_portfolio)
    prev_total = _total_value(prev_portfolio, prev_holdings)
    cur_total = _total_value(cur_portfolio, cur_holdings)
    has_previous = previous is not None

    if has_previous:
        added, removed, changed = _diff_positions(prev_holdings, cur_holdings, prev_total, cur_total)
    else:
        added, removed, changed = [], [], []

    prev_strategy = previous.get("strategy_content") if isinstance(previous, dict) else None
    cur_strategy = current.get("strategy_content") if isinstance(current, dict) else None

    return {
        "has_previous": has_previous,
        "positions": {
            "added": added,
            "removed": removed,
            "changed": changed,
        },
        "totals": _diff_totals(prev_total, cur_total, prev_portfolio, cur_portfolio, has_previous),
        "transactions": _diff_transactions(_transactions(previous), _transactions(current), has_previous),
        "strategy": diff_strategy(prev_strategy, cur_strategy),
    }


if __name__ == "__main__":
    from scripts.snapshot import load_previous, read_snapshot

    current = read_snapshot(CURRENT_PATH)
    previous = load_previous()
    result = diff_snapshots(previous, current) if current is not None else diff_snapshots(None, {})
    print(json.dumps(result, indent=2, ensure_ascii=False))
