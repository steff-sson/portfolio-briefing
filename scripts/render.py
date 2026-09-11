"""Renderer: Plain-Text + Ampel-Format C (Emoji ohne Text-Label).

Trimmt die Ausgabe aufs Wesentliche: Positionen nur in Kürzform (Name + Anteil
+ Ampel-Emoji), keine vollständige ISIN-Liste, Ampel-Emojis 🟢/🟡/🔴 am
Zeilenanfang (Format C: nur das Emoji, kein Text-Label).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

GREEN, YELLOW, RED = "🟢", "🟡", "🔴"
NONE_ACTION = "Keine Aktion nötig."


def _severity_ampel(sev: str) -> str:
    return {"red": RED, "yellow": YELLOW, "warn": YELLOW, "green": GREEN}.get(sev, GREEN)


def _position_pct(holding: dict, total: float) -> float:
    # value_eur kann None sein (ohne Umrechnung ausgeschlossene Position).
    value = holding.get("value_eur") or 0.0
    return value / total * 100.0 if total else 0.0


def _ampel_for_position(pct: float, max_pos_pct: float, warn_pct: float) -> str:
    if pct > max_pos_pct:
        return RED
    if pct > warn_pct:
        return YELLOW
    return GREEN


def render_briefing(data: dict[str, Any], max_position_pct: float = 5.0,
                    warn_position_pct: float = 3.0) -> str:
    """Baut das End-Briefing als Plain-Text (Ampel-Format C)."""
    lines: list[str] = []
    total = data.get("total_value_eur", 0.0) or sum(
        (h.get("value_eur") or 0.0) for h in data.get("holdings", [])
    )

    # Kopf
    cap = data.get("captured_at") or datetime.now(timezone.utc).isoformat()
    lines.append(f"Portfolio-Briefing — {cap[:10]}")
    lines.append(f"Gesamtwert: {total:,.0f} EUR | Cash: {data.get('cash_eur', 0):,.0f} EUR")
    # Alterswarnung (Fallback-Stufe 3: letzter Snapshot mit Alter).
    age = data.get("snapshot_age_hours")
    if age is not None:
        lines.append(f"🟡 Achtung: Datengrundlage ist ein Snapshot von vor ~{age:.0f} h "
                     f"(MCP re-auth / sc login bitte).")
    # FX-Befund (find. 4): genutzte Feldnamen + Entscheidung.
    fx_note = data.get("fx_note")
    if fx_note:
        lines.append(f"🟡 FX: {fx_note}")
    core = data.get("core_ratio")
    sat = data.get("satellite_ratio")
    if core is not None:
        lines.append(f"Core {core:.0f}% / Satellite {sat:.0f}%")
    lines.append("")

    # Handlungssignale (deterministisch)
    signals = data.get("signals", [])
    if signals:
        lines.append("## Signale")
        for s in signals:
            lines.append(f"{_severity_ampel(s.get('severity'))} {s.get('message')}")
        lines.append("")

    # Positionen (Kurzform + Ampel). Einzelpositions-Limit ist Satellite-Regel:
    # Core-Zeilen neutral (🟢), nur satellite/legacy bekommen die echte Ampel.
    lines.append("## Positionen")
    for h in data.get("holdings", []):
        pct = _position_pct(h, total)
        if str(h.get("category") or "").lower() == "core":
            amp = GREEN
        else:
            amp = _ampel_for_position(pct, max_position_pct, warn_position_pct)
        lines.append(f"{amp} {h.get('name')} — {pct:.1f}%")
    lines.append("")

    # Fundamentals (qualitative Bänder)
    funds = data.get("fundamentals", [])
    if funds:
        lines.append("## Fundamentaldaten")
        for f in funds:
            if f.get("error"):
                lines.append(f"🟡 {f.get('ticker')}: {f.get('error')}")
                continue
            lines.append(
                f"🟢 {f.get('ticker')} — fwdPE {_fmt(f.get('forward_pe'))} | "
                f"RevG {_pct(f.get('revenue_growth'))} | EarnG {_pct(f.get('earnings_growth'))} | "
                f"D/E {_fmt(f.get('debt_to_equity'))} | 52w {_pct(f.get('position_52w'))}"
            )
        lines.append("")

    # News (Treffer)
    news = data.get("news", [])
    if news:
        lines.append("## News")
        for n in news[:8]:
            lines.append(f"- [{n.get('source')}] {n.get('title')}")
        lines.append("")

    # LLM-Vorschläge
    suggestions = (data.get("suggestions") or "").strip()
    if suggestions:
        lines.append("## Vorschläge")
        if suggestions.lower().startswith("keine aktion"):
            lines.append(f"🟢 {NONE_ACTION}")
        else:
            lines.append(suggestions)
        lines.append("")

    # Bestätigungsfrage
    if suggestions and not suggestions.lower().startswith("keine aktion"):
        lines.append("Möchtest du eine dieser Maßnahmen umsetzen? (Bestätige, dann lege ich die Order nicht automatisch an.)")

    return "\n".join(lines)


def _fmt(v: Any) -> str:
    if v is None:
        return "–"
    return f"{v:.1f}"

def _pct(v: Any) -> str:
    if v is None:
        return "–"
    return f"{v*100:.0f}%"
