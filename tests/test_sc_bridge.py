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


# --- Watchlist: load_mock_watchlist (Dry-Run) + fetch_watchlist_from_sc (fail-closed) ---


def test_load_mock_watchlist_returns_mock_data(watchlist):
    """Mock-Watchlist wird wie der produktive Pfad normalisiert (value_eur/category ergaenzt)."""
    loaded = sc_bridge.load_mock_watchlist()
    assert len(loaded) == len(watchlist)
    assert loaded[0]["isin"] == watchlist[0]["isin"]
    assert loaded[0]["ticker"] == "NVDA"  # vorhandener Ticker bleibt
    assert loaded[0]["value_eur"] == 1250.0  # EUR-valuation -> value_eur
    assert loaded[0]["category"] == "unknown"  # ungemappt -> unknown
    assert loaded[4]["ticker"] == "MMM"


def test_load_mock_watchlist_missing_file_returns_empty(monkeypatch, tmp_path):
    monkeypatch.setattr(sc_bridge, "MOCK_WATCHLIST", tmp_path / "nope.json")

    assert sc_bridge.load_mock_watchlist() == []


def test_load_mock_watchlist_corrupt_returns_empty(monkeypatch, tmp_path):
    corrupt = tmp_path / "watchlist.json"
    corrupt.write_text("not json", encoding="utf-8")
    monkeypatch.setattr(sc_bridge, "MOCK_WATCHLIST", corrupt)

    assert sc_bridge.load_mock_watchlist() == []


def test_load_mock_watchlist_non_list_returns_empty(monkeypatch, tmp_path):
    not_list = tmp_path / "watchlist.json"
    not_list.write_text(json.dumps({"holdings": []}), encoding="utf-8")
    monkeypatch.setattr(sc_bridge, "MOCK_WATCHLIST", not_list)

    assert sc_bridge.load_mock_watchlist() == []


def test_fetch_watchlist_legacy_list(monkeypatch, watchlist):
    """Legacy-Format: direkte Liste. Items werden normalisiert (value_eur/category ergaenzt)."""
    monkeypatch.setattr(sc_bridge, "_sc_available", lambda: True)
    run, calls = _make_run(json.dumps(watchlist))
    monkeypatch.setattr(sc_bridge.subprocess, "run", run)

    got = sc_bridge.fetch_watchlist_from_sc()

    assert len(got) == len(watchlist)
    assert got[0]["isin"] == watchlist[0]["isin"]
    assert got[0]["ticker"] == "NVDA"
    assert got[0]["value_eur"] == 1250.0
    assert calls == [["sc", "broker", "watchlist", "--json"]]


def test_fetch_watchlist_empty_list_is_valid(monkeypatch):
    """Leere Watchlist ist ein legitimer Zustand (kein ScEmptyDataError)."""
    monkeypatch.setattr(sc_bridge, "_sc_available", lambda: True)
    run, _ = _make_run(json.dumps([]))
    monkeypatch.setattr(sc_bridge.subprocess, "run", run)

    assert sc_bridge.fetch_watchlist_from_sc() == []


def test_fetch_watchlist_wrapper_format(monkeypatch, watchlist):
    """CLI-Wrapper-Format: Items kommen aus data.result.items."""
    monkeypatch.setattr(sc_bridge, "_sc_available", lambda: True)
    wrapper = {
        "ok": True,
        "command": "sc broker watchlist --json",
        "data": {"result": {"count": len(watchlist), "items": watchlist}},
    }
    run, _ = _make_run(json.dumps(wrapper))
    monkeypatch.setattr(sc_bridge.subprocess, "run", run)

    got = sc_bridge.fetch_watchlist_from_sc()

    assert len(got) == len(watchlist)
    assert got[0]["isin"] == watchlist[0]["isin"]
    assert got[0]["value_eur"] == 1250.0  # normalisiert
    assert got[4]["ticker"] == "MMM"


def test_fetch_watchlist_missing_sc_raises(monkeypatch):
    monkeypatch.setattr(sc_bridge, "_sc_available", lambda: False)

    with pytest.raises(sc_bridge.ScNotAvailableError):
        sc_bridge.fetch_watchlist_from_sc()


def test_fetch_watchlist_called_process_error_raises(monkeypatch):
    monkeypatch.setattr(sc_bridge, "_sc_available", lambda: True)

    def _run_fails(cmd, *args, **kwargs):
        raise subprocess.CalledProcessError(returncode=1, cmd=cmd)

    monkeypatch.setattr(sc_bridge.subprocess, "run", _run_fails)

    with pytest.raises(sc_bridge.ScCommandError):
        sc_bridge.fetch_watchlist_from_sc()


def test_fetch_watchlist_invalid_json_raises(monkeypatch):
    monkeypatch.setattr(sc_bridge, "_sc_available", lambda: True)
    run, _ = _make_run("not json")
    monkeypatch.setattr(sc_bridge.subprocess, "run", run)

    with pytest.raises(sc_bridge.ScInvalidJsonError):
        sc_bridge.fetch_watchlist_from_sc()


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param({"ok": False, "command": "sc broker watchlist --json", "data": {"result": {"count": 0, "items": []}}}, id="ok-false"),
        pytest.param({"ok": True, "command": "sc broker watchlist --json", "data": {"result": {}}}, id="items-missing"),
        pytest.param({"ok": True, "command": "sc broker watchlist --json", "data": {"result": {"count": 1, "items": "not-a-list"}}}, id="items-not-list"),
        pytest.param({"ok": True, "command": "sc broker watchlist --json", "data": "not-a-dict"}, id="data-not-dict"),
        pytest.param({"ok": True, "command": "sc broker watchlist --json"}, id="data-missing"),
    ],
)
def test_fetch_watchlist_invalid_wrapper_structure_raises(monkeypatch, payload):
    """Ungueltige Wrapper-Struktur -> ScInvalidJsonError (fail-closed)."""
    monkeypatch.setattr(sc_bridge, "_sc_available", lambda: True)
    run, _ = _make_run(json.dumps(payload))
    monkeypatch.setattr(sc_bridge.subprocess, "run", run)

    with pytest.raises(sc_bridge.ScInvalidJsonError):
        sc_bridge.fetch_watchlist_from_sc()


def test_fetch_watchlist_auth_error_raises_specific(monkeypatch):
    """Auth-Marker in stderr -> spezifische Exception statt generischem ScCommandError."""
    monkeypatch.setattr(sc_bridge, "_sc_available", lambda: True)
    stderr = json.dumps({"ok": False, "error": {"code": "no_session", "message": "msg"}})
    monkeypatch.setattr(sc_bridge.subprocess, "run", _run_fails_with_stderr(stderr))

    with pytest.raises(sc_bridge.ScSessionExpiredError):
        sc_bridge.fetch_watchlist_from_sc()


def test_fetch_watchlist_never_falls_back_to_mock(monkeypatch, watchlist):
    """Regression: produktiver Abruf liefert NIE Mock-Daten — auch nicht bei sc-Ausfall."""
    monkeypatch.setattr(sc_bridge, "_sc_available", lambda: False)

    with pytest.raises(sc_bridge.ScNotAvailableError):
        sc_bridge.fetch_watchlist_from_sc()


# --- Watchlist-Normalisierung (value_eur/category/ticker, idempotent) ---


def test_normalize_watchlist_valuation_eur_becomes_value_eur():
    """Echtes sc-Schema: EUR-valuation -> value_eur, Rohdaten bleiben erhalten."""
    item = sc_bridge._normalize_watchlist_item(
        {"isin": "US5949724083", "name": "NVIDIA Corp.", "quantity": 0, "valuation": 1250.0, "valuation_currency": "EUR"},
        {},
    )
    assert item["value_eur"] == 1250.0
    assert item["valuation_currency"] == "EUR"


def test_normalize_watchlist_unmapped_is_unknown_not_satellite():
    """Nicht gemappter Einzelwert -> explizit "unknown", nie stillschweigend satellite."""
    item = sc_bridge._normalize_watchlist_item({"isin": "US5949724083"}, {})
    assert item["category"] == "unknown"
    assert item["ticker"] == ""


def test_normalize_watchlist_lookup_category_by_isin():
    """Gemappter ETF -> category aus config/etf_lookup.json (ISIN)."""
    item = sc_bridge._normalize_watchlist_item(
        {"isin": "IE00BK5BQT80", "name": "Vanguard FTSE All-World", "valuation": 5000.0, "valuation_currency": "EUR"},
        {"IE00BK5BQT80": {"name": "Vanguard FTSE All-World UCITS ETF", "sector": "Diversified", "category": "core"}},
    )
    assert item["category"] == "core"  # Lookup-Mapping
    assert item["value_eur"] == 5000.0


def test_normalize_watchlist_existing_fields_kept():
    """Vorhandene Felder (ticker/category/value_eur) haben Vorrang — idempotent."""
    item = sc_bridge._normalize_watchlist_item(
        {"isin": "US5949724083", "ticker": "NVDA", "category": "satellite", "value_eur": 12.0, "valuation": 999.0, "valuation_currency": "EUR"},
        {},
    )
    assert item["value_eur"] == 12.0
    assert item["category"] == "satellite"
    assert item["ticker"] == "NVDA"


def test_normalize_watchlist_non_eur_valuation_has_no_value_eur():
    """valuation in Fremdwaehrung -> KEIN value_eur (keine falsche EUR-Behauptung)."""
    item = sc_bridge._normalize_watchlist_item({"isin": "US0378331005", "valuation": 300.0, "valuation_currency": "USD"}, {})
    assert "value_eur" not in item


def test_normalize_watchlist_null_valuation_has_no_value_eur():
    """valuation null (fehlendes Quote) -> kein value_eur, category unknown."""
    item = sc_bridge._normalize_watchlist_item(
        {"isin": "LU2722255754", "valuation": None, "valuation_currency": None},
        {},
    )
    assert "value_eur" not in item
    assert item["category"] == "unknown"


def test_fetch_watchlist_wrapper_normalizes_real_schema(monkeypatch):
    """Wrapper-Format mit echtem sc-Schema: value_eur/category/ticker zentral normalisiert."""
    monkeypatch.setattr(sc_bridge, "_sc_available", lambda: True)
    items = [
        {"isin": "IE00BK5BQT80", "name": "Vanguard FTSE All-World", "quantity": 0, "valuation": 5000.0, "valuation_currency": "EUR"},
        {"isin": "US0378331005", "name": "Apple Inc.", "quantity": 0, "valuation": 2400.0, "valuation_currency": "EUR"},
        {"isin": "US5949724083", "name": "NVIDIA Corp.", "ticker": "NVDA", "quantity": 0, "valuation": None, "valuation_currency": None},
    ]
    wrapper = {"ok": True, "command": "sc broker watchlist --json", "data": {"result": {"count": 3, "items": items}}}
    run, _ = _make_run(json.dumps(wrapper))
    monkeypatch.setattr(sc_bridge.subprocess, "run", run)

    got = sc_bridge.fetch_watchlist_from_sc()

    by_isin = {i["isin"]: i for i in got}
    assert by_isin["IE00BK5BQT80"]["value_eur"] == 5000.0
    assert by_isin["IE00BK5BQT80"]["category"] == "core"  # Lookup-Mapping
    assert by_isin["US0378331005"]["value_eur"] == 2400.0
    assert by_isin["US0378331005"]["category"] == "unknown"  # ungemappt
    assert "value_eur" not in by_isin["US5949724083"]  # null-valuation -> kein value_eur
    assert by_isin["US5949724083"]["ticker"] == "NVDA"  # vorhandener Ticker bleibt
    assert "ticker" in by_isin["IE00BK5BQT80"] and by_isin["IE00BK5BQT80"]["ticker"] == ""  # fehlender Ticker -> ""


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


# --- Differenzierte Auth-Fehler (no_session / REFRESH_RELOGIN_REQUIRED / secret_storage_unavailable) ---


def _run_fails_with_stderr(stderr: str):
    """Mock fuer subprocess.run: non-zero Exit mit stderr-Marker (Auth-Fehler)."""

    def _run(cmd, *args, **kwargs):
        raise subprocess.CalledProcessError(returncode=1, cmd=cmd, stderr=stderr)

    return _run


@pytest.mark.parametrize(
    ("marker", "exc_cls"),
    [
        ("no_session", sc_bridge.ScSessionExpiredError),
        ("REFRESH_RELOGIN_REQUIRED", sc_bridge.ScReloginRequiredError),
        ("secret_storage_unavailable", sc_bridge.ScSecretStorageError),
    ],
)
def test_refresh_from_sc_auth_error_in_stderr_raises_specific(monkeypatch, marker, exc_cls):
    """Auth-Marker in stderr -> spezifische Exception statt generischem ScCommandError."""
    monkeypatch.setattr(sc_bridge, "_sc_available", lambda: True)
    stderr = json.dumps({"ok": False, "error": {"code": marker, "message": "msg"}})
    monkeypatch.setattr(sc_bridge.subprocess, "run", _run_fails_with_stderr(stderr))

    with pytest.raises(exc_cls):
        sc_bridge.refresh_from_sc()


@pytest.mark.parametrize(
    ("code", "exc_cls"),
    [
        ("no_session", sc_bridge.ScSessionExpiredError),
        ("REFRESH_RELOGIN_REQUIRED", sc_bridge.ScReloginRequiredError),
        ("secret_storage_unavailable", sc_bridge.ScSecretStorageError),
    ],
)
def test_refresh_from_sc_wrapper_ok_false_auth_code_raises_specific(monkeypatch, transactions, code, exc_cls):
    """Wrapper-Format mit ok=false + error.code-Auth-Marker -> spezifische Exception."""
    monkeypatch.setattr(sc_bridge, "_sc_available", lambda: True)
    payload = {"ok": False, "command": "sc broker holdings --json", "error": {"code": code, "message": "..."}}
    run, _ = _make_run(json.dumps(payload), json.dumps(transactions))
    monkeypatch.setattr(sc_bridge.subprocess, "run", run)

    with pytest.raises(exc_cls):
        sc_bridge.refresh_from_sc()


def test_auth_error_classes_are_sc_bridge_errors():
    """Alle Auth-Fehlerklassen sind ScBridgeError-Subklassen (fail-closed kompatibel)."""
    for cls in (sc_bridge.ScSessionExpiredError, sc_bridge.ScReloginRequiredError, sc_bridge.ScSecretStorageError):
        assert issubclass(cls, sc_bridge.ScBridgeError)


def test_auth_error_message_is_action_oriented_and_secret_free():
    """Alert-Messages nennen die Aktion (`sc login`) und keine Secrets/User-Daten."""
    for exc in (
        sc_bridge.ScSessionExpiredError("sc session expired (no_session) — run interactive `sc login`"),
        sc_bridge.ScReloginRequiredError("sc session expired (REFRESH_RELOGIN_REQUIRED) — run interactive `sc login`"),
        sc_bridge.ScSecretStorageError("sc secret storage unavailable (secret_storage_unavailable) — check system/keyring configuration"),
    ):
        for forbidden in ("token", "password", "email", "session.json"):
            assert forbidden not in str(exc)


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
