"""Health checks: recent briefing, Live-Config-Snapshot und sc CLI-Verfuegbarkeit.

Keine automatische Live-Abfrage (kein sc-/API-Call) ausserhalb des
Briefing-Orchestrators: der Zustand wird ausschliesslich aus lokalen Dateien
(Vault-Briefings, config/snapshot.current.json) bestimmt. Kein Mock-/Seed-
Fallback — fehlende Live-Daten ergeben einen roten Status.
"""
from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path

from scripts import send_telegram, snapshot

VAULT_DIR = Path.home() / "docs" / "notizen" / "portfolio-briefings"


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


def check_health() -> dict:
    age = _latest_briefing_age_hours()
    live_ok, live_note = _live_config_status()
    sc_ok = _sc_available()
    age_ok = age is not None and age < 48
    warnings: list[str] = []
    if age is None:
        warnings.append("Kein Briefing im Vault gefunden.")
    elif not age_ok:
        warnings.append(f"Letztes Briefing ist {age:.1f}h alt.")
    if not live_ok:
        warnings.append(live_note)
    if not sc_ok:
        warnings.append("sc CLI nicht im PATH (Live-Abruf erst nach `sc login` moeglich).")

    status = "green" if not warnings else "red"
    if warnings:
        alert = "Healthcheck portfolio-briefing:\n" + "\n".join(f"- {w}" for w in warnings)
        try:
            send_telegram.send_briefing(alert, "healthcheck")
        except Exception:
            pass

    return {
        "status": status,
        "latest_briefing_age_hours": age,
        "live_snapshot_ok": live_ok,
        "sc_available": sc_ok,
        "warnings": warnings,
    }


if __name__ == "__main__":
    print(check_health())
