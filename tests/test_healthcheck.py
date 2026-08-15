"""Tests fuer healthcheck: sc-Session-Status via `sc whoami --json`, handlungsorientierte Alerts.

Ausschliesslich Fake-Objekte, monkeypatch und tmp_path — keine echten
sc-/API-/Telegram-Aufrufe.
"""
from __future__ import annotations

import json
import subprocess

import pytest

from scripts import healthcheck, send_telegram, snapshot


def _run_ok(stdout: str):
    def _run(cmd, *args, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")

    return _run


def _run_nonzero(stderr: str = ""):
    def _run(cmd, *args, **kwargs):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr=stderr)

    return _run


def _run_timeout():
    def _run(cmd, *args, **kwargs):
        raise subprocess.TimeoutExpired(cmd, timeout=healthcheck.SC_WHOAMI_TIMEOUT_S)

    return _run


@pytest.fixture
def clean_base(monkeypatch, tmp_path):
    """Basis ohne rote Baseline-Warnings: frisches Briefing + Live-Snapshot vorhanden."""
    monkeypatch.setattr(healthcheck, "VAULT_DIR", tmp_path)
    (tmp_path / "2026-08-15-monday.md").write_text("# Briefing", encoding="utf-8")
    monkeypatch.setattr(
        snapshot,
        "read_snapshot",
        lambda path: {"captured_at": "2026-08-15T06:00:00+00:00"},
    )


# --- _sc_session_status: differenzierte Status-Klassifikation ---


def test_sc_session_ok(monkeypatch):
    """`sc whoami --json` liefert gueltige JSON mit User-Info -> "ok"."""
    monkeypatch.setattr(healthcheck, "_sc_available", lambda: True)
    whoami = json.dumps({"ok": True, "command": "sc whoami --json", "data": {"id": "u1", "email": "user@example.com"}})
    monkeypatch.setattr(healthcheck.subprocess, "run", _run_ok(whoami))

    status, detail = healthcheck._sc_session_status()

    assert status == "ok"
    assert "user@example.com" not in detail  # keine User-Daten im Detail


def test_sc_session_no_session(monkeypatch):
    """sc-Ausgabe enthaelt "no_session" -> "no_session" mit `sc login`-Aktion."""
    monkeypatch.setattr(healthcheck, "_sc_available", lambda: True)
    out = json.dumps({"ok": False, "error": {"code": "no_session", "message": "No active session. Run 'sc login'."}})
    monkeypatch.setattr(healthcheck.subprocess, "run", _run_nonzero(stderr=out))

    status, detail = healthcheck._sc_session_status()

    assert status == "no_session"
    assert "sc login" in detail


def test_sc_session_relogin_required(monkeypatch):
    """sc-Ausgabe enthaelt "REFRESH_RELOGIN_REQUIRED" -> "relogin_required"."""
    monkeypatch.setattr(healthcheck, "_sc_available", lambda: True)
    monkeypatch.setattr(
        healthcheck.subprocess,
        "run",
        _run_nonzero(stderr='{"error": {"code": "REFRESH_RELOGIN_REQUIRED"}}'),
    )

    status, detail = healthcheck._sc_session_status()

    assert status == "relogin_required"
    assert "sc login" in detail


def test_sc_session_secret_storage(monkeypatch):
    """sc-Ausgabe enthaelt "secret_storage_unavailable" -> "secret_storage_unavailable"."""
    monkeypatch.setattr(healthcheck, "_sc_available", lambda: True)
    monkeypatch.setattr(
        healthcheck.subprocess,
        "run",
        _run_nonzero(stderr='{"error": {"code": "secret_storage_unavailable"}}'),
    )

    status, detail = healthcheck._sc_session_status()

    assert status == "secret_storage_unavailable"
    assert "System" in detail


def test_sc_session_sc_missing(monkeypatch):
    """sc nicht im PATH -> "sc_missing" (bestehendes rotes Verhalten)."""
    monkeypatch.setattr(healthcheck, "_sc_available", lambda: False)

    status, detail = healthcheck._sc_session_status()

    assert status == "sc_missing"
    assert "PATH" in detail


def test_sc_session_error_on_unexpected_exit(monkeypatch):
    """Non-zero Exit ohne Auth-Marker -> "error" (diagnostisch), ohne Rohdaten."""
    monkeypatch.setattr(healthcheck, "_sc_available", lambda: True)
    monkeypatch.setattr(healthcheck.subprocess, "run", _run_nonzero(stderr="sc: internal boom"))

    status, detail = healthcheck._sc_session_status()

    assert status == "error"
    assert "boom" not in detail  # keine Rohdaten im Detail


def test_sc_session_error_on_invalid_json(monkeypatch):
    """Exit 0 + ungueltiges JSON -> "error"."""
    monkeypatch.setattr(healthcheck, "_sc_available", lambda: True)
    monkeypatch.setattr(healthcheck.subprocess, "run", _run_ok("not json"))

    status, _ = healthcheck._sc_session_status()

    assert status == "error"


def test_sc_session_error_on_timeout(monkeypatch):
    """Timeout -> "error", kein Haengen des Healthchecks."""
    monkeypatch.setattr(healthcheck, "_sc_available", lambda: True)
    monkeypatch.setattr(healthcheck.subprocess, "run", _run_timeout())

    status, detail = healthcheck._sc_session_status()

    assert status == "error"
    assert "timeout" in detail.lower()


# --- check_health: Status-Mapping (rot/gelb/grün) + handlungsorientierte Alerts ---


def _capture_alerts(monkeypatch):
    sent = []
    monkeypatch.setattr(send_telegram, "send_briefing", lambda text, mode: sent.append((text, mode)) or True)
    return sent


def test_check_health_red_on_no_session(monkeypatch, clean_base):
    sent = _capture_alerts(monkeypatch)
    monkeypatch.setattr(healthcheck, "_sc_available", lambda: True)
    monkeypatch.setattr(
        healthcheck.subprocess,
        "run",
        _run_nonzero(stderr='{"error": {"code": "no_session", "message": "No active session."}}'),
    )

    result = healthcheck.check_health()

    assert result["status"] == "red"
    assert result["sc_session_status"] == "no_session"
    assert result["sc_available"] is True
    assert len(sent) == 1 and sent[0][1] == "healthcheck"
    assert "`sc login` erforderlich" in sent[0][0]


def test_check_health_red_on_relogin_required(monkeypatch, clean_base):
    _capture_alerts(monkeypatch)
    monkeypatch.setattr(healthcheck, "_sc_available", lambda: True)
    monkeypatch.setattr(healthcheck.subprocess, "run", _run_nonzero(stderr="REFRESH_RELOGIN_REQUIRED"))

    result = healthcheck.check_health()

    assert result["status"] == "red"
    assert result["sc_session_status"] == "relogin_required"
    assert any("`sc login` erforderlich" in w for w in result["warnings"])


def test_check_health_red_on_secret_storage(monkeypatch, clean_base):
    _capture_alerts(monkeypatch)
    monkeypatch.setattr(healthcheck, "_sc_available", lambda: True)
    monkeypatch.setattr(healthcheck.subprocess, "run", _run_nonzero(stderr="secret_storage_unavailable"))

    result = healthcheck.check_health()

    assert result["status"] == "red"
    assert result["sc_session_status"] == "secret_storage_unavailable"
    assert any("Secret Storage" in w for w in result["warnings"])


def test_check_health_red_on_sc_missing(monkeypatch, clean_base):
    sent = _capture_alerts(monkeypatch)
    monkeypatch.setattr(healthcheck, "_sc_available", lambda: False)

    result = healthcheck.check_health()

    assert result["status"] == "red"
    assert result["sc_session_status"] == "sc_missing"
    assert result["sc_available"] is False
    assert len(sent) == 1


def test_check_health_yellow_on_unexpected_error(monkeypatch, clean_base):
    """Unerwarteter sc-Fehler (ohne Auth-Marker) -> gelber, diagnostischer Status."""
    _capture_alerts(monkeypatch)
    monkeypatch.setattr(healthcheck, "_sc_available", lambda: True)
    monkeypatch.setattr(healthcheck.subprocess, "run", _run_nonzero(stderr="internal boom"))

    result = healthcheck.check_health()

    assert result["status"] == "yellow"
    assert result["sc_session_status"] == "error"


def test_check_health_green_on_ok_session(monkeypatch, clean_base):
    """Alles ok (Briefing frisch, Snapshot da, Session aktiv) -> gruen, kein Alert."""
    sent = _capture_alerts(monkeypatch)
    monkeypatch.setattr(healthcheck, "_sc_available", lambda: True)
    monkeypatch.setattr(
        healthcheck.subprocess,
        "run",
        _run_ok(json.dumps({"ok": True, "command": "sc whoami --json", "data": {"id": "u1"}})),
    )

    result = healthcheck.check_health()

    assert result["status"] == "green"
    assert result["sc_session_status"] == "ok"
    assert sent == []  # kein Alert bei gesundem Zustand


def test_check_health_alert_contains_no_secrets(monkeypatch, clean_base):
    """Alert bei no_session enthaelt keine Session-/User-Daten (nur Status-Strings)."""
    sent = _capture_alerts(monkeypatch)
    monkeypatch.setattr(healthcheck, "_sc_available", lambda: True)
    whoami = json.dumps({"ok": False, "error": {"code": "no_session", "message": "No active session."}})
    monkeypatch.setattr(healthcheck.subprocess, "run", _run_nonzero(stderr=whoami))

    healthcheck.check_health()

    assert sent
    alert = sent[0][0]
    for forbidden in ("user@example.com", "token", "password", "session.json", "No active session"):
        assert forbidden not in alert
