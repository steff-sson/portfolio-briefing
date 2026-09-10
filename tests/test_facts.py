"""Unit tests for scripts.facts: determinism, completeness, JSON serializability."""
from __future__ import annotations

import json
import re

from scripts import analyze, facts


def _analysis_fixture() -> dict:
    """Hand-built analysis with Grenzwert-/Analysebefunde (deterministic, env-free)."""
    return {
        "generated_at": "2026-08-13T09:00:00+02:00",
        "overall_status": "red",
        "checks": {
            "positions": {
                "total_value_eur": 10000.0,
                "positions": [
                    {"isin": "US0378331005", "name": "Apple Inc.", "category": "satellite", "value_eur": 6000.0, "weight": 0.6}
                ],
            },
            "core_satellite": {"core_ratio": 0.4, "status": "green"},
            "sector_concentration": {"max_sector": "Technology", "max_ratio": 0.6, "status": "red"},
            "single_position": {"max_position": {"name": "Apple Inc.", "weight": 0.6}, "status": "red"},
            "drift": {"drift": 0.09, "status": "red"},
            "turnover": {"turnover_ratio": 0.02, "status": "green"},
            "thesis_deadlines": {"outdated": [{"file": "apple.md", "created": "2025-01-01"}], "status": "red"},
        },
    }


def test_full_package_from_mock_data(portfolio, transactions):
    """Vollstaendige Daten: Struktur, Durchreichung, summary aus analysis-Checks."""
    strategy = analyze.load_strategy()
    analysis = analyze.analyze_portfolio(portfolio, transactions, strategy)
    package = facts.build_facts_package(portfolio, transactions, analysis, news=[], strategy=strategy, mode="monday")

    assert set(package) == {
        "meta", "portfolio", "analysis", "news", "strategy", "transactions", "changes",
        "data_quality", "strategy_diff", "triggers", "watchlist",
        "deterministic_summary", "strategy_thresholds_pct", "open_points",
    }
    assert set(package["meta"]) == {"mode", "generated_at", "pipeline_version"}
    assert package["meta"]["mode"] == "monday"
    assert package["meta"]["pipeline_version"] == "2.0"

    # Briefing-Schnittstelle (Plan §6a): Ampel (7 Kategorien), Empfehlung,
    # Positionsvorschlaege — deterministisch aus analyze abgeleitet.
    summary = package["deterministic_summary"]
    assert set(summary["traffic_lights"]) == set(analyze.TRAFFIC_LIGHT_CATEGORIES)
    assert summary["recommendation"]["label"] in ("BUY", "SELL", "WATCH")
    assert isinstance(summary["position_actions"], list)
    assert len(summary["position_actions"]) <= 3

    # Durchgereichte Daten unveraendert (identische Objekte); changes fehlt -> None
    assert package["portfolio"] is portfolio
    assert package["analysis"] is analysis
    assert package["strategy"] is strategy
    assert package["transactions"] is transactions
    assert package["news"] == []
    assert package["changes"] is None
    assert package["watchlist"] == []  # ohne watchlist-Argument -> leere Watchlist
    # Watchlist-/Satellite-Signale ohne Watchlist-Daten: leere Listen (kein Crash)
    assert package["deterministic_summary"]["watchlist_signals"] == []
    assert package["deterministic_summary"]["satellite_sell_signals"] == []
    # Neue deterministische Felder: data_quality/strategy_diff None ohne Eingabe,
    # triggers deterministisch berechnet
    assert package["data_quality"] is None
    assert package["strategy_diff"] is None
    assert set(package["triggers"]) == {
        "has_boundary_violation", "has_relevant_changes", "has_thesis_news",
        "has_strategy_change", "ordered", "has_any_trigger",
    }

    # Prozent-Grenzwerte direkt aus der geladenen strategy.yaml (nie neu berechnet);
    # Erwartungswerte aus derselben Strategie abgeleitet, damit der Test unabhaengig
    # von den konkreten Werten der (gitignored) strategy.yaml bleibt.
    assert package["strategy_thresholds_pct"] == {
        "core_pct": strategy["portfolio"]["core_pct"],
        "satellite_pct": strategy["portfolio"]["satellite_pct"],
        "threshold_pct": strategy.get("portfolio", {}).get("rebalancing", {}).get("threshold_pct", 0.0),
        "max_position_pct": strategy.get("satellite_limits", {}).get("max_position_pct", 0.0),
        "max_sector_pct": strategy.get("satellite_limits", {}).get("max_sector_pct", 0.0),
        "max_turnover_annual_pct": strategy.get("satellite_limits", {}).get("max_turnover_annual_pct", 0.0),
    }

    summary = package["deterministic_summary"]
    assert summary["total_value_eur"] == 9680.0  # aus Mock-Portfolio
    assert summary["position_count"] == 6
    # Werte stammen aus denselben analysis-Checks (nie neu berechnet)
    assert summary["core_ratio"] == analysis["checks"]["core_satellite"]["core_ratio"]
    assert summary["max_position_weight"] == analysis["checks"]["single_position"]["max_position"]["weight"]
    assert summary["max_position_name"] == "Apple Inc."
    assert summary["max_sector"] == analysis["checks"]["sector_concentration"]["max_sector"]
    assert summary["max_sector_ratio"] == analysis["checks"]["sector_concentration"]["max_ratio"]
    assert summary["drift"] == analysis["checks"]["drift"]["drift"]
    assert summary["turnover_ratio"] == analysis["checks"]["turnover"]["turnover_ratio"]
    assert summary["outdated_theses"] == analysis["checks"]["thesis_deadlines"]["outdated"]

    # Status-Listen decken die 6 klassischen Status-Checks ab (Plan §6a: die
    # 7. Kategorie trades_per_quarter lebt in traffic_lights, nicht in den
    # red/yellow/green-Listen der bestehenden Struktur).
    for name in facts._STATUS_CHECKS:
        check = analysis["checks"][name]
        bucket = {"red": summary["red_checks"], "yellow": summary["yellow_checks"], "green": summary["green_checks"]}[check["status"]]
        assert name in bucket
    assert len(summary["red_checks"]) + len(summary["yellow_checks"]) + len(summary["green_checks"]) == len(facts._STATUS_CHECKS)
    assert "trades_per_quarter" in summary["traffic_lights"]


def test_empty_news(portfolio, transactions):
    """Leere News: news == [], Paketstruktur bleibt vollstaendig."""
    strategy = analyze.load_strategy()
    analysis = analyze.analyze_portfolio(portfolio, transactions, strategy)
    package = facts.build_facts_package(portfolio, transactions, analysis, news=[], strategy=strategy, mode="friday")

    assert package["news"] == []
    assert package["meta"]["mode"] == "friday"
    assert set(package["deterministic_summary"]) == {
        "total_value_eur", "position_count", "core_ratio", "max_position_weight", "max_position_name",
        "max_sector", "max_sector_ratio", "drift", "turnover_ratio", "outdated_theses",
        "red_checks", "yellow_checks", "green_checks",
        "data_quality_status", "data_quality_issues", "has_triggers",
        "traffic_lights", "recommendation", "position_actions",
        "position_perf_6m", "watchlist_signals", "satellite_sell_signals",
        # Phase 3: Positions-/Sektor-Details (additiv)
        "positions_detail", "sectors_detail",
    }


def test_analysis_findings_and_thresholds(portfolio, transactions):
    """Grenzwert-/Analysebefunde: red/yellow/green-Splitting, Schwellwerte, max-Position."""
    analysis = _analysis_fixture()
    package = facts.build_facts_package(portfolio, transactions, analysis, news=[], strategy={}, mode="monthly")

    summary = package["deterministic_summary"]
    # Sortierte (deterministische) Status-Listen; positions ohne Status fehlt
    assert summary["red_checks"] == ["drift", "sector_concentration", "single_position", "thesis_deadlines"]
    assert summary["green_checks"] == ["core_satellite", "turnover"]
    assert summary["yellow_checks"] == []
    # Werte aus den Befunden
    assert summary["total_value_eur"] == 10000.0  # aus analysis-checks, nicht aus portfolio
    assert summary["position_count"] == 1
    assert summary["max_position_weight"] == 0.6
    assert summary["max_position_name"] == "Apple Inc."
    assert summary["max_sector"] == "Technology"
    assert summary["max_sector_ratio"] == 0.6
    assert summary["drift"] == 0.09
    assert summary["turnover_ratio"] == 0.02
    assert summary["outdated_theses"] == [{"file": "apple.md", "created": "2025-01-01"}]
    # Leere Strategie: alle Grenzwerte 0.0 (verify skippt sie -> fail-closed)
    assert package["strategy_thresholds_pct"] == {
        "core_pct": 0.0,
        "satellite_pct": 0.0,
        "threshold_pct": 0.0,
        "max_position_pct": 0.0,
        "max_sector_pct": 0.0,
        "max_turnover_annual_pct": 0.0,
    }


def test_strategy_thresholds_extracted_from_strategy(portfolio, transactions):
    """Echte Prozent-Grenzwerte werden deterministisch aus der Strategie extrahiert."""
    strategy = {
        "portfolio": {
            "core_pct": 80.0,
            "satellite_pct": 20.0,
            "rebalancing": {"threshold_pct": 7.5},
        },
        "satellite_limits": {
            "max_position_pct": 6.0,
            "max_sector_pct": 18.0,
            "max_turnover_annual_pct": 40.0,
        },
    }
    package = facts.build_facts_package(portfolio, transactions, {}, news=[], strategy=strategy, mode="monday")
    assert package["strategy_thresholds_pct"] == {
        "core_pct": 80.0,
        "satellite_pct": 20.0,
        "threshold_pct": 7.5,
        "max_position_pct": 6.0,
        "max_sector_pct": 18.0,
        "max_turnover_annual_pct": 40.0,
    }


def test_partial_strategy_defaults_to_zero(portfolio, transactions):
    """Unvollstaendige Strategie: fehlende Felder -> 0.0 (keine Neuberechnung)."""
    package = facts.build_facts_package(
        portfolio, transactions, {}, news=[], strategy={"portfolio": {"core_pct": 70.0}}, mode="monday"
    )
    assert package["strategy_thresholds_pct"] == {
        "core_pct": 70.0,
        "satellite_pct": 0.0,
        "threshold_pct": 0.0,
        "max_position_pct": 0.0,
        "max_sector_pct": 0.0,
        "max_turnover_annual_pct": 0.0,
    }


def test_empty_portfolio_defaults(transactions):
    """Leeres Portfolio: max_position None -> Defaults, keine Fehler."""
    strategy = analyze.load_strategy()
    analysis = {
        "generated_at": "2026-08-13T09:00:00+02:00",
        "overall_status": "green",
        "checks": {
            "positions": {"total_value_eur": 0.0, "positions": []},
            "core_satellite": {"core_ratio": 0.0, "status": "green"},
            "sector_concentration": {"max_sector": "Unknown", "max_ratio": 0.0, "status": "green"},
            "single_position": {"max_position": None, "status": "green"},
            "drift": {"drift": 0.0, "status": "green"},
            "turnover": {"turnover_ratio": 0.0, "status": "green"},
            "thesis_deadlines": {"outdated": [], "status": "green"},
        },
    }
    package = facts.build_facts_package(
        {"total_value_eur": 0.0, "holdings": []}, transactions, analysis, news=[], strategy=strategy, mode="monday"
    )
    summary = package["deterministic_summary"]
    assert summary["max_position_weight"] == 0.0
    assert summary["max_position_name"] == ""
    assert summary["position_count"] == 0
    assert summary["red_checks"] == []
    assert summary["yellow_checks"] == []
    assert summary["green_checks"] == [
        "core_satellite", "drift", "sector_concentration", "single_position", "thesis_deadlines", "turnover",
    ]


def test_missing_checks_default_to_portfolio(portfolio, transactions):
    """Defensive Defaults: fehlende checks -> 0-Werte, portfolio-Fallbacks."""
    package = facts.build_facts_package(portfolio, transactions, {"checks": {}}, news=[], strategy={}, mode="monday")
    summary = package["deterministic_summary"]
    assert summary["total_value_eur"] == 9680.0  # Fallback auf portfolio
    assert summary["position_count"] == 6  # Fallback auf holdings
    assert summary["core_ratio"] == 0.0
    assert summary["max_position_weight"] == 0.0
    assert summary["max_position_name"] == ""
    assert summary["max_sector"] == ""
    assert summary["max_sector_ratio"] == 0.0
    assert summary["drift"] == 0.0
    assert summary["turnover_ratio"] == 0.0
    assert summary["outdated_theses"] == []
    assert summary["red_checks"] == []
    assert summary["yellow_checks"] == []
    assert summary["green_checks"] == []
    # Leere Strategie -> alle Grenzwerte 0.0, Struktur bleibt vollstaendig
    assert set(package["strategy_thresholds_pct"]) == {
        "core_pct", "satellite_pct", "threshold_pct", "max_position_pct", "max_sector_pct",
        "max_turnover_annual_pct",
    }


def test_deterministic_output(portfolio, transactions):
    """Determinismus: gleiche Eingaben -> identisches Paket (bis auf generated_at)."""
    strategy = analyze.load_strategy()
    analysis = analyze.analyze_portfolio(portfolio, transactions, strategy)
    first = facts.build_facts_package(portfolio, transactions, analysis, news=[], strategy=strategy, mode="monday")
    second = facts.build_facts_package(portfolio, transactions, analysis, news=[], strategy=strategy, mode="monday")

    assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}$", first["meta"]["generated_at"])
    first["meta"].pop("generated_at")
    second["meta"].pop("generated_at")
    assert first == second


def test_json_serializable(portfolio, transactions):
    """JSON-Serialisierbarkeit: json.dumps + roundtrip ohne Verlust."""
    strategy = analyze.load_strategy()
    analysis = analyze.analyze_portfolio(portfolio, transactions, strategy)
    package = facts.build_facts_package(portfolio, transactions, analysis, news=[], strategy=strategy, mode="monthly")

    dumped = json.dumps(package, ensure_ascii=False)
    assert json.loads(dumped) == package


# --- changes: Durchreichung + LLM-Reduktion ----------------------------------


def test_changes_passed_through_default_none(portfolio, transactions):
    """changes-Default None (Erstlauf/Dry-Run); uebergebenes Diff unveraendert durchgereicht."""
    package = facts.build_facts_package(portfolio, transactions, {}, news=[], strategy={}, mode="monday")
    assert package["changes"] is None

    changes = {
        "has_previous": True,
        "positions": {"added": [], "removed": [], "changed": []},
        "totals": {"prev_total_value_eur": 9680.0, "total_value_eur": 9680.0, "delta_eur": 0.0, "delta_pct": 0.0},
        "transactions": {
            "prev_count": 4, "count": 4, "added_count": 0, "removed_count": 0, "added": [], "removed": [],
        },
    }
    package = facts.build_facts_package(
        portfolio, transactions, {}, news=[], strategy={}, mode="monday", changes=changes
    )
    assert package["changes"] is changes  # Durchreichung ohne Mutation
    assert json.loads(json.dumps(package))["changes"] == changes  # JSON-serialisierbar


def test_reduce_changes_for_llm_strips_raw_transaction_records():
    """LLM-Reduktion: Transaktions-Records (added/removed) entfernt, Aggregate bleiben."""
    changes = {
        "has_previous": True,
        "positions": {
            "added": [{"isin": "IE00B3XXRP09", "name": "Vanguard FTSE EM", "category": "core", "value_eur": 100.0}],
            "removed": [],
            "changed": [],
        },
        "totals": {"prev_total_value_eur": 9680.0, "total_value_eur": 9780.0, "delta_eur": 100.0, "delta_pct": 1.03},
        "transactions": {
            "prev_count": 4,
            "count": 5,
            "added_count": 1,
            "removed_count": 0,
            "added": [{"date": "2026-08-01", "isin": "IE00B3XXRP09", "type": "buy", "quantity": 2.0, "price_eur": 50.0}],
            "removed": [],
        },
    }
    before = json.loads(json.dumps(changes))
    reduced = facts.reduce_changes_for_llm(changes)
    assert isinstance(reduced, dict)

    assert changes == before  # Input nicht mutiert
    assert reduced["has_previous"] is True
    assert reduced["transactions"]["added_count"] == 1  # Aggregate bleiben
    assert reduced["transactions"]["prev_count"] == 4
    assert "added" not in reduced["transactions"]  # Records entfernt
    assert "removed" not in reduced["transactions"]
    json.dumps(reduced)  # JSON-serialisierbar
    # None (Erstlauf/Dry-Run) bleibt None; Diffs ohne Transaktionen unveraendert
    assert facts.reduce_changes_for_llm(None) is None
    assert facts.reduce_changes_for_llm({"has_previous": False}) == {"has_previous": False}


# --- compute_triggers --------------------------------------------------------


def _trigger_package(**overrides) -> dict:
    pkg = {
        "deterministic_summary": {},
        "data_quality": None,
        "changes": None,
        "strategy_diff": None,
        "news": [],
    }
    pkg.update(overrides)
    return pkg


def test_compute_triggers_none_with_empty_package():
    result = facts.compute_triggers(_trigger_package())
    assert result == {
        "has_boundary_violation": False,
        "has_relevant_changes": False,
        "has_thesis_news": False,
        "has_strategy_change": False,
        "ordered": [],
        "has_any_trigger": False,
    }


def test_compute_triggers_boundary_violation_red_and_yellow():
    red = facts.compute_triggers(_trigger_package(deterministic_summary={"red_checks": ["drift"]}))
    assert red["has_boundary_violation"] is True
    assert red["ordered"] == ["boundary_violation"]

    yellow = facts.compute_triggers(
        _trigger_package(deterministic_summary={"red_checks": [], "yellow_checks": ["single_position"]})
    )
    assert yellow["has_boundary_violation"] is True


def test_compute_triggers_data_quality_marker_is_not_boundary():
    """Der 'data_quality'-Marker in red_checks zaehlt nicht als Grenzverletzung."""
    result = facts.compute_triggers(_trigger_package(deterministic_summary={"red_checks": ["data_quality"]}))
    assert result["has_boundary_violation"] is False
    assert result["has_any_trigger"] is False


def test_compute_triggers_relevant_changes():
    changes = {
        "has_previous": True,
        "positions": {"added": [{"isin": "X"}], "removed": [], "changed": []},
        "transactions": {"added_count": 0, "removed_count": 0},
    }
    result = facts.compute_triggers(_trigger_package(changes=changes))
    assert result["has_relevant_changes"] is True
    assert result["ordered"] == ["relevant_changes"]


def test_compute_triggers_first_run_has_no_changes_trigger():
    """Erstlauf (has_previous False) -> keine erfundenen Aenderungen."""
    changes = {
        "has_previous": False,
        "positions": {"added": [{"isin": "X"}], "removed": [], "changed": []},
        "transactions": {"added_count": 0, "removed_count": 0},
    }
    result = facts.compute_triggers(_trigger_package(changes=changes))
    assert result["has_relevant_changes"] is False
    assert result["has_any_trigger"] is False


def test_compute_triggers_relevant_changes_via_transactions():
    changes = {
        "has_previous": True,
        "positions": {"added": [], "removed": [], "changed": []},
        "transactions": {"added_count": 1, "removed_count": 0},
    }
    result = facts.compute_triggers(_trigger_package(changes=changes))
    assert result["has_relevant_changes"] is True


def test_compute_triggers_ignores_pure_value_moves():
    """Reine Werte-Bewegungen (keine Positions-/Transaktions-Aenderungen) -> kein Trigger."""
    changes = {
        "has_previous": True,
        "positions": {"added": [], "removed": [], "changed": []},
        "transactions": {"added_count": 0, "removed_count": 0},
        "totals": {"delta_eur": 193.6, "delta_pct": 2.0},
    }
    result = facts.compute_triggers(_trigger_package(changes=changes))
    assert result["has_relevant_changes"] is False
    assert result["has_any_trigger"] is False


def test_compute_triggers_thesis_news():
    result = facts.compute_triggers(_trigger_package(news=[{"title": "x", "thesis_relevant": True}]))
    assert result["has_thesis_news"] is True
    assert result["ordered"] == ["thesis_news"]

    not_relevant = facts.compute_triggers(_trigger_package(news=[{"title": "x", "thesis_relevant": False}]))
    assert not_relevant["has_thesis_news"] is False

    non_dict = facts.compute_triggers(_trigger_package(news=["nur-ein-string"]))
    assert non_dict["has_thesis_news"] is False


def test_compute_triggers_strategy_change():
    result = facts.compute_triggers(_trigger_package(strategy_diff={"has_changed": True}))
    assert result["has_strategy_change"] is True
    assert result["ordered"] == ["strategy_change"]

    no_change = facts.compute_triggers(_trigger_package(strategy_diff={"has_changed": False}))
    assert no_change["has_strategy_change"] is False


def test_compute_triggers_fixed_order_with_all_triggers():
    """Feste Reihenfolge: data_quality > strategy_change > boundary > changes > thesis_news."""
    pkg = _trigger_package(
        deterministic_summary={"red_checks": ["drift"], "yellow_checks": []},
        data_quality={"status": "incomplete", "issues": ["keine Holdings"]},
        changes={
            "has_previous": True,
            "positions": {"added": [{"isin": "X"}], "removed": [], "changed": []},
            "transactions": {"added_count": 0, "removed_count": 0},
        },
        strategy_diff={"has_changed": True},
        news=[{"title": "x", "thesis_relevant": True}],
    )
    result = facts.compute_triggers(pkg)
    assert result["has_any_trigger"] is True
    assert result["ordered"] == [
        "data_quality",
        "strategy_change",
        "boundary_violation",
        "relevant_changes",
        "thesis_news",
    ]


def test_compute_triggers_data_quality_precedence_suppresses_boundary():
    """Datenqualitaet hat Vorrang: 'data_quality' zuerst; die Suppression im
    summary (red_checks == ['data_quality']) blockiert die Grenzverletzung,
    Portfolio-Neuigkeiten bleiben aber als Trigger erhalten."""
    pkg = _trigger_package(
        deterministic_summary={"red_checks": ["data_quality"], "yellow_checks": []},
        data_quality={"status": "stale", "issues": ["Vorgaenger-Snapshot zu alt"]},
        changes={
            "has_previous": True,
            "positions": {"added": [], "removed": [], "changed": [{"isin": "X"}]},
            "transactions": {"added_count": 0, "removed_count": 0},
        },
    )
    result = facts.compute_triggers(pkg)
    assert result["ordered"][0] == "data_quality"
    assert "boundary_violation" not in result["ordered"]
    assert result["has_boundary_violation"] is False
    assert "relevant_changes" in result["ordered"]


def test_compute_triggers_data_quality_ok_is_not_a_trigger():
    pkg = _trigger_package(
        data_quality={"status": "ok", "issues": []},
        deterministic_summary={"red_checks": ["drift"], "yellow_checks": []},
    )
    result = facts.compute_triggers(pkg)
    assert "data_quality" not in result["ordered"]
    assert result["ordered"] == ["boundary_violation"]


# --- Phase 5b: Watchlist-/Satellite-Signale im Faktenpaket -------------------
#
# facts.build_facts_package bindet das deterministische Signalmodell
# (analyze.compute_watchlist_signals) ein: summary["watchlist_signals"] und
# summary["satellite_sell_signals"]. Keine neue Signal-Logik hier — nur die
# Einbindung, die Ausschlussregeln (Core-ETFs/SUSE-Legacy) und der
# Fundamentaldaten-Disclaimer (fundamentals_used: false) werden getestet.

_SIGNAL_STRATEGY = {
    "portfolio": {
        "core_pct": 80.0,
        "satellite_pct": 20.0,
        "rebalancing": {"threshold_pct": 5.0},
    },
    "satellite_limits": {
        "target_position_pct": 5.0,
        "max_position_pct": 10.0,
        "max_sector_pct": 20.0,
        "max_turnover_annual_pct": 30.0,
    },
    "sectors": {"preferred": ["technology", "ai", "energy"], "excluded": ["fossil_fuels", "defense"]},
}

_SIGNAL_PORTFOLIO = {
    "total_value_eur": 20000.0,
    "holdings": [
        {"isin": "IE00BK5BQT80", "name": "Vanguard FTSE All-World", "category": "core", "value_eur": 16000.0},
        {"isin": "US88579Y1010", "name": "3M Co.", "category": "satellite", "value_eur": 1000.0, "sector": "industrials"},
    ],
}

_SIGNAL_ANALYSIS = {
    "checks": {
        "positions": {
            "positions": [
                {"isin": "US88579Y1010", "name": "3M Co.", "category": "satellite", "value_eur": 1000.0, "weight": 0.05},
            ],
        },
        "sector_concentration": {},
        "single_position": {},
    },
}


def _signal_watchlist() -> list:
    """Watchlist: NVIDIA (BUY-Kandidat), 3M (SELL-Kandidat, gehalten),
    Vanguard Core-ETF (Ausschluss) und SUSE (Legacy-Ausschluss)."""
    return [
        {"isin": "US5949724083", "name": "NVIDIA Corp.", "category": "satellite", "sector": "technology", "value_eur": 500.0},
        {"isin": "US88579Y1010", "name": "3M Co.", "category": "satellite", "sector": "industrials", "value_eur": 1000.0},
        {"isin": "IE00BK5BQT80", "name": "Vanguard FTSE All-World UCITS ETF", "category": "core", "sector": "Diversified"},
        {"isin": "LU2722255754", "name": "SUSE", "category": "unknown", "sector": "software"},
    ]


def _signal_news() -> list:
    return [
        {"title": "NVIDIA meldet Rekord-Gewinn und starkes Wachstum", "summary": "", "source": "test"},
        {"title": "MMM (3M Co.) verliert weiter — Absturz und Verlustwarnung", "summary": "", "source": "test"},
    ]


def test_watchlist_signals_bound_into_summary():
    """Phase 5b: Signalmodell wird eingebunden — summary traegt beide Listen.

    NVIDIA (praeferierter Sektor + positive News) -> BUY in der Watchlist;
    3M (bestehende Satellite-Holding, negative News) -> SELL, und zwar in
    beiden Sektionen (Watchlist-Auswahl + Sell-/Reduce-Sektion).
    """
    package = facts.build_facts_package(
        _SIGNAL_PORTFOLIO,
        [],
        _SIGNAL_ANALYSIS,
        _signal_news(),
        _SIGNAL_STRATEGY,
        mode="monday",
        watchlist=_signal_watchlist(),
    )
    summary = package["deterministic_summary"]
    assert package["watchlist"] == _signal_watchlist()  # unveraendert durchgereicht

    buy = next(s for s in summary["watchlist_signals"] if s["isin"] == "US5949724083")
    assert buy["signal"] == "BUY"
    assert buy["score"] >= 3
    assert buy["fundamentals_used"] is False  # Fundamentaldaten-Disclaimer erhalten

    sell = next(s for s in summary["satellite_sell_signals"] if s["isin"] == "US88579Y1010")
    assert sell["signal"] == "SELL"
    assert sell["fundamentals_used"] is False


def test_signal_exclusions_core_etf_and_suse_legacy():
    """Ausschlussregeln: Core-ETF-Sparplan und SUSE/Legacy -> nie BUY/SELL.

    Der Vanguard Core-ETF (category core) und SUSE (LU2722255754, illiquide
    Legacy) sind weder Watchlist-Signale noch Sell-/Reduce-Kandidaten — die
    Renderer-Sektionen zeigen sie nicht als Trade. NO-SIGNAL-Eintraege
    (inkl. Ausschluss) werden in der Auswahl nicht uebernommen.
    """
    package = facts.build_facts_package(
        _SIGNAL_PORTFOLIO,
        [],
        _SIGNAL_ANALYSIS,
        _signal_news(),
        _SIGNAL_STRATEGY,
        mode="monday",
        watchlist=_signal_watchlist(),
    )
    summary = package["deterministic_summary"]
    all_signals = summary["watchlist_signals"] + summary["satellite_sell_signals"]
    isins = {s["isin"] for s in all_signals}
    # Core-ETF und SUSE/Legacy tauchen nirgends als Trade-Kandidat auf.
    assert "IE00BK5BQT80" not in isins
    assert "LU2722255754" not in isins
    # NVIDIA (BUY) und 3M (SELL) sind als einzige Kandidaten vertreten.
    assert "US5949724083" in isins
    assert "US88579Y1010" in isins
    # Kein einziges Signal-Objekt mit excluded=True in der Auswahl (gefiltert).
    assert not any(s.get("excluded") for s in all_signals)


def test_watchlist_signals_without_watchlist_are_empty(portfolio, transactions):
    """Ohne Watchlist-Argument (None/fehlend): leere Signal-Listen, kein Crash."""
    package = facts.build_facts_package(
        portfolio, transactions, _SIGNAL_ANALYSIS, news=[], strategy=_SIGNAL_STRATEGY, mode="monday"
    )
    assert package["watchlist"] == []
    assert package["deterministic_summary"]["watchlist_signals"] == []
    assert package["deterministic_summary"]["satellite_sell_signals"] == []
    json.dumps(package)  # JSON-serialisierbar


def test_signal_selection_max_three_and_deterministic_rank():
    """Max. 3 Watchlist-Signale; feste Rangfolge SELL > REDUCE > BUY > AVOID > WATCH.

    Die Auswahl ist deterministisch (Label-Rang zuerst, dann Score absteigend),
    unabhaengig von der Eingabe-Reihenfolge der Signale.
    """
    signals = [
        {"isin": "W1", "signal": "WATCH", "score": 5, "excluded": False},
        {"isin": "B2", "signal": "BUY", "score": 2, "excluded": False},
        {"isin": "S1", "signal": "SELL", "score": -5, "excluded": False},
        {"isin": "R1", "signal": "REDUCE", "score": -1, "excluded": False},
        {"isin": "A1", "signal": "AVOID", "score": -3, "excluded": False},
    ]
    selected = facts._select_watchlist_signals(signals)
    assert len(selected) == 3
    assert [s["isin"] for s in selected] == ["S1", "R1", "B2"]  # SELL > REDUCE > BUY

    # NO-SIGNAL-Eintraege (inkl. Core-ETF/Legacy-Ausschluss) fliegen raus.
    with_no_signal = signals + [
        {"isin": "NS1", "signal": "NO SIGNAL", "score": 0, "excluded": True},
        {"isin": "NS2", "signal": "NO SIGNAL", "score": 0, "excluded": True},
    ]
    assert facts._select_watchlist_signals(with_no_signal) == selected

    # Eingabe-Reihenfolge aendert nichts (stabiler Index nur als Tiebreaker).
    reversed_selected = facts._select_watchlist_signals(list(reversed(signals)))
    assert [s["isin"] for s in reversed_selected] == ["S1", "R1", "B2"]


def test_satellite_sell_signals_only_selected_satellites():
    """Sell-/Reduce-Sektion: nur SELL/REDUCE, keine excludierten, kein BUY/WATCH.

    Core-ETFs und SUSE sind excluded (kein Kandidat); WATCH/BUY erscheinen
    nie in der Sell-/Reduce-Sektion.
    """
    signals = [
        {"isin": "S1", "signal": "SELL", "score": -3, "excluded": False},
        {"isin": "R1", "signal": "REDUCE", "score": -1, "excluded": False},
        {"isin": "B1", "signal": "BUY", "score": 4, "excluded": False},
        {"isin": "W1", "signal": "WATCH", "score": 1, "excluded": False},
        {"isin": "X1", "signal": "SELL", "score": -4, "excluded": True},  # Core-ETF/SUSE
    ]
    sell = facts._split_satellite_sell_signals(signals)
    assert [s["isin"] for s in sell] == ["S1", "R1"]
    assert facts._split_satellite_sell_signals(None) == []
    assert facts._split_satellite_sell_signals(["nicht-dict"]) == []


def test_signal_json_serializable_and_deterministic():
    """Signal-Sektionen: JSON-serialisierbar und deterministisch (gleiche
    Eingaben -> gleiche Auswahl)."""
    package_a = facts.build_facts_package(
        _SIGNAL_PORTFOLIO, [], _SIGNAL_ANALYSIS, _signal_news(), _SIGNAL_STRATEGY, mode="monday",
        watchlist=_signal_watchlist(),
    )
    package_b = facts.build_facts_package(
        _SIGNAL_PORTFOLIO, [], _SIGNAL_ANALYSIS, _signal_news(), _SIGNAL_STRATEGY, mode="monday",
        watchlist=_signal_watchlist(),
    )
    summary_a = package_a["deterministic_summary"]
    summary_b = package_b["deterministic_summary"]
    json.dumps(package_a)  # serialisierbar
    assert summary_a["watchlist_signals"] == summary_b["watchlist_signals"]
    assert summary_a["satellite_sell_signals"] == summary_b["satellite_sell_signals"]
    assert all(s.get("fundamentals_used") is False for s in summary_a["watchlist_signals"])
    assert all(s.get("fundamentals_used") is False for s in summary_a["satellite_sell_signals"])


# --- P7: offene Punkte als untrusted Kontext ----------------------------------
#
# facts.build_facts_package(open_points=...) reduziert die offenen Punkte
# (telegram_inbound.load_open_points) auf {text, received_at} mit
# `untrusted: true` und begrenzt sie deterministisch (MAX_OPEN_POINTS,
# MAX_OPEN_POINT_CHARS). Ohne Argument bleibt das Paket strukturell
# unveraendert (rueckwaertskompatibel).

def _open_point(message_id: int, text: str, received_at: str = "2026-08-26T14:30:00+00:00") -> dict:
    return {
        "message_id": message_id,
        "chat_id": "-1001234567890",
        "text": text,
        "received_at": received_at,
        "status": "open",
        "briefing_date": None,
    }


def test_open_points_default_is_empty_and_backward_compatible(portfolio, transactions):
    """Ohne open_points-Argument: Paket enthaelt leere Liste (deterministisch),
    sonst strukturell unveraendert — bestehende Aufrufer bleiben unveraendert."""
    package = facts.build_facts_package(portfolio, transactions, {}, news=[], strategy={}, mode="monday")
    assert package["open_points"] == []
    json.dumps(package)  # JSON-serialisierbar


def test_open_points_land_marked_and_reduced(portfolio, transactions):
    """Offene Punkte landen als {text, received_at} mit untrusted: true im
    Paket; message_id/chat_id/status/briefing_date werden reduziert."""
    open_points = [
        _open_point(1, "SUSE endlich bewerten lassen!"),
        _open_point(2, "Sektorlimit anpassen?"),
    ]
    package = facts.build_facts_package(
        portfolio, transactions, {}, news=[], strategy={}, mode="monday", open_points=open_points
    )
    assert package["open_points"] == [
        {"text": "SUSE endlich bewerten lassen!", "received_at": "2026-08-26T14:30:00+00:00", "untrusted": True},
        {"text": "Sektorlimit anpassen?", "received_at": "2026-08-26T14:30:00+00:00", "untrusted": True},
    ]
    # Reduktion: keine Persistenz-/Chat-Felder im Paket
    for entry in package["open_points"]:
        assert "message_id" not in entry
        assert "chat_id" not in entry
        assert "status" not in entry
        assert "briefing_date" not in entry
    json.dumps(package)  # JSON-serialisierbar
    # Input nicht mutiert
    assert open_points[0]["status"] == "open"


def test_open_points_not_in_deterministic_summary(portfolio, transactions):
    """Offene Punkte sind KEINE deterministischen Fakten: sie erscheinen
    nicht in deterministic_summary und veraendern summary nicht."""
    base = facts.build_facts_package(portfolio, transactions, {}, news=[], strategy={}, mode="monday")
    with_points = facts.build_facts_package(
        portfolio,
        transactions,
        {},
        news=[],
        strategy={},
        mode="monday",
        open_points=[_open_point(1, "Sektorlimit anpassen?")],
    )
    assert base["deterministic_summary"] == with_points["deterministic_summary"]
    assert "open_points" not in with_points["deterministic_summary"]


def test_open_points_truncated_and_capped(portfolio, transactions):
    """Deterministische Begrenzung: max. MAX_OPEN_POINTS Punkte, jeder Text
    auf MAX_OPEN_POINT_CHARS Zeichen gekuerzt; Persistenzdatei nie mutiert."""
    long_text = "x" * (facts.MAX_OPEN_POINT_CHARS + 100)
    many = [_open_point(i, long_text) for i in range(1, facts.MAX_OPEN_POINTS + 5)]
    package = facts.build_facts_package(
        portfolio, transactions, {}, news=[], strategy={}, mode="monday", open_points=many
    )
    assert len(package["open_points"]) == facts.MAX_OPEN_POINTS
    for entry in package["open_points"]:
        assert len(entry["text"]) == facts.MAX_OPEN_POINT_CHARS
    # Originalliste unangetastet (Persistenz/Paket-Input nie mutiert)
    assert len(many) == facts.MAX_OPEN_POINTS + 4
    assert len(many[0]["text"]) == len(long_text)
    # Deterministisch: identische Eingabe -> identische Begrenzung
    again = facts.build_facts_package(
        portfolio, transactions, {}, news=[], strategy={}, mode="monday", open_points=many
    )
    assert package["open_points"] == again["open_points"]


def test_open_points_skips_invalid_entries(portfolio, transactions):
    """Nicht-dict/leere Eintraege werden uebersprungen; None -> leere Liste."""
    open_points = [
        None,
        "nur-ein-string",
        {"message_id": 3, "text": "", "received_at": "x"},  # leerer Text
        {"message_id": 4, "text": "gueltig", "received_at": "2026-08-26T00:00:00+00:00"},
        {"message_id": 5, "text": "ohne received_at"},
    ]
    package = facts.build_facts_package(
        portfolio, transactions, {}, news=[], strategy={}, mode="monday", open_points=open_points
    )
    assert package["open_points"] == [
        {"text": "gueltig", "received_at": "2026-08-26T00:00:00+00:00", "untrusted": True},
        {"text": "ohne received_at", "untrusted": True},
    ]
    package_none = facts.build_facts_package(
        portfolio, transactions, {}, news=[], strategy={}, mode="monday", open_points=None
    )
    assert package_none["open_points"] == []


# --- Phase 3: Positions-/Sektor-Details im deterministic_summary -------------
#
# facts._deterministic_summary traegt die additiven Detail-Felder
# ``positions_detail`` und ``sectors_detail`` (Plan §3.2/§8.3). Die Zahlen
# stammen aus den bereits berechneten analyze-Checks (nie neu berechnet);
# Limits nur fuer Satellite, Core/Legacy/unknown -> None (``--`` in der
# Briefing-Tabelle).

_P3_STRATEGY = {
    "portfolio": {
        "core_pct": 70.0,
        "satellite_pct": 30.0,
        "rebalancing": {"threshold_pct": 5.0},
    },
    "satellite_limits": {
        "target_position_pct": 5.0,
        "max_position_pct": 10.0,
        "max_sector_pct": 20.0,
        "max_turnover_annual_pct": 30.0,
    },
}

_P3_PORTFOLIO = {
    "total_value_eur": 10000.0,
    "holdings": [
        {"isin": "IE00BKM4GZ66", "name": "iShares Core MSCI EM IMI", "category": "core", "value_eur": 6000.0},
        {"isin": "US67066G1040", "name": "NVIDIA", "category": "satellite", "value_eur": 3000.0, "sector": "Technology"},
        {"isin": "LU2722255754", "name": "SUSE", "category": "legacy", "value_eur": None},
    ],
}


def _p3_analysis() -> dict:
    """Analyse-Befunde: 2 bewertete Positionen, 1 unbewertete Legacy; nur
    Satellite fliessen in die Sektor-Konzentration ein (Core/unknown nicht)."""
    return {
        "generated_at": "2026-08-28T09:00:00+02:00",
        "overall_status": "green",
        "checks": {
            "positions": {
                "total_value_eur": 9000.0,
                "positions": [
                    {"isin": "IE00BKM4GZ66", "name": "iShares Core MSCI EM IMI", "category": "core", "value_eur": 6000.0, "weight": 0.6667},
                    {"isin": "US67066G1040", "name": "NVIDIA", "category": "satellite", "value_eur": 3000.0, "weight": 0.3333, "sector": "Technology"},
                    {"isin": "LU2722255754", "name": "SUSE", "category": "legacy", "value_eur": 0.0, "weight": 0.0},
                ],
            },
            "core_satellite": {"core_ratio": 0.6667, "status": "green"},
            "sector_concentration": {
                "sector_ratios": {"Technology": 1.0},
                "max_sector": "Technology",
                "max_ratio": 1.0,
                "status": "red",
            },
            "single_position": {"max_position": {"name": "NVIDIA", "weight": 0.3333}, "status": "red"},
            "drift": {"drift": 0.0, "status": "green"},
            "turnover": {"turnover_ratio": 0.0, "status": "green"},
            "thesis_deadlines": {"outdated": [], "status": "green"},
        },
    }


def test_positions_detail_fields_and_limits():
    """Phase 3: positions_detail traegt pro Position Name/ISIN/Kategorie/Wert/
    Gewicht/Limit/Status — Limit nur fuer Satellite, Core/Legacy -> None."""
    analysis = _p3_analysis()
    package = facts.build_facts_package(
        _P3_PORTFOLIO, [], analysis, news=[], strategy=_P3_STRATEGY, mode="monday"
    )
    detail = package["deterministic_summary"]["positions_detail"]
    by_isin = {d["isin"]: d for d in detail}

    assert set(by_isin) == {"IE00BKM4GZ66", "US67066G1040", "LU2722255754"}
    core = by_isin["IE00BKM4GZ66"]
    assert core["name"] == "iShares Core MSCI EM IMI"
    assert core["category"] == "core"
    assert core["value_eur"] == 6000.0
    assert core["weight"] == 0.6667
    assert core["limit_pct"] is None  # Core: kein Satellite-Limit -> "--"
    assert core["status"] == "ok"

    satellite = by_isin["US67066G1040"]
    assert satellite["category"] == "satellite"
    assert satellite["value_eur"] == 3000.0
    assert satellite["limit_pct"] == 10.0  # max_position_pct 10.0%
    assert satellite["weight"] == 0.3333
    assert satellite["status"] == "rot"  # 33.3% > 10% Max

    legacy = by_isin["LU2722255754"]
    assert legacy["category"] == "legacy"
    assert legacy["value_eur"] == 0.0
    assert legacy["limit_pct"] is None
    assert legacy["status"] == "ok"


def test_sectors_detail_only_satellite_sectors():
    """Phase 3: sectors_detail nur Satellite-Sektoren — Core-ETF (Diversified)
    und unknown/Legacy tauchen NICHT als Sektor auf; Limit max_sector_pct;
    Status rot nur fuer den max-Sektor."""
    analysis = _p3_analysis()
    package = facts.build_facts_package(
        _P3_PORTFOLIO, [], analysis, news=[], strategy=_P3_STRATEGY, mode="monday"
    )
    sectors = package["deterministic_summary"]["sectors_detail"]

    assert [s["name"] for s in sectors] == ["Technology"]  # nur Satellite-Sektor
    tech = sectors[0]
    assert tech["value_eur"] == 3000.0  # Ratio 1.0 x Satellite-Summe 3000
    assert tech["ratio"] == 1.0
    assert tech["limit_pct"] == 20.0  # max_sector_pct 20.0%
    assert tech["status"] == "red"  # max-Sektor der roten Sektor-Ampel


def test_positions_detail_status_bands_with_target():
    """Phase 3: Status-Baender — unter Ziel green, zwischen Ziel/Max gelb,
    ueber Max rot; unbewertet nur bei Gewicht 0 mit Satellite-Limit."""
    detail = [
        facts._position_detail_status(0.03, 0.10, 0.05),  # 3% < Ziel 5%
        facts._position_detail_status(0.07, 0.10, 0.05),  # 7% zwischen Ziel/Max
        facts._position_detail_status(0.12, 0.10, 0.05),  # 12% > Max 10%
        facts._position_detail_status(0.0, 0.10, 0.05),  # unbewertet
        facts._position_detail_status(0.4, None, None),  # Core ohne Limit -> ok
    ]
    assert detail == ["gruen", "gelb", "rot", "unbewertet", "ok"]


def test_positions_detail_json_serializable_and_deterministic():
    """Phase 3: positions_detail/sectors_detail sind JSON-serialisierbar und
    deterministisch (identische Eingaben -> identische Details)."""
    analysis = _p3_analysis()
    first = facts.build_facts_package(
        _P3_PORTFOLIO, [], analysis, news=[], strategy=_P3_STRATEGY, mode="monday"
    )
    second = facts.build_facts_package(
        _P3_PORTFOLIO, [], analysis, news=[], strategy=_P3_STRATEGY, mode="monday"
    )
    json.dumps(first)  # serialisierbar
    assert first["deterministic_summary"]["positions_detail"] == second["deterministic_summary"]["positions_detail"]
    assert first["deterministic_summary"]["sectors_detail"] == second["deterministic_summary"]["sectors_detail"]


def test_positions_detail_empty_checks_fallback_to_holdings():
    """Phase 3: fehlende analyse-Checks (defensive Mocks) -> positions_detail
    aus den Holdings; Sektor-Details leer (keine Sektor-Analyse vorhanden)."""
    package = facts.build_facts_package(
        _P3_PORTFOLIO, [], {"checks": {}}, news=[], strategy=_P3_STRATEGY, mode="monday"
    )
    detail = package["deterministic_summary"]["positions_detail"]
    assert package["deterministic_summary"]["sectors_detail"] == []
    assert len(detail) == 3
    core = next(d for d in detail if d["isin"] == "IE00BKM4GZ66")
    assert core["category"] == "core"
    assert core["value_eur"] == 6000.0
    assert core["limit_pct"] is None
    satellite = next(d for d in detail if d["isin"] == "US67066G1040")
    assert satellite["limit_pct"] == 10.0
    assert satellite["weight"] == 0.0  # keine Gewichte ohne analyse-Checks
    assert satellite["status"] == "unbewertet"
