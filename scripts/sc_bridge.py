"""Bridge to scalable.capital broker CLI. Falls back to mock data if sc missing."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
MOCK_PORTFOLIO = ROOT / "tests" / "mock_data" / "portfolio.json"
MOCK_TRANSACTIONS = ROOT / "tests" / "mock_data" / "transactions.json"


def _sc_available() -> bool:
    return shutil.which("sc") is not None


def _run_sc(args: list[str]) -> dict | list:
    result = subprocess.run(
        ["sc", *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout)


def _write_json(path: Path, data: dict | list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def fetch_from_sc() -> tuple[dict, list]:
    """Fetch live portfolio and transactions from sc CLI."""
    if not _sc_available():
        raise FileNotFoundError("sc CLI not found in PATH")
    portfolio = dict(_run_sc(["broker", "holdings", "--json"]))
    transactions = list(_run_sc(["broker", "transactions", "--json"]))
    return portfolio, transactions


def load_mock() -> tuple[dict, list]:
    with open(MOCK_PORTFOLIO, encoding="utf-8") as f:
        portfolio = json.load(f)
    with open(MOCK_TRANSACTIONS, encoding="utf-8") as f:
        transactions = json.load(f)
    return portfolio, transactions


def update_config() -> tuple[dict, list]:
    """Update config/portfolio.json and config/transactions.json."""
    try:
        portfolio, transactions = fetch_from_sc()
    except (FileNotFoundError, subprocess.CalledProcessError, json.JSONDecodeError):
        portfolio, transactions = load_mock()
    _write_json(CONFIG_DIR / "portfolio.json", portfolio)
    _write_json(CONFIG_DIR / "transactions.json", transactions)
    return portfolio, transactions


def get_portfolio() -> dict:
    path = CONFIG_DIR / "portfolio.json"
    if not path.exists():
        update_config()
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def get_transactions() -> list:
    path = CONFIG_DIR / "transactions.json"
    if not path.exists():
        update_config()
    with open(path, encoding="utf-8") as f:
        return json.load(f)


if __name__ == "__main__":
    portfolio, transactions = update_config()
    print(f"Portfolio: {len(portfolio.get('holdings', []))} holdings")
    print(f"Transactions: {len(transactions)} entries")
