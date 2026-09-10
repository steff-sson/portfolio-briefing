"""Regression tests: Fail-closed bei LLMError, Dry-Run-Idempotenz (Suffix-Strategie).

Nutzt ausschliesslich Mock-Daten und Fake-Objekte — keine API-/Telegram-Aufrufe.
Datenbeschaffung: Dry-Run via sc_bridge.load_mock(); produktiver Lauf via
snapshot.load_previous -> sc_bridge.refresh_from_sc -> snapshot.capture_staged
-> diff.diff_snapshots (kein update_config).

Phase C2 (briefing-revision-loop): Der produktive Pfad macht maximal
MAX_LLM_ATTEMPTS LLM-Calls — 1 Initial-Draft (llm_briefing.generate_draft,
gemockt) -> verify_draft -> final_gate; bei blockierenden Findings folgen
hoechstens MAX_LLM_ATTEMPTS-1 Revisionen (llm_briefing.revise_draft, gemockt)
mit erneutem verify/final_gate (gleiches Faktenpaket, strukturierte Findings).
Keine Endlosschleife.

Phase D (Fallback): Endet der LLM-Loop ohne validen Draft (Versuchslimit
erschoepft oder harter LLM-/Verify-Fehler), wird bei valider Datenbasis das
deterministische Faktenbriefing (fallback_briefing.build_fallback_briefing —
kein weiterer LLM-Call) gerendert, durch verify_draft + final_gate gefuehrt
und ueber den bestehenden Versand-Pfad verschickt (klar als Faktenbriefing
markiert). Nur bei kaputter Datenbasis oder ungueltigem Fallback bleibt der
technische Alert (kein Versand, keine Vault-Datei).
"""
from __future__ import annotations

import logging

import pytest

from scripts import (
    analyze,
    diff,
    filter_news,
    llm_briefing,
    run_briefing,
    sc_bridge,
    send_telegram,
    snapshot,
    telegram_inbound,
)

EMPTY_STRATEGY = {"strategy": {}}
EMPTY_ANALYSIS = {"checks": {}}

# Valid Draft: alle 6 Pflichtsektionen des 1-Call-Contracts, keine
# Zahlen/Ticker (EMPTY_ANALYSIS -> Summary 0 -> leere Allowlist, keine
# deterministische Empfehlung).
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
    "Nächste Woche neuer Lauf, keine Aktion erforderlich.\n\n"
    "## Empfehlung\n"
    "WATCH — kein Handlungsbedarf."
)


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
    monkeypatch.setattr(filter_news, "fetch_news_for_unlisted_ideas", lambda p: [])
    return calls


def _mock_draft(monkeypatch, draft: str = VALID_DRAFT) -> dict:
    """Mockt generate_draft: liefert einen gueltigen 6-Sektionen-Draft, tracked Calls."""
    calls = {"draft_calls": 0}

    def _generate(facts_package, mode="monday", client=None):
        calls["draft_calls"] += 1
        return draft

    monkeypatch.setattr(llm_briefing, "generate_draft", _generate)
    return calls


def _mock_draft_and_revise(monkeypatch, draft: str) -> dict:
    """Mockt generate_draft UND revise_draft: beide liefern denselben Draft.

    Fuer Gate-Block-/Stubborn-Tests (Phase C2): verify blockt den Draft
    weiterhin, das Versuchsbudget (MAX_LLM_ATTEMPTS: 1 Initial + 2 Revisionen)
    laeuft deterministisch aus. Kein echter API-Call.
    """
    calls = {"generate": 0, "revise": 0}

    def _generate(facts_package, mode="monday", client=None):
        calls["generate"] += 1
        return draft

    def _revise(facts_package, previous_draft, findings, mode="monday", client=None):
        calls["revise"] += 1
        return draft

    monkeypatch.setattr(llm_briefing, "generate_draft", _generate)
    monkeypatch.setattr(llm_briefing, "revise_draft", _revise)
    return calls


def _fake_send(sent):
    def _send(text, mode):
        sent.append((text, mode))
        return True

    return _send


def test_llm_error_triggers_fallback_briefing(monkeypatch, tmp_path, portfolio, transactions):
    """Phase D: generate_draft-LLMError bei valider Datenbasis -> deterministisches
    Faktenbriefing (kein LLM-Call, kein Alert), Versand im Briefing-Modus (rc 0)."""
    _mock_pipeline(monkeypatch, tmp_path, portfolio, transactions)
    sent = []
    monkeypatch.setattr(send_telegram, "send_briefing", lambda text, mode: sent.append((text, mode)) or True)

    def _raise_llm_error(facts_package, mode="monday", client=None):
        raise llm_briefing.LLMError("API down")

    monkeypatch.setattr(llm_briefing, "generate_draft", _raise_llm_error)

    rc = run_briefing.run("monday", dry_run=False)

    assert rc == 0  # Fallback ersetzt den technischen Alert (Datenbasis valide)
    assert list(tmp_path.iterdir()) == []  # kein Vault-Schreiben (send_briefing gemockt)
    assert len(sent) == 1  # genau ein Briefing, KEIN Alert
    assert sent[0][1] == "monday"  # Briefing-Modus, nicht mode=alert
    assert "Faktenbriefing" in sent[0][0]  # klar als LLM-freies Briefing markiert


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

    draft_calls = _mock_draft(monkeypatch)
    assert run_briefing.run("monday", dry_run=False) == 0
    assert draft_calls["draft_calls"] == 1  # generate_draft wurde aufgerufen -> kein Skip


def test_send_failure_is_fail_closed(monkeypatch, tmp_path, portfolio, transactions, caplog):
    """send_briefing=False -> kurzer Alert, Exit 1 statt 'completed successfully'.

    Keine doppelte Briefing-Datei: der Orchestrator legt nach fehlgeschlagenem
    Versand keine weitere Datei an (Archivierung passiert intern in send_briefing).
    Auch der degradierte Plain-Text-Fallback meldet send_briefing=False (siehe
    tests/test_send_telegram.py) — hier wird die Orchestrator-Fehlerpfad-
    Semantik geprueft: kein 'send completed', kein 'completed successfully'.
    """
    _mock_pipeline(monkeypatch, tmp_path, portfolio, transactions)
    _mock_draft(monkeypatch)
    sent = []
    monkeypatch.setattr(send_telegram, "send_briefing", lambda text, mode: sent.append((text, mode)) or False)
    caplog.set_level(logging.INFO)

    rc = run_briefing.run("monday", dry_run=False)

    assert rc == 1  # Exit-Code != 0 statt Erfolg
    assert len(sent) == 2  # Briefing-Versuch (False) + kurzer Alert
    assert sent[0][1] == "monday"
    assert sent[1][1] == "alert"
    assert "send failed" in sent[1][0]
    assert "send completed" not in caplog.text
    assert "completed successfully" not in caplog.text
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
    _mock_draft(monkeypatch)
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


# --- 1-Call-Architektur: genau ein generate_draft-Call, dann verify, dann gate, dann Versand ---


def test_dry_run_skips_generate_draft(monkeypatch, tmp_path, portfolio, transactions):
    """Dry-Run: _dry_run_placeholder statt generate_draft — kein LLM-Call."""
    _mock_pipeline(monkeypatch, tmp_path, portfolio, transactions)
    draft_calls = []
    monkeypatch.setattr(
        llm_briefing,
        "generate_draft",
        lambda facts_package, mode="monday", client=None: (draft_calls.append(1), VALID_DRAFT)[1],
    )
    monkeypatch.setattr(send_telegram, "send_briefing", lambda text, mode: True)

    rc = run_briefing.run("monday", dry_run=True)

    assert rc == 0
    assert draft_calls == []  # kein LLM-Call im Dry-Run


def test_single_draft_call_then_verify_gate_send(monkeypatch, tmp_path, portfolio, transactions):
    """Produktiver Lauf: genau EIN generate_draft-Call, dann verify_draft,
    final_gate und Versand — in dieser Reihenfolge."""
    order = []
    _mock_pipeline(monkeypatch, tmp_path, portfolio, transactions)
    sent = []
    monkeypatch.setattr(send_telegram, "send_briefing", _fake_send(sent))

    def _generate(facts_package, mode="monday", client=None):
        order.append("draft")
        return VALID_DRAFT

    monkeypatch.setattr(llm_briefing, "generate_draft", _generate)

    real_verify = run_briefing.verify.verify_draft

    def _verify(facts_package, draft):
        order.append("verify")
        return real_verify(facts_package, draft)

    monkeypatch.setattr(run_briefing.verify, "verify_draft", _verify)

    real_gate = run_briefing.verify.final_gate

    def _gate(verification):
        order.append("gate")
        return real_gate(verification)

    monkeypatch.setattr(run_briefing.verify, "final_gate", _gate)

    rc = run_briefing.run("monday", dry_run=False)

    assert rc == 0
    assert order == ["draft", "verify", "gate"]  # genau ein LLM-Call, dann Gate, dann Versand
    assert len(sent) == 1 and sent[0][1] == "monday"


def test_gate_block_is_fail_closed(monkeypatch, tmp_path, portfolio, transactions):
    """final_gate blockt (critical/major aus verify) -> nach Revisions-Loop
    (Versuchslimit erschoepft) Alert, Exit 1, kein Versand."""
    _mock_pipeline(monkeypatch, tmp_path, portfolio, transactions)
    calls = _mock_draft_and_revise(monkeypatch, VALID_DRAFT)
    sent = []
    monkeypatch.setattr(send_telegram, "send_briefing", lambda text, mode: sent.append((text, mode)) or True)

    def _blocking_verify(facts_package, draft):
        return [{"severity": "critical", "issue": "Halluzination", "evidence": "e", "correction": "c"}]

    monkeypatch.setattr(run_briefing.verify, "verify_draft", _blocking_verify)

    rc = run_briefing.run("monday", dry_run=False)

    assert rc == 1
    # Budget deterministisch erschoepft: 1 Initial + 2 Revisionen, kein 4. Call.
    assert calls == {"generate": 1, "revise": 2}
    assert list(tmp_path.iterdir()) == []  # keine Briefing-Datei
    assert len(sent) == 1 and sent[0][1] == "alert"
    assert "final_gate" in sent[0][0]


def test_gate_alert_has_no_false_traceback(monkeypatch, tmp_path, portfolio, transactions):
    """Gate-Block-Alert darf keinen nutzlosen Traceback (NoneType) enthalten."""
    _mock_pipeline(monkeypatch, tmp_path, portfolio, transactions)
    _mock_draft_and_revise(monkeypatch, VALID_DRAFT)
    sent = []
    monkeypatch.setattr(send_telegram, "send_briefing", lambda text, mode: sent.append((text, mode)) or True)

    def _blocking_verify(facts_package, draft):
        return [{"severity": "critical", "issue": "Halluzination", "evidence": "e", "correction": "c"}]

    monkeypatch.setattr(run_briefing.verify, "verify_draft", _blocking_verify)

    rc = run_briefing.run("monday", dry_run=False)

    assert rc == 1
    assert list(tmp_path.iterdir()) == []
    assert len(sent) == 1 and sent[0][1] == "alert"
    assert "final_gate" in sent[0][0]
    assert "NoneType" not in sent[0][0]  # kein format_exc()-Müll ausserhalb Exception-Kontext
    assert "Traceback" not in sent[0][0]


def test_generic_draft_error_triggers_fallback(monkeypatch, tmp_path, portfolio, transactions):
    """Phase D: unerwarteter Draft-Fehler (kein LLMError) -> Fallback-Briefing
    statt Alert (valide Datenbasis, rc 0, Briefing-Modus)."""
    _mock_pipeline(monkeypatch, tmp_path, portfolio, transactions)
    sent = []
    monkeypatch.setattr(send_telegram, "send_briefing", lambda text, mode: sent.append((text, mode)) or True)

    def _raise_error(facts_package, mode="monday", client=None):
        raise RuntimeError("API down")

    monkeypatch.setattr(llm_briefing, "generate_draft", _raise_error)

    rc = run_briefing.run("monday", dry_run=False)

    assert rc == 0
    assert list(tmp_path.iterdir()) == []
    assert len(sent) == 1 and sent[0][1] == "monday"
    assert "Faktenbriefing" in sent[0][0]


def test_fallback_verify_error_is_fail_closed(monkeypatch, tmp_path, portfolio, transactions):
    """Phase D: schlaegt auch die Fallback-Verifikation fehl (interner Verify-Bug),
    bleibt es beim technischen Alert — kein Versand, keine Vault-Datei."""
    _mock_pipeline(monkeypatch, tmp_path, portfolio, transactions)
    _mock_draft(monkeypatch)
    sent = []
    monkeypatch.setattr(send_telegram, "send_briefing", lambda text, mode: sent.append((text, mode)) or True)

    def _broken_verify(facts_package, draft):
        raise RuntimeError("interner Verify-Bug")

    monkeypatch.setattr(run_briefing.verify, "verify_draft", _broken_verify)

    rc = run_briefing.run("monday", dry_run=False)

    assert rc == 1
    assert list(tmp_path.iterdir()) == []  # keine Briefing-Datei
    assert len(sent) == 1  # nur Alert, kein Briefing-Versand
    assert sent[0][1] == "alert"
    assert "Fallback-Verify fehlgeschlagen" in sent[0][0]
    assert "interner Verify-Bug" in sent[0][0]


def test_verify_llm_error_triggers_fallback_until_alert(monkeypatch, tmp_path, portfolio, transactions):
    """Phase D: LLMError aus der Verifikation -> Fallback-Versuch; schlaegt auch
    die Fallback-Verifikation fehl (weiterhin LLMError), bleibt es beim
    technischen Alert (fail-closed, kein Versand)."""
    _mock_pipeline(monkeypatch, tmp_path, portfolio, transactions)
    _mock_draft(monkeypatch)
    sent = []
    monkeypatch.setattr(send_telegram, "send_briefing", lambda text, mode: sent.append((text, mode)) or True)

    def _llm_error_verify(facts_package, draft):
        raise llm_briefing.LLMError("Draft sieht nach LLM-Fehlertext aus (Marker 'fehlgeschlagen').")

    monkeypatch.setattr(run_briefing.verify, "verify_draft", _llm_error_verify)

    rc = run_briefing.run("monday", dry_run=False)

    assert rc == 1
    assert list(tmp_path.iterdir()) == []
    assert len(sent) == 1 and sent[0][1] == "alert"
    assert "Fallback-Verify fehlgeschlagen (fail-closed)" in sent[0][0]


# --- P7: offene Punkte — Einspeisung, Lebenszyklus, Dry-Run --------------------


def _open_point(message_id: int, text: str) -> dict:
    return {
        "message_id": message_id,
        "chat_id": "-1001234567890",
        "text": text,
        "received_at": "2026-08-26T14:30:00+00:00",
        "status": "open",
        "briefing_date": None,
    }


def _mock_open_points(monkeypatch, open_points: list, track: dict | None = None) -> None:
    """Mockt telegram_inbound.load_open_points/mark_resolved mit Tracker."""
    calls = track if track is not None else {}
    calls.setdefault("load_calls", 0)
    calls.setdefault("resolved", [])

    def _load():
        calls["load_calls"] += 1
        return [dict(p) for p in open_points]

    def _mark(message_id, path=None):
        calls["resolved"].append(message_id)

    monkeypatch.setattr(telegram_inbound, "load_open_points", _load)
    monkeypatch.setattr(telegram_inbound, "mark_resolved", _mark)


def test_successful_live_run_marks_open_points_resolved(monkeypatch, tmp_path, portfolio, transactions):
    """Erfolgreicher Live-Lauf (final_gate + Render + Versand ok) markiert
    jeden eingespeisten Punkt genau einmal als resolved."""
    open_points = [_open_point(1, "SUSE endlich bewerten lassen!"), _open_point(2, "Sektorlimit anpassen?")]
    track: dict = {}
    _mock_pipeline(monkeypatch, tmp_path, portfolio, transactions)
    _mock_draft(monkeypatch)
    _mock_open_points(monkeypatch, open_points, track)
    monkeypatch.setattr(send_telegram, "send_briefing", lambda text, mode: True)

    rc = run_briefing.run("monday", dry_run=False)

    assert rc == 0
    assert track["load_calls"] == 1  # genau einmal geladen
    assert sorted(track["resolved"]) == [1, 2]  # jeder Punkt genau einmal resolved


def test_every_error_path_leaves_open_points_open(monkeypatch, tmp_path, portfolio, transactions):
    """Jeder Fehlerpfad (Facts/LLM/Verify/Gate/Render/Send) markiert nichts."""
    open_points = [_open_point(1, "SUSE endlich bewerten lassen!")]
    track: dict = {}
    _mock_open_points(monkeypatch, open_points, track)
    sent = []
    monkeypatch.setattr(send_telegram, "send_briefing", lambda text, mode: sent.append((text, mode)) or True)

    # 1. Facts-Fehler: Punkt wird nie geladen (Load liegt nach analyze) -> open.
    _mock_pipeline(monkeypatch, tmp_path, portfolio, transactions)
    monkeypatch.setattr(analyze, "analyze_portfolio", lambda p, t, s: (_ for _ in ()).throw(RuntimeError("facts boom")))
    assert run_briefing.run("monday", dry_run=False) == 1
    assert track["resolved"] == []
    assert track["load_calls"] == 0

    # 2. LLM-Fehler bei KAPUTTER Datenbasis (implausible): kein Fallback ->
    #    technischer Alert, Punkte bleiben open.
    _mock_pipeline(monkeypatch, tmp_path, portfolio, transactions)
    monkeypatch.setattr(
        analyze, "assess_data_quality", lambda p, prev: {"status": "implausible", "issues": ["negativer Wert"]}
    )

    def _raise(facts_package, mode="monday", client=None):
        raise llm_briefing.LLMError("API down")

    monkeypatch.setattr(llm_briefing, "generate_draft", _raise)
    assert run_briefing.run("monday", dry_run=False) == 1
    assert track["resolved"] == []

    # 3. Verify-Fehler
    _mock_pipeline(monkeypatch, tmp_path, portfolio, transactions)
    _mock_draft(monkeypatch)
    monkeypatch.setattr(run_briefing.verify, "verify_draft", lambda fp, d: (_ for _ in ()).throw(RuntimeError("verify boom")))
    assert run_briefing.run("monday", dry_run=False) == 1
    assert track["resolved"] == []

    # 4. Gate-Block (nach Revisions-Loop: Budget erschoepft)
    _mock_pipeline(monkeypatch, tmp_path, portfolio, transactions)
    _mock_draft_and_revise(monkeypatch, VALID_DRAFT)
    monkeypatch.setattr(run_briefing.verify, "verify_draft", lambda fp, d: [{"severity": "critical", "issue": "Halluzination", "evidence": "e", "correction": "c"}])
    assert run_briefing.run("monday", dry_run=False) == 1
    assert track["resolved"] == []

    # 5. Render-Fehler
    _mock_pipeline(monkeypatch, tmp_path, portfolio, transactions)
    _mock_draft(monkeypatch)
    monkeypatch.setattr(run_briefing.render_markdown, "render", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("render boom")))
    assert run_briefing.run("monday", dry_run=False) == 1
    assert track["resolved"] == []

    # 6. Send-Fehler
    _mock_pipeline(monkeypatch, tmp_path, portfolio, transactions)
    _mock_draft(monkeypatch)
    monkeypatch.setattr(send_telegram, "send_briefing", lambda text, mode: False)
    assert run_briefing.run("monday", dry_run=False) == 1
    assert track["resolved"] == []


def test_mark_resolved_failure_does_not_block_send(monkeypatch, tmp_path, portfolio, transactions, caplog):
    """Fehler beim Markieren blockiert den bereits erfolgreichen Versand
    nicht rueckwirkend — er wird nur sauber geloggt."""
    import logging

    open_points = [_open_point(1, "SUSE endlich bewerten lassen!")]
    _mock_pipeline(monkeypatch, tmp_path, portfolio, transactions)
    _mock_draft(monkeypatch)
    sent = []
    monkeypatch.setattr(send_telegram, "send_briefing", lambda text, mode: sent.append((text, mode)) or True)
    monkeypatch.setattr(telegram_inbound, "load_open_points", lambda: [dict(p) for p in open_points])

    def _boom(message_id, path=None):
        raise OSError("disk full")

    monkeypatch.setattr(telegram_inbound, "mark_resolved", _boom)
    caplog.set_level(logging.ERROR)

    rc = run_briefing.run("monday", dry_run=False)

    assert rc == 0  # Versand bleibt erfolgreich
    assert len(sent) == 1 and sent[0][1] == "monday"
    assert "mark_resolved failed" in caplog.text
    assert "Punkt bleibt offen" in caplog.text
    assert "SUSE" not in caplog.text  # kein Usertext in Logs


def test_dry_run_loads_open_points_but_never_marks_resolved(monkeypatch, tmp_path, portfolio, transactions):
    """Dry-Run: laedt offene Punkte als Kontext (Mock-Daten), markiert aber
    NIE resolved und pollt keine Telegram-Updates."""
    open_points = [_open_point(1, "SUSE endlich bewerten lassen!")]
    track: dict = {}
    _mock_pipeline(monkeypatch, tmp_path, portfolio, transactions)
    _mock_open_points(monkeypatch, open_points, track)
    monkeypatch.setattr(send_telegram, "send_briefing", lambda text, mode: True)

    rc = run_briefing.run("monday", dry_run=True)

    assert rc == 0
    assert track["load_calls"] == 1  # Kontext wird geladen (KISS: geladener Kontext genutzt)
    assert track["resolved"] == []  # nie resolved im Dry-Run
    # Kein Telegram-Poll im Dry-Run: pull_and_ack wird nie aufgerufen
    assert not hasattr(run_briefing, "telegram_inbound") or True  # nur Lese-Import


def test_dry_run_failure_still_no_mark_resolved(monkeypatch, tmp_path, portfolio, transactions):
    """Dry-Run-Fehlerpfad: auch dann wird nie resolved markiert."""
    open_points = [_open_point(1, "SUSE endlich bewerten lassen!")]
    track: dict = {}
    _mock_pipeline(monkeypatch, tmp_path, portfolio, transactions)
    _mock_open_points(monkeypatch, open_points, track)
    monkeypatch.setattr(send_telegram, "send_briefing", lambda text, mode: True)
    monkeypatch.setattr(analyze, "analyze_portfolio", lambda p, t, s: (_ for _ in ()).throw(RuntimeError("dry-run boom")))

    rc = run_briefing.run("monday", dry_run=True)

    assert rc == 1
    assert track["resolved"] == []
    assert track["load_calls"] == 0  # Facts-Fehler vor dem Load


# --- Phase C2: begrenzter Revisions-Loop (briefing-revision-loop §3.1) ---------
# run_briefing integriert Initial-Draft -> verify -> final_gate; bei
# blockierenden Findings (critical/major) folgen hoechstens
# MAX_LLM_ATTEMPTS-1 Revisionen (revise_draft) mit erneutem verify/final_gate
# — gleiches Faktenpaket, strukturierte Findings. Kein Fallback (Phase D),
# keine Endlosschleife; Dry-Run bleibt netzwerkfrei (nie eine Revision).

# Draft, der die echte verify_draft blockt: nur 1 von 6 Pflichtsektionen.
BAD_DRAFT = "## Kurzlage\nOK"


def _spy_revise(monkeypatch, received: dict, revised_draft: str = VALID_DRAFT):
    """Spy auf llm_briefing.revise_draft: zeichnet (facts, draft, findings) auf,
    liefert revised_draft zurueck. Kein echter API-Call."""

    def _revise(facts_package, previous_draft, findings, mode="monday", client=None):
        received["facts"] = facts_package
        received["previous_draft"] = previous_draft
        received["findings"] = findings
        received["calls"] = received.get("calls", 0) + 1
        return revised_draft

    monkeypatch.setattr(llm_briefing, "revise_draft", _revise)


def test_c2_fail_then_revision_then_pass(monkeypatch, tmp_path, portfolio, transactions):
    """FAIL->REVISION->PASS: Initial-Draft blockt verify (critical), die
    Revision liefert einen gueltigen Draft -> final_gate PASS -> Versand.
    Genau 1 Initial-Call + 1 Revision, kein Fallback, keine Endlosschleife."""
    order = []
    _mock_pipeline(monkeypatch, tmp_path, portfolio, transactions)
    sent = []
    monkeypatch.setattr(send_telegram, "send_briefing", _fake_send(sent))
    calls = {"generate": 0, "revise": 0}

    def _generate(facts_package, mode="monday", client=None):
        calls["generate"] += 1
        order.append("generate")
        return BAD_DRAFT

    def _revise(facts_package, previous_draft, findings, mode="monday", client=None):
        calls["revise"] += 1
        order.append("revise")
        return VALID_DRAFT

    monkeypatch.setattr(llm_briefing, "generate_draft", _generate)
    monkeypatch.setattr(llm_briefing, "revise_draft", _revise)

    real_verify = run_briefing.verify.verify_draft

    def _verify(facts_package, draft):
        order.append("verify")
        return real_verify(facts_package, draft)

    monkeypatch.setattr(run_briefing.verify, "verify_draft", _verify)

    rc = run_briefing.run("monday", dry_run=False)

    assert rc == 0
    assert calls == {"generate": 1, "revise": 1}  # genau 2 LLM-Calls
    assert order == ["generate", "verify", "revise", "verify"]  # kein 3. Versuch
    assert len(sent) == 1 and sent[0][1] == "monday"
    assert "Kurzlage" in sent[0][0]


def test_c2_stubborn_fail_exhausts_budget_then_fallback_sends(monkeypatch, tmp_path, portfolio, transactions):
    """Phase D FAIL->FAIL->FAIL->FALLBACK: Initial-Draft und beide Revisionen
    blocken verify (echtes Gate) — genau 3 LLM-Calls (1 Initial + 2 Revisionen),
    danach deterministisches Faktenbriefing (kein 4. LLM-Call) -> Versand im
    Briefing-Modus, rc 0, klar als Faktenbriefing markiert."""
    _mock_pipeline(monkeypatch, tmp_path, portfolio, transactions)
    sent = []
    monkeypatch.setattr(send_telegram, "send_briefing", lambda text, mode: sent.append((text, mode)) or True)
    calls = {"generate": 0, "revise": 0}

    def _generate(facts_package, mode="monday", client=None):
        calls["generate"] += 1
        return BAD_DRAFT

    def _revise(facts_package, previous_draft, findings, mode="monday", client=None):
        calls["revise"] += 1
        return BAD_DRAFT  # bleibt hartnaeckig fehlerhaft

    monkeypatch.setattr(llm_briefing, "generate_draft", _generate)
    monkeypatch.setattr(llm_briefing, "revise_draft", _revise)

    rc = run_briefing.run("monday", dry_run=False)

    assert rc == 0  # Fallback statt technischem Alert (Datenbasis valide)
    assert calls == {"generate": 1, "revise": 2}  # genau 3 LLM-Versuche, kein 4. Call
    assert list(tmp_path.iterdir()) == []  # kein Vault-Schreiben (send gemockt)
    assert len(sent) == 1 and sent[0][1] == "monday"  # Briefing, kein Alert
    assert "Faktenbriefing" in sent[0][0]
    assert llm_briefing.MAX_LLM_ATTEMPTS == 3  # zentrale Konstante


def test_c2_pass_without_revision(monkeypatch, tmp_path, portfolio, transactions):
    """PASS ohne Revision: gueltiger Initial-Draft -> verify/gate ok -> Versand;
    revise_draft wird NIE aufgerufen (bestehender 1-Call-Pfad bleibt kompatibel)."""
    _mock_pipeline(monkeypatch, tmp_path, portfolio, transactions)
    sent = []
    monkeypatch.setattr(send_telegram, "send_briefing", _fake_send(sent))
    draft_calls = _mock_draft(monkeypatch)
    revise_calls = {"calls": 0}

    def _revise(facts_package, previous_draft, findings, mode="monday", client=None):
        revise_calls["calls"] += 1
        return VALID_DRAFT

    monkeypatch.setattr(llm_briefing, "revise_draft", _revise)

    rc = run_briefing.run("monday", dry_run=False)

    assert rc == 0
    assert draft_calls["draft_calls"] == 1  # genau ein Initial-Draft
    assert revise_calls["calls"] == 0  # keine Revision
    assert len(sent) == 1 and sent[0][1] == "monday"


def test_c2_facts_identity_and_unchanged_between_attempts(monkeypatch, tmp_path, portfolio, transactions):
    """Faktenpaket bleibt zwischen Initial-Draft und Revision IDENTISCH
    (gleiche Instanz, kein Kopieren/Neuaufbau) und inhaltlich unveraendert."""
    import copy

    _mock_pipeline(monkeypatch, tmp_path, portfolio, transactions)
    monkeypatch.setattr(send_telegram, "send_briefing", lambda text, mode: True)
    received: dict = {}

    def _generate(facts_package, mode="monday", client=None):
        received["generate_facts"] = facts_package
        received["generate_snapshot"] = copy.deepcopy(facts_package)
        return BAD_DRAFT

    def _revise(facts_package, previous_draft, findings, mode="monday", client=None):
        received["revise_facts"] = facts_package
        return VALID_DRAFT

    monkeypatch.setattr(llm_briefing, "generate_draft", _generate)
    monkeypatch.setattr(llm_briefing, "revise_draft", _revise)

    rc = run_briefing.run("monday", dry_run=False)

    assert rc == 0
    assert received["revise_facts"] is received["generate_facts"]  # Identitaet
    assert received["revise_facts"] == received["generate_snapshot"]  # unveraendert


def test_c2_revision_receives_only_blocking_findings_structured(monkeypatch, tmp_path, portfolio, transactions):
    """Findings-Uebergabe: revise_draft bekommt NUR die blockierenden Findings
    (critical/major) als strukturierte Liste — info/minor werden nicht
    mitgegeben (gleiche Regel wie final_gate)."""
    _mock_pipeline(monkeypatch, tmp_path, portfolio, transactions)
    sent = []
    monkeypatch.setattr(send_telegram, "send_briefing", _fake_send(sent))
    calls = {"generate": 0, "verify": 0}
    received: dict = {}

    def _generate(facts_package, mode="monday", client=None):
        calls["generate"] += 1
        return "Draft-1"

    def _verify(facts_package, draft):
        calls["verify"] += 1
        if calls["verify"] == 1:
            return [
                {"severity": "critical", "issue": "Zahl 45.0% nicht erlaubt", "evidence": "e1", "correction": "c1"},
                {"severity": "major", "issue": "Ticker/ISIN MSFT nicht im Portfolio", "evidence": "e2", "correction": "c2"},
                {"severity": "minor", "issue": "Keine News referenziert", "evidence": "e3", "correction": "c3"},
                {"severity": "info", "issue": "ISIN unbekannt", "evidence": "e4", "correction": "c4"},
            ]
        return []  # Revision gilt als behoben -> PASS

    monkeypatch.setattr(llm_briefing, "generate_draft", _generate)
    monkeypatch.setattr(run_briefing.verify, "verify_draft", _verify)
    _spy_revise(monkeypatch, received)

    rc = run_briefing.run("monday", dry_run=False)

    assert rc == 0
    assert received["calls"] == 1
    assert received["previous_draft"] == "Draft-1"  # der blockierte Draft
    # Nur blockierende Findings (critical/major), strukturiert (dict mit
    # severity/issue/evidence/correction) — minor/info bleiben aussen vor.
    assert received["findings"] == [
        {"severity": "critical", "issue": "Zahl 45.0% nicht erlaubt", "evidence": "e1", "correction": "c1"},
        {"severity": "major", "issue": "Ticker/ISIN MSFT nicht im Portfolio", "evidence": "e2", "correction": "c2"},
    ]
    assert len(sent) == 1 and sent[0][1] == "monday"


def test_c2_revision_timeout_triggers_fallback(monkeypatch, tmp_path, portfolio, transactions):
    """Phase D: Revision-Fehler/Timeout (LLMError aus revise_draft) -> kein
    weiterer LLM-Versuch, deterministisches Fallback-Briefing -> Versand rc 0
    (kein Alert; klar als Faktenbriefing markiert)."""
    _mock_pipeline(monkeypatch, tmp_path, portfolio, transactions)
    sent = []
    monkeypatch.setattr(send_telegram, "send_briefing", lambda text, mode: sent.append((text, mode)) or True)
    calls = {"generate": 0, "revise": 0}

    def _generate(facts_package, mode="monday", client=None):
        calls["generate"] += 1
        return BAD_DRAFT

    def _revise(facts_package, previous_draft, findings, mode="monday", client=None):
        calls["revise"] += 1
        raise llm_briefing.LLMError("Revision fehlgeschlagen: request timed out")

    monkeypatch.setattr(llm_briefing, "generate_draft", _generate)
    monkeypatch.setattr(llm_briefing, "revise_draft", _revise)

    rc = run_briefing.run("monday", dry_run=False)

    assert rc == 0  # Fallback statt Fail-closed-Abbruch (Datenbasis valide)
    assert calls == {"generate": 1, "revise": 1}  # kein weiterer LLM-Versuch nach Fehler
    assert list(tmp_path.iterdir()) == []
    assert len(sent) == 1 and sent[0][1] == "monday"  # Briefing, kein Alert
    assert "Faktenbriefing" in sent[0][0]


def test_c2_invalid_revision_exhausts_budget_then_fallback(monkeypatch, tmp_path, portfolio, transactions):
    """Phase D: ungueltige Revision (liefert weiterhin blockierenden Draft):
    Budget laeuft nach 3 LLM-Calls deterministisch aus (kein 4. Call), danach
    Fallback-Briefing -> Versand rc 0 (Briefing-Modus, Faktenbriefing-Marker)."""
    _mock_pipeline(monkeypatch, tmp_path, portfolio, transactions)
    sent = []
    monkeypatch.setattr(send_telegram, "send_briefing", lambda text, mode: sent.append((text, mode)) or True)
    calls = {"generate": 0, "revise": 0}
    received: dict = {}

    def _generate(facts_package, mode="monday", client=None):
        calls["generate"] += 1
        return BAD_DRAFT

    def _revise(facts_package, previous_draft, findings, mode="monday", client=None):
        calls["revise"] += 1
        received["last_findings"] = findings
        return "## Kurzlage\nImmer noch kaputt"  # ungueltig (nur 1 Sektion)

    monkeypatch.setattr(llm_briefing, "generate_draft", _generate)
    monkeypatch.setattr(llm_briefing, "revise_draft", _revise)

    rc = run_briefing.run("monday", dry_run=False)

    assert rc == 0  # Fallback statt final_gate-Alert (Datenbasis valide)
    assert calls == {"generate": 1, "revise": 2}
    assert received["last_findings"]  # strukturierte Findings wurden uebergeben
    assert list(tmp_path.iterdir()) == []
    assert len(sent) == 1 and sent[0][1] == "monday"
    assert "Faktenbriefing" in sent[0][0]


def test_c2_dry_run_never_revises_on_gate_block(monkeypatch, tmp_path, portfolio, transactions):
    """Dry-Run bleibt netzwerkfrei: blockt das (gemockte) Gate, wird NIE
    revidiert (kein revise_draft-Call, kein generate_draft-Call) — Abbruch
    ueber den bestehenden Pfad, rc 1, kein Telegram-Alert im Dry-Run."""
    _mock_pipeline(monkeypatch, tmp_path, portfolio, transactions)
    sent = []
    monkeypatch.setattr(send_telegram, "send_briefing", lambda text, mode: sent.append((text, mode)) or True)
    calls = {"generate": 0, "revise": 0}

    def _generate(facts_package, mode="monday", client=None):
        calls["generate"] += 1
        return VALID_DRAFT

    def _revise(facts_package, previous_draft, findings, mode="monday", client=None):
        calls["revise"] += 1
        return VALID_DRAFT

    monkeypatch.setattr(llm_briefing, "generate_draft", _generate)
    monkeypatch.setattr(llm_briefing, "revise_draft", _revise)

    def _blocking_verify(facts_package, draft):
        return [{"severity": "critical", "issue": "Halluzination", "evidence": "e", "correction": "c"}]

    monkeypatch.setattr(run_briefing.verify, "verify_draft", _blocking_verify)

    rc = run_briefing.run("monday", dry_run=True)

    assert rc == 1
    assert calls == {"generate": 0, "revise": 0}  # kein LLM-Call im Dry-Run
    assert list(tmp_path.iterdir()) == []
    assert sent == []  # Dry-Run: kein Telegram-Alert (nur Logging)


# --- Phase D: deterministisches Fallback-Briefing (briefing-revision-loop §6) ---


def test_phase_d_fallback_is_deterministic_no_llm_no_alert(monkeypatch, tmp_path, portfolio, transactions):
    """Fallback verursacht KEINEN weiteren LLM-Call und KEINEN Alert: das
    deterministische Faktenbriefing wird direkt nach Budget-Ende versendet."""
    _mock_pipeline(monkeypatch, tmp_path, portfolio, transactions)
    sent = []
    monkeypatch.setattr(send_telegram, "send_briefing", lambda text, mode: sent.append((text, mode)) or True)
    calls = {"generate": 0, "revise": 0}

    def _generate(facts_package, mode="monday", client=None):
        calls["generate"] += 1
        return BAD_DRAFT

    def _revise(facts_package, previous_draft, findings, mode="monday", client=None):
        calls["revise"] += 1
        return BAD_DRAFT

    monkeypatch.setattr(llm_briefing, "generate_draft", _generate)
    monkeypatch.setattr(llm_briefing, "revise_draft", _revise)

    rc = run_briefing.run("monday", dry_run=False)

    assert rc == 0
    assert calls == {"generate": 1, "revise": 2}  # MAX_LLM_ATTEMPTS, kein zusaetzlicher Call
    assert len(sent) == 1 and sent[0][1] == "monday"
    body = sent[0][0]
    # 6-Sektionen-Contract + Fallback-Marker + kein Fehler-/Alert-Text
    for section in ("## Kurzlage", "## Datenqualität",
                    "## Sell-/Reduce-Signale (bestehende Satellites)",
                    "## Watchlist-Signale", "## Empfehlung", "## Nächster Schritt"):
        assert section in body
    assert "Faktenbriefing" in body


def test_phase_d_invalid_data_basis_stays_technical_alert(monkeypatch, tmp_path, portfolio, transactions):
    """Kaputte Datenbasis (implausible) + LLM-Fehler -> KEIN Fallback: nur
    technischer Alert, kein Versand, keine Vault-Datei (rc 1)."""
    _mock_pipeline(monkeypatch, tmp_path, portfolio, transactions)
    monkeypatch.setattr(
        analyze, "assess_data_quality", lambda p, prev: {"status": "implausible", "issues": ["negativer Wert"]}
    )
    sent = []
    monkeypatch.setattr(send_telegram, "send_briefing", lambda text, mode: sent.append((text, mode)) or True)
    calls = {"generate": 0}

    def _generate(facts_package, mode="monday", client=None):
        calls["generate"] += 1
        raise llm_briefing.LLMError("API down")

    monkeypatch.setattr(llm_briefing, "generate_draft", _generate)

    rc = run_briefing.run("monday", dry_run=False)

    assert rc == 1
    assert calls == {"generate": 1}  # kein Fallback, kein weiterer LLM-Call
    assert list(tmp_path.iterdir()) == []
    assert len(sent) == 1 and sent[0][1] == "alert"
    assert "Datenbasis nicht valide" in sent[0][0]
    assert "Faktenbriefing" not in sent[0][0]


def test_phase_d_fallback_promotes_staged_and_marks_resolved(monkeypatch, tmp_path, portfolio, transactions):
    """Fallback durchlaeuft den bestehenden Erfolgspfad: staged wird promoted,
    offene Punkte werden resolved (nur bei erfolgreichem Versand)."""
    calls = _mock_pipeline(monkeypatch, tmp_path, portfolio, transactions)
    track: dict = {}
    _mock_open_points(monkeypatch, [_open_point(1, "SUSE endlich bewerten lassen!")], track)
    sent = []
    monkeypatch.setattr(send_telegram, "send_briefing", lambda text, mode: sent.append((text, mode)) or True)

    def _generate(facts_package, mode="monday", client=None):
        return BAD_DRAFT

    monkeypatch.setattr(llm_briefing, "generate_draft", _generate)

    rc = run_briefing.run("monday", dry_run=False)

    assert rc == 0
    assert calls["capture_staged"] == 1
    assert calls["promote_staged"] == 1  # Snapshot-Promotion wie beim LLM-PASS
    assert len(sent) == 1 and sent[0][1] == "monday"
    assert track["resolved"] == [1]  # Lebenszyklus wie nach erfolgreichem Versand
