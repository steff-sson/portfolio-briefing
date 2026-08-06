"""Health checks: recent briefing and sc CLI availability."""
from __future__ import annotations

import shutil
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

from scripts import send_telegram


VAULT_DIR = Path.home() / "docs" / "notizen" / "portfolio-briefings"


def _latest_briefing_age_hours() -> float | None:
    if not VAULT_DIR.exists():
        return None
    files = sorted(VAULT_DIR.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        return None
    mtime = datetime.fromtimestamp(files[0].stat().st_mtime)
    return (datetime.now() - mtime).total_seconds() / 3600


def _sc_authenticated() -> bool:
    if shutil.which("sc") is None:
        return False
    try:
        subprocess.run(
            ["sc", "broker", "overview", "--json"],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        return True
    except Exception:
        return False


def check_health() -> dict:
    age = _latest_briefing_age_hours()
    sc_ok = _sc_authenticated()
    age_ok = age is not None and age < 48
    warnings: list[str] = []
    if age is None:
        warnings.append("Kein Briefing im Vault gefunden.")
    elif not age_ok:
        warnings.append(f"Letztes Briefing ist {age:.1f}h alt.")
    if not sc_ok:
        warnings.append("sc CLI nicht verfügbar oder nicht authentifiziert.")

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
        "sc_authenticated": sc_ok,
        "warnings": warnings,
    }


if __name__ == "__main__":
    print(check_health())
