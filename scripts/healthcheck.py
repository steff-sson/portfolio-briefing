"""Health checks: recent briefing, Live-Config-Snapshot und sc-Session-Status.

Der Zustand wird ueberwiegend aus lokalen Dateien bestimmt (Vault-Briefings,
config/snapshot.current.json). Einziger Live-Aufruf ist der leichte Auth-Probe
`sc whoami --json` (10s Timeout): er prueft die sc-Session (no_session /
REFRESH_RELOGIN_REQUIRED / secret_storage_unavailable) und haelt sie damit
aktiv (Idle-Timeout 24h). Kein Broker-Datenabruf, keine Secrets im Alert —
nur Status-Strings. Kein Mock-/Seed-Fallback — fehlende Live-Daten ergeben
einen roten Status.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

from scripts import send_telegram, snapshot

VAULT_DIR = Path.home() / "docs" / "notizen" / "portfolio-briefings"

# Lockerer Timeout: `sc whoami` darf den Healthcheck nicht haengen lassen.
SC_WHOAMI_TIMEOUT_S = 10


def _latest_briefing_age_hours() -> float | None:
    if not VAULT_DIR.exists():
        return None
    files = sorted(VAULT_DIR.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        return None
    mtime = datetime.fromtimestamp(files[0].stat().st_mtime)
    return (datetime.now() - mtime).total_seconds() / 3600


def _live_config_status() -> tuple[bool, str]:
    """Live-Config-Zustand ohne Live-Abfrage: Snapshot vorhanden + lesbar?

    Erstlauf/Seed-Migration steht aus, solange config/snapshot.current.json
    fehlt oder korrupt ist (roter Status, kein Mock-Fallback).
    """
    current = snapshot.read_snapshot(snapshot.CURRENT_PATH)
    if current is None:
        return False, "Kein Live-Snapshot (config/snapshot.current.json fehlt/korrupt) — Erstlauf/Seed-Migration noetig."
    return True, f"Live-Snapshot vorhanden (captured_at={current.get('captured_at')})"


def _sc_available() -> bool:
    return shutil.which("sc") is not None


def _sc_session_status() -> tuple[str, str]:
    """sc-Session-Status via `sc whoami --json` (de-facto Auth-Check, kein `sc health`).

    Rueckgabe: (status, detail) mit status ∈ {"ok", "no_session",
    "relogin_required", "secret_storage_unavailable", "sc_missing", "error"}.
    Detail-Strings enthalten ausschliesslich Status-Informationen — niemals
    Session-, Token- oder User-Daten.
    """
    if not _sc_available():
        return "sc_missing", "sc CLI nicht im PATH (Live-Abruf erst nach `sc login` moeglich)."
    try:
        result = subprocess.run(
            ["sc", "whoami", "--json"],
            capture_output=True,
            text=True,
            timeout=SC_WHOAMI_TIMEOUT_S,
            check=False,  # returncode wird unten explizit ausgewertet
        )
    except subprocess.TimeoutExpired:
        return "error", f"`sc whoami --json` timeout ({SC_WHOAMI_TIMEOUT_S}s) — sc CLI reagiert nicht."
    except OSError as e:
        return "error", f"`sc whoami --json` konnte nicht ausgefuehrt werden: {e}"

    output = f"{result.stdout}\n{result.stderr}"
    # Fehlerklassen der CLI (Quelle: github.com/ScalableCapital/scalable-cli):
    # no_session / REFRESH_RELOGIN_REQUIRED / secret_storage_unavailable.
    if "REFRESH_RELOGIN_REQUIRED" in output:
        return "relogin_required", "sc-Session erfordert Re-Login (REFRESH_RELOGIN_REQUIRED) — interaktives `sc login` erforderlich."
    if "no_session" in output:
        return "no_session", "sc-Session abgelaufen (no_session) — interaktives `sc login` erforderlich."
    if "secret_storage_unavailable" in output:
        return "secret_storage_unavailable", "sc Secret Storage nicht verfuegbar (secret_storage_unavailable) — System-/Keyring-Pruefung erforderlich."
    if result.returncode != 0:
        return "error", f"`sc whoami --json` unerwarteter Fehler (exit code {result.returncode})."
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return "error", "`sc whoami --json` lieferte kein gueltiges JSON."
    if isinstance(data, dict) and data.get("ok") is False:
        return "error", "`sc whoami --json` meldete ok=false ohne bekannten Auth-Marker."
    return "ok", "sc-Session aktiv."


def check_health() -> dict:
    age = _latest_briefing_age_hours()
    live_ok, live_note = _live_config_status()
    sc_status, sc_detail = _sc_session_status()
    age_ok = age is not None and age < 48
    red: list[str] = []
    yellow: list[str] = []
    if age is None:
        red.append("Kein Briefing im Vault gefunden.")
    elif not age_ok:
        red.append(f"Letztes Briefing ist {age:.1f}h alt.")
    if not live_ok:
        red.append(live_note)
    if sc_status == "ok":
        pass  # Session aktiv, kein Hinweis noetig
    elif sc_status in {"no_session", "relogin_required"}:
        # Handlungsorientiert: die Aktion (`sc login`) ist Teil des Alerts.
        red.append("sc-Session abgelaufen — `sc login` erforderlich. " + sc_detail)
    elif sc_status == "secret_storage_unavailable":
        red.append("sc Secret Storage nicht verfuegbar — System-Pruefung erforderlich. " + sc_detail)
    elif sc_status == "sc_missing":
        red.append(sc_detail)
    else:  # "error": gelb, unerwarteter Fehler (diagnostisch)
        yellow.append(sc_detail)

    if red or yellow:
        alert = "Healthcheck portfolio-briefing:\n" + "\n".join(f"- {w}" for w in red + yellow)
        try:
            send_telegram.send_briefing(alert, "healthcheck")
        except Exception:
            pass

    if red:
        status = "red"
    elif yellow:
        status = "yellow"
    else:
        status = "green"

    return {
        "status": status,
        "latest_briefing_age_hours": age,
        "live_snapshot_ok": live_ok,
        "sc_available": sc_status != "sc_missing",
        "sc_session_status": sc_status,
        "warnings": red + yellow,
    }


if __name__ == "__main__":
    print(check_health())
