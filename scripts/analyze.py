"""Portfolio analysis: structure, drift, turnover, thesis deadlines."""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
THESIS_DIR = Path.home() / "docs" / "notizen" / "portfolio-theses"


Status = str


def load_strategy() -> dict:
    with open(CONFIG_DIR / "strategy.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_etf_lookup() -> dict:
    path = CONFIG_DIR / "etf_lookup.json"
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _status(value: float, min_v: float, max_v: float) -> Status:
    if value < min_v or value > max_v:
        return "red"
    margin = (max_v - min_v) * 0.05
    if value < min_v + margin or value > max_v - margin:
        return "yellow"
    return "green"


def _holding_value(h: dict) -> float:
    return float(h.get("value_eur", h.get("value", 0)) or 0)


def calculate_positions(portfolio: dict) -> dict:
    total = float(portfolio.get("total_value_eur", 0) or portfolio.get("total_value", 0))
    holdings = portfolio.get("holdings", [])
    if not total:
        total = sum(_holding_value(h) for h in holdings)
    positions = []
    for h in holdings:
        val = _holding_value(h)
        positions.append({
            "isin": h.get("isin"),
            "name": h.get("name"),
            "category": h.get("category", "satellite"),
            "value_eur": val,
            "weight": round(val / total, 4) if total else 0,
        })
    return {"total_value_eur": total, "positions": positions}


def calculate_core_satellite(positions: list, strategy: dict) -> dict:
    core_cfg = strategy.get("strategy", {})
    total = sum(p["value_eur"] for p in positions)
    core_value = sum(p["value_eur"] for p in positions if p["category"] == "core")
    ratio = core_value / total if total else 0
    target = core_cfg.get("core_ratio_target", 0.35)
    min_v = core_cfg.get("core_ratio_min", 0.30)
    max_v = core_cfg.get("core_ratio_max", 0.45)
    return {
        "core_value_eur": core_value,
        "satellite_value_eur": total - core_value,
        "core_ratio": round(ratio, 4),
        "target_ratio": target,
        "status": _status(ratio, min_v, max_v),
    }


def calculate_sector_concentration(positions: list, strategy: dict) -> dict:
    sectors = strategy.get("strategy", {}).get("sectors", {})
    total = sum(p["value_eur"] for p in positions)
    sector_values: dict[str, float] = {}
    for p in positions:
        sector = sectors.get(p["name"], "Unknown")
        sector_values[sector] = sector_values.get(sector, 0) + p["value_eur"]
    sector_ratios = {s: round(v / total, 4) if total else 0 for s, v in sector_values.items()}
    max_sector = max(sector_ratios.items(), key=lambda kv: kv[1])[0] if sector_ratios else "Unknown"
    max_ratio = sector_ratios.get(max_sector, 0)
    threshold = strategy.get("strategy", {}).get("sector_concentration_max", 0.35)
    return {
        "sector_ratios": sector_ratios,
        "max_sector": max_sector,
        "max_ratio": max_ratio,
        "threshold": threshold,
        "status": "red" if max_ratio > threshold else "green" if max_ratio < threshold * 0.9 else "yellow",
    }


def calculate_single_position_max(positions: list, strategy: dict) -> dict:
    threshold = strategy.get("strategy", {}).get("single_position_max", 0.25)
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
    target = strategy.get("strategy", {}).get("core_ratio_target", 0.35)
    threshold = strategy.get("strategy", {}).get("drift_threshold", 0.05)
    actual = sum(p["weight"] for p in positions if p["category"] == "core")
    drift = abs(actual - target)
    return {
        "core_ratio_actual": round(actual, 4),
        "target": target,
        "drift": round(drift, 4),
        "threshold": threshold,
        "status": "red" if drift > threshold else "green" if drift < threshold * 0.6 else "yellow",
    }


def calculate_turnover(transactions: list, portfolio: dict) -> dict:
    total = float(portfolio.get("total_value_eur", 0) or portfolio.get("total_value", 0))
    total_volume = sum(float(t.get("price_eur", 0) * t.get("quantity", 0)) for t in transactions)
    turnover = total_volume / total if total else 0
    return {
        "transaction_count": len(transactions),
        "total_volume_eur": round(total_volume, 2),
        "turnover_ratio": round(turnover, 4),
        "status": "red" if turnover > 0.10 else "green" if turnover < 0.05 else "yellow",
    }


def _parse_thesis_date(md: Path) -> datetime | None:
    text = md.read_text(encoding="utf-8")
    match = re.search(r"^created:\s*(\d{4}-\d{2}-\d{2})", text, re.MULTILINE)
    if match:
        return datetime.strptime(match.group(1), "%Y-%m-%d")
    return None


def check_thesis_deadlines(strategy: dict) -> dict:
    months = strategy.get("strategy", {}).get("thesis_deadline_months", 6)
    cutoff = datetime.now() - timedelta(days=months * 30)
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
        "turnover": calculate_turnover(transactions, portfolio),
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
