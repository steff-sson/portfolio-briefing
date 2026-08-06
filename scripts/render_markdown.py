"""Render final markdown document with frontmatter."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path


TAGS = {
    "monday": ["portfolio", "briefing", "monday"],
    "friday": ["portfolio", "briefing", "friday"],
    "monthly": ["portfolio", "briefing", "monthly"],
}


def render(briefing_text: str, mode: str, date: str | None = None) -> str:
    if date is None:
        date = datetime.now().strftime("%Y-%m-%d")
    tags = TAGS.get(mode, ["portfolio", "briefing"])
    status = "draft" if "fehlgeschlagen" in briefing_text.lower() else "active"
    tags_line = " ".join(f"#{t}" for t in tags)
    frontmatter = f"""---
tags: [{', '.join(tags)}]
created: {date}
status: {status}
---

{tags_line}

# Portfolio Briefing — {mode} — {date}

{briefing_text}
"""
    return frontmatter


if __name__ == "__main__":
    print(render("Ruhige Woche. Alle Grenzen eingehalten.", "monday"))
