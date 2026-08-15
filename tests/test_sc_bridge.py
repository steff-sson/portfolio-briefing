"""Tests fuer sc_bridge: fail-closed Live-Abruf, Mock nur via load_mock().

Ausschliesslich Fake-Objekte, monkeypatch und tmp_path — keine echten
sc-/API-/Telegram-Aufrufe.
"""
from __future__ import annotations

import json
import subprocess

import pytest

from scripts import sc_bridge


def _completed(stdout: str) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(["sc"], 0, stdout=stdout)


def _make_run(*stdouts: str):
    """Mock fuer subprocess.run: liefert je Aufruf einen stdout-Wert, zeichnet cmds auf."""
    calls: list[list[str]] = []
    remaining = list(stdouts)

    def _run(cmd, *args, **kwargs):
        calls.append(list(cmd))
        return _completed(remaining.pop(0))

    return _run, calls


# --- load_mock: einziger Mock-Zugang (Dry-Run), ausschliesslich tests/mock_data/ ---


def test_load_mock_returns_mock_data(portfolio, transactions):
    loaded_portfolio, loaded_transactions = sc_bridge.load_mock()
    assert loaded_portfolio == portfolio
    assert loaded_transactions == transactions
    assert loaded_portfolio["holdings"]


# --- refresh_from_sc: produktiver Live-Abruf, fail-closed, kein Mock-Fallback ---


@pytest.fixture
def wrapper_holdings_payload(portfolio) -> dict:
    """CLI-Wrapper-Format fuer holdings: {ok, command, data: {..., result: {count, items}}}."""
    return {
        "ok": True,
        "command": "sc broker holdings --json",
        "data": {
            "total_value_eur": portfolio["total_value_eur"],
            "cash_eur": portfolio["cash_eur"],
            "result": {"count": len(portfolio["holdings"]), "items": portfolio["holdings"]},
        },
    }


@pytest.fixture
def wrapper_transactions_payload(transactions) -> dict:
    """CLI-Wrapper-Format fuer transactions: Items liegen in data.result.items."""
    return {
        "ok": True,
        "command": "sc broker transactions --json",
        "data": {"result": {"count": len(transactions), "items": transactions}},
    }


def test_refresh_success(monkeypatch, portfolio, transactions):
    """Legacy-Format: Holdings-Dict mit "holdings", Transactions als direkte Liste."""
    monkeypatch.setattr(sc_bridge, "_sc_available", lambda: True)
    run, calls = _make_run(json.dumps(portfolio), json.dumps(transactions))
    monkeypatch.setattr(sc_bridge.subprocess, "run", run)

    got_portfolio, got_transactions = sc_bridge.refresh_from_sc()

    assert got_portfolio == portfolio
    assert got_transactions == transactions
    assert calls == [["sc", "broker", "holdings", "--json"], ["sc", "broker", "transactions", "--json"]]


def test_refresh_success_wrapper_format(monkeypatch, portfolio, transactions, wrapper_holdings_payload, wrapper_transactions_payload):
    """CLI-Wrapper-Format: Holdings und Transactions kommen aus data.result.items."""
    monkeypatch.setattr(sc_bridge, "_sc_available", lambda: True)
    run, calls = _make_run(json.dumps(wrapper_holdings_payload), json.dumps(wrapper_transactions_payload))
    monkeypatch.setattr(sc_bridge.subprocess, "run", run)

    got_portfolio, got_transactions = sc_bridge.refresh_from_sc()

    assert got_portfolio["holdings"] == portfolio["holdings"]
    assert got_portfolio["total_value_eur"] == portfolio["total_value_eur"]
    assert got_portfolio["cash_eur"] == portfolio["cash_eur"]
    assert got_transactions == transactions
    assert calls == [["sc", "broker", "holdings", "--json"], ["sc", "broker", "transactions", "--json"]]


def test_refresh_missing_sc_raises(monkeypatch):
    monkeypatch.setattr(sc_bridge, "_sc_available", lambda: False)

    with pytest.raises(sc_bridge.ScNotAvailableError):
        sc_bridge.refresh_from_sc()


def test_refresh_called_process_error_raises(monkeypatch):
    monkeypatch.setattr(sc_bridge, "_sc_available", lambda: True)

    def _run_fails(cmd, *args, **kwargs):
        raise subprocess.CalledProcessError(returncode=1, cmd=cmd)

    monkeypatch.setattr(sc_bridge.subprocess, "run", _run_fails)

    with pytest.raises(sc_bridge.ScCommandError):
        sc_bridge.refresh_from_sc()


def test_refresh_invalid_json_raises(monkeypatch):
    monkeypatch.setattr(sc_bridge, "_sc_available", lambda: True)
    run, _ = _make_run("not json")
    monkeypatch.setattr(sc_bridge.subprocess, "run", run)

    with pytest.raises(sc_bridge.ScInvalidJsonError):
        sc_bridge.refresh_from_sc()


def test_refresh_unexpected_json_shape_raises(monkeypatch):
    monkeypatch.setattr(sc_bridge, "_sc_available", lambda: True)
    run, _ = _make_run("[]", "[]")
    monkeypatch.setattr(sc_bridge.subprocess, "run", run)

    with pytest.raises(sc_bridge.ScInvalidJsonError):
        sc_bridge.refresh_from_sc()


def test_refresh_empty_holdings_raises(monkeypatch):
    monkeypatch.setattr(sc_bridge, "_sc_available", lambda: True)
    run, _ = _make_run(json.dumps({"holdings": []}), "[]")
    monkeypatch.setattr(sc_bridge.subprocess, "run", run)

    with pytest.raises(sc_bridge.ScEmptyDataError):
        sc_bridge.refresh_from_sc()


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param({"ok": False, "command": "sc broker holdings --json", "data": {"result": {"count": 0, "items": []}}}, id="ok-false"),
        pytest.param({"command": "sc broker holdings --json", "data": {"result": {"count": 0, "items": []}}}, id="ok-missing"),
        pytest.param({"ok": True, "command": "sc broker holdings --json"}, id="data-missing"),
        pytest.param({"ok": True, "command": "sc broker holdings --json", "data": {}}, id="result-missing"),
        pytest.param({"ok": True, "command": "sc broker holdings --json", "data": {"result": {}}}, id="items-missing"),
        pytest.param({"ok": True, "command": "sc broker holdings --json", "data": {"result": {"count": 1, "items": "not-a-list"}}}, id="items-not-list"),
        pytest.param({"ok": True, "command": "sc broker holdings --json", "data": "not-a-dict"}, id="data-not-dict"),
    ],
)
def test_refresh_invalid_wrapper_structure_raises(monkeypatch, payload):
    """Ungueltige Wrapper-Struktur (ok=false, data/result/items fehlen oder falscher Typ)."""
    monkeypatch.setattr(sc_bridge, "_sc_available", lambda: True)
    run, _ = _make_run(json.dumps(payload))
    monkeypatch.setattr(sc_bridge.subprocess, "run", run)

    with pytest.raises(sc_bridge.ScInvalidJsonError):
        sc_bridge.refresh_from_sc()


def test_refresh_wrapper_empty_holdings_raises(monkeypatch, wrapper_transactions_payload):
    """Leere Holdings im Wrapper-Format -> ScEmptyDataError (fail-closed)."""
    monkeypatch.setattr(sc_bridge, "_sc_available", lambda: True)
    wrapper_holdings = {
        "ok": True,
        "command": "sc broker holdings --json",
        "data": {"result": {"count": 0, "items": []}},
    }
    run, _ = _make_run(json.dumps(wrapper_holdings), json.dumps(wrapper_transactions_payload))
    monkeypatch.setattr(sc_bridge.subprocess, "run", run)

    with pytest.raises(sc_bridge.ScEmptyDataError):
        sc_bridge.refresh_from_sc()


# --- update_config: kein Mock-Fallback, kein Schreiben im Fehlerfall ---


def test_error_path_never_falls_back_to_mock(monkeypatch):
    """Regression: fehlendes sc -> update_config wirft, liefert NIE Mock-Daten."""
    monkeypatch.setattr(sc_bridge, "_sc_available", lambda: False)

    with pytest.raises(sc_bridge.ScNotAvailableError):
        sc_bridge.update_config()


def test_update_config_writes_config_on_success(monkeypatch, tmp_path, portfolio, transactions):
    monkeypatch.setattr(sc_bridge, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(sc_bridge, "_sc_available", lambda: True)
    run, _ = _make_run(json.dumps(portfolio), json.dumps(transactions))
    monkeypatch.setattr(sc_bridge.subprocess, "run", run)

    got_portfolio, got_transactions = sc_bridge.update_config()

    assert got_portfolio == portfolio
    assert got_transactions == transactions
    assert json.loads((tmp_path / "portfolio.json").read_text(encoding="utf-8")) == portfolio
    assert json.loads((tmp_path / "transactions.json").read_text(encoding="utf-8")) == transactions


def test_update_config_does_not_write_on_error(monkeypatch, tmp_path):
    """Fail-closed: kein Schreiben von Config-Dateien (Demo/Mock) im Fehlerfall."""
    monkeypatch.setattr(sc_bridge, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(sc_bridge, "_sc_available", lambda: False)

    with pytest.raises(sc_bridge.ScNotAvailableError):
        sc_bridge.update_config()

    assert list(tmp_path.iterdir()) == []


# --- get_portfolio/get_transactions: read-only, definierter Fehler bei fehlender Config ---


def test_get_portfolio_reads_config_file(monkeypatch, tmp_path, portfolio):
    (tmp_path / "portfolio.json").write_text(json.dumps(portfolio), encoding="utf-8")
    monkeypatch.setattr(sc_bridge, "CONFIG_DIR", tmp_path)

    assert sc_bridge.get_portfolio() == portfolio


def test_get_transactions_reads_config_file(monkeypatch, tmp_path, transactions):
    (tmp_path / "transactions.json").write_text(json.dumps(transactions), encoding="utf-8")
    monkeypatch.setattr(sc_bridge, "CONFIG_DIR", tmp_path)

    assert sc_bridge.get_transactions() == transactions


def test_get_portfolio_missing_config_is_defined_error_and_read_only(monkeypatch, tmp_path):
    """Fehlende Config -> ConfigMissingError (auch FileNotFoundError), keine Datei wird erzeugt."""
    monkeypatch.setattr(sc_bridge, "CONFIG_DIR", tmp_path)

    with pytest.raises(sc_bridge.ConfigMissingError):
        sc_bridge.get_portfolio()
    with pytest.raises(FileNotFoundError):
        sc_bridge.get_transactions()

    assert list(tmp_path.iterdir()) == []  # read-only: nichts automatisch erzeugt


# --- Holdings-Normalisierung (echtes sc-Schema: valuation/valuation_currency) ---

SC_HOLDING = {
    "isin": "DE000A3E00M1",
    "name": "Ionos Group",
    "quantity": 10,
    "valuation": 331.7,
    "valuation_currency": "EUR",
    "security_type": "STOCK",
}


def test_normalize_holding_valuation_eur_becomes_value_eur():
    """Echtes sc-Schema: valuation EUR -> value_eur, kein kuenstliches category."""
    h = sc_bridge._normalize_holding(dict(SC_HOLDING), {})
    assert h["value_eur"] == 331.7
    assert h["valuation_currency"] == "EUR"  # Rohdaten bleiben erhalten


def test_normalize_holding_unmapped_is_unknown_not_satellite():
    """Nicht gemappter Einzelwert -> explizit "unknown", nie stillschweigend satellite."""
    h = sc_bridge._normalize_holding(dict(SC_HOLDING), {})
    assert h["category"] == "unknown"


def test_normalize_holding_lookup_category_by_isin():
    """Gemappter ETF -> category aus config/etf_lookup.json (ISIN), value_eur aus valuation."""
    lookup = {"IE00BK5BQT80": {"name": "Vanguard FTSE All-World UCITS ETF", "sector": "Diversified", "category": "core"}}
    h = sc_bridge._normalize_holding(
        {"isin": "IE00BK5BQT80", "name": "Vanguard FTSE All-World", "valuation": 1850.0, "valuation_currency": "EUR"},
        lookup,
    )
    assert h["category"] == "core"
    assert h["value_eur"] == 1850.0


def test_normalize_holding_existing_category_and_value_eur_kept():
    """Vorhandene Felder (z.B. Mock/Legacy) haben Vorrang — Normalisierung ist idempotent."""
    h = sc_bridge._normalize_holding(
        {"isin": "IE00BK5BQT80", "category": "satellite", "value_eur": 12.0, "valuation": 999.0, "valuation_currency": "EUR"},
        {"IE00BK5BQT80": {"category": "core"}},
    )
    assert h["value_eur"] == 12.0
    assert h["category"] == "satellite"


def test_normalize_holding_non_eur_valuation_has_no_value_eur():
    """valuation in Fremdwaehrung -> KEIN value_eur (keine falsche EUR-Behauptung)."""
    h = sc_bridge._normalize_holding({"isin": "US0378331005", "valuation": 300.0, "valuation_currency": "USD"}, {})
    assert "value_eur" not in h
    assert h["category"] == "unknown"


def test_normalize_holding_null_valuation_has_no_value_eur():
    """valuation null (fehlendes Quote) -> kein value_eur, category unknown."""
    h = sc_bridge._normalize_holding(
        {"isin": "LU2722255754", "name": "LU2722255754", "valuation": None, "valuation_currency": None},
        {},
    )
    assert "value_eur" not in h
    assert h["category"] == "unknown"


def test_refresh_wrapper_real_sc_schema_normalizes_holdings(monkeypatch, transactions):
    """Wrapper-Format mit echtem sc-Schema: value_eur/category zentral normalisiert."""
    monkeypatch.setattr(sc_bridge, "_sc_available", lambda: True)
    items = [
        {"isin": "IE00BK5BQT80", "name": "Vanguard FTSE All-World", "valuation": 1850.0, "valuation_currency": "EUR"},
        {"isin": "US0378331005", "name": "Apple Inc.", "valuation": 2400.0, "valuation_currency": "EUR"},
    ]
    wrapper = {"ok": True, "command": "sc broker holdings --json", "data": {"result": {"count": 2, "items": items}}}
    run, _ = _make_run(json.dumps(wrapper), json.dumps(transactions))
    monkeypatch.setattr(sc_bridge.subprocess, "run", run)

    got_portfolio, _ = sc_bridge.refresh_from_sc()

    by_isin = {h["isin"]: h for h in got_portfolio["holdings"]}
    assert by_isin["IE00BK5BQT80"]["value_eur"] == 1850.0
    assert by_isin["IE00BK5BQT80"]["category"] == "core"  # Lookup-Mapping
    assert by_isin["US0378331005"]["value_eur"] == 2400.0
    assert by_isin["US0378331005"]["category"] == "unknown"  # ungemappt


def test_refresh_wrapper_aggregates_total_only_if_complete(monkeypatch, transactions):
    """Ohne Summenfeld: total_value_eur nur aggregieren, wenn JEDE Holding einen EUR-Wert hat."""
    monkeypatch.setattr(sc_bridge, "_sc_available", lambda: True)
    complete = [
        {"isin": "A", "valuation": 100.0, "valuation_currency": "EUR"},
        {"isin": "B", "valuation": 200.0, "valuation_currency": "EUR"},
    ]
    wrapper_complete = {"ok": True, "command": "sc broker holdings --json", "data": {"result": {"count": 2, "items": complete}}}
    run, _ = _make_run(json.dumps(wrapper_complete), json.dumps(transactions))
    monkeypatch.setattr(sc_bridge.subprocess, "run", run)

    got_portfolio, _ = sc_bridge.refresh_from_sc()
    assert got_portfolio["total_value_eur"] == 300.0

    incomplete = [
        {"isin": "A", "valuation": 100.0, "valuation_currency": "EUR"},
        {"isin": "B", "valuation": None, "valuation_currency": None},
    ]
    wrapper_incomplete = {"ok": True, "command": "sc broker holdings --json", "data": {"result": {"count": 2, "items": incomplete}}}
    run, _ = _make_run(json.dumps(wrapper_incomplete), json.dumps(transactions))
    monkeypatch.setattr(sc_bridge.subprocess, "run", run)

    got_portfolio, _ = sc_bridge.refresh_from_sc()
    assert "total_value_eur" not in got_portfolio  # unvollstaendig -> nicht autoritativ
