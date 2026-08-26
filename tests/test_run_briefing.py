"""Regression tests: Fail-closed bei LLMError, Dry-Run-Idempotenz (Suffix-Strategie).

Nutzt ausschliesslich Mock-Daten und Fake-Objekte — keine API-/Telegram-Aufrufe.
Datenbeschaffung: Dry-Run via sc_bridge.load_mock(); produktiver Lauf via
snapshot.load_previous -> sc_bridge.refresh_from_sc -> snapshot.capture_staged
-> diff.diff_snapshots (kein update_config).
"""
from __future__ import annotations

import pytest

from scripts import (
    analyze,
    diff,
    filter_news,
    final_briefing,
    llm_humanize,
    llm_review,
    run_briefing,
    sc_bridge,
    send_telegram,
    snapshot,
)

EMPTY_STRATEGY = {"strategy": {}}
EMPTY_ANALYSIS = {"checks": {}}

# Valid Draft: alle 5 Pflichtsektionen des kurzen Output-Contracts, keine
# Zahlen/Ticker (EMPTY_ANALYSIS -> Summary 0).
VALID_DRAFT = (
    "## Kurzlage\n"
    "OK\n\n"
    "## Datenqualität\n"
    "—\n\n"
    "## Sell-/Reduce-Signale (bestehende Satellites)\n"
    "Keine Sell-/Reduce-Signale.\n\n"
    "Fundamentaldaten (Umsatz, Gewinn, Cashflow, Verschuldung, Bewertung) "
    "nicht automatisch verfügbar und fließen nicht in das Signal ein.\n\n"
    "## Watchlist-Signale\n"
    "Keine Watchlist-Signale (NO SIGNAL für alle Positionen).\n\n"
    "Fundamentaldaten (Umsatz, Gewinn, Cashflow, Verschuldung, Bewertung) "
    "nicht automatisch verfügbar und fließen nicht in das Signal ein.\n\n"
    "## Nächster Schritt\n"
    "Nächste Woche neuer Lauf, keine Aktion erforderlich."
)
PASS_REVIEW = {"findings": [], "overall_verdict": "pass"}


def _mock_pipeline(monkeypatch, tmp_path, portfolio, transactions) -> dict:
    """Mockt alle Pipeline-Abhaengigkeiten ausser LLM/Versand (werden je Test gesetzt).

    Datenbeschaffung: snapshot.load_previous/capture_staged und
    diff.diff_snapshots laufen mit den Mock-Daten (rein, deterministisch);
    refresh_from_sc und load_mock liefern die Mock-Daten. Staged-Lifecycle
    (capture_staged/promote_staged/discard_staged) wird mit Call-Tracker
    gemockt. Rueckgabe: ``calls``-Tracker.
    """
    calls = {"capture_staged": 0, "promote_staged": 0, "discard_staged": 0}
    monkeypatch.setattr(run_briefing, "_setup_logging", lambda: None)
    monkeypatch.setattr(run_briefing, "VAULT_DIR", tmp_path)
    previous = snapshot.build_snapshot(
        portfolio, transactions, mode="monday", captured_at="2026-08-06T10:00:00+00:00"
    )
    monkeypatch.setattr(snapshot, "load_previous", lambda: previous)

    def _capture_staged(p, t, **kw):
        calls["capture_staged"] += 1
        return {
            "snapshot": snapshot.build_snapshot(p, t, **kw),
            "staged_path": str(tmp_path / "snapshot.staged.json"),
        }

    def _promote_staged():
        calls["promote_staged"] += 1
        return {"snapshot": {}, "previous": previous, "archive_path": None}

    def _discard_staged():
        calls["discard_staged"] += 1
        return True

    monkeypatch.setattr(snapshot, "capture_staged", _capture_staged)
    monkeypatch.setattr(snapshot, "promote_staged", _promote_staged)
    monkeypatch.setattr(snapshot, "discard_staged", _discard_staged)
    monkeypatch.setattr(sc_bridge, "refresh_from_sc", lambda: (portfolio, transactions))
    monkeypatch.setattr(sc_bridge, "load_mock", lambda: (portfolio, transactions))
    monkeypatch.setattr(sc_bridge, "fetch_watchlist_from_sc", lambda: [])
    monkeypatch.setattr(sc_bridge, "load_mock_watchlist", lambda: [])
    monkeypatch.setattr(analyze, "load_strategy", lambda: EMPTY_STRATEGY)
    monkeypatch.setattr(analyze, "analyze_portfolio", lambda p, t, s: EMPTY_ANALYSIS)
    monkeypatch.setattr(filter_news, "fetch_and_filter_news", lambda p: [])
    return calls


def test_llm_error_is_fail_closed(monkeypatch, tmp_path, portfolio, transactions):
    """Bug-Regression: Humanizer-LLMError -> Exit != 0, kein Versand, keine Vault-Datei, nur Alert."""
    _mock_pipeline(monkeypatch, tmp_path, portfolio, transactions)
    sent = []
    monkeypatch.setattr(send_telegram, "send_briefing", lambda text, mode: sent.append((text, mode)) or True)

    def _raise_llm_error(*args, **kwargs):
        raise llm_humanize.LLMError("API down")

    monkeypatch.setattr(llm_humanize, "humanize_briefing", _raise_llm_error)

    rc = run_briefing.run("monday", dry_run=False)

    assert rc == 1  # Exit-Code != 0
    assert list(tmp_path.iterdir()) == []  # keine Vault-Datei geschrieben
    assert len(sent) == 1  # nur der Alert, kein Briefing
    assert sent[0][1] == "alert"
    assert "API down" in sent[0][0]


def test_dry_run_archives_with_suffix_and_no_telegram(monkeypatch, tmp_path, portfolio, transactions):
    _mock_pipeline(monkeypatch, tmp_path, portfolio, transactions)
    sent = []
    monkeypatch.setattr(send_telegram, "send_briefing", lambda text, mode: sent.append((text, mode)) or True)

    date_pattern = "-monday-dryrun.md"
    rc = run_briefing.run("monday", dry_run=True)

    assert rc == 0
    archived = [p.name for p in tmp_path.iterdir()]
    assert archived and all(name.endswith(date_pattern) for name in archived)
    assert not any(name.endswith("-monday.md") for name in archived)
    content = (tmp_path / archived[0]).read_text(encoding="utf-8")
    assert "status: draft" in content
    assert "Dry-Run — kein LLM-Call" in content
    assert sent == []  # kein Telegram-Aufruf im Dry-Run


def test_dry_run_does_not_block_real_run(monkeypatch, tmp_path, portfolio, transactions):
    """Bug-Regression: Dry-Run-Datei mit Suffix darf realen Lauf am selben Tag nicht blocken."""
    _mock_pipeline(monkeypatch, tmp_path, portfolio, transactions)
    monkeypatch.setattr(send_telegram, "send_briefing", lambda text, mode: True)

    assert run_briefing.run("monday", dry_run=True) == 0

    # Suffix-Strategie: realer Lauf schaut nur auf {date}-{mode}.md
    archived = [p.name for p in tmp_path.iterdir()]
    assert archived and all(name.endswith("-monday-dryrun.md") for name in archived)
    assert run_briefing._already_run_today("monday") is False

    generated = []

    def _fake_humanize(*args, **kwargs):
        generated.append(1)
        return VALID_DRAFT

    monkeypatch.setattr(llm_humanize, "humanize_briefing", _fake_humanize)
    monkeypatch.setattr(llm_review, "review_draft", lambda facts_package, draft: PASS_REVIEW)
    assert run_briefing.run("monday", dry_run=False) == 0
    assert generated  # Humanizer wurde aufgerufen -> kein Skip durch Dry-Run-Datei


def test_send_failure_is_fail_closed(monkeypatch, tmp_path, portfolio, transactions):
    """send_briefing=False -> kurzer Alert, Exit 1 statt 'completed successfully'.

    Keine doppelte Briefing-Datei: der Orchestrator legt nach fehlgeschlagenem
    Versand keine weitere Datei an (Archivierung passiert intern in send_briefing).
    """
    _mock_pipeline(monkeypatch, tmp_path, portfolio, transactions)
    sent = []
    monkeypatch.setattr(send_telegram, "send_briefing", lambda text, mode: sent.append((text, mode)) or False)
    monkeypatch.setattr(llm_humanize, "humanize_briefing", lambda briefing_text, mode="monday": VALID_DRAFT)
    monkeypatch.setattr(llm_review, "review_draft", lambda facts_package, draft: PASS_REVIEW)

    rc = run_briefing.run("monday", dry_run=False)

    assert rc == 1  # Exit-Code != 0 statt Erfolg
    assert len(sent) == 2  # Briefing-Versuch (False) + kurzer Alert
    assert sent[0][1] == "monday"
    assert sent[1][1] == "alert"
    assert "send failed" in sent[1][0]
    assert list(tmp_path.iterdir()) == []  # keine Briefing-Datei (send_briefing gemockt)


# --- Datenbeschaffung: Dry-Run (load_mock) vs. produktiver Lauf (refresh/snapshot/diff) ---


def test_dry_run_uses_mock_without_refresh_snapshot_or_telegram(monkeypatch, tmp_path, portfolio, transactions):
    """Dry-Run: ausschliesslich load_mock — kein refresh, kein Snapshot/Diff, kein Telegram."""
    _mock_pipeline(monkeypatch, tmp_path, portfolio, transactions)
    refresh_calls = []
    capture_staged_calls = []
    promote_calls = []
    discard_calls = []
    diff_calls = []
    sent = []
    monkeypatch.setattr(sc_bridge, "refresh_from_sc", lambda: (refresh_calls.append(1), (portfolio, transactions))[1])
    monkeypatch.setattr(snapshot, "capture_staged", lambda *a, **kw: (capture_staged_calls.append(1), {})[1])
    monkeypatch.setattr(snapshot, "promote_staged", lambda: (promote_calls.append(1), {})[1])
    monkeypatch.setattr(snapshot, "discard_staged", lambda: (discard_calls.append(1), True)[1])
    monkeypatch.setattr(diff, "diff_snapshots", lambda prev, cur: (diff_calls.append(1), {})[1])
    monkeypatch.setattr(send_telegram, "send_briefing", lambda text, mode: sent.append((text, mode)) or True)

    rc = run_briefing.run("monday", dry_run=True)

    assert rc == 0
    assert refresh_calls == []  # kein sc-Aufruf im Dry-Run
    assert capture_staged_calls == []  # kein Snapshot-Schreiben im Dry-Run
    assert promote_calls == []  # kein Promote im Dry-Run
    assert discard_calls == []  # kein Discard im Dry-Run
    assert diff_calls == []  # kein Diff im Dry-Run
    assert sent == []  # kein Telegram im Dry-Run
    archived = [p.name for p in tmp_path.iterdir()]
    assert archived and all(name.endswith("-monday-dryrun.md") for name in archived)


def test_dry_run_skips_news_fetch_non_dry_run_fetches(monkeypatch, tmp_path, portfolio, transactions):
    """Dry-Run ist vollstaendig netzwerkfrei: fetch_and_filter_news wird NICHT
    aufgerufen (news=[]). Non-Dry-Run behaelt den normalen News-Fetch."""
    _mock_pipeline(monkeypatch, tmp_path, portfolio, transactions)
    news_calls = []
    monkeypatch.setattr(
        filter_news,
        "fetch_and_filter_news",
        lambda p: (news_calls.append(1), [{"title": "News", "summary": "s"}])[1],
    )
    monkeypatch.setattr(send_telegram, "send_briefing", lambda text, mode: True)

    # Dry-Run: kein RSS-/News-Fetch, trotzdem erfolgreiches Archiving
    assert run_briefing.run("monday", dry_run=True) == 0
    assert news_calls == []  # fetch_and_filter_news im Dry-Run nie aufgerufen

    # Non-Dry-Run: normaler News-Fetch laeuft weiterhin
    monkeypatch.setattr(llm_humanize, "humanize_briefing", lambda briefing_text, mode="monday": VALID_DRAFT)
    monkeypatch.setattr(llm_review, "review_draft", lambda facts_package, draft: PASS_REVIEW)
    assert run_briefing.run("monday", dry_run=False) == 0
    assert news_calls == [1]  # genau ein News-Fetch im produktiven Lauf


@pytest.mark.parametrize(
    ("exc", "msg"),
    [
        (sc_bridge.ScNotAvailableError("sc CLI not found in PATH"), "sc CLI not found"),
        (sc_bridge.ScEmptyDataError("sc returned an empty holdings list"), "empty holdings"),
    ],
)
def test_sc_refresh_error_is_fail_closed(exc, msg, monkeypatch, tmp_path, portfolio, transactions):
    """refresh_from_sc-Fehler -> Alert, Exit 1, keine Vault-Datei, kein Snapshot (kein Mock)."""
    _mock_pipeline(monkeypatch, tmp_path, portfolio, transactions)
    sent = []
    capture_staged_calls = []
    monkeypatch.setattr(send_telegram, "send_briefing", lambda text, mode: sent.append((text, mode)) or True)
    monkeypatch.setattr(snapshot, "capture_staged", lambda *a, **kw: (capture_staged_calls.append(1), {})[1])

    def _raise_sc_error():
        raise exc

    monkeypatch.setattr(sc_bridge, "refresh_from_sc", _raise_sc_error)

    rc = run_briefing.run("monday", dry_run=False)

    assert rc == 1
    assert list(tmp_path.iterdir()) == []  # keine Briefing-Datei
    assert capture_staged_calls == []  # kein Snapshot nach Fehler
    assert len(sent) == 1 and sent[0][1] == "alert"
    assert msg in sent[0][0]


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (sc_bridge.ScSessionExpiredError("sc session expired (no_session) — run interactive `sc login`"), "sc login"),
        (sc_bridge.ScReloginRequiredError("sc session expired (REFRESH_RELOGIN_REQUIRED) — run interactive `sc login`"), "sc login"),
        (sc_bridge.ScSecretStorageError("sc secret storage unavailable (secret_storage_unavailable) — check system/keyring configuration"), "Secret Storage"),
    ],
)
def test_sc_auth_error_alert_is_action_oriented(exc, expected, monkeypatch, tmp_path, portfolio, transactions):
    """Differenzierte sc-Auth-Fehler -> handlungsorientierter Alert mit Aktion (Exit 1, kein Versand)."""
    _mock_pipeline(monkeypatch, tmp_path, portfolio, transactions)
    sent = []
    monkeypatch.setattr(send_telegram, "send_briefing", lambda text, mode: sent.append((text, mode)) or True)

    def _raise_auth_error():
        raise exc

    monkeypatch.setattr(sc_bridge, "refresh_from_sc", _raise_auth_error)

    rc = run_briefing.run("monday", dry_run=False)

    assert rc == 1
    assert list(tmp_path.iterdir()) == []  # keine Briefing-Datei
    assert len(sent) == 1 and sent[0][1] == "alert"
    assert expected in sent[0][0]
    assert "token" not in sent[0][0] and "password" not in sent[0][0]


def test_dry_run_error_sends_no_telegram_alert(monkeypatch, tmp_path, portfolio, transactions):
    """Dry-Run-Fehler: Exit 1, KEIN Telegram-Alert (nur Logging), keine Briefing-Datei."""
    _mock_pipeline(monkeypatch, tmp_path, portfolio, transactions)
    sent = []
    monkeypatch.setattr(send_telegram, "send_briefing", lambda text, mode: sent.append((text, mode)) or True)

    def _broken_analyze(p, t, s):
        raise RuntimeError("dry-run analyze bug")

    monkeypatch.setattr(analyze, "analyze_portfolio", _broken_analyze)

    rc = run_briefing.run("monday", dry_run=True)

    assert rc == 1
    assert sent == []  # kein Telegram im Dry-Run
    assert list(tmp_path.iterdir()) == []


def test_non_dry_run_error_still_sends_alert(monkeypatch, tmp_path, portfolio, transactions):
    """Non-Dry-Run-Fehler darf weiterhin technische Alerts senden."""
    _mock_pipeline(monkeypatch, tmp_path, portfolio, transactions)
    sent = []
    monkeypatch.setattr(send_telegram, "send_briefing", lambda text, mode: sent.append((text, mode)) or True)

    def _broken_analyze(p, t, s):
        raise RuntimeError("live analyze bug")

    monkeypatch.setattr(analyze, "analyze_portfolio", _broken_analyze)

    rc = run_briefing.run("monday", dry_run=False)

    assert rc == 1
    assert len(sent) == 1 and sent[0][1] == "alert"
    assert "live analyze bug" in sent[0][0]


# --- P5: Humanizer-Pfad (deterministische Quelle -> LLM-Humanizer -> Verify-Absatz) ---


def _real_humanized() -> str:
    """Deterministisch gerenderter Text, minimal gehumanisiert (Contract-konform)."""
    return (
        "## Kurzlage\n"
        "Alles im grünen Bereich.\n\n"
        "## Datenqualität\n"
        "—\n\n"
        "## Sell-/Reduce-Signale (bestehende Satellites)\n"
        "Keine Verkaufs- oder Reduktionssignale für bestehende Satellites.\n\n"
        "Fundamentaldaten (Umsatz, Gewinn, Cashflow, Verschuldung, Bewertung) "
        "nicht automatisch verfügbar und fließen nicht in das Signal ein.\n\n"
        "## Watchlist-Signale\n"
        "Keine Watchlist-Signale (NO SIGNAL für alle Positionen).\n\n"
        "Fundamentaldaten (Umsatz, Gewinn, Cashflow, Verschuldung, Bewertung) "
        "nicht automatisch verfügbar und fließen nicht in das Signal ein.\n\n"
        "## Nächster Schritt\n"
        "Nächste Woche neuer Lauf, keine Aktion erforderlich."
    )


def test_run_deterministic_before_humanize(monkeypatch, tmp_path, portfolio, transactions):
    """Produktiver Lauf: render_final_briefing wird VOR humanize_briefing
    aufgerufen (deterministische Quelle zuerst); verify/review sehen den
    gehumanisierten Text. Dry-Run ueberspringt den Humanizer (kein LLM-Call)."""
    order = []
    _mock_pipeline(monkeypatch, tmp_path, portfolio, transactions)
    sent = []
    real_render = final_briefing.render_final_briefing
    monkeypatch.setattr(send_telegram, "send_briefing", lambda text, mode: sent.append((text, mode)) or True)

    def _render(facts_package, mode="monday"):
        order.append("render")
        return real_render(facts_package, mode=mode)

    def _humanize(briefing_text, mode="monday"):
        order.append("humanize")
        return _real_humanized()

    monkeypatch.setattr(final_briefing, "render_final_briefing", _render)
    monkeypatch.setattr(llm_humanize, "humanize_briefing", _humanize)
    monkeypatch.setattr(llm_review, "review_draft", lambda facts_package, draft: PASS_REVIEW)

    rc = run_briefing.run("monday", dry_run=False)

    assert rc == 0
    assert order == ["render", "humanize"]  # Render VOR Humanizer
    assert len(sent) == 1 and sent[0][1] == "monday"
    # Versendeter Text = gehumanisierte Fassung + Verify-Absatz (Humanizer-Text,
    # nicht der deterministische Render-Output).
    assert "Alles im grünen Bereich." in sent[0][0]
    assert "## Verifizierung" in sent[0][0]

    # Dry-Run: kein Humanizer-Call (kein LLM), kein Render (Platzhalter-Pfad).
    order.clear()
    assert run_briefing.run("monday", dry_run=True) == 0
    assert order == []


def test_run_humanize_failure_fail_closed_no_fallback(monkeypatch, tmp_path, portfolio, transactions):
    """Humanizer-Fehler -> fail-closed: rc 1, KEIN Versand (kein Fallback auf
    die deterministische Rohfassung), keine Vault-Datei, nur Alert."""
    _mock_pipeline(monkeypatch, tmp_path, portfolio, transactions)
    sent = []
    render_calls = {"n": 0}
    real_render = final_briefing.render_final_briefing
    monkeypatch.setattr(send_telegram, "send_briefing", lambda text, mode: sent.append((text, mode)) or True)
    monkeypatch.setattr(final_briefing, "render_final_briefing", lambda facts_package, mode="monday": (render_calls.__setitem__("n", render_calls["n"] + 1), real_render(facts_package, mode=mode))[1])

    def _humanize_fail(briefing_text, mode="monday"):
        raise llm_humanize.LLMError("Humanize-API down")

    monkeypatch.setattr(llm_humanize, "humanize_briefing", _humanize_fail)

    rc = run_briefing.run("monday", dry_run=False)

    assert rc == 1
    assert render_calls["n"] == 1  # Render lief (Quelle), Humanizer schlug fehl
    assert list(tmp_path.iterdir()) == []  # keine Vault-Datei
    # Nur der Alert, kein Briefing-Versand: kein Fallback auf die
    # deterministische Rohfassung (User-Vorgabe, kein Rohtext-Fallback).
    assert len(sent) == 1 and sent[0][1] == "alert"
    assert "humanize failed" in sent[0][0]
    assert "kein Fallback" in sent[0][0]


def test_run_verify_paragraph_appended_last_not_humanized(monkeypatch, tmp_path, portfolio, transactions):
    """Nach final_gate PASS: Verify-Absatz (render_verify_paragraph) ist der
    LETZTE Absatz des versendeten Texts und laeuft NICHT durch den Humanizer."""
    order = []
    _mock_pipeline(monkeypatch, tmp_path, portfolio, transactions)
    sent = []
    monkeypatch.setattr(send_telegram, "send_briefing", lambda text, mode: sent.append((text, mode)) or True)
    monkeypatch.setattr(llm_humanize, "humanize_briefing", lambda briefing_text, mode="monday": (order.append("humanize"), _real_humanized())[1])
    monkeypatch.setattr(llm_review, "review_draft", lambda facts_package, draft: PASS_REVIEW)
    monkeypatch.setattr(final_briefing, "render_verify_paragraph", lambda verification, review, gate_reason="": (order.append("verify_paragraph"), "## Verifizierung\n\n- Verify: PASS — 0 findings.\n- Review: PASS — 0 findings (overall_verdict: pass).\n- final_gate: pass.")[1])

    rc = run_briefing.run("monday", dry_run=False)

    assert rc == 0
    assert order == ["humanize", "verify_paragraph"]  # Verify-Absatz NACH Humanizer
    sent_text = sent[0][0]
    # Verify-Absatz steht am Ende (nach "## Nächster Schritt").
    assert sent_text.rstrip().endswith("- final_gate: pass.")
    assert "## Verifizierung" in sent_text
    assert sent_text.index("## Verifizierung") > sent_text.index("## Nächster Schritt")
