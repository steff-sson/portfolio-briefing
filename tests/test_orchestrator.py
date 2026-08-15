"""Orchestrator tests (Phase 4, two-stage pipeline + revise loop) with fake LLM.

Keine echten API-/Telegram-Aufrufe: generate_draft, review_draft und
revise_draft werden gemockt, send_briefing wird gefaked. Fail-closed-Pfade
duerfen keine Briefing-Datei im Vault anlegen. Datenbeschaffung: Dry-Run
via load_mock; produktiver Lauf via load_previous -> refresh_from_sc ->
capture_staged -> diff.diff_snapshots; staged wird erst nach final_gate +
Render promoted (kein update_config).
"""
from __future__ import annotations

from scripts import (
    analyze,
    diff,
    facts,
    filter_news,
    llm_briefing,
    llm_review,
    llm_revise,
    run_briefing,
    sc_bridge,
    send_telegram,
    snapshot,
    verify,
)

EMPTY_STRATEGY = {"strategy": {}}
EMPTY_ANALYSIS = {"checks": {}}

# Valid Draft: alle 5 Pflichtsektionen des Output-Contracts, keine Zahlen.
VALID_DRAFT = (
    "## Kurzlage\n"
    "Apple (AAPL) konform.\n\n"
    "## Datenqualität\n"
    "—\n\n"
    "## Entscheidungsrelevante Punkte\n"
    "—\n\n"
    "## Strategie-Abgleich\n"
    "—\n\n"
    "## Relevante News & Veränderungen\n"
    "—"
)
PASS_REVIEW = {"findings": [], "overall_verdict": "pass"}
REVISE_REVIEW = {
    "findings": [{"severity": "major", "issue": "Zahl weicht ab", "evidence": "e", "correction": "c"}],
    "overall_verdict": "revise",
}


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
    monkeypatch.setattr(analyze, "load_strategy", lambda: EMPTY_STRATEGY)
    monkeypatch.setattr(analyze, "analyze_portfolio", lambda p, t, s: EMPTY_ANALYSIS)
    monkeypatch.setattr(filter_news, "fetch_and_filter_news", lambda p: news or [])
    return calls


def _fake_send(sent):
    def _send(text, mode):
        sent.append((text, mode))
        return True

    return _send


def test_pass_pipeline_sends_briefing(monkeypatch, tmp_path, portfolio, transactions):
    """Pass: Draft + Review ok -> Versand, rc 0."""
    _mock_pipeline(monkeypatch, tmp_path, portfolio, transactions)
    sent = []
    monkeypatch.setattr(send_telegram, "send_briefing", _fake_send(sent))
    monkeypatch.setattr(llm_briefing, "generate_draft", lambda facts_package, mode="monday": VALID_DRAFT)
    monkeypatch.setattr(llm_review, "review_draft", lambda facts_package, draft: PASS_REVIEW)

    rc = run_briefing.run("monday", dry_run=False)

    assert rc == 0
    assert len(sent) == 1
    assert sent[0][1] == "monday"
    assert "Kurzlage" in sent[0][0]


def test_block_verdict_is_fail_closed(monkeypatch, tmp_path, portfolio, transactions):
    _mock_pipeline(monkeypatch, tmp_path, portfolio, transactions)
    sent = []
    monkeypatch.setattr(send_telegram, "send_briefing", _fake_send(sent))
    monkeypatch.setattr(llm_briefing, "generate_draft", lambda facts_package, mode="monday": VALID_DRAFT)
    review = {"findings": [{"severity": "critical", "issue": "Halluzination", "evidence": "e", "correction": "c"}], "overall_verdict": "block"}
    monkeypatch.setattr(llm_review, "review_draft", lambda facts_package, draft: review)

    rc = run_briefing.run("monday", dry_run=False)

    assert rc == 1
    assert list(tmp_path.iterdir()) == []
    assert len(sent) == 1 and sent[0][1] == "alert"


def test_malformed_review_is_fail_closed(monkeypatch, tmp_path, portfolio, transactions):
    """Malformed Review (LLMError aus review_draft) -> Fail-closed, keine Vault-Datei."""
    _mock_pipeline(monkeypatch, tmp_path, portfolio, transactions)
    sent = []
    monkeypatch.setattr(send_telegram, "send_briefing", _fake_send(sent))
    monkeypatch.setattr(llm_briefing, "generate_draft", lambda facts_package, mode="monday": VALID_DRAFT)

    def _bad_review(facts_package, draft):
        raise llm_review.LLMError("Review-Antwort ist kein gueltiges JSON")

    monkeypatch.setattr(llm_review, "review_draft", _bad_review)

    rc = run_briefing.run("monday", dry_run=False)

    assert rc == 1
    assert list(tmp_path.iterdir()) == []
    assert len(sent) == 1 and sent[0][1] == "alert"
    assert "Review" in sent[0][0]


def test_missing_review_key_is_fail_closed(monkeypatch, tmp_path, portfolio, transactions):
    """Fehlender Key im Review (LLMError aus review_draft) -> Fail-closed."""
    _mock_pipeline(monkeypatch, tmp_path, portfolio, transactions)
    sent = []
    monkeypatch.setattr(send_telegram, "send_briefing", _fake_send(sent))
    monkeypatch.setattr(llm_briefing, "generate_draft", lambda facts_package, mode="monday": VALID_DRAFT)

    def _missing_key(facts_package, draft):
        raise llm_review.LLMError("Review-Antwort enthaelt keinen 'findings'-Schluessel.")

    monkeypatch.setattr(llm_review, "review_draft", _missing_key)

    rc = run_briefing.run("monday", dry_run=False)

    assert rc == 1
    assert list(tmp_path.iterdir()) == []
    assert len(sent) == 1 and sent[0][1] == "alert"


def test_major_verify_finding_blocks(monkeypatch, tmp_path, portfolio, transactions):
    """Major Verify-Finding (unbekannter Ticker) blockt Versand trotz Pass-Review."""
    _mock_pipeline(monkeypatch, tmp_path, portfolio, transactions)
    sent = []
    monkeypatch.setattr(send_telegram, "send_briefing", _fake_send(sent))
    bad_draft = VALID_DRAFT.replace("AAPL", "MSFT")  # MSFT nicht im Portfolio
    monkeypatch.setattr(llm_briefing, "generate_draft", lambda facts_package, mode="monday": bad_draft)
    monkeypatch.setattr(llm_review, "review_draft", lambda facts_package, draft: PASS_REVIEW)

    rc = run_briefing.run("monday", dry_run=False)

    assert rc == 1
    assert list(tmp_path.iterdir()) == []
    assert len(sent) == 1 and sent[0][1] == "alert"


def test_critical_verify_finding_blocks(monkeypatch, tmp_path, portfolio, transactions):
    """Critical Verify-Finding (fehlende Sektion) blockt Versand trotz Pass-Review."""
    _mock_pipeline(monkeypatch, tmp_path, portfolio, transactions)
    sent = []
    monkeypatch.setattr(send_telegram, "send_briefing", _fake_send(sent))
    broken_draft = "## Kurzlage\nOK"  # nur 1 von 5 Sektionen
    monkeypatch.setattr(llm_briefing, "generate_draft", lambda facts_package, mode="monday": broken_draft)
    monkeypatch.setattr(llm_review, "review_draft", lambda facts_package, draft: PASS_REVIEW)

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
    monkeypatch.setattr(llm_briefing, "generate_draft", lambda facts_package, mode="monday": VALID_DRAFT)
    monkeypatch.setattr(llm_review, "review_draft", lambda facts_package, draft: PASS_REVIEW)

    rc = run_briefing.run("monday", dry_run=False)

    assert rc == 0
    assert len(sent) == 1 and sent[0][1] == "monday"


def test_dry_run_writes_suffix_file_without_llm(monkeypatch, tmp_path, portfolio, transactions):
    """Dry-Run: kein LLM-Call (generate_draft/review_draft ungemockt -> wuerden scheitern), rc 0."""
    _mock_pipeline(monkeypatch, tmp_path, portfolio, transactions)
    sent = []
    monkeypatch.setattr(send_telegram, "send_briefing", _fake_send(sent))

    rc = run_briefing.run("monday", dry_run=True)

    assert rc == 0
    assert sent == []  # kein Telegram
    archived = [p.name for p in tmp_path.iterdir()]
    assert archived and all(name.endswith("-monday-dryrun.md") for name in archived)


def test_generic_verify_error_is_fail_closed(monkeypatch, tmp_path, portfolio, transactions):
    """Unerwarteter (nicht-LLM) Verify-Fehler -> fail-closed: Alert, kein Versand, keine Vault-Datei."""
    _mock_pipeline(monkeypatch, tmp_path, portfolio, transactions)
    sent = []
    monkeypatch.setattr(send_telegram, "send_briefing", _fake_send(sent))
    monkeypatch.setattr(llm_briefing, "generate_draft", lambda facts_package, mode="monday": VALID_DRAFT)

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
    monkeypatch.setattr(llm_briefing, "generate_draft", lambda facts_package, mode="monday": VALID_DRAFT)

    def _llm_error_verify(facts_package, draft):
        raise llm_briefing.LLMError("Draft sieht nach LLM-Fehlertext aus (Marker 'fehlgeschlagen').")

    monkeypatch.setattr(verify, "verify_draft", _llm_error_verify)

    rc = run_briefing.run("monday", dry_run=False)

    assert rc == 1
    assert list(tmp_path.iterdir()) == []
    assert len(sent) == 1 and sent[0][1] == "alert"
    assert "verify failed (fail-closed)" in sent[0][0]


def test_gate_alert_has_no_false_traceback(monkeypatch, tmp_path, portfolio, transactions):
    """Gate-Block-Alert darf keinen nutzlosen Traceback (NoneType) enthalten."""
    _mock_pipeline(monkeypatch, tmp_path, portfolio, transactions)
    sent = []
    monkeypatch.setattr(send_telegram, "send_briefing", _fake_send(sent))
    monkeypatch.setattr(llm_briefing, "generate_draft", lambda facts_package, mode="monday": VALID_DRAFT)
    review = {"findings": [{"severity": "critical", "issue": "Halluzination", "evidence": "e", "correction": "c"}], "overall_verdict": "block"}
    monkeypatch.setattr(llm_review, "review_draft", lambda facts_package, draft: review)

    rc = run_briefing.run("monday", dry_run=False)

    assert rc == 1
    assert list(tmp_path.iterdir()) == []
    assert len(sent) == 1 and sent[0][1] == "alert"
    assert "final_gate" in sent[0][0]
    assert "NoneType" not in sent[0][0]  # kein format_exc()-Müll ausserhalb Exception-Kontext
    assert "Traceback" not in sent[0][0]


def _sequential_review(*responses):
    """Review-Mock, der je Aufruf die naechste Antwort liefert (letzte wiederholt)."""
    calls = {"n": 0}

    def _review(facts_package, draft):
        response = responses[min(calls["n"], len(responses) - 1)]
        calls["n"] += 1
        return response

    return _review


def test_pass_verdict_skips_revision(monkeypatch, tmp_path, portfolio, transactions):
    """overall_verdict=pass -> kein Revise-Aufruf, unveraendert zum final_gate, Versand."""
    _mock_pipeline(monkeypatch, tmp_path, portfolio, transactions)
    sent = []
    revise_calls = []
    monkeypatch.setattr(send_telegram, "send_briefing", _fake_send(sent))
    monkeypatch.setattr(llm_briefing, "generate_draft", lambda facts_package, mode="monday": VALID_DRAFT)
    monkeypatch.setattr(llm_review, "review_draft", lambda facts_package, draft: PASS_REVIEW)

    def _no_revise(facts_package, draft, review):
        revise_calls.append(1)
        raise AssertionError("revise_draft darf bei pass nicht aufgerufen werden")

    monkeypatch.setattr(llm_revise, "revise_draft", _no_revise)

    rc = run_briefing.run("monday", dry_run=False)

    assert rc == 0
    assert revise_calls == []  # keine Revision
    assert len(sent) == 1 and sent[0][1] == "monday"


def test_revise_then_successful_revision_sends(monkeypatch, tmp_path, portfolio, transactions):
    """revise -> Revision -> erneutes Review pass -> final_gate pass -> Versand."""
    _mock_pipeline(monkeypatch, tmp_path, portfolio, transactions)
    sent = []
    revise_calls = []
    monkeypatch.setattr(send_telegram, "send_briefing", _fake_send(sent))
    monkeypatch.setattr(llm_briefing, "generate_draft", lambda facts_package, mode="monday": VALID_DRAFT)
    monkeypatch.setattr(llm_review, "review_draft", _sequential_review(REVISE_REVIEW, PASS_REVIEW))
    monkeypatch.setattr(llm_revise, "revise_draft", lambda facts_package, draft, review: (revise_calls.append(1), VALID_DRAFT)[1])

    rc = run_briefing.run("monday", dry_run=False)

    assert rc == 0
    assert len(revise_calls) == 1  # genau eine Revision
    assert len(sent) == 1 and sent[0][1] == "monday"


def test_revise_second_verify_blocks(monkeypatch, tmp_path, portfolio, transactions):
    """Revision -> erneuter verify_draft findet major-Finding -> fail-closed, kein Versand."""
    _mock_pipeline(monkeypatch, tmp_path, portfolio, transactions)
    sent = []
    monkeypatch.setattr(send_telegram, "send_briefing", _fake_send(sent))
    monkeypatch.setattr(llm_briefing, "generate_draft", lambda facts_package, mode="monday": VALID_DRAFT)
    monkeypatch.setattr(llm_review, "review_draft", _sequential_review(REVISE_REVIEW, PASS_REVIEW))
    bad_revision = VALID_DRAFT.replace("AAPL", "MSFT")  # MSFT nicht im Portfolio -> major
    monkeypatch.setattr(llm_revise, "revise_draft", lambda facts_package, draft, review: bad_revision)

    rc = run_briefing.run("monday", dry_run=False)

    assert rc == 1
    assert list(tmp_path.iterdir()) == []  # keine Briefing-Datei
    assert len(sent) == 1 and sent[0][1] == "alert"


def test_max_revisions_blocks_after_one_attempt(monkeypatch, tmp_path, portfolio, transactions):
    """Review bleibt revise -> genau eine Revision, dann MAX_REVISIONS -> final_gate blockt."""
    _mock_pipeline(monkeypatch, tmp_path, portfolio, transactions)
    sent = []
    revise_calls = []
    monkeypatch.setattr(send_telegram, "send_briefing", _fake_send(sent))
    monkeypatch.setattr(llm_briefing, "generate_draft", lambda facts_package, mode="monday": VALID_DRAFT)
    monkeypatch.setattr(llm_review, "review_draft", _sequential_review(REVISE_REVIEW))
    monkeypatch.setattr(llm_revise, "revise_draft", lambda facts_package, draft, review: (revise_calls.append(1), VALID_DRAFT)[1])

    rc = run_briefing.run("monday", dry_run=False)

    assert rc == 1
    assert len(revise_calls) == 1  # keine Endlosschleife
    assert list(tmp_path.iterdir()) == []
    assert len(sent) == 1 and sent[0][1] == "alert"
    assert "final_gate" in sent[0][0]


def test_revise_llm_error_is_fail_closed(monkeypatch, tmp_path, portfolio, transactions):
    """Revise-Call wirft LLMError -> fail-closed, kein Versand, keine Vault-Datei."""
    _mock_pipeline(monkeypatch, tmp_path, portfolio, transactions)
    sent = []
    monkeypatch.setattr(send_telegram, "send_briefing", _fake_send(sent))
    monkeypatch.setattr(llm_briefing, "generate_draft", lambda facts_package, mode="monday": VALID_DRAFT)
    monkeypatch.setattr(llm_review, "review_draft", lambda facts_package, draft: REVISE_REVIEW)

    def _broken_revise(facts_package, draft, review):
        raise llm_revise.LLMError("Revision fehlgeschlagen: API down")

    monkeypatch.setattr(llm_revise, "revise_draft", _broken_revise)

    rc = run_briefing.run("monday", dry_run=False)

    assert rc == 1
    assert list(tmp_path.iterdir()) == []
    assert len(sent) == 1 and sent[0][1] == "alert"
    assert "revise failed (fail-closed)" in sent[0][0]


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
    monkeypatch.setattr(llm_briefing, "generate_draft", lambda facts_package, mode="monday": VALID_DRAFT)
    monkeypatch.setattr(llm_review, "review_draft", lambda facts_package, draft: PASS_REVIEW)
    monkeypatch.setattr(send_telegram, "send_briefing", _fake_send([]))

    rc = run_briefing.run("monday", dry_run=False)

    assert rc == 0
    assert order == ["load_previous", "refresh", "capture_staged", "diff", "analyze"]
    assert "load_mock" not in order  # produktiver Lauf nutzt nie Mock-Daten


def test_changes_flow_into_facts_package(monkeypatch, tmp_path, portfolio, transactions):
    """Diff-Ergebnis fliessen als changes in das Faktenpaket (generate_draft sieht es)."""
    captured = {}
    _mock_pipeline(monkeypatch, tmp_path, portfolio, transactions)
    previous = snapshot.build_snapshot(portfolio, transactions, mode="monday", captured_at="2026-08-06T10:00:00+00:00")
    changes = diff.diff_snapshots(
        previous,
        snapshot.build_snapshot(portfolio, transactions, mode="monday", captured_at="2026-08-13T10:00:00+00:00"),
    )
    monkeypatch.setattr(diff, "diff_snapshots", lambda prev, cur: changes)
    monkeypatch.setattr(
        llm_briefing,
        "generate_draft",
        lambda facts_package, mode="monday": (captured.update(facts=facts_package), VALID_DRAFT)[1],
    )
    monkeypatch.setattr(llm_review, "review_draft", lambda facts_package, draft: PASS_REVIEW)
    monkeypatch.setattr(send_telegram, "send_briefing", _fake_send([]))

    rc = run_briefing.run("monday", dry_run=False)

    assert rc == 0
    assert captured["facts"]["changes"] == changes
    assert captured["facts"]["changes"]["has_previous"] is True


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
    """Fehler nach capture_staged (Review-LLMError) -> staged verworfen, kein promote.

    Die produktive Baseline (snapshot.current.json) darf durch einen
    fehlgeschlagenen Lauf nie fortgeschrieben werden.
    """
    calls = _mock_pipeline(monkeypatch, tmp_path, portfolio, transactions)
    sent = []
    monkeypatch.setattr(send_telegram, "send_briefing", _fake_send(sent))
    monkeypatch.setattr(llm_briefing, "generate_draft", lambda facts_package, mode="monday": VALID_DRAFT)

    def _bad_review(facts_package, draft):
        raise llm_review.LLMError("Review-Antwort ist kein gueltiges JSON")

    monkeypatch.setattr(llm_review, "review_draft", _bad_review)

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
    monkeypatch.setattr(llm_briefing, "generate_draft", lambda facts_package, mode="monday": VALID_DRAFT)
    monkeypatch.setattr(llm_review, "review_draft", lambda facts_package, draft: PASS_REVIEW)

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
    monkeypatch.setattr(llm_briefing, "generate_draft", lambda facts_package, mode="monday": VALID_DRAFT)
    review = {"findings": [{"severity": "critical", "issue": "Halluzination", "evidence": "e", "correction": "c"}], "overall_verdict": "block"}
    monkeypatch.setattr(llm_review, "review_draft", lambda facts_package, draft: review)

    rc = run_briefing.run("monday", dry_run=False)

    assert rc == 1
    assert calls["capture_staged"] == 1
    assert calls["promote_staged"] == 0  # Gate-Block: Baseline unangetastet
    assert calls["discard_staged"] >= 1  # staged verworfen
    assert list(tmp_path.iterdir()) == []
    assert len(sent) == 1 and sent[0][1] == "alert"
    assert "final_gate" in sent[0][0]
