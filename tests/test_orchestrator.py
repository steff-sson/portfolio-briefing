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
    final_briefing,
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

# Valid Draft: alle Pflichtsektionen des Output-Contracts, keine Zahlen.
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
    monkeypatch.setattr(sc_bridge, "fetch_watchlist_from_sc", lambda: [])
    monkeypatch.setattr(sc_bridge, "load_mock_watchlist", lambda: [])
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
    """Major Verify-Finding (unbekannter Ticker im GERENDERTEN Text) blockt
    Versand trotz Pass-Review. Der produktive Pfad prueft seit Teil 2 den
    gerenderten Text (final_briefing.render_final_briefing), nicht den
    rohen LLM-Draft."""
    _mock_pipeline(monkeypatch, tmp_path, portfolio, transactions)
    sent = []
    monkeypatch.setattr(send_telegram, "send_briefing", _fake_send(sent))
    monkeypatch.setattr(llm_briefing, "generate_draft", lambda facts_package, mode="monday": VALID_DRAFT)

    def _bad_render(facts_package, mode="monday"):
        # Gerenderter Text enthaelt einen Ticker, den das Portfolio nicht kennt.
        # Kurzer Output-Contract (Phase 5): keine '## Empfehlung'-Sektion mehr —
        # den unbekannten Ticker ans Ende der Kurzlage (vor der naechsten
        # Sektion) haengen, die verify gegen das Portfolio prueft.
        return final_briefing.render_final_briefing(facts_package, mode=mode).replace(
            "## Datenqualität",
            "MSFT (MSFT) im Fokus — Empfehlung: nicht bestimmbar.\n\n## Datenqualität",
            1,
        )

    monkeypatch.setattr(final_briefing, "render_final_briefing", _bad_render)
    monkeypatch.setattr(llm_review, "review_draft", lambda facts_package, draft: PASS_REVIEW)

    rc = run_briefing.run("monday", dry_run=False)

    assert rc == 1
    assert list(tmp_path.iterdir()) == []
    assert len(sent) == 1 and sent[0][1] == "alert"


def test_critical_verify_finding_blocks(monkeypatch, tmp_path, portfolio, transactions):
    """Critical Verify-Finding (fehlende Sektion im GERENDERTEN Text) blockt
    Versand trotz Pass-Review (Teil 2: verify prueft den gerenderten Text)."""
    _mock_pipeline(monkeypatch, tmp_path, portfolio, transactions)
    sent = []
    monkeypatch.setattr(send_telegram, "send_briefing", _fake_send(sent))
    monkeypatch.setattr(llm_briefing, "generate_draft", lambda facts_package, mode="monday": VALID_DRAFT)

    def _broken_render(facts_package, mode="monday"):
        # Nur 1 von 6 Sektionen -> verify findet critical (fehlende Sektion).
        return "## Kurzlage\nOK"

    monkeypatch.setattr(final_briefing, "render_final_briefing", _broken_render)
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


# --- Teil 2: final_briefing-Hook im Orchestrator ------------------------------
# Der produktive Pfad rendert nach dem LLM-Draft (und nach jeder Revision) via
# final_briefing.render_final_briefing und prueft ab da den GERENDERTEN Text.
# Der Dry-Run-Pfad bleibt unveraendert (_dry_run_placeholder, kein Render).


def test_final_render_hook_runs_after_draft(monkeypatch, tmp_path, portfolio, transactions):
    """Produktiver Lauf: render_final_briefing wird nach dem Draft genau einmal
    aufgerufen, verify/review sehen den GERENDERTEN Text, Versand rc 0."""
    _mock_pipeline(monkeypatch, tmp_path, portfolio, transactions)
    sent = []
    seen = {}
    render_calls = {"n": 0}
    real_render = final_briefing.render_final_briefing
    monkeypatch.setattr(send_telegram, "send_briefing", _fake_send(sent))
    monkeypatch.setattr(llm_briefing, "generate_draft", lambda facts_package, mode="monday": VALID_DRAFT)
    monkeypatch.setattr(llm_review, "review_draft", lambda facts_package, draft: seen.update(reviewed=draft) or PASS_REVIEW)

    def _render(facts_package, mode="monday"):
        render_calls["n"] += 1
        out = real_render(facts_package, mode=mode)
        seen["rendered"] = out
        return out

    monkeypatch.setattr(final_briefing, "render_final_briefing", _render)

    rc = run_briefing.run("monday", dry_run=False)

    assert rc == 0
    assert render_calls["n"] == 1  # genau ein Render nach dem Draft
    assert seen["reviewed"] == seen["rendered"]  # Review prueft den gerenderten Text
    assert "# Portfolio-Briefing — Montag" in seen["rendered"]
    assert len(sent) == 1 and sent[0][1] == "monday"


def test_final_render_hook_runs_again_after_revision(monkeypatch, tmp_path, portfolio, transactions):
    """Nach einer Revision wird erneut gerendert (Teil 2): verify/review des
    zweiten Durchgangs pruefen den RE-RENDERTEN Text, nicht den rohen
    Revise-Output."""
    _mock_pipeline(monkeypatch, tmp_path, portfolio, transactions)
    sent = []
    seen = []
    render_calls = {"n": 0}
    real_render = final_briefing.render_final_briefing
    monkeypatch.setattr(send_telegram, "send_briefing", _fake_send(sent))
    monkeypatch.setattr(llm_briefing, "generate_draft", lambda facts_package, mode="monday": VALID_DRAFT)
    monkeypatch.setattr(llm_review, "review_draft", lambda facts_package, draft: seen.append(draft) or (PASS_REVIEW if len(seen) > 1 else REVISE_REVIEW))
    monkeypatch.setattr(llm_revise, "revise_draft", lambda facts_package, draft, review: VALID_DRAFT)

    def _render(facts_package, mode="monday"):
        render_calls["n"] += 1
        return real_render(facts_package, mode=mode)

    monkeypatch.setattr(final_briefing, "render_final_briefing", _render)

    rc = run_briefing.run("monday", dry_run=False)

    assert rc == 0
    assert render_calls["n"] == 2  # Render nach Draft + Re-Render nach Revision
    assert len(seen) == 2  # zwei Reviews (vor + nach Revision)
    assert seen[0] == seen[1]  # beide pruefen den gerenderten (identischen) Text
    assert len(sent) == 1 and sent[0][1] == "monday"


def test_dry_run_skips_final_render_hook(monkeypatch, tmp_path, portfolio, transactions):
    """Dry-Run: render_final_briefing wird NICHT aufgerufen — der Platzhalter
    _dry_run_placeholder bleibt der unveraenderte Dry-Run-Pfad (Teil 2)."""
    _mock_pipeline(monkeypatch, tmp_path, portfolio, transactions)
    sent = []
    render_calls = {"n": 0}
    monkeypatch.setattr(send_telegram, "send_briefing", _fake_send(sent))
    monkeypatch.setattr(final_briefing, "render_final_briefing", lambda facts_package, mode="monday": (render_calls.__setitem__("n", render_calls["n"] + 1), "GERENDERT")[1])

    rc = run_briefing.run("monday", dry_run=True)

    assert rc == 0
    assert render_calls["n"] == 0  # kein Render im Dry-Run
    assert sent == []  # kein Telegram
    archived = [p.name for p in tmp_path.iterdir()]
    assert archived and all(name.endswith("-monday-dryrun.md") for name in archived)
    content = (tmp_path / archived[0]).read_text(encoding="utf-8")
    assert "Dry-Run — kein LLM-Call" in content  # Platzhalter-Inhalt unveraendert
    assert "GERENDERT" not in content


def test_final_render_error_is_fail_closed(monkeypatch, tmp_path, portfolio, transactions):
    """Render-Fehler im produktiven Pfad -> fail-closed: Alert, kein Versand,
    keine Vault-Datei, staged wird verworfen."""
    _mock_pipeline(monkeypatch, tmp_path, portfolio, transactions)
    sent = []
    monkeypatch.setattr(send_telegram, "send_briefing", _fake_send(sent))
    monkeypatch.setattr(llm_briefing, "generate_draft", lambda facts_package, mode="monday": VALID_DRAFT)

    def _broken_render(facts_package, mode="monday"):
        raise RuntimeError("Renderer-Bug")

    monkeypatch.setattr(final_briefing, "render_final_briefing", _broken_render)

    rc = run_briefing.run("monday", dry_run=False)

    assert rc == 1
    assert list(tmp_path.iterdir()) == []  # keine Briefing-Datei
    assert len(sent) == 1 and sent[0][1] == "alert"
    assert "final render failed" in sent[0][0]
    assert "Renderer-Bug" in sent[0][0]


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


# --- GLM-Review-Kontrakt: deterministische Sektionen --------------------------
# Die finalen Sektionen werden von Python deterministisch gerendert
# (final_briefing.py); GLM darf dort keine eigenen Status-, Zahlen-,
# Kategorien-, Transaktions- oder Strategieänderungen behaupten. Liefert
# das Review-JSON trotzdem klar halluzinierte Findings, filtert der
# Orchestrator genau diese — echte Findings bleiben fail-closed.

# Die drei bekannten halluzinierten Findings des echten Laufs 2026-08-19:
# Datenqualität, fehlende Ampelkategorien, Transaktions-/Strategieänderungen.
HALLUCINATED_FINDINGS = [
    {
        "severity": "major",
        "issue": "Datenqualität: Status stimmt nicht mit dem Faktenpaket überein",
        "evidence": "Draft: 'Datenqualität: ok.' — Faktenpaket: data_quality issues vorhanden",
        "correction": "Datenqualität aus dem Faktenpaket übernehmen",
    },
    {
        "severity": "major",
        "issue": "Strategie-Abgleich: fehlende Ampelkategorie",
        "evidence": "Strategie-Abgleich enthält nur 6 Kategorien statt der 7 verbindlichen",
        "correction": "Alle 7 Ampelkategorien ergänzen",
    },
    {
        "severity": "major",
        "issue": "Transaktionszahlen widersprechen dem Faktenpaket",
        "evidence": "Draft nennt '3 neu, 2 entfernt', Faktenpaket added_count/removed_count 0",
        "correction": "Transaktionszahlen 1:1 aus dem Faktenpaket übernehmen",
    },
]
HALLUCINATED_REVIEW = {"findings": HALLUCINATED_FINDINGS, "overall_verdict": "block"}

# Echte (nicht-deterministische) Review-Findings: bleiben fail-closed blockierend.
REAL_REVIEW_FINDINGS = [
    {
        "severity": "critical",
        "issue": "Kaufanweisung außerhalb der Optionen-Sektion",
        "evidence": "Draft enthält 'kaufen Sie Apple' außerhalb von ## Entscheidungsrelevante Punkte",
        "correction": "Kaufanweisung entfernen",
    },
    {
        "severity": "major",
        "issue": "Kurzlage widerspricht der deterministischen Zusammenfassung",
        "evidence": "Draft: 'Alle Grenzen eingehalten' — deterministic_summary: red_checks enthält drift",
        "correction": "Kurzlage an red_checks anpassen",
    },
]


def test_hallucinated_deterministic_review_does_not_block(monkeypatch, tmp_path, portfolio, transactions):
    """Regression (echter Lauf 2026-08-19): ein Review mit den drei bekannten
    halluzinierten Findings (Datenqualität, Ampelkategorien, Transaktionen/
    Strategie) blockt den Gate NICHT mehr — die deterministisch gerenderten
    Sektionen sind kein Review-Bereich für Faktenbehauptungen."""
    _mock_pipeline(monkeypatch, tmp_path, portfolio, transactions)
    sent = []
    monkeypatch.setattr(send_telegram, "send_briefing", _fake_send(sent))
    monkeypatch.setattr(llm_briefing, "generate_draft", lambda facts_package, mode="monday": VALID_DRAFT)
    monkeypatch.setattr(llm_review, "review_draft", lambda facts_package, draft: HALLUCINATED_REVIEW)

    rc = run_briefing.run("monday", dry_run=False)

    assert rc == 0
    assert len(sent) == 1 and sent[0][1] == "monday"  # Briefing versendet, nur kein Alert
    assert all(mode != "alert" for _, mode in sent)


def test_real_review_finding_still_blocks(monkeypatch, tmp_path, portfolio, transactions):
    """Fail-closed: echte nicht-deterministische Review-Findings (unerlaubte
    Kaufanweisung, Kurzlage-Verstoß) bleiben trotz Filterung blockierend —
    kein Umgehen des Gates durch den Kontrakt-Filter."""
    _mock_pipeline(monkeypatch, tmp_path, portfolio, transactions)
    sent = []
    monkeypatch.setattr(send_telegram, "send_briefing", _fake_send(sent))
    monkeypatch.setattr(llm_briefing, "generate_draft", lambda facts_package, mode="monday": VALID_DRAFT)
    review = {"findings": REAL_REVIEW_FINDINGS, "overall_verdict": "block"}
    monkeypatch.setattr(llm_review, "review_draft", lambda facts_package, draft: review)

    rc = run_briefing.run("monday", dry_run=False)

    assert rc == 1
    assert list(tmp_path.iterdir()) == []  # keine Briefing-Datei
    assert len(sent) == 1 and sent[0][1] == "alert"
    assert "final_gate" in sent[0][0]


def test_filter_removes_hallucinated_deterministic_findings():
    """Unit: die drei bekannten halluzinierten Findings werden gefiltert."""
    filtered = run_briefing._filter_review_findings(HALLUCINATED_REVIEW)
    assert filtered["findings"] == []
    # Alle Findings halluziniert -> overall_verdict hat keine Basis mehr: pass,
    # damit ein reines Halluzinations-Review den Gate nicht blockiert.
    assert filtered["overall_verdict"] == "pass"


def test_filter_keeps_real_review_findings():
    """Unit: echte Review-Findings bleiben erhalten (fail-closed), Verdict bleibt."""
    review = {"findings": REAL_REVIEW_FINDINGS, "overall_verdict": "block"}
    filtered = run_briefing._filter_review_findings(review)
    assert filtered["findings"] == REAL_REVIEW_FINDINGS
    assert filtered["overall_verdict"] == "block"


def test_filter_mixed_keeps_remaining_findings_and_verdict():
    """Unit: gemischtes Review — nur halluzinierte Findings fliegen raus,
    echte bleiben, overall_verdict bleibt unangetastet (fail-closed)."""
    review = {
        "findings": [
            HALLUCINATED_FINDINGS[0],
            {
                "severity": "major",
                "issue": "Kurzlage verzerrt: rot statt gelb für Drift",
                "evidence": "deterministic_summary: yellow_checks=[drift], Draft behauptet rot",
                "correction": "Kurzlage an yellow_checks anpassen",
            },
        ],
        "overall_verdict": "revise",
    }
    filtered = run_briefing._filter_review_findings(review)
    assert len(filtered["findings"]) == 1
    assert filtered["findings"][0]["issue"] == "Kurzlage verzerrt: rot statt gelb für Drift"
    assert filtered["overall_verdict"] == "revise"  # Verdict bleibt: echte Findings übrig


def test_filter_defensive_on_invalid_input():
    """Unit: Nicht-Dict/fehlender findings-Key bleiben unverändert (defensiv)."""
    assert run_briefing._filter_review_findings(None) is None
    assert run_briefing._filter_review_findings("nope") == "nope"
    review = {"overall_verdict": "pass"}
    assert run_briefing._filter_review_findings(review) is review
    review = {"findings": "kaputt", "overall_verdict": "pass"}
    assert run_briefing._filter_review_findings(review) is review


def test_filter_does_not_mutate_input():
    """Unit: Filter erzeugt keine Mutation des Eingabe-Reviews."""
    import copy

    review = copy.deepcopy(HALLUCINATED_REVIEW)
    run_briefing._filter_review_findings(review)
    assert review == HALLUCINATED_REVIEW


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
    """Revision -> erneuter verify_draft findet major-Finding im GERENDERTEN
    Text -> fail-closed, kein Versand. Der Re-Render nach der Revision (Teil 2)
    ist die Basis des zweiten verify-Laufs — ein Renderer-Output mit
    unbekanntem Ticker blockt, nicht der rohe Revise-Output."""
    _mock_pipeline(monkeypatch, tmp_path, portfolio, transactions)
    sent = []
    monkeypatch.setattr(send_telegram, "send_briefing", _fake_send(sent))
    monkeypatch.setattr(llm_briefing, "generate_draft", lambda facts_package, mode="monday": VALID_DRAFT)
    monkeypatch.setattr(llm_review, "review_draft", _sequential_review(REVISE_REVIEW, PASS_REVIEW))
    monkeypatch.setattr(llm_revise, "revise_draft", lambda facts_package, draft, review: VALID_DRAFT)

    # Referenz auf den ungemockten Renderer vor dem Monkeypatch.
    real_render = final_briefing.render_final_briefing
    render_calls = {"n": 0}

    def _flaky_render(facts_package, mode="monday"):
        # Erster Render ok; der Re-Render nach der Revision liefert einen Text
        # mit unbekanntem Ticker -> zweiter verify-Lauf findet major-Finding.
        render_calls["n"] += 1
        rendered = real_render(facts_package, mode=mode)
        if render_calls["n"] > 1:
            # Kurzer Output-Contract (Phase 5): keine '## Empfehlung'-Sektion
            # mehr — den unbekannten Ticker ans Ende der Kurzlage (vor der
            # naechsten Sektion) haengen, die verify gegen das Portfolio prueft.
            return rendered.replace(
                "## Datenqualität",
                "MSFT (MSFT) im Fokus — Empfehlung: nicht bestimmbar.\n\n## Datenqualität",
                1,
            )
        return rendered

    monkeypatch.setattr(final_briefing, "render_final_briefing", _flaky_render)

    rc = run_briefing.run("monday", dry_run=False)

    assert rc == 1
    assert render_calls["n"] == 2  # Render nach Draft + Re-Render nach Revision
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
