"""Tests für data.py — Snapshot-Loader + Validatoren (quellenneutral)."""
from __future__ import annotations

import json

import pytest

from scripts import data as data_mod
from tests.conftest import MOCK_DIR


def _load_mock(name: str):
    return json.loads((MOCK_DIR / name).read_text(encoding="utf-8"))


def test_load_snapshot_normalizes_holdings():
    snap = data_mod.load_snapshot(MOCK_DIR)
    assert len(snap.holdings) >= 5
    vanguard = next(h for h in snap.holdings if h["isin"] == "IE00BK5BQT80")
    assert vanguard["name"] == "Vanguard FTSE All-World (Acc)"
    assert vanguard["security_type"] == "ETF"
    assert vanguard["quantity"] == 35.0
    assert vanguard["value_eur"] == pytest.approx(35.0 * 128.4)
    assert vanguard["is_outdated"] is False


def test_crypto_etp_zero_balance_becomes_info_not_position():
    snap = data_mod.load_snapshot(MOCK_DIR)
    # Nullbestand (Ethereum/Solana/Cardano) → info-Issue, nicht in holdings.
    zeroes = [i.message for i in snap.issues if "Bestand 0" in i.message]
    assert len(zeroes) >= 3
    etp_isins = {h["isin"] for h in snap.holdings if h["security_type"] == "CRYPTO_ETP"}
    # Nur BTC (filled 1.5) ist als ETP-Position enthalten.
    assert etp_isins == {"DE000A3GM1F0"}
    btc = next(h for h in snap.holdings if h["isin"] == "DE000A3GM1F0")
    assert btc["quantity"] == 1.5


def test_missing_required_file_is_error(tmp_path):
    snap = data_mod.load_snapshot(tmp_path)
    assert snap.has_fatal
    assert any("Pflicht-Datei fehlt" in i.message for i in snap.issues)


def test_usd_position_converted_with_eurusd():
    # 8 AAPL à 222 USD = 1776 USD; mit EURUSD 1.08 → 1918.08 EUR.
    snap = data_mod.load_snapshot(MOCK_DIR, eurusd=1.08)
    apple = next(h for h in snap.holdings if h["isin"] == "US0378331005")
    assert apple["currency"] == "USD"
    assert apple["fx_applied"] is True
    assert apple["value_eur"] == pytest.approx(1776.0 * 1.08)


def test_usd_position_excluded_without_eurusd():
    # Ohne Kurs darf eine USD-Position NICHT als EUR einfließen (kein falscher Wert).
    snap = data_mod.load_snapshot(MOCK_DIR, eurusd=None)
    apple = next(h for h in snap.holdings if h["isin"] == "US0378331005")
    assert apple["value_eur"] is None
    assert any("ohne Umrechnung" in i.message for i in snap.issues)
    # Ausgeschlossene Position trägt NICHT zum total bei.
    assert not any(
        h["isin"] == "US0378331005" and (h.get("value_eur") or 0) > 0 for h in snap.holdings
    )


def test_eur_position_uses_quantity_times_mid():
    snap = data_mod.load_snapshot(MOCK_DIR, eurusd=1.08)
    sap = next(h for h in snap.holdings if h["isin"] == "DE0007164600")
    assert sap["currency"] == "EUR"
    assert sap["fx_applied"] is False
    assert sap["value_eur"] == pytest.approx(6.0 * 228.0)


def test_apply_strategy_classification():
    snap = data_mod.load_snapshot(MOCK_DIR)
    strategy = {
        "holdings_classification": {
            "isins": {
                "IE00BK5BQT80": {"category": "core", "confirmed": True, "source": "user"},
                "US0378331005": {"category": "satellite", "confirmed": True, "source": "user"},
            }
        }
    }
    data_mod.apply_strategy_classification(snap, strategy)
    by_isin = {h["isin"]: h["category"] for h in snap.holdings}
    assert by_isin["IE00BK5BQT80"] == "core"
    assert by_isin["US0378331005"] == "satellite"
    # Unbestätigt/fehlend → bleibt unknown (fail-closed)
    assert by_isin["NL0010273215"] == "unknown"


def test_parse_watchlist_structure():
    raw = _load_mock("watchlist.json")
    items, issues = data_mod.parse_watchlist(raw)
    assert len(items) == len(raw)
    assert issues == []
    apple = next(w for w in items if w["isin"] == "US0378331005")
    assert apple["security_type"] == "STOCK"
    assert apple["currency"] == "USD"
    assert apple["is_outdated"] is False


def test_parse_watchlist_skips_missing_isin():
    items, issues = data_mod.parse_watchlist([{"name": "Ohne ISIN"}])
    assert items == []
    assert len(issues) == 1
    assert issues[0].severity == "warn"
