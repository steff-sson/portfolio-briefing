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


# --- Echtes Scalable-MCP-Schema (top-level midPrice/currency, PII-frei) ---

def _write_portfolio(tmp_path, holdings, crypto=None):
    import json
    (tmp_path / "portfolio.json").write_text(
        json.dumps({"collectedAt": "2026-09-11T08:00:00Z",
                    "holdings": holdings, "cryptoHoldings": crypto or []}),
        encoding="utf-8")
    (tmp_path / "watchlist.json").write_text(json.dumps([]), encoding="utf-8")


def test_real_mcp_schema_top_level_midprice_normalizes_value(tmp_path):
    # Echtes MCP-Schema: midPrice/currency auf Holding-Ebene (kein currentQuote).
    holdings = [{
        "isin": "DE0000000001", "name": "Fake Fonds", "currency": "EUR",
        "midPrice": 120.0, "savingsPlan": False,
        "position": {"blocked": 0, "filled": 10.0, "pending": 0},
    }]
    _write_portfolio(tmp_path, holdings)
    snap = data_mod.load_snapshot(tmp_path)
    h = snap.holdings[0]
    assert h["quantity"] == 10.0
    assert h["currency"] == "EUR"
    assert h["value_eur"] == pytest.approx(10.0 * 120.0)
    assert not any(i.severity == "error" for i in snap.issues)


def test_real_mcp_schema_missing_midprice_is_warn_and_excluded(tmp_path):
    # Gefüllte Position ohne midPrice → warn + Ausschluss aus Holdings/Total
    # (Hinweisposten; User-Entscheidung 2026-09-11, illiquider Fall SUSE).
    holdings = [{
        "isin": "DE0000000002", "name": "Fake Ohne Preis", "currency": "EUR",
        "position": {"blocked": 0, "filled": 5.0, "pending": 0},
    }]
    _write_portfolio(tmp_path, holdings)
    snap = data_mod.load_snapshot(tmp_path)
    assert not snap.has_fatal  # warn, kein Fehler
    assert any("Kurs nicht verfügbar (illiquide?)" in i.message and i.severity == "warn"
               for i in snap.issues)
    # Positionsartefakt NICHT in holdings (kein LLM-Futter ohne Wert).
    assert all(h["isin"] != "DE0000000002" for h in snap.holdings)


def test_missing_price_position_excluded_from_total(tmp_path):
    # Warn-Position (kein Kurs) trägt NICHT zum total bei; bewertete Position schon.
    holdings = [
        {"isin": "DE0000000002", "name": "Fake Ohne Preis", "currency": "EUR",
         "position": {"filled": 5.0, "blocked": 0, "pending": 0}},
        {"isin": "DE0000000003", "name": "Fake Mit Preis", "currency": "EUR",
         "midPrice": 100.0, "position": {"filled": 2.0, "blocked": 0, "pending": 0}},
    ]
    _write_portfolio(tmp_path, holdings)
    snap = data_mod.load_snapshot(tmp_path)
    assert snap.total_value_eur == pytest.approx(2.0 * 100.0)
    assert len(snap.holdings) == 1
    assert snap.holdings[0]["isin"] == "DE0000000003"


def test_real_mcp_schema_usd_top_level_midprice_converted(tmp_path):
    # USD-Holding mit top-level midPrice/currency → EURUSD-Umrechnung.
    holdings = [{
        "isin": "US0000000001", "name": "Fake US Aktie", "currency": "USD",
        "midPrice": 200.0, "position": {"filled": 8.0, "blocked": 0, "pending": 0},
    }]
    _write_portfolio(tmp_path, holdings)
    snap = data_mod.load_snapshot(tmp_path, eurusd=1.08)
    h = snap.holdings[0]
    assert h["currency"] == "USD"
    assert h["fx_applied"] is True
    assert h["value_eur"] == pytest.approx(8.0 * 200.0 * 1.08)
