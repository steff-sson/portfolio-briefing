"""Deterministic facts package builder — LLM-free, serializable input contract.

Stufe 1 der Two-Stage-Briefing-Pipeline (Plan §3.1/§4.1): baut aus bereits
berechneten Portfolio-/Analyse-/News-Daten ein stabiles, explizit strukturiertes
Faktenpaket. `deterministic_summary` wird aus denselben `analysis["checks"]`
abgeleitet (nie neu berechnet) und enthaelt die Schluesselzahlen, die das LLM
nur referenzieren darf. Kein LLM-Aufruf, keine Secrets.
"""
from __future__ import annotations

from datetime import datetime, timezone

from scripts import analyze

PIPELINE_VERSION = "2.0"

# Offene Punkte aus dem Telegram-Rückkanal (P7): deterministische Begrenzung
# des untrusted Prompt-Kontexts. MAX_OPEN_POINTS begrenzt die Anzahl der
# eingespeisten Punkte, MAX_OPEN_POINT_CHARS die Länge jedes Texts. Beim
# Kürzen wird die Persistenzdatei (config/setup/open_points.json) NIE mutiert
# — es entstehen nur neue, begrenzte Paket-Objekte (Input bleibt unverändert).
MAX_OPEN_POINTS = 10
MAX_OPEN_POINT_CHARS = 500

# Reihenfolge der Checks mit Status-Feld, wie von analyze.analyze_portfolio erzeugt.
_STATUS_CHECKS = [
    "core_satellite",
    "sector_concentration",
    "single_position",
    "drift",
    "turnover",
    "thesis_deadlines",
]

# Prozent-Grenzwerte aus config/strategy.yaml, die das LLM referenzieren darf.
_STRATEGY_THRESHOLD_KEYS = (
    "core_pct",
    "satellite_pct",
    "threshold_pct",
    "max_position_pct",
    "max_sector_pct",
    "max_turnover_annual_pct",
)


def _get_check(analysis: dict, check_name: str, key: str, default: object = None) -> object:
    """Return analysis["checks"][check_name][key] or default (never recomputed)."""
    check = analysis.get("checks", {}).get(check_name)
    if not isinstance(check, dict):
        return default
    return check.get(key, default)


def _float(value: object) -> float:
    """Coerce a numeric value from analysis to float (None/invalid -> 0.0)."""
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return 0.0
    return 0.0


def _str(value: object) -> str:
    """Coerce a value to str (None -> empty string)."""
    return str(value) if value is not None else ""


def _status_lists(analysis: dict) -> tuple[list[str], list[str], list[str]]:
    """Split status-bearing checks into red/yellow/green lists (sorted, deterministic)."""
    red: list[str] = []
    yellow: list[str] = []
    green: list[str] = []
    for name in sorted(_STATUS_CHECKS):
        status = _get_check(analysis, name, "status")
        if status == "red":
            red.append(name)
        elif status == "yellow":
            yellow.append(name)
        elif status == "green":
            green.append(name)
    return red, yellow, green


def _position_limit_pct(position: dict, strategy: dict) -> float | None:
    """Geltendes Einzelpositions-Limit (Ratio) — nur fuer Satellite.

    Core-/Legacy-/unknown-Positionen haben kein Satellite-Limit -> None
    (die Briefing-Tabelle zeigt dort ``--`` statt einer Zahl).
    """
    if _str(position.get("category")).lower() != "satellite":
        return None
    satellite_limits = strategy.get("satellite_limits", {}) if isinstance(strategy, dict) else {}
    if not isinstance(satellite_limits, dict):
        return None
    limit = satellite_limits.get("max_position_pct")
    if not isinstance(limit, (int, float)) or isinstance(limit, bool):
        return None
    return float(limit) / 100.0


def _positions_detail(
    portfolio: dict, analysis: dict, strategy: dict, total_value_eur: float
) -> list[dict]:
    """Positions-Details fuer die Briefing-Tabelle — additive, deterministische Fakten.

    Quelle: die bereits berechneten Positionen aus ``analysis["checks"]["positions"]``
    (Name/ISIN/Kategorie/Wert/Gewicht — nie neu berechnet). ``strategy`` liefert
    nur das geltende Satellite-Einzelpositionslimit (``max_position_pct``) und die
    Schwellen fuer den Status. Es entstehen KEINE neuen Finanzberechnungen:
    fehlt eine Position, fallen name/isin/category/limit/status auf sichere
    Defaults, value_eur/weight auf 0 zurueck.
    """
    positions = _get_check(analysis, "positions", "positions", [])
    if not isinstance(positions, list):
        positions = []
    holdings = portfolio.get("holdings", [])
    if not isinstance(holdings, list):
        holdings = []
    holdings_by_isin = {str(h.get("isin")): h for h in holdings if isinstance(h, dict)}
    target_ratio = _pct_value(strategy, ("portfolio", "rebalancing", "threshold_pct"))

    detail: list[dict] = []
    for index, p in enumerate(positions):
        if not isinstance(p, dict):
            continue
        isin = _str(p.get("isin"))
        holding = holdings_by_isin.get(isin) or {}
        category = _str(p.get("category") or holding.get("category"))
        limit_ratio = _position_limit_pct(p, strategy)
        weight = _float(p.get("weight"))
        value = _float(p.get("value_eur")) if p.get("value_eur") is not None else _float(
            holding.get("value_eur")
        )
        if weight <= 0 and value > 0 and total_value_eur > 0:
            weight = round(value / total_value_eur, 4)
        status = _position_detail_status(weight, limit_ratio, target_ratio)
        detail.append({
            "name": _str(p.get("name") or holding.get("name")),
            "isin": isin,
            "category": category if category else "unknown",
            "value_eur": value,
            "weight": round(weight, 4),
            "limit_pct": round(limit_ratio * 100, 1) if limit_ratio is not None else None,
            "status": status,
        })
    # Fallback auf Holdings ohne analyse-Positionen (defensive Tests/Dry-Run):
    # keine Neuberechnung der Gewichte — Kategorie/Name/ISIN/Wert aus den
    # Holdings, Limit nur fuer Satellite, Status mit Gewicht 0.
    if not detail:
        for h in holdings:
            if not isinstance(h, dict):
                continue
            category = _str(h.get("category"))
            limit_ratio = _position_limit_pct(h, strategy)
            detail.append({
                "name": _str(h.get("name")),
                "isin": _str(h.get("isin")),
                "category": category if category else "unknown",
                "value_eur": _float(h.get("value_eur")),
                "weight": 0.0,
                "limit_pct": round(limit_ratio * 100, 1) if limit_ratio is not None else None,
                "status": _position_detail_status(0.0, limit_ratio, target_ratio),
            })
    return detail


def _sectors_detail(analysis: dict, strategy: dict) -> list[dict]:
    """Sektor-Details (nur Satellite-Sektoren) — additive, deterministische Fakten.

    Quelle: die bereits berechnete Sektor-Konzentration aus
    ``analysis["checks"]["sector_concentration"]`` (``sector_ratios`` — nur
    Satellite-Positionen; Core/Legacy/unknown werden dort nicht als Satellite
    gewertet, siehe analyze.calculate_sector_concentration). Core-ETFs sind
    bewusst breit gestreut und erscheinen hier NIE als konzentrierter Sektor.
    Sektor-Werte: Anteil-Ratio x Satellite-Summenwert (keine neue
    Finanzberechnung — beide Zahlen sind autoritative Paket-Fakten). Limit
    ``max_sector_pct`` gilt nur fuer Satellite-Sektoren; Status aus dem
    bestehenden ``status`` der Sektor-Konzentration (nur bei rot, sonst green).
    """
    sector_check = _get_check(analysis, "sector_concentration", "sector_ratios")
    if not isinstance(sector_check, dict) or not sector_check:
        return []
    ratios = sector_check
    max_sector_pct = _str(_get_check(analysis, "sector_concentration", "max_sector"))
    overall_status = _get_check(analysis, "sector_concentration", "status")
    threshold = _pct_value(strategy, ("satellite_limits", "max_sector_pct"))
    # Satellite-Summenwert aus den analysierten Positionen (autoritativ, nur
    # Satellite — identisch zur Sektor-Ratio-Berechnungsbasis in analyze).
    positions = _get_check(analysis, "positions", "positions", [])
    if not isinstance(positions, list):
        positions = []
    satellite_value = sum(
        _float(p.get("value_eur"))
        for p in positions
        if isinstance(p, dict) and _str(p.get("category")).lower() == "satellite"
    )
    entries: list[dict] = []
    for sector, ratio in ratios.items():
        if not isinstance(ratio, (int, float)) or isinstance(ratio, bool):
            continue
        sector_status = "green"
        if overall_status == "red" and _str(sector) == max_sector_pct:
            sector_status = "red"
        entries.append({
            "name": _str(sector),
            "value_eur": round(_float(ratio) * satellite_value, 2),
            "ratio": round(_float(ratio), 4),
            "limit_pct": round(threshold * 100, 1) if threshold is not None else None,
            "status": sector_status,
        })
    entries.sort(key=lambda e: (-e["ratio"], e["name"]))
    return entries


def _position_detail_status(weight: float, limit_ratio: float | None, target_ratio: float | None) -> str:
    """Status einer Position: unter Ziel green, zwischen Ziel/Max yellow, ueber Max red.

    Nur Satellite hat ein Limit (limit_ratio != None): Status wird gegen Ziel
    (target_position_pct, sonst 50% des Limits) und Maximum bewertet. Ohne
    Limit (Core/Legacy/unknown) -> "ok" (keine Grenzverletzung moeglich).
    """
    if limit_ratio is None:
        return "ok"
    if weight <= 0:
        return "unbewertet"
    target = target_ratio if target_ratio is not None else limit_ratio * 0.5
    if weight > limit_ratio:
        return "rot"
    if weight > target:
        return "gelb"
    return "gruen"


def _pct_value(strategy: dict, path: tuple[str, ...]) -> float | None:
    """Prozentwert (75.0 = 75%) aus einer Strategy-Pfad-Kette -> Ratio.

    Löst ``strategy[path[0]][path[1]]...`` auf; nur numerische Werte werden
    umgerechnet (fehlend/ungueltig -> None, nie 0.0 erfunden).
    """
    value: object = strategy if isinstance(strategy, dict) else {}
    for key in path:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) / 100.0


def _deterministic_summary(
    portfolio: dict, analysis: dict, data_quality: dict | None = None, strategy: dict | None = None
) -> dict:
    """Extract the numbers the LLM may reference — all from analysis["checks"].

    ``data_quality`` (aus analyze.assess_data_quality) steuert die
    Suppression: bei Status != "ok" werden KEINE Portfolio-Befunde als
    red/yellow markiert — stattdessen nur ``data_quality`` als einziger
    roter Punkt. green_checks bleiben unveraendert (kein Handlungsbedarf).
    """
    max_position = _get_check(analysis, "single_position", "max_position")
    if isinstance(max_position, dict):
        max_position_weight = _float(max_position.get("weight"))
        max_position_name = _str(max_position.get("name"))
    else:
        max_position_weight = 0.0
        max_position_name = ""

    positions = _get_check(analysis, "positions", "positions", [])
    if not isinstance(positions, list):
        positions = []
    position_count = len(positions) if positions else len(portfolio.get("holdings", []))

    red_checks, yellow_checks, green_checks = _status_lists(analysis)

    dq = data_quality if isinstance(data_quality, dict) else {}
    dq_status = dq.get("status")
    dq_issues = dq.get("issues", [])
    if not isinstance(dq_issues, list):
        dq_issues = []
    if dq_status is not None and dq_status != "ok":
        red_checks = ["data_quality"]
        yellow_checks = []

    total_value_eur = _float(
        _get_check(analysis, "positions", "total_value_eur", portfolio.get("total_value_eur"))
    )

    return {
        "total_value_eur": total_value_eur,
        "position_count": position_count,
        "core_ratio": _float(_get_check(analysis, "core_satellite", "core_ratio")),
        "max_position_weight": max_position_weight,
        "max_position_name": max_position_name,
        "max_sector": _str(_get_check(analysis, "sector_concentration", "max_sector")),
        "max_sector_ratio": _float(_get_check(analysis, "sector_concentration", "max_ratio")),
        "drift": _float(_get_check(analysis, "drift", "drift")),
        "turnover_ratio": _float(_get_check(analysis, "turnover", "turnover_ratio")),
        "outdated_theses": _get_check(analysis, "thesis_deadlines", "outdated", []),
        "red_checks": red_checks,
        "yellow_checks": yellow_checks,
        "green_checks": green_checks,
        "data_quality_status": dq_status,
        "data_quality_issues": dq_issues,
        # Phase 3 (Plan §3.2): Positions-/Sektor-Details — additive Fakten
        # aus den bereits berechneten analyze-Checks (keine neuen
        # Finanzberechnungen ausserhalb autoritativer Fakten).
        "positions_detail": _positions_detail(portfolio, analysis, strategy or {}, total_value_eur),
        "sectors_detail": _sectors_detail(analysis, strategy or {}),
    }


def _strategy_thresholds_pct(strategy: dict) -> dict:
    """Extract the real percentage limits from the strategy config (never recomputed).

    Values are already in percent (e.g. core_pct 75.0 = 75%) and read directly
    from the strategy structure (portfolio.core_pct, portfolio.satellite_pct,
    portfolio.rebalancing.threshold_pct, satellite_limits.*). Missing/invalid
    values default to 0.0 — verify skips non-positive thresholds, so without
    real limits unknown numbers stay critical (fail-closed).
    """
    portfolio = strategy.get("portfolio", {}) if isinstance(strategy, dict) else {}
    rebalancing = portfolio.get("rebalancing", {}) if isinstance(portfolio, dict) else {}
    satellite_limits = strategy.get("satellite_limits", {}) if isinstance(strategy, dict) else {}
    return {
        "core_pct": _float(portfolio.get("core_pct")),
        "satellite_pct": _float(portfolio.get("satellite_pct")),
        "threshold_pct": _float(rebalancing.get("threshold_pct")),
        "max_position_pct": _float(satellite_limits.get("max_position_pct")),
        "max_sector_pct": _float(satellite_limits.get("max_sector_pct")),
        "max_turnover_annual_pct": _float(satellite_limits.get("max_turnover_annual_pct")),
    }


# Maximale Anzahl gerenderter Signale je Sektion (Phase 5, kurzer Output).
MAX_SATELLITE_SELL_SIGNALS = 3
MAX_WATCHLIST_SIGNALS = 3


def _signal_sort_key(signal: object, index: int) -> tuple:
    """Deterministischer Sortierschluessel fuer Signal-Objekte.

    Nicht-Signale (NO SIGNAL) ans Ende, dann absteigend nach Score,
    dann stabil nach Listenindex (kein Vergleich nicht-vergleichbarer
    Werte). Signal-Labels haben eine feste Rangfolge (SELL > REDUCE >
    BUY > AVOID > WATCH > NO SIGNAL), damit die Auswahl nicht von der
    Sortierung nicht-vergleichbarer Felder abhaengt.
    """
    _RANK = {"SELL": 6, "REDUCE": 5, "BUY": 4, "AVOID": 3, "WATCH": 2, "NO SIGNAL": 1}
    if not isinstance(signal, dict):
        return (0, 0.0, 0, index)
    label = str(signal.get("signal", "NO SIGNAL"))
    score = signal.get("score")
    score = score if isinstance(score, (int, float)) and not isinstance(score, bool) else 0.0
    # Negativer Rank: hoeherer Rang (SELL 6) sortiert zuerst (absteigend),
    # danach Score absteigend — siehe Docstring-Rangfolge.
    # Negativer Rank: hoeherer Rang (SELL 6) sortiert zuerst (absteigend),
    # danach Score absteigend — siehe Docstring-Rangfolge.
    return (-_RANK.get(label, 0), -float(score), 1 if signal.get("excluded") else 0, index)


def _split_satellite_sell_signals(signals: list) -> list[dict]:
    """Sell-/Reduce-Kandidaten aus den Signal-Objekten (bestehende Satellites).

    Nur SELL/REDUCE-Signale fuer bestehende Satellite-Holdings; Core-ETFs
    (excluded) und SUSE/Legacy (excluded) sind ausgeschlossen (Phase 5).
    """
    result: list[dict] = []
    if not isinstance(signals, list):
        return result
    for signal in signals:
        if not isinstance(signal, dict):
            continue
        if signal.get("excluded"):
            continue
        if signal.get("signal") in ("SELL", "REDUCE"):
            result.append(signal)
    return result


def _select_watchlist_signals(signals: list) -> list[dict]:
    """Max. ``MAX_WATCHLIST_SIGNALS`` Watchlist-Signale (deterministisch).

    Reine NO-SIGNAL-Eintraege (inkl. Core-ETF/Legacy-Ausschluss) werden
    nicht in die Auswahl uebernommen — die Sektion zeigt sonst
    "Keine Watchlist-Signale", was der Abschluss-Text abbildet.
    """
    if not isinstance(signals, list):
        return []
    ranked = sorted(
        (s for s in signals if isinstance(s, dict) and s.get("signal") != "NO SIGNAL"),
        key=lambda s: _signal_sort_key(s, 0),
    )
    return ranked[:MAX_WATCHLIST_SIGNALS]


def _has_relevant_changes(changes: dict | None) -> bool:
    """Relevante Portfolio-Veränderungen: Positionen added/removed/changed oder
    Transaktionen added/removed. Reine Werte-Bewegungen unter den Diff-Schwellen
    zaehlen nicht (kein Trigger). Erstlauf (has_previous False) -> False.
    """
    if not isinstance(changes, dict) or changes.get("has_previous") is not True:
        return False
    positions = changes.get("positions")
    if isinstance(positions, dict) and (positions.get("added") or positions.get("removed") or positions.get("changed")):
        return True
    txns = changes.get("transactions")
    if isinstance(txns, dict) and (txns.get("added_count") or txns.get("removed_count")):
        return True
    return False


def compute_triggers(facts_package: dict) -> dict:
    """Deterministische Trigger-Flags + geordnete Trigger-Liste (Phase 3).

    Reihenfolge ist fest im Code (Pipeline-Logik, nicht konfigurierbar):
    1. data_quality (falls nicht ok — hat Vorrang, blockiert Portfolio-Trigger)
    2. strategy_change
    3. boundary_violation
    4. relevant_changes
    5. thesis_news

    Der "data_quality"-Marker in red_checks zaehlt nicht als Grenzverletzung.
    """
    summary = facts_package.get("deterministic_summary", {})
    data_quality = facts_package.get("data_quality")
    data_quality_status = data_quality.get("status") if isinstance(data_quality, dict) else None
    changes = facts_package.get("changes")
    strategy_diff = facts_package.get("strategy_diff")
    news = facts_package.get("news", [])

    red = summary.get("red_checks") or []
    yellow = summary.get("yellow_checks") or []
    boundary_checks = [c for c in list(red) + list(yellow) if c != "data_quality"]
    has_boundary = bool(boundary_checks)
    has_changes = _has_relevant_changes(changes)
    has_news = any(
        isinstance(n, dict) and n.get("thesis_relevant") for n in (news if isinstance(news, list) else [])
    )
    has_strategy = strategy_diff is not None and bool(strategy_diff.get("has_changed", False))
    dq_not_ok = data_quality_status is not None and data_quality_status != "ok"

    ordered: list[str] = []
    if dq_not_ok:
        ordered.append("data_quality")
    if has_strategy:
        ordered.append("strategy_change")
    if has_boundary:
        ordered.append("boundary_violation")
    if has_changes:
        ordered.append("relevant_changes")
    if has_news:
        ordered.append("thesis_news")

    return {
        "has_boundary_violation": has_boundary,
        "has_relevant_changes": has_changes,
        "has_thesis_news": has_news,
        "has_strategy_change": has_strategy,
        "ordered": ordered,
        "has_any_trigger": bool(ordered),
    }


def _reduce_open_points(open_points: object) -> list[dict]:
    """Reduziert offene Punkte auf den untrusted Text — begrenzt + markiert.

    P7: Offene Punkte sind untrusted user input aus dem Telegram-Rückkanal.
    Nur ``text`` und ``received_at`` werden ins Faktenpaket übernommen
    (keine ``message_id``/``chat_id`` — Reduktion auf den reinen Text);
    jeder Eintrag erhält ``untrusted: true``. Deterministische Begrenzung
    gegen einen unbounded Prompt-/Kostenvektor: maximal
    ``MAX_OPEN_POINTS`` Punkte, jeder Text auf ``MAX_OPEN_POINT_CHARS``
    Zeichen gekürzt (dokumentiert in den Konstanten). Die Persistenzdatei
    wird NIE mutiert — der Input bleibt unverändert, es entstehen nur neue
    begrenzte Paket-Objekte. Nicht-dict/fehlerhafte Einträge werden
    übersprungen; None/leer -> [].
    """
    if not isinstance(open_points, list):
        return []
    reduced: list[dict] = []
    for point in open_points[:MAX_OPEN_POINTS]:
        if not isinstance(point, dict):
            continue
        text = point.get("text")
        if not isinstance(text, str) or not text.strip():
            continue
        received_at = point.get("received_at")
        entry: dict = {"text": text[:MAX_OPEN_POINT_CHARS], "untrusted": True}
        if isinstance(received_at, str) and received_at.strip():
            entry["received_at"] = received_at
        reduced.append(entry)
    return reduced


def build_facts_package(
    portfolio: dict,
    transactions: list,
    analysis: dict,
    news: list,
    strategy: dict,
    mode: str,
    changes: dict | None = None,
    data_quality: dict | None = None,
    previous_snapshot: dict | None = None,
    current_captured_at: str | None = None,
    watchlist: list | None = None,
    open_points: list | None = None,
) -> dict:
    """Build the deterministic, JSON-serializable facts package.

    Pure function of its inputs (no LLM, no secrets): portfolio/analysis/news/
    strategy/transactions/changes are passed through unchanged,
    `deterministic_summary` is extracted from the precomputed
    `analysis["checks"]` and `strategy_thresholds_pct` from the strategy
    config's percentage limits.

    ``changes`` ist das deterministische Diff-Ergebnis (diff.diff_snapshots)
    gegen den letzten Snapshot — None im Erstlauf/Dry-Run. LLM-Kontexte
    reduzieren es separat (``reduce_changes_for_llm``): Roh-Transaktions-
    Records bleiben aus den Prompts.

    ``data_quality`` kommt aus analyze.assess_data_quality (vom Orchestrator
    berechnet, inkl. Vorgaenger-Snapshot); None bei fehlender Bewertung
    (Dry-Run/Erstlauf) -> keine Suppression roter Befunde.

    ``previous_snapshot`` (optional): liefert die Kurs-/Wertdaten fuer die
    6-Monats-Performance (``position_perf_6m``). Fehlen Daten oder liegt kein
    Snapshot im 6-Monats-Fenster, bleibt die Performance fail-closed (kein
    SELL aus der Performance-Regel). ``current_captured_at`` (optional) ist
    der ISO-Zeitpunkt des aktuellen Stands fuer die Fensterpruefung.

    ``strategy_diff`` stammt aus ``changes["strategy"]`` (diff.diff_strategy)
    und enthaelt nur Feld-Pfade + Hashes — nie Strategiewerte. ``triggers``
    (compute_triggers) ist die deterministische Trigger-Basis fuer die
    Optionen-Generierung.

    ``watchlist`` (optional, Phase 5): normalisierte Watchlist-Items
    (sc_bridge.normalize_watchlist_items) bzw. leer, wenn keine Watchlist
    vorhanden ist (legitimer Zustand -> leere Signal-Sektionen).

    ``open_points`` (optional, P7): offene Punkte aus dem Telegram-Rückkanal
    (telegram_inbound.load_open_points). Sie sind UNTRUSTED user input und
    werden als separater Kontext ``open_points`` ins Paket geschrieben —
    reduziert auf ``{text, received_at}`` mit ``untrusted: true`` pro Eintrag,
    deterministisch begrenzt (``_reduce_open_points``: MAX_OPEN_POINTS,
    MAX_OPEN_POINT_CHARS). Sie fliessen NICHT in ``deterministic_summary``
    (keine deterministischen Fakten/Zahlenquelle) und koennen weder Zahlen,
    Labels, Ampel, Strategie noch Prompt-Instruktionen ueberschreiben.
    Ohne Argument (None) bleibt das Paket strukturell unveraendert
    (rueckwaertskompatibel).
    """
    summary = _deterministic_summary(portfolio, analysis, data_quality, strategy)
    strategy_diff = changes.get("strategy") if isinstance(changes, dict) else None
    # Briefing-Schnittstelle (Plan §6a): Ampel (7 Kategorien), Gesamt-Empfehlung
    # und Top-3-Positionsvorschlaege — deterministisch aus analyze abgeleitet.
    # Nur wenn die Analyse tatsaechlich Checks enthaelt (echte Daten); leere
    # Analyse (Test-Mocks/Defensiv) erzeugt keine Briefing-Entscheidungen.
    checks = analysis.get("checks") if isinstance(analysis, dict) else None
    if isinstance(checks, dict) and checks:
        # 6-Monats-Performance aus dem Vorgaenger-Snapshot (fail-closed bei
        # fehlenden Daten: Positionen ohne Daten erzeugen keinen SELL).
        position_perf = analyze.compute_position_perf_6m(
            portfolio, previous_snapshot, current_captured_at
        )
        decisions = analyze.build_briefing_decisions(
            analysis, strategy, transactions, data_quality, position_perf=position_perf
        )
        summary["traffic_lights"] = decisions["traffic_lights"]
        summary["recommendation"] = decisions["recommendation"]
        summary["position_actions"] = decisions["position_actions"]
        summary["position_perf_6m"] = position_perf
    else:
        summary["traffic_lights"] = {}
        summary["recommendation"] = {}
        summary["position_actions"] = []
        summary["position_perf_6m"] = {}

    # Watchlist-/Satellite-Signale (Plan Phase 5): deterministisch aus
    # analyze.compute_watchlist_signals. Signale nutzen NIE Fundamentaldaten
    # (KGV/Gewinn/Umsatz/Cashflow/Verschuldung/Bewertung) — das Signal-Objekt
    # traegt ``fundamentals_used: false``, damit der Renderer den Disclaimer
    # in jeder Signal-Sektion verankern kann. Ohne Watchlist-Daten
    # (None/fehlend) -> leere Listen (kein Crash, aber transparent "keine
    # Watchlist-Positionen"). Core-ETF-Sparplaene sind Strategie-Setup und
    # erscheinen nie als Satellite-Trade; ETFs sind keine Einzelpositionen
    # (compute_watchlist_signals wendet die Ausschlussregeln an).
    watchlist_items = watchlist if isinstance(watchlist, list) else []
    if watchlist_items:
        signal_items = analyze.compute_watchlist_signals(
            watchlist_items,
            portfolio,
            analysis if isinstance(analysis, dict) else {},
            news,
            strategy,
            previous_snapshot=previous_snapshot,
            current_captured_at=current_captured_at,
        )
        summary["watchlist_signals"] = _select_watchlist_signals(signal_items)
        summary["satellite_sell_signals"] = _split_satellite_sell_signals(signal_items)
    else:
        summary["watchlist_signals"] = []
        summary["satellite_sell_signals"] = []
    package = {
        "meta": {
            "mode": mode,
            "generated_at": datetime.now(tz=timezone.utc).astimezone().isoformat(timespec="seconds"),
            "pipeline_version": PIPELINE_VERSION,
        },
        "portfolio": portfolio,
        "analysis": analysis,
        "news": news,
        "strategy": strategy,
        "transactions": transactions,
        "watchlist": watchlist_items,
        "changes": changes,
        "data_quality": data_quality,
        "strategy_diff": strategy_diff,
        "deterministic_summary": summary,
        "strategy_thresholds_pct": _strategy_thresholds_pct(strategy),
        "open_points": _reduce_open_points(open_points),
    }
    triggers = compute_triggers(package)
    package["triggers"] = triggers
    summary["has_triggers"] = triggers["has_any_trigger"]
    return package


def reduce_changes_for_llm(changes: dict | None) -> dict | None:
    """Reduzierte Aenderungsdaten fuer LLM-Kontexte (ohne Roh-Transaktions-Records).

    Entfernt aus dem Diff-Ergebnis die Transaktions-Datensaetze ``added``/
    ``removed`` (Zeitpunkt/ISIN/Typ/Menge/Preis) — die MVP-GRENZE verbietet
    die Behandlung einzelner Trades. Aggregierte Diff-Felder (Counts,
    Volumen-Delta, Positions-Diff) bleiben erhalten. None (Erstlauf/Dry-Run)
    bleibt None. Mutiert den Input nicht.
    """
    if not isinstance(changes, dict):
        return None
    reduced = {key: value for key, value in changes.items() if key != "transactions"}
    txns = changes.get("transactions")
    if isinstance(txns, dict):
        reduced["transactions"] = {
            key: value for key, value in txns.items() if key not in ("added", "removed")
        }
    return reduced


if __name__ == "__main__":
    import json

    from scripts import analyze, sc_bridge

    portfolio = sc_bridge.get_portfolio()
    transactions = sc_bridge.get_transactions()
    strategy = analyze.load_strategy()
    analysis = analyze.analyze_portfolio(portfolio, transactions, strategy)
    news: list = []
    package = build_facts_package(portfolio, transactions, analysis, news, strategy, mode="monday")
    print(json.dumps(package, indent=2, ensure_ascii=False))
