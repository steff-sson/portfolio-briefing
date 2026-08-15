"""Regression tests: Status kommt explizit vom Orchestrator, keine Fehlertext-Heuristik."""
from __future__ import annotations

import pytest

from scripts import render_markdown


def test_status_draft_explicit():
    md = render_markdown.render("Inhalt", "monday", date="2026-08-13", status="draft")
    assert "status: draft" in md


def test_status_active_explicit():
    md = render_markdown.render("Inhalt", "monday", date="2026-08-13", status="active")
    assert "status: active" in md


def test_status_default_active():
    md = render_markdown.render("Inhalt", "monday", date="2026-08-13")
    assert "status: active" in md


def test_error_text_does_not_flip_status():
    """Bug-Regression: 'fehlgeschlagen' im Body darf den Status nicht mehr umschreiben."""
    md = render_markdown.render(
        "Briefing-Generierung fehlgeschlagen: API-Key fehlt.",
        "monday",
        date="2026-08-13",
        status="active",
    )
    assert "status: active" in md
    assert "status: draft" not in md


class TestReadableHeader:
    """Stil-Fix: lesbarer Header statt technischem mode-Token —
    'Portfolio-Briefing — Montag/Freitag/Monatsrückblick, YYYY-MM-DD'."""

    @pytest.mark.parametrize(
        ("mode", "label"),
        [("monday", "Montag"), ("friday", "Freitag"), ("monthly", "Monatsrückblick")],
    )
    def test_header_uses_german_label(self, mode, label):
        md = render_markdown.render("Inhalt", mode, date="2026-08-13", status="active")
        assert f"# Portfolio-Briefing — {label}, 2026-08-13" in md

    def test_header_no_raw_mode_token(self):
        for mode in ("monday", "friday", "monthly"):
            md = render_markdown.render("Inhalt", mode, date="2026-08-13")
            assert f"# Portfolio-Briefing — {mode} —" not in md
            assert f"— {mode} —" not in md

    def test_frontmatter_and_status_preserved(self):
        md = render_markdown.render("Inhalt", "friday", date="2026-08-13", status="draft")
        assert "created: 2026-08-13" in md
        assert "status: draft" in md
        assert "tags: [portfolio, briefing, friday]" in md

    def test_unknown_mode_falls_back_to_mode_token(self):
        md = render_markdown.render("Inhalt", "unknown", date="2026-08-13")
        assert "# Portfolio-Briefing — unknown, 2026-08-13" in md
