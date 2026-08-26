"""Orchestrator tests (Phase 4, 1-Call-Architektur) with fake LLM.

Keine echten API-/Telegram-Aufrufe: generate_draft (der EINZIGE LLM-Call)
wird gemockt, send_briefing wird gefaked. Fail-closed-Pfade duerfen keine
Briefing-Datei im Vault anlegen. Datenbeschaffung: Dry-Run via load_mock;
produktiver Lauf via load_previous -> refresh_from_sc -> capture_staged ->
diff.diff_snapshots; staged wird erst nach final_gate + Render promoted
(kein update_config).

1-Call-Architektur: facts -> EIN generate_draft-Call -> verify_draft ->
final_gate (verification-only) -> render_markdown -> send_telegram. Keine
Humanize-/Review-/Revise-Stufe, kein Render-Hook zwischen Draft und Verify.
"""
from __future__ import annotations

from scripts import (
    analyze,
    diff,
    facts,
    filter_news,
    llm_briefing,
    run_briefing,
    sc_bridge,
    send_telegram,
    snapshot,
    verify,
)

EMPTY_STRATEGY = {"strategy": {}}
EMPTY_ANALYSIS = {"checks": {}}

# Valid Draft: alle 6 Pflichtsektionen des 1-Call-Contracts, keine Zahlen.
VALID_DRAFT = (
    "## Kurzlage\n"
    "Apple (AAPL) konform.\n\n"
    "## Datenqualität\n"
    "—\n\n"
    "## Sell-/Reduce-Signale (bestehende Satellites)\n"
    "Keine Sell-/Reduce-Signale.\n\n"
    "Fundamentaldaten (Umsatz, Gewinn, Cashflow, Verschuldung, Bewertung) nicht automatisch verfügbar und fließen nicht in das Signal ein.\n\n"
    "## Watchlist-Signale\n"
    "Keine Watchlist-Signale.\n\n"
    "Fundamentaldaten (Umsatz, Gewinn, Cashflow, Verschuldung, Bewertung) nicht automatisch verfügbar und fließen nicht in das Signal ein.\n\n"
    "## Nächster Schritt\n"
    "Nächste Woche neuer Lauf, keine Aktion erforderlich.\n\n"
    "## Empfehlung\n"
    "WATCH — kein Handlungsbedarf."
)


def _mock_pipeline(monkeypatch, tmp_path, portfolio, transactions, news=None) -> dict:
    """Mockt alle Pipeline-Abhaengigkeiten ausser LLM/Versand (werden je Test gesetzt).

    Datenbeschaffung: snapshot.load_previous/capture_staged und
    diff.diff_snapshots laufen mit den Mock-Daten (rein, deterministisch);
    refresh_from_sc und load_mock liefern die Mock-Daten. capture_staged/
    promote_staged/discard_staged werden mit Call-Tracker gemockt (staged
    Lifecycle: Baseline bleibt bis final_gate+Render unangetastet).
    Rueckgabe: ``calls``-Tracker {"capture_staged", "promote_staged",
    "discard_staged"}.
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
    monkeypatch.setattr(filter_news, "fetch_and_filter_news", lambda p: news or [])
    monkeypatch.setattr(filter_news, "fetch_news_for_unlisted_ideas", lambda p: [])
    return calls


def _fake_send(sent):
    def _send(text, mode):
        sent.append((text, mode))
        return True

    return _send


def _mock_draft(monkeypatch, draft: str = VALID_DRAFT, fail: Exception | None = None) -> dict:
    """Mockt generate_draft (der einzige LLM-Call), tracked Calls."""
    calls = {"draft_calls": 0}

    def _generate(facts_package, mode="monday", client=None):
        calls["draft_calls"] += 1
        if fail is not None:
            raise fail
        return draft

    monkeypatch.setattr(llm_briefing, "generate_draft", _generate)
    return calls


def test_pass_pipeline_sends_briefing(monkeypatch, tmp_path, portfolio, transactions):
    """Pass: Draft ok -> verify/gate ok -> Versand, rc 0. Genau ein LLM-Call."""
    _mock_pipeline(monkeypatch, tmp_path, portfolio, transactions)
    sent = []
    monkeypatch.setattr(send_telegram, "send_briefing", _fake_send(sent))
    draft_calls = _mock_draft(monkeypatch)

    rc = run_briefing.run("monday", dry_run=False)

    assert rc == 0
    assert draft_calls["draft_calls"] == 1  # genau EIN generate_draft-Call
    assert len(sent) == 1
    assert sent[0][1] == "monday"
    assert "Kurzlage" in sent[0][0]


def test_draft_llm_error_is_fail_closed(monkeypatch, tmp_path, portfolio, transactions):
    """generate_draft wirft LLMError -> Alert, Exit 1, kein Versand, keine Vault-Datei."""
    _mock_pipeline(monkeypatch, tmp_path, portfolio, transactions)
    sent = []
    monkeypatch.setattr(send_telegram, "send_briefing", _fake_send(sent))
    _mock_draft(monkeypatch, fail=llm_briefing.LLMError("Draft-Generierung fehlgeschlagen: API-Key fehlt."))

    rc = run_briefing.run("monday", dry_run=False)

    assert rc == 1
    assert list(tmp_path.iterdir()) == []
    assert len(sent) == 1 and sent[0][1] == "alert"
    assert "draft failed (fail-closed)" in sent[0][0]


def test_draft_generic_error_is_fail_closed(monkeypatch, tmp_path, portfolio, transactions):
    """Unerwarteter Draft-Fehler (kein LLMError) -> fail-closed, Alert, kein Versand."""
    _mock_pipeline(monkeypatch, tmp_path, portfolio, transactions)
    sent = []
    monkeypatch.setattr(send_telegram, "send_briefing", _fake_send(sent))
    _mock_draft(monkeypatch, fail=RuntimeError("API down"))

    rc = run_briefing.run("monday", dry_run=False)

    assert rc == 1
    assert list(tmp_path.iterdir()) == []
    assert len(sent) == 1 and sent[0][1] == "alert"
    assert "draft failed" in sent[0][0]


def test_major_verify_finding_blocks(monkeypatch, tmp_path, portfolio, transactions):
    """Major Verify-Finding (unbekannter Ticker im Draft) blockt Versand."""
    _mock_pipeline(monkeypatch, tmp_path, portfolio, transactions)
    sent = []
    monkeypatch.setattr(send_telegram, "send_briefing", _fake_send(sent))

    def _bad_draft(facts_package, mode="monday", client=None):
        # Draft enthaelt einen Ticker, den das Portfolio nicht kennt.
        return VALID_DRAFT.replace(
            "Apple (AAPL) konform.",
            "Microsoft (MSFT) im Fokus.",
            1,
        )

    monkeypatch.setattr(llm_briefing, "generate_draft", _bad_draft)

    rc = run_briefing.run("monday", dry_run=False)

    assert rc == 1
    assert list(tmp_path.iterdir()) == []
    assert len(sent) == 1 and sent[0][1] == "alert"


def test_critical_verify_finding_blocks(monkeypatch, tmp_path, portfolio, transactions):
    """Critical Verify-Finding (fehlende Sektion im Draft) blockt Versand."""
    _mock_pipeline(monkeypatch, tmp_path, portfolio, transactions)
    sent = []
    monkeypatch.setattr(send_telegram, "send_briefing", _fake_send(sent))

    def _broken_draft(facts_package, mode="monday", client=None):
        # Nur 1 von 6 Sektionen -> verify findet critical (fehlende Sektion).
        return "## Kurzlage\nOK"

    monkeypatch.setattr(llm_briefing, "generate_draft", _broken_draft)

    rc = run_briefing.run("monday", dry_run=False)

    assert rc == 1
    assert list(tmp_path.iterdir()) == []
    assert len(sent) == 1 and sent[0][1] == "alert"


def test_minor_verify_finding_does_not_block(monkeypatch, tmp_path, portfolio, transactions):
    """Minor Verify-Finding (fehlende News-Referenz) blockt Versand nicht."""
    news = [{"title": "Reuters meldet Quartalszahlen"}]
    _mock_pipeline(monkeypatch, tmp_path, portfolio, transactions, news=news)
    sent = []
    monkeypatch.setattr(send_telegram, "send_briefing", _fake_send(sent))
    _mock_draft(monkeypatch)

    rc = run_briefing.run("monday", dry_run=False)

    assert rc == 0
    assert len(sent) == 1 and sent[0][1] == "monday"


# --- Teil 2: 1-Call-Flow-Reihenfolge ------------------------------------------
# Der produktive Pfad: EIN generate_draft-Call -> verify_draft -> final_gate ->
# render_markdown -> send_telegram. Kein Render-Hook zwischen Draft und Verify.

BLOCKING_VERIFICATION = [
    {"severity": "critical", "issue": "Halluzination", "evidence": "e", "correction": "c"}
]


def test_single_call_flow_draft_verify_gate_send(monkeypatch, tmp_path, portfolio, transactions):
    """Produktiver Lauf: genau ein generate_draft-Call, dann verify, dann gate,
    dann Versand — in dieser Reihenfolge."""
    order = []
    _mock_pipeline(monkeypatch, tmp_path, portfolio, transactions)
    sent = []
    monkeypatch.setattr(send_telegram, "send_briefing", _fake_send(sent))

    def _generate(facts_package, mode="monday", client=None):
        order.append("draft")
        return VALID_DRAFT

    monkeypatch.setattr(llm_briefing, "generate_draft", _generate)

    real_verify = verify.verify_draft

    def _verify(facts_package, draft):
        order.append("verify")
        return real_verify(facts_package, draft)

    monkeypatch.setattr(verify, "verify_draft", _verify)

    real_gate = verify.final_gate

    def _gate(verification):
        order.append("gate")
        return real_gate(verification)

    monkeypatch.setattr(verify, "final_gate", _gate)

    rc = run_briefing.run("monday", dry_run=False)

    assert rc == 0
    assert order == ["draft", "verify", "gate"]  # kein Humanize/Review/Revise/Render-Hook
    assert len(sent) == 1 and sent[0][1] == "monday"


def test_dry_run_skips_generate_draft(monkeypatch, tmp_path, portfolio, transactions):
    """Dry-Run: _dry_run_placeholder statt generate_draft — kein LLM-Call."""
    _mock_pipeline(monkeypatch, tmp_path, portfolio, transactions)
    sent = []
    draft_calls = _mock_draft(monkeypatch)
    monkeypatch.setattr(send_telegram, "send_briefing", _fake_send(sent))

    rc = run_briefing.run("monday", dry_run=True)

    assert rc == 0
    assert draft_calls["draft_calls"] == 0  # kein LLM-Call im Dry-Run
    assert sent == []  # kein Telegram
    archived = [p.name for p in tmp_path.iterdir()]
    assert archived and all(name.endswith("-monday-dryrun.md") for name in archived)
    content = (tmp_path / archived[0]).read_text(encoding="utf-8")
    assert "Dry-Run — kein LLM-Call" in content  # Platzhalter-Inhalt unveraendert


def test_generic_verify_error_is_fail_closed(monkeypatch, tmp_path, portfolio, transactions):
    """Unerwarteter (nicht-LLM) Verify-Fehler -> fail-closed: Alert, kein Versand, keine Vault-Datei."""
    _mock_pipeline(monkeypatch, tmp_path, portfolio, transactions)
    sent = []
    monkeypatch.setattr(send_telegram, "send_briefing", _fake_send(sent))
    _mock_draft(monkeypatch)

    def _broken_verify(facts_package, draft):
        raise RuntimeError("interner Verify-Bug")

    monkeypatch.setattr(verify, "verify_draft", _broken_verify)

    rc = run_briefing.run("monday", dry_run=False)

    assert rc == 1
    assert list(tmp_path.iterdir()) == []  # keine Briefing-Datei
    assert len(sent) == 1  # nur Alert, kein Briefing-Versand
    assert sent[0][1] == "alert"
    assert "verify failed" in sent[0][0]
    assert "interner Verify-Bug" in sent[0][0]


def test_verify_llm_error_path_is_fail_closed(monkeypatch, tmp_path, portfolio, transactions):
    """Separater LLMError-Pfad der Verifikation bleibt erhalten (fail-closed, Alert)."""
    _mock_pipeline(monkeypatch, tmp_path, portfolio, transactions)
    sent = []
    monkeypatch.setattr(send_telegram, "send_briefing", _fake_send(sent))
    _mock_draft(monkeypatch)

    def _llm_error_verify(facts_package, draft):
        raise llm_briefing.LLMError("Draft sieht nach LLM-Fehlertext aus (Marker 'fehlgeschlagen').")

    monkeypatch.setattr(verify, "verify_draft", _llm_error_verify)

    rc = run_briefing.run("monday", dry_run=False)

    assert rc == 1
    assert list(tmp_path.iterdir()) == []
    assert len(sent) == 1 and sent[0][1] == "alert"
    assert "verify failed (fail-closed)" in sent[0][0]


def test_gate_block_is_fail_closed(monkeypatch, tmp_path, portfolio, transactions):
    """final_gate blockt (critical/major) -> Alert, Exit 1, kein Versand, keine Vault-Datei."""
    _mock_pipeline(monkeypatch, tmp_path, portfolio, transactions)
    sent = []
    monkeypatch.setattr(send_telegram, "send_briefing", _fake_send(sent))
    _mock_draft(monkeypatch)

    def _blocking_verify(facts_package, draft):
        return BLOCKING_VERIFICATION

    monkeypatch.setattr(verify, "verify_draft", _blocking_verify)

    rc = run_briefing.run("monday", dry_run=False)

    assert rc == 1
    assert list(tmp_path.iterdir()) == []
    assert len(sent) == 1 and sent[0][1] == "alert"
    assert "final_gate" in sent[0][0]


def test_gate_alert_has_no_false_traceback(monkeypatch, tmp_path, portfolio, transactions):
    """Gate-Block-Alert darf keinen nutzlosen Traceback (NoneType) enthalten."""
    _mock_pipeline(monkeypatch, tmp_path, portfolio, transactions)
    sent = []
    monkeypatch.setattr(send_telegram, "send_briefing", _fake_send(sent))
    _mock_draft(monkeypatch)

    def _blocking_verify(facts_package, draft):
        return BLOCKING_VERIFICATION

    monkeypatch.setattr(verify, "verify_draft", _blocking_verify)

    rc = run_briefing.run("monday", dry_run=False)

    assert rc == 1
    assert list(tmp_path.iterdir()) == []
    assert len(sent) == 1 and sent[0][1] == "alert"
    assert "final_gate" in sent[0][0]
    assert "NoneType" not in sent[0][0]  # kein format_exc()-Müll ausserhalb Exception-Kontext
    assert "Traceback" not in sent[0][0]


def test_render_error_is_fail_closed(monkeypatch, tmp_path, portfolio, transactions):
    """Render-Fehler im produktiven Pfad -> fail-closed: Alert, kein Versand,
    keine Vault-Datei, staged wird verworfen."""
    _mock_pipeline(monkeypatch, tmp_path, portfolio, transactions)
    _mock_draft(monkeypatch)
    sent = []
    monkeypatch.setattr(send_telegram, "send_briefing", _fake_send(sent))

    def _broken_render(briefing_text, mode, date=None, status="active"):
        raise RuntimeError("Renderer-Bug")

    monkeypatch.setattr(run_briefing.render_markdown, "render", _broken_render)

    rc = run_briefing.run("monday", dry_run=False)

    assert rc == 1
    assert list(tmp_path.iterdir()) == []  # keine Briefing-Datei
    assert len(sent) == 1 and sent[0][1] == "alert"
    assert "render failed" in sent[0][0]
    assert "Renderer-Bug" in sent[0][0]


# --- Datenbeschaffung: Reihenfolge + changes-Durchreichung ---


def test_non_dry_run_refresh_capture_diff_before_analyze(monkeypatch, tmp_path, portfolio, transactions):
    """Reihenfolge: load_previous -> refresh -> capture_staged -> diff -> analyze; kein load_mock."""
    order = []
    _mock_pipeline(monkeypatch, tmp_path, portfolio, transactions)
    previous = snapshot.build_snapshot(portfolio, transactions, mode="monday", captured_at="2026-08-06T10:00:00+00:00")
    changes = diff.diff_snapshots(
        previous,
        snapshot.build_snapshot(portfolio, transactions, mode="monday", captured_at="2026-08-13T10:00:00+00:00"),
    )
    monkeypatch.setattr(snapshot, "load_previous", lambda: (order.append("load_previous"), previous)[1])
    monkeypatch.setattr(sc_bridge, "refresh_from_sc", lambda: (order.append("refresh"), (portfolio, transactions))[1])
    monkeypatch.setattr(sc_bridge, "load_mock", lambda: (order.append("load_mock"), (portfolio, transactions))[1])
    monkeypatch.setattr(
        snapshot,
        "capture_staged",
        lambda p, t, **kw: (
            order.append("capture_staged"),
            {"snapshot": snapshot.build_snapshot(p, t, **kw), "staged_path": str(tmp_path / "snapshot.staged.json")},
        )[1],
    )
    monkeypatch.setattr(diff, "diff_snapshots", lambda prev, cur: (order.append("diff"), changes)[1])
    monkeypatch.setattr(analyze, "analyze_portfolio", lambda p, t, s: (order.append("analyze"), EMPTY_ANALYSIS)[1])
    _mock_draft(monkeypatch)
    monkeypatch.setattr(send_telegram, "send_briefing", _fake_send([]))

    rc = run_briefing.run("monday", dry_run=False)

    assert rc == 0
    assert order == ["load_previous", "refresh", "capture_staged", "diff", "analyze"]
    assert "load_mock" not in order  # produktiver Lauf nutzt nie Mock-Daten


def test_changes_flow_into_facts_package(monkeypatch, tmp_path, portfolio, transactions):
    """Diff-Ergebnis fliessen als changes in das Faktenpaket (build_facts_package
    erhaelt die changes des Diffs und reicht sie 1:1 durch)."""
    captured = {}
    _mock_pipeline(monkeypatch, tmp_path, portfolio, transactions)
    previous = snapshot.build_snapshot(portfolio, transactions, mode="monday", captured_at="2026-08-06T10:00:00+00:00")
    changes = diff.diff_snapshots(
        previous,
        snapshot.build_snapshot(portfolio, transactions, mode="monday", captured_at="2026-08-13T10:00:00+00:00"),
    )
    monkeypatch.setattr(diff, "diff_snapshots", lambda prev, cur: changes)
    real_build = facts.build_facts_package

    def _build_facts(*args, **kwargs):
        pkg = real_build(*args, **kwargs)
        captured["changes"] = pkg.get("changes")
        return pkg

    monkeypatch.setattr(facts, "build_facts_package", _build_facts)
    _mock_draft(monkeypatch)
    monkeypatch.setattr(send_telegram, "send_briefing", _fake_send([]))

    rc = run_briefing.run("monday", dry_run=False)

    assert rc == 0
    assert captured["changes"] == changes
    assert captured["changes"]["has_previous"] is True


def test_dry_run_passes_changes_none(monkeypatch, tmp_path, portfolio, transactions):
    """Dry-Run: build_facts_package erhaelt changes=None (kein Snapshot/Diff)."""
    captured = {}
    _mock_pipeline(monkeypatch, tmp_path, portfolio, transactions)

    def _fake_build(*args, **kwargs):
        captured["kwargs"] = kwargs
        return {"portfolio": portfolio, "news": [], "deterministic_summary": {}, "strategy_thresholds_pct": {}}

    monkeypatch.setattr(facts, "build_facts_package", _fake_build)

    rc = run_briefing.run("monday", dry_run=True)

    assert rc == 0
    assert captured["kwargs"]["changes"] is None


def test_snapshot_capture_error_is_fail_closed(monkeypatch, tmp_path, portfolio, transactions):
    """snapshot.capture_staged-Fehler -> fail-closed: rc 1, kein Output, kein Mock, nur Alert."""
    _mock_pipeline(monkeypatch, tmp_path, portfolio, transactions)
    mock_calls = []
    monkeypatch.setattr(sc_bridge, "load_mock", lambda: (mock_calls.append(1), (portfolio, transactions))[1])

    def _broken_capture_staged(p, t, **kw):
        raise OSError("Snapshot-Verzeichnis nicht beschreibbar")

    monkeypatch.setattr(snapshot, "capture_staged", _broken_capture_staged)
    sent = []
    monkeypatch.setattr(send_telegram, "send_briefing", _fake_send(sent))

    rc = run_briefing.run("monday", dry_run=False)

    assert rc == 1
    assert mock_calls == []  # kein Mock-Fallback im produktiven Lauf
    assert list(tmp_path.iterdir()) == []  # keine Briefing-Datei
    assert len(sent) == 1 and sent[0][1] == "alert"
    assert "facts failed" in sent[0][0]


# --- Staged-Snapshot-Lifecycle (P0.1): Baseline erst nach final_gate+Render ----


def test_failed_run_discards_staged_snapshot(monkeypatch, tmp_path, portfolio, transactions):
    """Fehler nach capture_staged (Draft-LLMError) -> staged verworfen, kein promote.

    Die produktive Baseline (snapshot.current.json) darf durch einen
    fehlgeschlagenen Lauf nie fortgeschrieben werden.
    """
    calls = _mock_pipeline(monkeypatch, tmp_path, portfolio, transactions)
    sent = []
    monkeypatch.setattr(send_telegram, "send_briefing", _fake_send(sent))
    _mock_draft(monkeypatch, fail=llm_briefing.LLMError("API down"))

    rc = run_briefing.run("monday", dry_run=False)

    assert rc == 1
    assert calls["capture_staged"] == 1  # Live-Stand wurde staged
    assert calls["promote_staged"] == 0  # Baseline NICHT fortgeschrieben
    assert calls["discard_staged"] >= 1  # staged verworfen (Start-Cleanup + finally)
    assert list(tmp_path.iterdir()) == []
    assert len(sent) == 1 and sent[0][1] == "alert"


def test_successful_run_promotes_staged(monkeypatch, tmp_path, portfolio, transactions):
    """Erfolgreicher Lauf: staged wird genau einmal promoted (nach final_gate+Render).

    Nach Promotion wird staged im finally nicht erneut verworfen — der
    Start-Cleanup-Discard ist der einzige discard-Aufruf.
    """
    calls = _mock_pipeline(monkeypatch, tmp_path, portfolio, transactions)
    sent = []
    monkeypatch.setattr(send_telegram, "send_briefing", _fake_send(sent))
    _mock_draft(monkeypatch)

    rc = run_briefing.run("monday", dry_run=False)

    assert rc == 0
    assert calls["capture_staged"] == 1
    assert calls["promote_staged"] == 1  # staged -> Baseline
    assert calls["discard_staged"] == 1  # nur Start-Cleanup (finally skipped nach promote)
    assert len(sent) == 1 and sent[0][1] == "monday"


def test_staged_snapshot_not_promoted_on_gate_block(monkeypatch, tmp_path, portfolio, transactions):
    """final_gate blockt -> staged verworfen, keine Promotion, kein Versand."""
    calls = _mock_pipeline(monkeypatch, tmp_path, portfolio, transactions)
    sent = []
    monkeypatch.setattr(send_telegram, "send_briefing", _fake_send(sent))
    _mock_draft(monkeypatch)

    def _blocking_verify(facts_package, draft):
        return BLOCKING_VERIFICATION

    monkeypatch.setattr(verify, "verify_draft", _blocking_verify)

    rc = run_briefing.run("monday", dry_run=False)

    assert rc == 1
    assert calls["capture_staged"] == 1
    assert calls["promote_staged"] == 0  # Gate-Block: Baseline unangetastet
    assert calls["discard_staged"] >= 1  # staged verworfen
    assert list(tmp_path.iterdir()) == []
    assert len(sent) == 1 and sent[0][1] == "alert"
    assert "final_gate" in sent[0][0]
