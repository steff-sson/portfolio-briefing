"""Tests fuer scripts/classify_holdings.py (Plan Phase 1, Klassifikations-Flow).

Abdeckung: Snapshot-Laden, Vorschlags-Logik, Tabellen-/Summary-Formatierung,
Antwort-Serialisierung (answers.reviewed.yaml-Mechanismus) und Merge-Logik.
Keine echten API-/Snapshot-Schreibzugriffe — reine lokale Fixtures.
"""
from __future__ import annotations

import json

import yaml

from scripts import classify_holdings


def _holding(isin: str, name: str, category: str = "unknown", value_eur: float | None = 0.0) -> dict:
    h: dict[str, object] = {"isin": isin, "name": name, "category": category}
    if value_eur is not None:
        h["value_eur"] = value_eur
    return h


# --- Snapshot-Laden ------------------------------------------------------------


def test_load_snapshot_returns_none_for_missing_file(tmp_path):
    assert classify_holdings.load_snapshot(tmp_path / "fehlt.json") is None


def test_load_snapshot_accepts_schema_v2(tmp_path):
    path = tmp_path / "snapshot.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "captured_at": "2026-08-26T10:00:00+00:00",
                "portfolio": {"holdings": [{"isin": "US0378331005"}]},
                "transactions": [],
            }
        ),
        encoding="utf-8",
    )
    data = classify_holdings.load_snapshot(path)
    assert data is not None
    assert data["portfolio"]["holdings"][0]["isin"] == "US0378331005"


def test_load_snapshot_rejects_unknown_schema_version(tmp_path):
    path = tmp_path / "snapshot.json"
    path.write_text(json.dumps({"schema_version": 99, "portfolio": {"holdings": []}}), encoding="utf-8")
    assert classify_holdings.load_snapshot(path) is None


def test_load_snapshot_rejects_corrupt_json(tmp_path):
    path = tmp_path / "snapshot.json"
    path.write_text("{kaputt", encoding="utf-8")
    assert classify_holdings.load_snapshot(path) is None


# --- Vorschlags-Logik ----------------------------------------------------------


def test_suggestion_uses_raw_category():
    holding = _holding("IE00B57X3V84", "iShares ETF", category="satellite", value_eur=100.0)
    assert classify_holdings._category_suggestion(holding, {}) == "satellite"


def test_suggestion_etf_name_falls_back_to_core():
    holding = _holding("IE00B57X3V84", "iShares Core MSCI World UCITS ETF (Acc)", category="unknown")
    assert classify_holdings._category_suggestion(holding, {}) == "core"


def test_suggestion_legacy_isin():
    holding = _holding("LU2722255754", "LU2722255754", category="unknown", value_eur=None)
    assert classify_holdings._category_suggestion(holding, {}) == "legacy"


def test_suggestion_from_etf_lookup():
    holding = _holding("IE00BKM4GZ66", "iShares Core MSCI EM IMI", category="unknown")
    lookup = {"IE00BKM4GZ66": {"category": "satellite", "sector": "Diversified"}}
    # Vorschlag folgt dem (noch unkorrigierten) Lookup; User muss bestaetigen.
    assert classify_holdings._category_suggestion(holding, lookup) == "satellite"


def test_suggestion_unknown_when_nothing_known():
    holding = _holding("US5949724083", "Strategy", category="unknown")
    assert classify_holdings._category_suggestion(holding, {}) == "unknown"


# --- Tabellen-/Summary-Formatierung --------------------------------------------


def test_format_holdings_table():
    holdings = [
        _holding("IE00B57X3V84", "ETF A", category="unknown", value_eur=7500.0),
        _holding("US0378331005", "Apple", category="unknown", value_eur=2500.0),
        _holding("LU2722255754", "SUSE", category="unknown", value_eur=None),
    ]
    lines = classify_holdings.format_holdings_table(holdings)
    assert lines[0] == "ISIN | Name | Wert (EUR) | Anteil (%) | Datenqualitaet"
    assert lines[1] == "---|---|---|---|---"
    assert "| 7 500.00 | 75.0 | ok" in lines[2]
    assert "| 2 500.00 | 25.0 | ok" in lines[3]
    # Unbewertet: kein Wert, kein Anteil, Datenqualitaet incomplete.
    assert "| — | 0.0 | incomplete" in lines[4]


def test_format_summary_shows_strategy_targets():
    holdings = [_holding("IE00B57X3V84", "ETF A", value_eur=1000.0)]
    strategy = {
        "portfolio": {"core_pct": 70.0, "satellite_pct": 30.0},
        "satellite_limits": {"max_position_pct": 10.0, "max_sector_pct": 20.0},
    }
    lines = classify_holdings.format_summary(holdings, strategy)
    assert any("Core-Ziel: 70.0% | Satellite-Ziel: 30.0%" in l for l in lines)
    assert any("max_position 10.0%" in l for l in lines)


# --- Serialisierung / answers.reviewed.yaml ------------------------------------


def test_build_classification_entries_confirmed_format():
    holdings = [
        _holding("IE00B57X3V84", "ETF A"),
        _holding("LU2722255754", "SUSE", value_eur=None),
    ]
    entries = classify_holdings.build_classification_entries(
        holdings, {"IE00B57X3V84": "core", "LU2722255754": "legacy"}
    )
    assert set(entries) == {"holdings.classification.IE00B57X3V84", "holdings.classification.LU2722255754"}
    core = entries["holdings.classification.IE00B57X3V84"]
    assert core["value"] == {"category": "core", "confirmed": True, "source": "user"}
    assert core["status"] == "answered"
    assert core["source"] == "review"
    assert core["date"] == classify_holdings.today_str()


def test_build_classification_entries_skips_unknown_isins():
    entries = classify_holdings.build_classification_entries(
        [_holding("IE00B57X3V84", "ETF A")], {"NICHT_IM_BESTAND": "core"}
    )
    assert entries == {}


def test_merge_into_reviewed_preserves_existing_answers():
    reviewed = {
        "answers": {
            "portfolio.core_pct": {"value": 70.0},
            "holdings.classification.ALT": {"value": {"category": "core", "confirmed": True}},
        },
        "meta": {"created": "2026-08-14", "reviewed": True},
    }
    new_entries = {
        "holdings.classification.IE00B57X3V84": {
            "value": {"category": "core", "confirmed": True, "source": "user"},
            "status": "answered",
            "date": "2026-08-28",
            "source": "review",
        }
    }
    merged = classify_holdings.merge_into_reviewed(reviewed, new_entries)
    assert merged["answers"]["portfolio.core_pct"]["value"] == 70.0  # unveraendert
    assert "holdings.classification.ALT" in merged["answers"]  # unveraendert
    assert merged["answers"]["holdings.classification.IE00B57X3V84"] == new_entries[
        "holdings.classification.IE00B57X3V84"
    ]
    assert merged["meta"]["reviewed"] is True
    assert merged["meta"]["reviewed_at"] == classify_holdings.today_str()


def test_write_reviewed_roundtrip(tmp_path):
    target = tmp_path / "answers.reviewed.yaml"
    reviewed = {
        "answers": {
            "holdings.classification.IE00B57X3V84": {
                "value": {"category": "core", "confirmed": True, "source": "user"},
                "status": "answered",
                "date": "2026-08-28",
                "source": "review",
            }
        },
        "meta": {"reviewed": True, "reviewed_at": "2026-08-28"},
    }
    classify_holdings.write_reviewed(target, reviewed)
    loaded = yaml.safe_load(target.read_text(encoding="utf-8"))
    assert loaded["answers"]["holdings.classification.IE00B57X3V84"]["value"] == {
        "category": "core",
        "confirmed": True,
        "source": "user",
    }


# --- Flow / Eingabe -------------------------------------------------------------


def test_ask_category_accepts_numbers_and_rejects_unknown(monkeypatch, capsys):
    inputs = iter(["99", "2"])
    monkeypatch.setattr("builtins.input", lambda _: next(inputs))
    holding = _holding("US0378331005", "Apple", value_eur=1000.0)
    assert classify_holdings._ask_category(holding, "satellite", None) == "satellite"
    out = capsys.readouterr().out
    assert "Ungueltige Eingabe '99'" in out


def test_ask_category_rejects_unknown_text(monkeypatch, capsys):
    inputs = iter(["mega", "3"])
    monkeypatch.setattr("builtins.input", lambda _: next(inputs))
    holding = _holding("LU2722255754", "SUSE", value_eur=None)
    assert classify_holdings._ask_category(holding, "legacy", None) == "legacy"


def test_ask_category_keeps_confirmed_on_empty_input(monkeypatch):
    inputs = iter([""])
    monkeypatch.setattr("builtins.input", lambda _: next(inputs))
    holding = _holding("IE00B57X3V84", "ETF A")
    assert classify_holdings._ask_category(holding, "core", "core") == "core"


def test_ask_continue_yes_and_no(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda _: "j")
    assert classify_holdings._ask_continue() is True
    monkeypatch.setattr("builtins.input", lambda _: "n")
    assert classify_holdings._ask_continue() is False


def test_run_flow_writes_reviewed_and_returns_entries(monkeypatch, tmp_path):
    holdings = [
        _holding("IE00B57X3V84", "iShares Core MSCI World UCITS ETF (Acc)", value_eur=1000.0),
        _holding("LU2722255754", "SUSE", value_eur=None),
    ]
    target = tmp_path / "answers.reviewed.yaml"
    inputs = iter(["1", "1", "j"])
    monkeypatch.setattr("builtins.input", lambda _: next(inputs))
    result = classify_holdings.run_flow(holdings, reviewed={}, strategy={}, answers_path=target)
    assert result["written"] is True
    assert result["entries"] == {
        "IE00B57X3V84": "core",
        "LU2722255754": "core",
    }
    loaded = yaml.safe_load(target.read_text(encoding="utf-8"))
    assert loaded["answers"]["holdings.classification.LU2722255754"]["value"]["confirmed"] is True


def test_run_flow_aborts_without_writing_on_no(monkeypatch, tmp_path):
    holdings = [_holding("IE00B57X3V84", "ETF A", value_eur=1000.0)]
    target = tmp_path / "answers.reviewed.yaml"
    answers = iter(["1", "n"])
    monkeypatch.setattr("builtins.input", lambda _: next(answers))
    result = classify_holdings.run_flow(holdings, reviewed={}, strategy={}, answers_path=target)
    assert result["written"] is False
    assert not target.exists()


def test_main_fails_closed_without_snapshot(monkeypatch, tmp_path, capsys):
    assert classify_holdings.main(["--snapshot", str(tmp_path / "fehlt.json")]) == 1
    assert "Snapshot nicht lesbar" in capsys.readouterr().err
