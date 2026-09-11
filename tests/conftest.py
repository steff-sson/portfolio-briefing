"""Shared pytest fixtures and sys.path setup (KISS-Rewrite)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

MOCK_DIR = Path(__file__).resolve().parent / "mock_data"


def _read(name: str):
    return json.loads((MOCK_DIR / name).read_text(encoding="utf-8"))


def sample_holdings():
    """Normierte Holdings mit expliziten Kategorien für Checks-Tests."""
    return [
        {"isin": "IE00BK5BQT80", "name": "Vanguard", "category": "core", "value_eur": 5000.0},
        {"isin": "IE00B4L5Y983", "name": "MSCI World", "category": "core", "value_eur": 2000.0},
        {"isin": "US0378331005", "name": "Apple", "category": "satellite", "value_eur": 600.0, "sector": "technology", "ticker": "AAPL"},
        {"isin": "DE0007164600", "name": "SAP", "category": "satellite", "value_eur": 700.0, "sector": "technology", "ticker": "SAP"},
        {"isin": "NL0010273215", "name": "ASML", "category": "satellite", "value_eur": 800.0, "sector": "technology", "ticker": "ASML"},
    ]
