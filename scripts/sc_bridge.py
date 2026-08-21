"""Bridge to scalable.capital broker CLI (fail-closed).

Produktiver Abruf ausschliesslich ueber refresh_from_sc(): frischer Live-Abruf
von `sc broker holdings --json` und `sc broker transactions --json`. Akzeptiert
sowohl das Legacy-Format (Holdings-Dict mit "holdings", Transactions-Liste) als
auch das CLI-Wrapper-Format {ok, command, data: {..., result: {count, items}}}.
Bei fehlendem sc, non-zero Exit, ungueltigem JSON, ok=false, fehlender Struktur,
ungueltigen items oder leerer Holdings-Liste wird eine definierte
ScBridgeError-Subklasse geworfen — niemals Mock-Fallback.

Watchlist (Plan Phase 2): separater Live-Abruf ueber fetch_watchlist_from_sc()
(`sc broker watchlist --json`), gleiche Wrapper-/Legacy-Toleranz wie Holdings.
Leere Watchlist ist ein legitimer Zustand -> [] ohne Exception (kein
ScEmptyDataError). Items werden zentral normalisiert (normalize_watchlist_items):
value_eur aus vorhandenem value_eur-Feld oder aus valuation mit
valuation_currency EUR; category aus vorhandenem category-Feld oder aus
config/etf_lookup.json (ISIN), sonst explizit "unknown".

Holdings werden zentral normalisiert (normalize_holdings): value_eur kommt aus
dem vorhandenen value_eur-Feld oder aus valuation mit valuation_currency EUR
(nicht aus Nicht-EUR-Werten); die Core/Satellite-Kategorie kommt aus dem
vorhandenen category-Feld oder aus config/etf_lookup.json (ISIN), sonst
explizit "unknown" — es wird nie stillschweigend "satellite" behauptet.
Summenfelder (total_value_eur, cash_eur) werden aus dem Payload uebernommen
oder — nur wenn fachlich eindeutig, d.h. jede Holding hat einen EUR-Wert —
aus den Holdings aggregiert. Es werden keine Rohdaten geloggt.

Mock-Daten gibt es nur ueber load_mock()/load_mock_watchlist() und nur fuer
Dry-Run (tests/mock_data/). get_portfolio()/get_transactions() sind
read-only-Leser der config/-Dateien und erzeugen nichts automatisch.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
MOCK_PORTFOLIO = ROOT / "tests" / "mock_data" / "portfolio.json"
MOCK_TRANSACTIONS = ROOT / "tests" / "mock_data" / "transactions.json"
MOCK_WATCHLIST = ROOT / "tests" / "mock_data" / "watchlist.json"


class ScBridgeError(Exception):
    """Basis fuer alle sc_bridge-Fehler (fail-closed, kein Mock-Fallback)."""


class ScNotAvailableError(ScBridgeError):
    """sc CLI nicht im PATH gefunden."""


class ScCommandError(ScBridgeError):
    """sc-Prozess endete mit non-zero Exit-Code."""


class ScSessionExpiredError(ScBridgeError):
    """sc-Session abgelaufen (no_session) — interaktives `sc login` erforderlich."""


class ScReloginRequiredError(ScBridgeError):
    """sc-Session erfordert Re-Login (REFRESH_RELOGIN_REQUIRED) — `sc login` erforderlich."""


class ScSecretStorageError(ScBridgeError):
    """sc Secret Storage nicht verfuegbar (secret_storage_unavailable) — System-Pruefung."""


class ScInvalidJsonError(ScBridgeError):
    """sc-Ausgabe war kein gueltiges JSON (oder unerwartete Form)."""


class ScEmptyDataError(ScBridgeError):
    """sc lieferte eine leere Holdings-Liste."""


class ConfigMissingError(ScBridgeError, FileNotFoundError):
    """config/-Datei fehlt — get_portfolio()/get_transactions() sind read-only."""


def _sc_available() -> bool:
    return shutil.which("sc") is not None


# Auth-Fehlerklassen der scalable-cli (Quelle: github.com/ScalableCapital/scalable-cli,
# Issue #5): no_session / REFRESH_RELOGIN_REQUIRED / secret_storage_unavailable.
_AUTH_MARKERS: tuple[tuple[str, type[ScBridgeError]], ...] = (
    ("REFRESH_RELOGIN_REQUIRED", ScReloginRequiredError),
    ("no_session", ScSessionExpiredError),
    ("secret_storage_unavailable", ScSecretStorageError),
)

_AUTH_MESSAGES: dict[str, str] = {
    "REFRESH_RELOGIN_REQUIRED": "sc session expired (REFRESH_RELOGIN_REQUIRED) — run interactive `sc login`",
    "no_session": "sc session expired (no_session) — run interactive `sc login`",
    "secret_storage_unavailable": "sc secret storage unavailable (secret_storage_unavailable) — check system/keyring configuration",
}


def _auth_error_from_text(text: str) -> ScBridgeError | None:
    """Erkennt auth-spezifische sc-Fehlerklassen in stdout/stderr-Text.

    Liefert die passende Exception-Instanz oder None (kein Auth-Marker).
    Messages enthalten nur Status-Strings — niemals Session-/Token-/User-Daten.
    """
    for marker, exc_cls in _AUTH_MARKERS:
        if marker in text:
            return exc_cls(_AUTH_MESSAGES[marker])
    return None


def _run_sc(args: list[str]) -> str:
    """Fuehrt sc aus und liefert stdout als Text; non-zero Exit -> ScCommandError.

    Auth-spezifische CLI-Fehler (no_session / REFRESH_RELOGIN_REQUIRED /
    secret_storage_unavailable) in stdout/stderr werden als spezifische
    ScBridgeError-Subklasse geworfen (differenzierte, handlungsorientierte
    Fehler statt generischem ScCommandError).
    """
    try:
        result = subprocess.run(
            ["sc", *args],
            capture_output=True,
            text=True,
            check=True,
        )
    except subprocess.CalledProcessError as e:
        auth_error = _auth_error_from_text(f"{e.stderr or ''}\n{e.stdout or ''}")
        if auth_error is not None:
            raise auth_error from e
        raise ScCommandError(f"sc {' '.join(args)} exited with code {e.returncode}") from e
    return result.stdout


def _parse_json(stdout: str, source: str) -> dict | list:
    try:
        return json.loads(stdout)
    except json.JSONDecodeError as e:
        raise ScInvalidJsonError(f"invalid JSON from {source}") from e


def _write_json(path: Path, data: dict | list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def load_etf_lookup() -> dict:
    """Read-only: config/etf_lookup.json (fehlend/korrupt -> leeres Dict).

    Reine Metadaten-Anreicherung (category/sector); ein fehlender Lookup darf
    den produktiven Abruf nicht blockieren — Holdings bleiben dann "unknown".
    """
    try:
        with open(CONFIG_DIR / "etf_lookup.json", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _as_float(value: object) -> float | None:
    """Float-Konvertierung; None/bool/nicht-numerisch -> None (kein Crash)."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float, str)):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _normalize_holding(holding: dict, lookup: dict) -> dict:
    """Normalisiert eine Holding auf value_eur + category (Rest bleibt erhalten).

    value_eur: vorhandenes value_eur-Feld hat Vorrang; sonst wird valuation nur
    uebernommen, wenn valuation_currency "EUR" ist. Fehlende/Nicht-EUR-Werte
    erzeugen kein value_eur (nachgelagert 0 — analyze fail-closed).
    category: vorhandenes category-Feld hat Vorrang; sonst Lookup per ISIN;
    sonst explizit "unknown" statt stillschweigend "satellite".
    """
    normalized = dict(holding)
    existing = _as_float(normalized.get("value_eur"))
    if existing is not None:
        normalized["value_eur"] = existing
    else:
        valuation = _as_float(normalized.get("valuation"))
        if valuation is not None and normalized.get("valuation_currency") == "EUR":
            normalized["value_eur"] = valuation
    category = normalized.get("category")
    if not category:
        entry = lookup.get(normalized.get("isin")) if isinstance(lookup, dict) else None
        if isinstance(entry, dict):
            category = entry.get("category")
    normalized["category"] = category or "unknown"
    return normalized


def normalize_holdings(holdings: list) -> list:
    """Zentrale Holdings-Normalisierung (Wrapper- und Legacy-Format).

    Idempotent: vorhandene value_eur/category-Felder (z.B. Mock-Daten) bleiben
    unveraendert. Erzeugt keine Rohdaten-Logs.
    """
    lookup = load_etf_lookup()
    return [_normalize_holding(h, lookup) for h in holdings]


def _normalize_watchlist_item(item: dict, lookup: dict) -> dict:
    """Normalisiert ein Watchlist-Item auf ein einheitliches Signal-Schema.

    Ergaenzt value_eur (vorhandenes Feld oder EUR-valuation), category
    (vorhandenes Feld oder Lookup per ISIN, sonst "unknown") und ticker
    (vorhandenes Feld, sonst leer). quantity/valuation bleiben als Rohdaten
    erhalten (meist 0/None bei Watchlist-Positionen). Rest bleibt erhalten.
    """
    normalized = dict(item)
    existing = _as_float(normalized.get("value_eur"))
    if existing is not None:
        normalized["value_eur"] = existing
    else:
        valuation = _as_float(normalized.get("valuation"))
        if valuation is not None and normalized.get("valuation_currency") == "EUR":
            normalized["value_eur"] = valuation
    category = normalized.get("category")
    if not category:
        entry = lookup.get(normalized.get("isin")) if isinstance(lookup, dict) else None
        if isinstance(entry, dict):
            category = entry.get("category")
    normalized["category"] = category or "unknown"
    if "ticker" not in normalized or not normalized.get("ticker"):
        normalized["ticker"] = ""
    return normalized


def normalize_watchlist_items(items: list) -> list:
    """Zentrale Watchlist-Normalisierung (Wrapper- und Legacy-Format).

    Idempotent: vorhandene value_eur/category/ticker-Felder (z.B. Mock-Daten)
    bleiben unveraendert. Erzeugt keine Rohdaten-Logs.
    """
    lookup = load_etf_lookup()
    return [_normalize_watchlist_item(i, lookup) for i in items]


def _aggregate_total_value_eur(holdings: list) -> float | None:
    """Summe aus Holdings, nur wenn fachlich eindeutig (jede Holding hat EUR-Wert).

    Fehlt auch nur eine value_eur (z.B. valuation null), ist die Summe
    unvollstaendig und wird nicht als autoritatives Summenfeld ausgegeben.
    """
    total = 0.0
    for h in holdings:
        value = _as_float(h.get("value_eur"))
        if value is None:
            return None
        total += value
    return round(total, 2)


def _extract_items(payload: dict | list, source: str, allow_list: bool = True) -> list:
    """Item-Liste aus Legacy- oder CLI-Wrapper-Format (fail-closed, kein Mock).

    Legacy-Holdings: {"holdings": [...]}; Legacy-Transactions: direkte Liste
    (nur wenn allow_list=True, bei Holdings ist eine Liste ungueltig).
    Wrapper-Format: {ok: true, command, data: {..., result: {count, items}}} —
    Items kommen aus data.result.items; ok=false, fehlende data/result-Struktur
    oder ungueltige items -> ScInvalidJsonError.
    """
    if isinstance(payload, list):
        if allow_list:
            return payload  # Legacy-Transactions: direkte Liste
        raise ScInvalidJsonError(f"unexpected JSON shape from {source}")
    if not isinstance(payload, dict):
        raise ScInvalidJsonError(f"unexpected JSON shape from {source}")
    if "holdings" in payload:
        holdings = payload.get("holdings")
        if not isinstance(holdings, list):
            raise ScInvalidJsonError(f"unexpected JSON shape from {source}")
        return holdings  # Legacy-Holdings
    # CLI-Wrapper-Format
    if payload.get("ok") is not True:
        auth_error = _auth_error_from_text(json.dumps(payload))
        if auth_error is not None:
            raise auth_error
        raise ScInvalidJsonError(f"sc command failed (ok != true) from {source}")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise ScInvalidJsonError(f"unexpected JSON shape from {source}")
    result = data.get("result")
    if not isinstance(result, dict):
        raise ScInvalidJsonError(f"unexpected JSON shape from {source}")
    items = result.get("items")
    if not isinstance(items, list):
        raise ScInvalidJsonError(f"unexpected JSON shape from {source}")
    return items


# --- Transaktions-Normalisierung (Plan §9) -----------------------------------
#
# Das rohe sc-Transaktionsschema ist unnormalisiert: SECURITY_TRANSACTION /
# CASH_TRANSACTION gemischt, side/type/security_transaction_type mehrdeutig,
# amount-Vorzeichen unterschiedlich interpretiert, is_cancellation getrennt.
# normalize_transactions erzeugt daraus ein einheitliches, periodisiertes
# Schema pro Ereignis (Basis fuer Wochenvergleich + Turnover).

_TRANSACTION_TYPE_BUY = "kauf"
_TRANSACTION_TYPE_SELL = "verkauf"
_TRANSACTION_TYPE_SAVINGS_PLAN = "sparplan"
_TRANSACTION_TYPE_DEPOSIT = "einzahlung"
_TRANSACTION_TYPE_DISTRIBUTION = "ausschuettung"
_TRANSACTION_TYPE_OTHER = "sonstiges"


def _txn_executed_at(txn: dict) -> str:
    """ISO-Zeitpunkt (last_event_datetime), Fallback auf leeren String."""
    value = txn.get("last_event_datetime")
    return str(value) if value else ""


def _txn_side(txn: dict) -> str:
    """Seite (BUY/SELL/other) aus side; bei CASH-Typen 'other'."""
    side = txn.get("side")
    if side in ("BUY", "SELL"):
        return side
    return "other"


def _txn_transaction_type(txn: dict) -> str:
    """Normalisierter Transaktionstyp (kauf/verkauf/sparplan/einzahlung/...)."""
    txn_type = txn.get("type")
    if txn_type == "CASH_TRANSACTION":
        cash_type = txn.get("cash_transaction_type")
        if cash_type == "DEPOSIT":
            return _TRANSACTION_TYPE_DEPOSIT
        if cash_type == "DISTRIBUTION":
            return _TRANSACTION_TYPE_DISTRIBUTION
        return _TRANSACTION_TYPE_OTHER
    side = _txn_side(txn)
    sec_type = txn.get("security_transaction_type")
    if sec_type == "SAVINGS_PLAN":
        return _TRANSACTION_TYPE_SAVINGS_PLAN
    if side == "BUY":
        return _TRANSACTION_TYPE_BUY
    if side == "SELL":
        return _TRANSACTION_TYPE_SELL
    return _TRANSACTION_TYPE_OTHER


def _txn_isin(txn: dict) -> str:
    """ISIN: direktes Feld oder related_isin bei Cash-Transaktionen."""
    isin = txn.get("isin") or txn.get("related_isin")
    return str(isin) if isin else ""


def _txn_quantity(txn: dict) -> float:
    value = _as_float(txn.get("quantity"))
    return value if value is not None else 0.0


def _txn_price_eur(txn: dict) -> float | None:
    """Preis je Einheit (EUR): limit_price > quote_mid_price > abgeleitet.

    Bei SECURITY_TRANSACTION ist limit_price der Ausfuehrungspreis, sonst
    fallback auf quote_mid_price. Fehlt beides, wird der Preis aus
    amount/quantity abgeleitet (nur wenn quantity > 0).
    """
    for key in ("limit_price", "quote_mid_price", "price_eur", "price"):
        value = _as_float(txn.get(key))
        if value is not None and value > 0:
            return value
    quantity = _txn_quantity(txn)
    amount = _as_float(txn.get("amount"))
    if quantity > 0 and amount is not None and abs(amount) > 0:
        return round(abs(amount) / quantity, 6)
    return None


def _txn_amount_eur(txn: dict) -> float:
    """Transaktionsvolumen (EUR): amount oder quantity * price (absolut)."""
    amount = _as_float(txn.get("amount"))
    if amount is not None:
        return abs(amount)
    price = _txn_price_eur(txn)
    if price is not None:
        return round(price * _txn_quantity(txn), 2)
    return 0.0


def _txn_currency(txn: dict) -> str:
    value = txn.get("currency")
    return str(value) if value else "EUR"


def normalize_transactions(transactions: list) -> list:
    """Normalisiert Roh-Transaktionen auf ein einheitliches Schema (Plan §9).

    Pro Ereignis ein Eintrag mit einheitlichen Feldern:
      isin, side (BUY/SELL/other), quantity, price_eur, amount_eur, currency,
      executed_at (ISO), transaction_type (kauf/verkauf/sparplan/einzahlung/
      ausschuettung/sonstiges), cancelled (bool), source_id (Original-ID).

    Idempotent: bereits normalisierte Eintraege (mit "transaction_type" und
    "executed_at") bleiben unveraendert (z.B. Mock-Daten im Legacy-Format
    mit "date"/"type": "buy" bleiben fuer den Turnover lesbar).
    """
    normalized: list[dict] = []
    for txn in transactions:
        if not isinstance(txn, dict):
            continue
        # Bereits normalisiert: echtes neues Schema (transaction_type + executed_at)
        # ODER Legacy-Mock-Format (date + type buy/sell) — beides bleibt unveraendert.
        if txn.get("transaction_type") and txn.get("executed_at"):
            normalized.append(txn)
            continue
        if txn.get("date") and txn.get("type") in ("buy", "sell"):
            normalized.append(txn)
            continue
        entry = {
            "isin": _txn_isin(txn),
            "side": _txn_side(txn),
            "quantity": _txn_quantity(txn),
            "price_eur": _txn_price_eur(txn),
            "amount_eur": _txn_amount_eur(txn),
            "currency": _txn_currency(txn),
            "executed_at": _txn_executed_at(txn),
            "transaction_type": _txn_transaction_type(txn),
            "cancelled": bool(txn.get("is_cancellation", False)),
            "source_id": str(txn.get("id", "")),
        }
        normalized.append(entry)
    return normalized


def _portfolio_from_payload(payload: dict | list, holdings: list) -> dict:
    """Normalisiert den Holdings-Payload auf das Portfolio-Legacy-Format.

    Legacy {"holdings": [...]} -> Payload mit bereits normalisierten Holdings.
    Wrapper-Format -> dict mit "holdings" plus Summenfeldern: vorhandene Werte
    aus data werden uebernommen; fehlen sie, wird total_value_eur nur
    aggregiert, wenn jede Holding einen EUR-Wert hat (fachlich eindeutig).
    """
    if isinstance(payload, dict) and "holdings" in payload:
        portfolio = dict(payload)
        portfolio["holdings"] = holdings  # bereits normalisiert
        return portfolio
    portfolio: dict = {"holdings": holdings}
    data = payload.get("data") if isinstance(payload, dict) else None
    summary_source = data if isinstance(data, dict) else payload
    if isinstance(summary_source, dict):
        for key in ("total_value_eur", "cash_eur", "total_value", "cash"):
            if key in summary_source and summary_source[key] is not None:
                portfolio[key] = summary_source[key]
    if "total_value_eur" not in portfolio and "total_value" not in portfolio:
        aggregated = _aggregate_total_value_eur(holdings)
        if aggregated is not None:
            portfolio["total_value_eur"] = aggregated
    return portfolio


def refresh_from_sc() -> tuple[dict, list]:
    """Frischer Live-Abruf von `sc broker holdings --json` + `sc broker transactions --json`.

    Akzeptiert das Legacy-Format (Holdings-Dict mit "holdings", Transactions-
    Liste) sowie das CLI-Wrapper-Format {ok, command, data: {..., result:
    {count, items}}}: Holdings und Transactions kommen dann aus data.result.items.
    Holdings werden zentral normalisiert (value_eur aus EUR-valuation,
    category aus Lookup/unknown — siehe Modul-Docstring).

    Fail-closed: fehlendes sc, non-zero Exit, ungueltiges JSON, ok=false,
    fehlende Struktur, ungueltige items oder leere Holdings-Liste ->
    definierte ScBridgeError-Subklasse. Es gibt KEINEN Mock-Fallback —
    Mock-Daten nur ueber load_mock() (Dry-Run).
    """
    if not _sc_available():
        raise ScNotAvailableError("sc CLI not found in PATH")
    holdings_payload = _parse_json(_run_sc(["broker", "holdings", "--json"]), "sc broker holdings --json")
    holdings = _extract_items(holdings_payload, "sc broker holdings --json", allow_list=False)
    if not holdings:
        raise ScEmptyDataError("sc returned an empty holdings list")
    holdings = normalize_holdings(holdings)
    transactions_payload = _parse_json(_run_sc(["broker", "transactions", "--json"]), "sc broker transactions --json")
    transactions = _extract_items(transactions_payload, "sc broker transactions --json")
    transactions = normalize_transactions(transactions)
    return _portfolio_from_payload(holdings_payload, holdings), transactions


def fetch_watchlist_from_sc() -> list:
    """Frischer Live-Abruf von `sc broker watchlist --json` (Plan Phase 2).

    Akzeptiert das Legacy-Format (direkte Liste) sowie das CLI-Wrapper-Format
    {ok, command, data: {..., result: {count, items}}}. Items werden zentral
    normalisiert (normalize_watchlist_items: value_eur aus EUR-valuation,
    category aus Lookup/unknown — siehe Modul-Docstring).

    Fail-closed: fehlendes sc, non-zero Exit, ungueltiges JSON, ok=false,
    fehlende Struktur oder ungueltige items -> definierte ScBridgeError-
    Subklasse. Eine LEERE Watchlist ist ein legitimer Zustand (keine
    Watchlist-Positionen) und liefert [] ohne Exception. Es gibt KEINEN
    Mock-Fallback — Mock-Daten nur ueber load_mock_watchlist() (Dry-Run).
    """
    if not _sc_available():
        raise ScNotAvailableError("sc CLI not found in PATH")
    payload = _parse_json(_run_sc(["broker", "watchlist", "--json"]), "sc broker watchlist --json")
    items = _extract_items(payload, "sc broker watchlist --json", allow_list=True)
    return normalize_watchlist_items(items)


def load_mock() -> tuple[dict, list]:
    """Mock-Daten ausschliesslich fuer Dry-Run, aus tests/mock_data/.

    Niemals im produktiven Pfad verwenden — produktiver Abruf ist refresh_from_sc().
    """
    with open(MOCK_PORTFOLIO, encoding="utf-8") as f:
        portfolio = json.load(f)
    with open(MOCK_TRANSACTIONS, encoding="utf-8") as f:
        transactions = json.load(f)
    return portfolio, transactions


def load_mock_watchlist() -> list:
    """Mock-Watchlist ausschliesslich fuer Dry-Run (Plan Phase 2).

    Liest tests/mock_data/watchlist.json; fehlende/korrupte Datei -> [] ohne
    Exception (leere Watchlist ist ein legitimer Zustand). Niemals im
    produktiven Pfad verwenden — produktiver Abruf ist fetch_watchlist_from_sc().
    """
    try:
        with open(MOCK_WATCHLIST, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    return normalize_watchlist_items(data)


def update_config() -> tuple[dict, list]:
    """Nur fuer manuelle Seed-Migration/Erstlauf (nicht Teil der Pipeline).

    run_briefing.py nutzt diese Funktion nicht mehr — Persistenz laeuft
    ausschliesslich ueber snapshot.capture() (config/snapshot.current.json).
    Diese Funktion liest frisch von sc und schreibt die config/-Dateien
    (Legacy-Format) — nur fuer die manuelle Erstbefuellung geeignet.
    Fail-closed: bei ScBridgeError wird nichts geschrieben, kein Mock-Fallback.
    """
    portfolio, transactions = refresh_from_sc()
    _write_json(CONFIG_DIR / "portfolio.json", portfolio)
    _write_json(CONFIG_DIR / "transactions.json", transactions)
    return portfolio, transactions


def get_portfolio() -> dict:
    """Read-only: liest config/portfolio.json; erzeugt keine Dateien, kein Mock-Fallback.

    ConfigMissingError bei fehlender Datei.
    """
    path = CONFIG_DIR / "portfolio.json"
    if not path.exists():
        raise ConfigMissingError("config/portfolio.json missing (read-only access, no auto-update)")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def get_transactions() -> list:
    """Read-only: liest config/transactions.json; erzeugt keine Dateien, kein Mock-Fallback.

    ConfigMissingError bei fehlender Datei.
    """
    path = CONFIG_DIR / "transactions.json"
    if not path.exists():
        raise ConfigMissingError("config/transactions.json missing (read-only access, no auto-update)")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


if __name__ == "__main__":
    try:
        portfolio, transactions = update_config()
    except ScBridgeError as e:
        print(f"sc_bridge error: {e}", file=sys.stderr)
        sys.exit(1)
    print(f"Portfolio: {len(portfolio.get('holdings', []))} holdings")
    print(f"Transactions: {len(transactions)} entries")