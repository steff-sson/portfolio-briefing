"""Render final markdown document with frontmatter."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path


TAGS = {
    "monday": ["portfolio", "briefing", "monday"],
    "friday": ["portfolio", "briefing", "friday"],
    "monthly": ["portfolio", "briefing", "monthly"],
}

# Lesbare Header-Bezeichnung statt technischem mode-Token.
MODE_LABELS = {
    "monday": "Montag",
    "friday": "Freitag",
    "monthly": "Monatsrückblick",
}


def render(briefing_text: str, mode: str, date: str | None = None, status: str = "active") -> str:
    """Render markdown with frontmatter.

    Status kommt explizit vom Aufrufer (Orchestrator) — kein
    Fehlertext-Heuristik-Fallback ("fehlgeschlagen" im Body).
    """
    if date is None:
        date = datetime.now().strftime("%Y-%m-%d")
    tags = TAGS.get(mode, ["portfolio", "briefing"])
    label = MODE_LABELS.get(mode, mode)
    tags_line = " ".join(f"#{t}" for t in tags)
    frontmatter = f"""---
tags: [{', '.join(tags)}]
created: {date}
status: {status}
---

{tags_line}

# Portfolio-Briefing — {label}, {date}

{briefing_text}
"""
    return frontmatter


if __name__ == "__main__":
    print(render("Ruhige Woche. Alle Grenzen eingehalten.", "monday"))
