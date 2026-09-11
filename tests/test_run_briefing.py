"""Tests für run_briefing.py — Fixture-Daten, Guardrail, Dry-Run-Kontrakt."""
from __future__ import annotations

import shutil

import pytest

from scripts import run_briefing


def test_guardrail_read_tools_only():
    """Write-Tools dürfen nie im Auto-Pfad/Pull-Prompt auftauchen."""
    write_tools = [
        "submit_buy_order", "cancel_order", "add_watchlist_item",
        "remove_watchlist_item", "create_price_alert", "remove_price_alert",
        "upsert_savings_plan", "remove_savings_plan",
    ]
    for tool in write_tools:
        assert tool not in run_briefing.PULL_PROMPT
        assert not any(tool in t for t in run_briefing.READ_TOOLS)
    # Alle Read-Tools tragen das scalable_-Prefix (P0-Befund 2).
    assert all(t.startswith("scalable_") for t in run_briefing.READ_TOOLS)


def test_fixtures_data_coherent():
    data = run_briefing._load_fixtures()
    assert data["total_value_eur"] > 0
    assert data["holdings"]
    assert data["watchlist"]
    assert data["fundamentals"]
    assert data["suggestions"]
    # Crypto-ETP-Nullfälle sind info, nie fatal.
    assert not any(i["severity"] == "error" for i in data["issues"])


def test_dry_run_returns_zero_and_writes_artifact(tmp_path, monkeypatch):
    import scripts.run_briefing as rb

    monkeypatch.setattr(rb, "REPORT_DIR", tmp_path)
    rc = rb.run("monday", dry_run=True)
    assert rc == 0
    artifacts = list(tmp_path.glob("*-monday-dryrun.md"))
    assert len(artifacts) == 1
    assert "Portfolio-Briefing" in artifacts[0].read_text(encoding="utf-8")


def test_dry_run_makes_no_live_calls(tmp_path, monkeypatch, capsys):
    import scripts.run_briefing as rb

    monkeypatch.setattr(rb, "REPORT_DIR", tmp_path)
    monkeypatch.setattr(rb, "_pull_phase_a", lambda: (_ for _ in ()).throw(AssertionError("no pull")))

    rc = rb.run("monday", dry_run=True)
    assert rc == 0
    out = capsys.readouterr().out
    assert "Fixtures" in out


# --- Finding 5: strikte CLI (argparse) ---

def test_unknown_flag_is_usage_error():
    with pytest.raises(SystemExit) as e:
        run_briefing.main(["--dryrun"])  # Tippfehler → Usage-Fehler, nie stiller Live-Run
    assert e.value.code == 2


def test_unknown_mode_is_usage_error():
    with pytest.raises(SystemExit):
        run_briefing.main(["sunday"])


# --- Finding 2: Fail-closed + Fallback Stufe 3 + Alert ---

def test_live_pull_failure_without_snapshot_alerts_and_exits(tmp_path, monkeypatch):
    import scripts.run_briefing as rb

    monkeypatch.setattr(rb, "DATA_DIR", tmp_path)  # kein Snapshot vorhanden
    monkeypatch.setattr(rb, "_pull_phase_a", lambda: (_ for _ in ()).throw(RuntimeError("net down")))
    monkeypatch.setattr(rb, "_pull_phase_b", lambda: False)
    alerts = []
    monkeypatch.setattr(rb, "_send_alert", lambda msg, dry_run=False: alerts.append(msg) or False)

    rc = rb.run("monday", dry_run=False)
    assert rc == 1
    assert alerts, "Fehlerpfad muss einen Alert senden (nie stumm)"


def test_live_fallback_stage3_uses_snapshot_with_age_warning(tmp_path, monkeypatch):
    import scripts.run_briefing as rb

    shutil.copy(rb.MOCK_DIR / "portfolio.json", tmp_path / "portfolio.json")
    shutil.copy(rb.MOCK_DIR / "watchlist.json", tmp_path / "watchlist.json")
    monkeypatch.setattr(rb, "DATA_DIR", tmp_path)
    monkeypatch.setattr(rb, "_pull_phase_a", lambda: (_ for _ in ()).throw(RuntimeError("down")))
    monkeypatch.setattr(rb, "_pull_phase_b", lambda: False)
    alerts = []
    monkeypatch.setattr(rb, "_send_alert", lambda msg, dry_run=False: alerts.append(msg) or False)
    base = {"holdings": [], "watchlist": [], "quotes": [], "news": [], "fundamentals": [],
            "signals": [], "core_ratio": 0.0, "satellite_ratio": 0.0, "total_value_eur": 0.0,
            "cash_eur": 0.0, "captured_at": "2026-01-01T00:00:00+00:00", "fx_note": "",
            "issues": []}
    monkeypatch.setattr(rb, "_build_common", lambda snap, strat, eurusd: dict(base))
    monkeypatch.setattr(rb, "_fetch_eurusd", lambda: None)

    data = rb._acquire_live_data(dry_run=True)
    assert "snapshot_age_hours" in data
    assert alerts, "Stufe 3 sendet Re-Auth-Ping (Alert)"


def test_live_sanity_fail_alerts_and_no_briefing_send(tmp_path, monkeypatch):
    import scripts.run_briefing as rb

    monkeypatch.setattr(rb, "REPORT_DIR", tmp_path)
    data = rb._load_fixtures()
    data["suggestions"] = "Kaufe US1234567890 zu 9.999"  # Sanity → Verletzung
    monkeypatch.setattr(rb, "_acquire_live_data", lambda dry_run=False: dict(data))
    monkeypatch.setattr(rb.brief, "generate_suggestions", lambda d: data["suggestions"])
    alerts = []
    monkeypatch.setattr(rb, "_send_alert", lambda msg, dry_run=False: alerts.append(msg) or False)
    sent = []
    monkeypatch.setattr(rb, "send_telegram_send", lambda text, mode: sent.append(mode) or True)

    rc = rb.run("monday", dry_run=False)
    assert rc == 1
    assert alerts, "Sanity-Fail muss alerten"
    assert any("sanity" in a.lower() for a in alerts)
    assert sent == [], "kein Briefing-Versand bei Sanity-Fail (fail-closed)"


def test_live_llm_failure_alerts(tmp_path, monkeypatch):
    import scripts.run_briefing as rb

    monkeypatch.setattr(rb, "REPORT_DIR", tmp_path)
    data = rb._load_fixtures()
    monkeypatch.setattr(rb, "_acquire_live_data", lambda dry_run=False: dict(data))
    monkeypatch.setattr(rb.brief, "generate_suggestions",
                        lambda d: (_ for _ in ()).throw(RuntimeError("llm down")))
    alerts = []
    monkeypatch.setattr(rb, "_send_alert", lambda msg, dry_run=False: alerts.append(msg) or False)

    rc = rb.run("monday", dry_run=False)
    assert rc == 1
    assert alerts and any("LLM" in a for a in alerts)



def test_corrupt_snapshot_alerts_and_exits(tmp_path, monkeypatch):
    # Korrupte data/*.json → AlertExit/Alert statt Crash (Fail-closed, P3-Next).
    import scripts.run_briefing as rb

    (tmp_path / "portfolio.json").write_text("{ kaputt", encoding="utf-8")
    (tmp_path / "watchlist.json").write_text("[]", encoding="utf-8")
    monkeypatch.setattr(rb, "DATA_DIR", tmp_path)
    monkeypatch.setattr(rb, "_pull_phase_a", lambda: (_ for _ in ()).throw(RuntimeError("down")))
    monkeypatch.setattr(rb, "_pull_phase_b", lambda: False)
    monkeypatch.setattr(rb, "_fetch_eurusd", lambda: None)
    alerts = []
    monkeypatch.setattr(rb, "_send_alert", lambda msg, dry_run=False: alerts.append(msg) or False)

    rc = rb.run("friday", dry_run=False)
    assert rc == 1
    assert alerts, "Korrupter Snapshot muss alerten (nie stumm)"
    assert any("Snapshot" in a for a in alerts)
