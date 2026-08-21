"""Shared pytest fixtures and sys.path setup."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

MOCK_DATA = Path(__file__).resolve().parent / "mock_data"


@pytest.fixture
def portfolio() -> dict:
    return json.loads((MOCK_DATA / "portfolio.json").read_text(encoding="utf-8"))


@pytest.fixture
def transactions() -> list:
    return json.loads((MOCK_DATA / "transactions.json").read_text(encoding="utf-8"))


@pytest.fixture
def watchlist() -> list:
    """Rohdaten aus tests/mock_data/watchlist.json (wie sie load_mock_watchlist liest)."""
    return json.loads((MOCK_DATA / "watchlist.json").read_text(encoding="utf-8"))
