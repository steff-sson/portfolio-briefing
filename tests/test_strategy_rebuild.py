"""Tests fuer den Strategie-Neuaufbau (Plan strategy-setup-rebuild.md, Phasen A-E).

Abdeckung: Schema-SSoT + fail-closed Validierung, deterministischer Setup-Flow
(Emission/Backup/Hash/Doku), Transaktionsnormalisierung + Wochenvergleich,
SUSE-Datenqualitaetsregel, Ampel (7 Kategorien), Gesamt-Empfehlung
(BUY/SELL/WATCH), Top-3-Positionsvorschlaege, 6-Monats-Performance (fail-closed)
und Neukaufideen (Quellen-/Duplikatpruefung).

Keine echten API-/Broker-/Telegram-Aufrufe — reine lokale Daten + Monkeypatch.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from scripts import analyze, filter_news, sc_bridge, setup_strategy, snapshot
from scripts.setup_strategy import SetupError

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"

FULL_STRATEGY = {
    "meta": {
        "version": 1,
        "created": "2026-08-14",
        "last_reviewed": "2026-08-19",
        "next_review": "2026-11-14",
        "cooling_off_days": 7,
    },
    "investor": {
        "horizon": ">10y",
        "income_source": "monthly_savings",
        "purpose": "retirement",
        "monthly_savings_eur": 120,
        "experience_years": 4,
        "leverage": "none",
        "check_frequency": "weekly",
        "personal_context": "Beispiel: separate private Vorsorge (bewusst informativ)",
    },
    "portfolio": {
        "core_pct": 70.0,
        "satellite_pct": 30.0,
        "core_description": "Beispiel-Core: global diversifizierter ETF",
        "rebalancing": {"method": "threshold", "threshold_pct": 5.0},
    },
    "satellite_limits": {
        "target_position_pct": 5.0,
        "warn_position_pct": 7.5,
        "max_position_pct": 10.0,
        "max_sector_pct": 20.0,
        "max_positions": 10,
        "max_turnover_annual_pct": 30.0,
        "max_trades_per_quarter": 5,
    },
    "sectors": {"preferred": ["healthcare", "consumer"], "excluded": ["tobacco"]},
    "thesis": {"required": True},
    "alerts": {"on_thesis_expiring_soon_days": 30},
    "review_schedule": {
        "quarterly_strategy_review": True,
        "annual_full_review": True,
        "triggers": ["life_event", "market_crash_20pct", "income_change_30pct"],
    },
}


# --- Schema-SSoT + fail-closed Validierung (Phase A) --------------------------


def test_schema_file_exists_and_parses():
    schema = analyze.load_strategy_schema()
    sections = schema["sections"]
    assert "portfolio" in sections
    assert "satellite_limits" in sections
    assert "investor" in sections
    assert sections["portfolio"]["required"] is True
    assert sections["satellite_limits"]["required"] is True


def test_validate_strategy_unknown_field_fail_closed():
    import copy

    bad = copy.deepcopy(FULL_STRATEGY)
    bad["portfolio"]["neues_feld"] = 123
    result = analyze.validate_strategy(bad)
    assert result["valid"] is False
    assert any("Unbekanntes Feld 'portfolio.neues_feld'" in e for e in result["errors"])


def test_validate_strategy_unknown_section_fail_closed():
    import copy

    bad = copy.deepcopy(FULL_STRATEGY)
    bad["future_section"] = {"x": 1}
    result = analyze.validate_strategy(bad)
    assert result["valid"] is False
    assert any("Unbekannte Sektion 'future_section'" in e for e in result["errors"])


def test_validate_strategy_missing_analyserelevant_field():
    import copy

    bad = copy.deepcopy(FULL_STRATEGY)
    del bad["satellite_limits"]["max_trades_per_quarter"]
    result = analyze.validate_strategy(bad)
    assert result["valid"] is False
    assert any("max_trades_per_quarter" in e for e in result["errors"])


def test_validate_strategy_max_trades_ge_one():
    import copy

    bad = copy.deepcopy(FULL_STRATEGY)
    bad["satellite_limits"]["max_trades_per_quarter"] = 0
    result = analyze.validate_strategy(bad)
    assert result["valid"] is False
    assert any("max_trades_per_quarter muss >= 1 sein" in e for e in result["errors"])


def test_validate_strategy_date_format():
    import copy

    bad = copy.deepcopy(FULL_STRATEGY)
    bad["meta"]["created"] = "14.08.2026"
    result = analyze.validate_strategy(bad)
    assert result["valid"] is False
    assert any("meta.created muss ein Datum sein" in e for e in result["errors"])


# --- Deterministischer Setup-Flow (Phase B) -----------------------------------


def _answers_fixture() -> dict:
    return yaml.safe_load((CONFIG_DIR / "setup" / "answers.reviewed.yaml").read_text(encoding="utf-8"))


def test_emit_writes_schema_valid_strategy(monkeypatch, tmp_path):
    """Emission: strategy.yaml enthaelt nur analyserelevante + dokumentationsrelevante
    Felder; bewusst informative Felder (external_provision, personal_context,
    check_frequency) fehlen dort (Datenmodell-Trennung)."""
    strategy_path = tmp_path / "strategy.yaml"
    version_path = tmp_path / "strategy-version.txt"
    monkeypatch.setattr(setup_strategy, "STRATEGY_PATH", strategy_path)
    monkeypatch.setattr(setup_strategy, "VERSION_PATH", version_path)
    monkeypatch.setattr(setup_strategy, "BACKUP_DIR", tmp_path / "backup")
    monkeypatch.setattr(setup_strategy, "ANSWERS_REVIEWED_PATH", CONFIG_DIR / "setup" / "answers.reviewed.yaml")

    result = setup_strategy.emit()

    assert strategy_path.exists()
    emitted = yaml.safe_load(strategy_path.read_text(encoding="utf-8"))
    assert emitted["satellite_limits"]["max_trades_per_quarter"] == 5
    assert emitted["portfolio"]["core_pct"] == 70.0
    # Bewusst informative Felder NICHT emittiert.
    assert "external_provision" not in emitted["investor"]
    assert "personal_context" not in emitted["investor"]
    assert "check_frequency" not in emitted["investor"]
    # Version + Hash-Datei.
    assert result["version"] >= 1
    assert version_path.exists()
    assert "hash:" in version_path.read_text(encoding="utf-8")
    # Emittierte Strategie ist schema-valid.
    validation = analyze.validate_strategy(emitted)
    assert validation["valid"] is True


def test_emit_creates_backup_of_previous(monkeypatch, tmp_path):
    strategy_path = tmp_path / "strategy.yaml"
    strategy_path.write_text("meta:\n  version: 1\n", encoding="utf-8")
    version_path = tmp_path / "strategy-version.txt"
    monkeypatch.setattr(setup_strategy, "STRATEGY_PATH", strategy_path)
    monkeypatch.setattr(setup_strategy, "VERSION_PATH", version_path)
    monkeypatch.setattr(setup_strategy, "BACKUP_DIR", tmp_path / "backup")
    monkeypatch.setattr(setup_strategy, "ANSWERS_REVIEWED_PATH", CONFIG_DIR / "setup" / "answers.reviewed.yaml")

    result = setup_strategy.emit()

    backups = list((tmp_path / "backup").glob("strategy-*.yaml"))
    assert len(backups) == 1
    assert "version: 1" in backups[0].read_text(encoding="utf-8")
    assert result["version"] == 2  # Vorgaenger-Version 1 -> 2


def test_emit_fails_without_reviewed_answers(monkeypatch, tmp_path):
    monkeypatch.setattr(setup_strategy, "ANSWERS_REVIEWED_PATH", tmp_path / "fehlt.yaml")
    with pytest.raises(SetupError):
        setup_strategy.emit()


def test_validate_answers_against_schema():
    answers = _answers_fixture()
    schema = analyze.load_strategy_schema()
    errors = setup_strategy._validate_answers_structure(answers, schema)
    assert errors == []


def test_validate_answers_missing_analyserelevant_blocked():
    answers = _answers_fixture()
    del answers["answers"]["satellite_limits.max_trades_per_quarter"]
    schema = analyze.load_strategy_schema()
    errors = setup_strategy._validate_answers_structure(answers, schema)
    assert any("max_trades_per_quarter" in e and "Analyserelevante" in e for e in errors)


# --- Transaktionsnormalisierung + Wochenvergleich (Phase C) -------------------


def test_normalize_transactions_live_shape():
    """Live-Snapshot: 20 Eintraege -> normalisiert mit einheitlichen Feldern."""
    data = json.loads((CONFIG_DIR / "snapshot.current.json").read_text(encoding="utf-8"))
    txns = sc_bridge.normalize_transactions(data["transactions"])
    assert len(txns) == 20
    required = {"isin", "side", "quantity", "price_eur", "amount_eur", "currency", "executed_at", "transaction_type", "cancelled", "source_id"}
    assert all(required.issubset(t.keys()) for t in txns)
    types = {t["transaction_type"] for t in txns}
    assert {"kauf", "verkauf", "sparplan", "einzahlung", "ausschuettung"}.issubset(types)


def test_normalize_transactions_legacy_mock_passthrough():
    """Mock-Legacy-Format (date/type buy) bleibt fuer Turnover lesbar."""
    txns = sc_bridge.normalize_transactions([
        {"date": "2026-01-15", "isin": "US0378331005", "type": "buy", "quantity": 5, "price_eur": 195.0},
    ])
    assert txns == [{"date": "2026-01-15", "isin": "US0378331005", "type": "buy", "quantity": 5, "price_eur": 195.0}]


def test_calculate_trades_in_quarter():
    txns = sc_bridge.normalize_transactions([
        {"isin": "X", "side": "BUY", "last_event_datetime": "2026-07-01T10:00:00Z", "type": "SECURITY_TRANSACTION"},
        {"isin": "Y", "side": "BUY", "last_event_datetime": "2026-08-01T10:00:00Z", "type": "SECURITY_TRANSACTION"},
        {"isin": "Z", "cash_transaction_type": "DEPOSIT", "last_event_datetime": "2026-08-02T10:00:00Z", "type": "CASH_TRANSACTION"},
    ])
    result = analyze.calculate_trades_in_quarter(txns, quarter="2026-Q3", strategy=FULL_STRATEGY)
    assert result["trade_count"] == 2  # Cash-Bewegung zaehlt nicht
    assert result["status"] == "green"  # 2 < 5


def test_calculate_trades_in_quarter_over_limit():
    txns = sc_bridge.normalize_transactions([
        {"isin": f"I{i}", "side": "BUY", "last_event_datetime": f"2026-03-0{i}T10:00:00Z", "type": "SECURITY_TRANSACTION"}
        for i in range(1, 7)
    ])
    result = analyze.calculate_trades_in_quarter(txns, quarter="2026-Q1", strategy=FULL_STRATEGY)
    assert result["trade_count"] == 6
    assert result["status"] == "red"


def test_calculate_weekly_trades():
    txns = sc_bridge.normalize_transactions([
        {"isin": "X", "side": "BUY", "last_event_datetime": "2026-08-04T10:00:00Z", "type": "SECURITY_TRANSACTION"},
        {"isin": "Y", "side": "SELL", "last_event_datetime": "2026-08-05T10:00:00Z", "type": "SECURITY_TRANSACTION"},
        {"isin": "Z", "side": "BUY", "last_event_datetime": "2026-08-12T10:00:00Z", "type": "SECURITY_TRANSACTION"},
    ])
    result = analyze.calculate_weekly_trades(txns, year=2026, week=32)  # 03.08.-09.08.2026
    assert result["trade_count"] == 2
    assert result["buy_count"] == 1
    assert result["sell_count"] == 1


def test_calculate_turnover_uses_normalized_amounts():
    txns = sc_bridge.normalize_transactions([
        {"isin": "X", "side": "BUY", "amount": -1000.0, "last_event_datetime": "2026-08-01T10:00:00Z", "type": "SECURITY_TRANSACTION"},
    ])
    portfolio = {"total_value_eur": 10000.0}
    result = analyze.calculate_turnover(txns, portfolio, FULL_STRATEGY)
    assert result["total_volume_eur"] == 1000.0
    assert result["turnover_ratio"] == pytest.approx(0.1)
    assert result["status"] == "green"


# --- SUSE-Datenqualitaetsregel (Phase E / §8) ---------------------------------


def _suse_portfolio() -> dict:
    return {
        "total_value_eur": 1000.0,
        "holdings": [
            {"isin": "LU2722255754", "name": "SUSE", "category": "unknown", "valuation": None, "valuation_currency": None},
            {"isin": "US0378331005", "name": "Apple", "category": "satellite", "value_eur": 1000.0},
        ],
    }


def test_suse_rule_position_kept_unvalued():
    """SUSE: Position bleibt erhalten (keine Loeschung), kein 0-Wert, Gesamtwert unvollstaendig."""
    portfolio = _suse_portfolio()
    dq = analyze.assess_data_quality(portfolio, None)
    assert dq["status"] == "incomplete"
    assert any("LU2722255754" in i and "kein Bewertungswert" in i for i in dq["issues"])

    result = analyze.calculate_positions(portfolio)
    by_isin = {p["isin"]: p for p in result["positions"]}
    assert "LU2722255754" in by_isin  # nicht geloescht
    assert by_isin["LU2722255754"]["category"] == "unknown"
    assert by_isin["LU2722255754"]["weight"] == 0.0  # kein kuenstlicher Wert
    # Gesamtwert nur aggregiert, wenn jede Holding einen EUR-Wert hat -> hier 1000 (Apple).
    assert result["total_value_eur"] == 1000.0


# --- Ampel, Empfehlung, Positionsvorschlaege (Phase D / §6a) ------------------


def _analysis_fixture() -> dict:
    return {
        "checks": {
            "positions": {
                "total_value_eur": 10000.0,
                "positions": [
                    {"isin": "IE00B57X3V84", "name": "ETF A", "category": "satellite", "value_eur": 6000.0, "weight": 0.6, "sector": "Tech"},
                    {"isin": "US0378331005", "name": "Apple", "category": "satellite", "value_eur": 2000.0, "weight": 0.2, "sector": "Tech"},
                    {"isin": "IE00BK5BQT80", "name": "Core ETF", "category": "core", "value_eur": 2000.0, "weight": 0.2, "sector": "Diversified"},
                ],
            },
            "core_satellite": {"core_ratio": 0.2, "status": "red"},
            "sector_concentration": {"max_sector": "Tech", "max_ratio": 0.8, "status": "red"},
            "single_position": {"max_position": {"name": "ETF A", "weight": 0.6}, "status": "red"},
            "drift": {"drift": 0.5, "status": "red"},
            "turnover": {"turnover_ratio": 0.1, "status": "green"},
            "thesis_deadlines": {"outdated": [], "status": "green"},
            "trades_per_quarter": {"quarter": "2026-Q3", "trade_count": 1, "max_trades_per_quarter": 5, "status": "green"},
        }
    }


def test_traffic_lights_seven_categories():
    lights = analyze.build_traffic_lights(_analysis_fixture(), FULL_STRATEGY, {"status": "ok", "issues": []})
    assert set(lights) == set(analyze.TRAFFIC_LIGHT_CATEGORIES)
    assert len(lights) == 7
    for light in lights.values():
        assert light["status"] in ("green", "yellow", "red")
        assert light["reason"]


def test_recommendation_sell_requires_4_of_7_red():
    """Gesamt-SELL erst bei >= 4 von 7 roten Kategorien; einzelne rote -> kein SELL."""
    lights = {
        "a": {"status": "red", "reason": "x"},
        "b": {"status": "red", "reason": "x"},
        "c": {"status": "red", "reason": "x"},
        "d": {"status": "yellow", "reason": "x"},
        "e": {"status": "green", "reason": "x"},
        "f": {"status": "green", "reason": "x"},
        "g": {"status": "green", "reason": "x"},
    }
    assert analyze.build_recommendation(lights, FULL_STRATEGY)["label"] == "WATCH"  # 3 rot, 3 gruen
    lights2 = dict(lights)
    lights2["d"] = {"status": "red", "reason": "x"}
    assert analyze.build_recommendation(lights2, FULL_STRATEGY)["label"] == "SELL"  # 4 rot


def test_recommendation_buy_requires_4_green_and_savings():
    lights = {
        "a": {"status": "green", "reason": "x"},
        "b": {"status": "green", "reason": "x"},
        "c": {"status": "green", "reason": "x"},
        "d": {"status": "green", "reason": "x"},
        "e": {"status": "red", "reason": "x"},
        "f": {"status": "red", "reason": "x"},
        "g": {"status": "red", "reason": "x"},
    }
    # Sparrate > 0 -> BUY
    assert analyze.build_recommendation(lights, FULL_STRATEGY)["label"] == "BUY"
    # Sparrate 0 -> WATCH (kein BUY)
    strategy_no_savings = json.loads(json.dumps(FULL_STRATEGY))
    strategy_no_savings["investor"]["monthly_savings_eur"] = 0
    assert analyze.build_recommendation(lights, strategy_no_savings)["label"] == "WATCH"


def test_position_actions_max_three_only_changes():
    """Position_actions: max. 3, nur Aenderungen (aufstocken/reduzieren/verkaufen),
    konkrete ISIN genannt, keine HOLD-Ausgabe."""
    lights = analyze.build_traffic_lights(_analysis_fixture(), FULL_STRATEGY, {"status": "ok", "issues": []})
    actions = analyze.build_position_actions(_analysis_fixture()["checks"]["positions"]["positions"], lights, FULL_STRATEGY)
    assert len(actions) <= 3
    assert all(a["action"] in analyze.POSITION_ACTION_TYPES for a in actions)
    assert all(a["isin"] for a in actions)
    assert all(a["action"] != "halten" for a in actions)


def test_position_actions_perf_fail_closed():
    """6-Monats-Performance: negativ -> Reduktionsvorschlag NUR wenn Datenfeld
    vorhanden; fehlendes position_perf_6m_pct -> kein SELL aus der Performance-Regel."""
    positions = [
        {"isin": "A", "name": "A", "category": "satellite", "value_eur": 500.0, "weight": 0.05, "sector": "Tech"},
        {"isin": "B", "name": "B", "category": "satellite", "value_eur": 500.0, "weight": 0.05, "sector": "Tech"},
    ]
    lights = {c: {"status": "green", "reason": "ok"} for c in analyze.TRAFFIC_LIGHT_CATEGORIES}
    # Ohne Datenfeld: keine perf-basierte Reduktion (fail-closed).
    actions_no_data = analyze.build_position_actions(positions, lights, FULL_STRATEGY)
    assert not any(a["reason"].startswith("6-Monats-Performance") for a in actions_no_data)
    # Mit negativem Datenfeld: Reduktionsvorschlag.
    positions_with_perf = [
        {**positions[0], "position_perf_6m_pct": -0.15},
        {**positions[1], "position_perf_6m_pct": 0.05},
    ]
    actions_with_data = analyze.build_position_actions(positions_with_perf, lights, FULL_STRATEGY)
    assert any(a["isin"] == "A" and a["action"] == "reduzieren" for a in actions_with_data)


def test_build_briefing_decisions_integration():
    decisions = analyze.build_briefing_decisions(
        _analysis_fixture(), FULL_STRATEGY, [], {"status": "ok", "issues": []}
    )
    assert set(decisions["traffic_lights"]) == set(analyze.TRAFFIC_LIGHT_CATEGORIES)
    assert decisions["recommendation"]["label"] in ("BUY", "SELL", "WATCH")
    assert isinstance(decisions["position_actions"], list)


# --- Finding 2: Thesis-Zuordnung zu Positionen (strukturierte outdated-Daten) -


def test_position_actions_thesis_matching_by_analysis():
    """Abgelaufene Thesen werden ueber die strukturierten thesis_deadlines/outdated-
    Daten (file + created) per ISIN/Ticker/Dateinamen korrekt Positionen zugeordnet."""
    positions = [
        {"isin": "US0378331005", "name": "Apple Inc.", "ticker": "AAPL", "category": "satellite", "value_eur": 500.0, "weight": 0.05, "sector": "Tech"},
        {"isin": "IE00B57X3V84", "name": "iShares Dow Jones Global Leaders", "category": "satellite", "value_eur": 500.0, "weight": 0.05, "sector": "Diversified"},
        {"isin": "NL0010273215", "name": "ASML Holding", "ticker": "ASML", "category": "satellite", "value_eur": 500.0, "weight": 0.05, "sector": "Tech"},
    ]
    lights = {c: {"status": "green", "reason": "ok"} for c in analyze.TRAFFIC_LIGHT_CATEGORIES}
    lights["thesis_deadlines"] = {"status": "red", "reason": "Abgelaufene Thesen: apple.md, asml.md."}
    analysis = {
        "checks": {
            "thesis_deadlines": {
                "outdated": [
                    {"file": "apple.md", "created": "2025-01-01"},
                    {"file": "asml.md", "created": "2025-01-01"},
                ],
                "status": "red",
            }
        }
    }
    actions = analyze.build_position_actions(positions, lights, FULL_STRATEGY, analysis=analysis)
    thesis_actions = [a for a in actions if a["reason"].startswith("Abgelaufene Thesis")]
    assert len(thesis_actions) == 2
    by_isin = {a["isin"]: a for a in thesis_actions}
    assert by_isin["US0378331005"]["action"] == "verkaufen"  # apple.md -> Apple/AAPL
    assert by_isin["NL0010273215"]["action"] == "verkaufen"  # asml.md -> ASML


def test_position_actions_thesis_isin_match():
    """Thesis-Dateiname mit ISIN wird exakt zugeordnet (kein Name-Substring)."""
    positions = [
        {"isin": "IE00B57X3V84", "name": "iShares Dow Jones Global Leaders", "category": "satellite", "value_eur": 500.0, "weight": 0.05, "sector": "Diversified"},
    ]
    lights = {c: {"status": "green", "reason": "ok"} for c in analyze.TRAFFIC_LIGHT_CATEGORIES}
    lights["thesis_deadlines"] = {"status": "red", "reason": "x"}
    analysis = {"checks": {"thesis_deadlines": {"outdated": [{"file": "ie00b57x3v84.md", "created": "2025-01-01"}], "status": "red"}}}
    actions = analyze.build_position_actions(positions, lights, FULL_STRATEGY, analysis=analysis)
    thesis_actions = [a for a in actions if a["reason"].startswith("Abgelaufene Thesis")]
    assert len(thesis_actions) == 1
    assert thesis_actions[0]["isin"] == "IE00B57X3V84"


def test_position_actions_thesis_no_false_positive():
    """Thesis ohne passende Position erzeugt KEINEN erfundenen Vorschlag (fail-closed)."""
    positions = [
        {"isin": "US0378331005", "name": "Apple Inc.", "ticker": "AAPL", "category": "satellite", "value_eur": 500.0, "weight": 0.05, "sector": "Tech"},
    ]
    lights = {c: {"status": "green", "reason": "ok"} for c in analyze.TRAFFIC_LIGHT_CATEGORIES}
    lights["thesis_deadlines"] = {"status": "red", "reason": "x"}
    analysis = {"checks": {"thesis_deadlines": {"outdated": [{"file": "unrelated-company.md", "created": "2025-01-01"}], "status": "red"}}}
    actions = analyze.build_position_actions(positions, lights, FULL_STRATEGY, analysis=analysis)
    assert not any(a["reason"].startswith("Abgelaufene Thesis") for a in actions)


def test_match_thesis_to_position_helpers():
    """Helfer: _match_thesis_to_position matcht per ISIN/Ticker/Name, nie per reason-String."""
    positions = [
        {"isin": "US0378331005", "name": "Apple Inc.", "ticker": "AAPL", "category": "satellite", "value_eur": 100.0, "weight": 0.1},
        {"isin": "NL0010273215", "name": "ASML Holding", "ticker": "ASML", "category": "satellite", "value_eur": 100.0, "weight": 0.1},
    ]
    assert analyze._match_thesis_to_position("AAPL.md", positions)["isin"] == "US0378331005"  # type: ignore[index]
    assert analyze._match_thesis_to_position("asml-holding.md", positions)["isin"] == "NL0010273215"  # type: ignore[index]
    assert analyze._match_thesis_to_position("US0378331005.md", positions)["isin"] == "US0378331005"  # type: ignore[index]
    assert analyze._match_thesis_to_position("irgendwas.md", positions) is None


# --- Finding 3: 6-Monats-Performance aus dem Vorgaenger-Snapshot --------------


def _snapshot_6m(captured_at: str, holdings: list) -> dict:
    return {
        "schema_version": 2,
        "captured_at": captured_at,
        "portfolio": {"holdings": holdings},
        "transactions": [],
    }


def test_compute_position_perf_6m_uses_previous_snapshot():
    """Live-Pipeline: position_perf_6m wird aus dem Vorgaenger-Snapshot im
    6-Monats-Fenster berechnet (Wert-Differenz, absolut, kein Benchmark)."""
    current = {"holdings": [{"isin": "US0378331005", "value_eur": 1200.0, "quantity": 10, "quote_mid_price": 120.0}]}
    previous = _snapshot_6m(
        "2026-02-17T14:00:00+00:00",
        [{"isin": "US0378331005", "value_eur": 1000.0, "quantity": 10, "quote_mid_price": 100.0}],
    )
    perf = analyze.compute_position_perf_6m(current, previous, current_captured_at="2026-08-18T14:00:00+00:00")
    assert perf.get("US0378331005") == pytest.approx(0.2, abs=0.001)


def test_compute_position_perf_6m_fail_closed_outside_window():
    """Kein Snapshot im 6-Monats-Fenster -> leeres Ergebnis (fail-closed, kein SELL)."""
    current = {"holdings": [{"isin": "US0378331005", "value_eur": 1200.0, "quantity": 10}]}
    previous = _snapshot_6m(
        "2026-08-13T14:00:00+00:00",  # nur 5 Tage alt -> NICHT im 6-Monats-Fenster
        [{"isin": "US0378331005", "value_eur": 1000.0, "quantity": 10}],
    )
    perf = analyze.compute_position_perf_6m(current, previous)
    assert perf == {}


def test_compute_position_perf_6m_fail_closed_missing_values():
    """Fehlende Kurs-/Wertdaten (z.B. SUSE) -> ISIN fehlt im Ergebnis (fail-closed)."""
    current = {"holdings": [{"isin": "LU2722255754", "valuation": None, "valuation_currency": None}]}
    previous = _snapshot_6m(
        "2026-02-17T14:00:00+00:00",
        [{"isin": "LU2722255754", "valuation": None, "valuation_currency": None}],
    )
    perf = analyze.compute_position_perf_6m(current, previous)
    assert "LU2722255754" not in perf


def test_facts_package_position_perf_6m_wired():
    """facts.build_facts_package berechnet position_perf_6m aus previous_snapshot
    und reicht es in deterministic_summary durch."""
    from scripts import facts

    portfolio = {
        "holdings": [{"isin": "US0378331005", "value_eur": 1200.0, "quantity": 10, "quote_mid_price": 120.0}],
        "total_value_eur": 1200.0,
    }
    transactions: list = []
    analysis = {
        "checks": {
            "positions": {
                "total_value_eur": 1200.0,
                "positions": [{"isin": "US0378331005", "name": "Apple", "category": "satellite", "value_eur": 1200.0, "weight": 1.0, "sector": "Tech"}],
            },
            "core_satellite": {"core_ratio": 0.0, "status": "red"},
            "sector_concentration": {"max_sector": "Tech", "max_ratio": 1.0, "status": "red"},
            "single_position": {"max_position": {"name": "Apple", "weight": 1.0}, "status": "red"},
            "drift": {"drift": 1.0, "status": "red"},
            "turnover": {"turnover_ratio": 0.0, "status": "green"},
            "thesis_deadlines": {"outdated": [], "status": "green"},
            "trades_per_quarter": {"trade_count": 0, "max_trades_per_quarter": 5, "status": "green"},
        }
    }
    previous = _snapshot_6m(
        "2026-02-17T14:00:00+00:00",
        [{"isin": "US0378331005", "value_eur": 1000.0, "quantity": 10, "quote_mid_price": 100.0}],
    )
    package = facts.build_facts_package(
        portfolio, transactions, analysis, [], FULL_STRATEGY, mode="monday",
        data_quality={"status": "ok", "issues": []}, previous_snapshot=previous,
        current_captured_at="2026-08-18T14:00:00+00:00",
    )
    summary = package["deterministic_summary"]
    assert summary["position_perf_6m"].get("US0378331005") == pytest.approx(0.2, abs=0.001)
    # Fail-closed: ohne previous_snapshot kein perf-Wert, kein SELL aus der Regel.
    package_no_prev = facts.build_facts_package(
        portfolio, transactions, analysis, [], FULL_STRATEGY, mode="monday",
        data_quality={"status": "ok", "issues": []}, previous_snapshot=None,
    )
    assert package_no_prev["deterministic_summary"]["position_perf_6m"] == {}


# --- Neukaufideen: Quellen-/Duplikatpruefung (Phase D / §6a) ------------------


def test_news_independence_two_publishers():
    news = [
        {"title": "Neuigkeit", "source": "reuters", "published": "2026-08-01"},
        {"title": "Andere Headline", "source": "cnbc", "published": "2026-08-01"},
    ]
    result = filter_news.evaluate_news_independence(news)
    assert result["independent"] is True
    assert set(result["publishers"]) == {"cnbc", "reuters"}


def test_news_independence_yahoo_not_automatically_independent():
    news = [
        {"title": "Neuigkeit", "source": "yahoo_finance", "published": "2026-08-01"},
        {"title": "Andere Headline", "source": "yahoo_finance", "published": "2026-08-01"},
    ]
    result = filter_news.evaluate_news_independence(news)
    assert result["independent"] is False  # Yahoo zaehlt nicht automatisch unabhaengig


def test_news_duplicates_count_once():
    news = [
        {"title": "Gleiche Meldung", "source": "reuters", "published": "2026-08-01"},
        {"title": "Gleiche Meldung", "source": "cnbc", "published": "2026-08-01"},
        {"title": "Echte Zweitmeldung", "source": "marketwatch", "published": "2026-08-01"},
    ]
    result = filter_news.evaluate_news_independence(news)
    # Duplikat zaehlt nur einmal -> Publisher: reuters + marketwatch = 2 unabhaengig
    assert result["independent"] is True
    assert "gleiche meldung" in result["duplicate_titles"]


# --- Zusatz: Neukaufideen-Newsbasis in der Pipeline (gezielte Recherche) ------


def test_fetch_news_for_unlisted_ideas_uses_candidate_isins(monkeypatch):
    """fetch_news_for_unlisted_ideas recherchiert gezielt unbekannte Wertpapiere
    (Kandidaten-ISINs/-Namen), nicht nur Bestandspositionen."""
    captured: dict = {}
    monkeypatch.setattr(
        filter_news,
        "fetch_news_for_keywords",
        lambda keywords: (captured.setdefault("keywords", keywords), [])[1],
    )
    portfolio = {"holdings": [{"isin": "US0378331005", "name": "Apple"}, {"isin": "LU2722255754", "name": "SUSE"}]}
    result = filter_news.fetch_news_for_unlisted_ideas(portfolio, candidate_isins=["US5949724083"], candidate_names=["NVIDIA"])
    assert result == []
    assert "US5949724083" in captured["keywords"]
    assert "NVIDIA" in captured["keywords"]


def test_fetch_news_for_unlisted_ideas_default_unmapped_isins(monkeypatch):
    """Ohne explizite Kandidaten werden unbekannte (nicht im etf_lookup gemappte)
    ISINs des Portfolios als potenzielle Neukauf-Kandidaten recherchiert."""
    captured: dict = {}
    monkeypatch.setattr(
        filter_news,
        "fetch_news_for_keywords",
        lambda keywords: (captured.setdefault("keywords", keywords), [])[1],
    )
    portfolio = {"holdings": [{"isin": "US0378331005", "name": "Apple"}, {"isin": "LU2722255754", "name": "SUSE"}]}
    filter_news.fetch_news_for_unlisted_ideas(portfolio)
    # LU2722255754 ist nicht im etf_lookup -> Kandidat; US0378331005 auch nicht gemappt.
    assert "LU2722255754" in captured["keywords"]


def test_run_briefing_merges_idea_news_in_production(monkeypatch, tmp_path, portfolio, transactions):
    """Produktiver Lauf: gezielt recherchierte Neukauf-News werden zu den
    Bestands-News hinzugefuegt (keine Duplikate)."""
    from scripts import analyze as analyze_mod
    from scripts import llm_briefing, llm_review, run_briefing, send_telegram

    monkeypatch.setattr(run_briefing, "_setup_logging", lambda: None)
    monkeypatch.setattr(run_briefing, "VAULT_DIR", tmp_path)
    previous = {
        "schema_version": 2,
        "captured_at": "2026-08-06T10:00:00+00:00",
        "portfolio": {"holdings": portfolio["holdings"]},
        "transactions": transactions,
    }
    monkeypatch.setattr(snapshot, "load_previous", lambda: previous)

    def _capture_staged(p, t, **kw):
        return {"snapshot": snapshot.build_snapshot(p, t, **kw), "staged_path": str(tmp_path / "s.json")}

    monkeypatch.setattr(snapshot, "capture_staged", _capture_staged)
    monkeypatch.setattr(snapshot, "promote_staged", lambda: {"snapshot": {}, "previous": previous, "archive_path": None})
    monkeypatch.setattr(snapshot, "discard_staged", lambda: True)
    monkeypatch.setattr(sc_bridge, "refresh_from_sc", lambda: (portfolio, transactions))
    # Watchlist-Abruf (Phase 6 Interface): produktiver Abruf ist fail-closed;
    # eine leere Watchlist ist ein legitimer Zustand (kein Mock-Fallback).
    monkeypatch.setattr(sc_bridge, "fetch_watchlist_from_sc", lambda: [])
    # Leere Analyse -> keine deterministischen Briefing-Entscheidungen (kein Label-Zwang).
    monkeypatch.setattr(analyze_mod, "analyze_portfolio", lambda p, t, s: {"checks": {}})

    captured = {}

    def _fake_fetch(p):
        captured["base"] = [{"title": "Apple News", "source": "reuters", "published": "2026-08-01"}]
        return captured["base"]

    def _fake_idea_news(p):
        captured["idea"] = [
            {"title": "NVIDIA News", "source": "cnbc", "published": "2026-08-01"},
            {"title": "Apple News", "source": "marketwatch", "published": "2026-08-01"},  # Duplikat
        ]
        return captured["idea"]

    monkeypatch.setattr(filter_news, "fetch_and_filter_news", _fake_fetch)
    monkeypatch.setattr(filter_news, "fetch_news_for_unlisted_ideas", _fake_idea_news)

    # Draft-Nachrichten-Referenz prueft nur Titel, die im Faktenpaket sind.
    seen_titles = set()

    def _draft(facts_package, mode="monday"):
        seen_titles.update(n.get("title", "") for n in facts_package.get("news", []))
        return (
            "## Kurzlage\nOK\n\n## Datenqualität\n—\n\n## Entscheidungsrelevante Punkte\n—\n\n"
            "## Strategie-Abgleich\n—\n\n## Relevante News & Veränderungen\nApple News.\n\n"
            "## Empfehlung\nWATCH — kein Handlungsbedarf."
        )

    monkeypatch.setattr(llm_briefing, "generate_draft", _draft)
    monkeypatch.setattr(llm_review, "review_draft", lambda facts_package, draft: {"findings": [], "overall_verdict": "pass"})
    sent = []
    monkeypatch.setattr(send_telegram, "send_briefing", lambda text, mode: sent.append((text, mode)) or True)

    rc = run_briefing.run("monday", dry_run=False)

    assert rc == 0
    assert "base" in captured and "idea" in captured
    # Die News im Faktenpaket enthalten die gezielte Recherche; Apple-News nur einmal
    # (Duplikat aus der Ideen-Recherche wird nicht doppelt uebernommen).
    assert "NVIDIA News" in seen_titles
    assert "Apple News" in seen_titles


# --- Doku-Generierung (Phase B / §7) ------------------------------------------


def test_render_strategy_doc_values_match_strategy(monkeypatch, tmp_path):
    from scripts import render_strategy_doc

    strategy_path = tmp_path / "strategy.yaml"
    strategy_path.write_text(
        yaml.safe_dump(FULL_STRATEGY, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    # Neutrale Antworten-Fixture — der Test darf nicht von lokalen, nicht
    # versionierten persönlichen Daten (config/setup/answers.reviewed.yaml) abhängen.
    answers_path = tmp_path / "answers.reviewed.yaml"
    answers_path.write_text(
        yaml.safe_dump(
            {
                "answers": {
                    "investor.personal_context": {
                        "value": "Beispiel: separate private Vorsorge (bewusst informativ)"
                    }
                }
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(render_strategy_doc, "STRATEGY_PATH", strategy_path)
    monkeypatch.setattr(render_strategy_doc, "ANSWERS_REVIEWED_PATH", answers_path)
    doc = render_strategy_doc.render_strategy_doc()
    assert "70.0%" in doc  # Zielquote aus Fixture
    assert "5" in doc  # max. Trades pro Quartal
    assert "private Vorsorge" in doc  # bewusst informative Antwort sichtbar
    assert "SUSE" in doc  # Abgrenzung


def test_strategy_hash_version_consistency():
    strategy = json.loads(json.dumps(FULL_STRATEGY))
    h1 = analyze.strategy_hash(strategy)
    strategy["portfolio"]["core_pct"] = 75.0
    h2 = analyze.strategy_hash(strategy)
    assert h1 != h2
