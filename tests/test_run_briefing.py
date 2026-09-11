"""Tests für run_briefing.py — Fixture-Daten, Guardrail, Dry-Run-Kontrakt."""
from __future__ import annotations

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
    # Sollte nie aufgerufen werden: pull/send/LLM im Dry-Run.
    monkeypatch.setattr(rb, "_pull_phase_a", lambda: (_ for _ in ()).throw(AssertionError("no pull")))

    rc = rb.run("monday", dry_run=True)
    assert rc == 0
    out = capsys.readouterr().out
    assert "Fixtures" in out
