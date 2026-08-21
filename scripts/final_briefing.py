"""Deterministischer deutscher Final-Renderer (Teil 1, ohne LLM).

Baut aus dem Faktenpaket (``facts.build_facts_package``) das finale Briefing
in exakt den kurzen Sektionen (Phase 5):

    ## Kurzlage
    ## Datenqualität
    ## Sell-/Reduce-Signale (bestehende Satellites)
    ## Watchlist-Signale
    ## Nächster Schritt

Alle Inhalte stammen 1:1 aus ``deterministic_summary`` (traffic_lights,
recommendation, position_actions, satellite_sell_signals, watchlist_signals)
bzw. aus den durchgereichten Fakten (portfolio/news/changes/strategy_diff/
data_quality). Es gibt bewusst KEINE LLM-Optionen und keine Neukaufideen —
der Renderer gibt nur deterministische Fakten aus. Keine Aenderung an der
Pipeline: ``render_final_briefing`` ist ein reines Modul ohne Seiteneffekte
(kein IO, kein Netz, keine Secrets).

Jede Signal-Sektion traegt den Fundamentaldaten-Disclaimer: Signale basieren
ausschliesslich auf Strategie-Fit, Portfolio-Fit, 7-Tage-RSS-News und
sc-Kursen — KEINE Fundamentaldaten (Umsatz, Gewinn, Cashflow, Verschuldung,
Bewertung) werden verwendet (Plan §"Nicht im Scope": Fundamentaldaten sind
nicht automatisch verfuegbar und fliessen nie in ein Signal).
"""
from __future__ import annotations

# Die sieben verbindlichen Briefing-Kategorien in fester Reihenfolge —
# identisch zu analyze.TRAFFIC_LIGHT_CATEGORIES, hier ohne Import-Zyklus
# bewusst als Konstante dupliziert (fachlicher Contract, Plan §6a).
TRAFFIC_LIGHT_CATEGORIES = (
    "core_satellite",
    "sector_concentration",
    "single_position",
    "thesis_deadlines",
    "turnover",
    "trades_per_quarter",
    "data_quality",
)

# Deutsche Lesarten der Kategorien (identisch zu verify._CATEGORY_GERMAN).
_CATEGORY_LABELS = {
    "core_satellite": "Core-/Satelliten-Aufteilung",
    "sector_concentration": "Sektorkonzentration",
    "single_position": "Einzelposition",
    "thesis_deadlines": "Thesen-Fristen",
    "turnover": "Umschlag",
    "trades_per_quarter": "Trades/Quartal",
    "data_quality": "Datenqualität",
}

# Deutsche Ampel-Lesarten (Plan §6a: grün/gelb/rot).
_STATUS_LABELS = {"green": "grün", "yellow": "gelb", "red": "rot"}

# Deutsche Lesarten der Status-Checknamen (red/yellow/green_checks in
# deterministic_summary — identisch zu facts._STATUS_CHECKS + data_quality).
# "drift" -> "Drift" ist die erlaubte deutsche Lesart (verify prüft "drift"
# nur klein und blockt rohe technische Bezeichner im Finaltext).
_CHECK_LABELS = {
    "core_satellite": "Core-/Satelliten-Aufteilung",
    "sector_concentration": "Sektorkonzentration",
    "single_position": "Einzelposition",
    "drift": "Drift",
    "turnover": "Umschlag",
    "thesis_deadlines": "Thesen-Fristen",
    "data_quality": "Datenqualität",
}

# Erlaubte Aktionen der Positionsvorschläge (identisch zu
# analyze.POSITION_ACTION_TYPES) — nur Änderungen, keine HOLD-Ausgabe.
_POSITION_ACTIONS = ("aufstocken", "reduzieren", "verkaufen")

# Deutsche Signal-Label (Phase 5): REDUCE ist die erlaubte Lesart fuer
# bestehende Satellites mit Reduktionsbedarf (SELL nur bei harten Verkaeufen).
_SIGNAL_LABELS = {
    "BUY": "BUY",
    "SELL": "SELL",
    "REDUCE": "REDUCE",
    "AVOID": "AVOID",
    "WATCH": "WATCH",
    "NO SIGNAL": "NO SIGNAL",
}

# Dimensionen-Label (Phase 5): Kurzform je Teil-Score (D1-D4).
_DIMENSION_LABELS = {
    "strategy_fit": "D1 Strategie-Fit",
    "portfolio_fit": "D2 Portfolio-Fit",
    "news_sentiment": "D3 7-Tage-News",
    "price_development": "D4 Kursentwicklung",
}

# Fundamentaldaten-Disclaimer (Phase 5): hart verankert in jeder Signal-Sektion.
# Signale stammen NIE aus Fundamentaldaten (Umsatz/Gewinn/Cashflow/
# Verschuldung/Bewertung sind nicht automatisch verfuegbar und fliessen nicht
# in ein Signal) — nur Strategie-Fit, Portfolio-Fit, 7-Tage-RSS-News, sc-Kurse.
# Der Text beginnt mit dem verify-Contract-Substring
# (verify.FUNDAMENTALS_DISCLAIMER: "Fundamentaldaten (...) nicht automatisch
# verfügbar"), damit der Versand-Gate (verify) den Disclaimer im gerenderten
# Text per Substring findet. "sind" vor "nicht" wuerde den Match brechen
# (fail-closed: Sektion wuerde blocken).
FUNDAMENTALS_DISCLAIMER = (
    "Fundamentaldaten (Umsatz, Gewinn, Cashflow, Verschuldung, Bewertung) "
    "nicht automatisch verfügbar und fließen nicht in das Signal ein. "
    "Signale basieren ausschließlich auf Strategie-Fit, Portfolio-Fit, "
    "7-Tage-RSS-News und sc-Kursen."
)

# Deterministische, nummernfreie Gegenargument-/Risiko-Fallbacks pro
# Aktionstyp — greifen nur, wenn das Faktenobjekt kein separates
# gegenargument/risiko-Feld liefert (defensiv, damit jede gerenderte
# Positionsaktion Begründung UND Gegenargument/Risiko enthält; verify
# blockt sonst major "Option ohne Gegenargument/Risiko"). Bewusst ohne
# Zahlen: Positionsgewichte sind nicht Teil der verify-Allowlist.
_ACTION_COUNTER_FALLBACKS = {
    "aufstocken": "Bei fallenden Kursen vergrößert sich die Position vorübergehend.",
    "reduzieren": "Eine Kurserholung kann das Aufwärtspotenzial der reduzierten Position erhöhen.",
    "verkaufen": "Ein späterer Wiedereinstieg kann höhere Kosten verursachen.",
}


def _as_dict(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def _as_list(value: object) -> list:
    return value if isinstance(value, list) else []


def _safe_str(value: object) -> str:
    """Sicherer String: None -> "", alles andere unveraendert als Text."""
    return str(value) if value is not None else ""


def _strip_final_period(text: str) -> str:
    """Bestehenden Schlusspunkt entfernen (der Block haengt selbst einen an)."""
    return text.removesuffix(".")


def _format_pct(ratio: object, digits: int = 1) -> str:
    """Ratio (0.248) -> '24.8%'. None/invalid -> '—' (nie erfundene Werte)."""
    if not isinstance(ratio, (int, float)) or isinstance(ratio, bool):
        return "—"
    try:
        return f"{float(ratio) * 100:.{digits}f}%"
    except (TypeError, ValueError):
        return "—"


def _format_eur(value: object) -> str:
    """EUR-Wert -> '12.345,67 €'. None/invalid -> '—' (nie erfundene Werte)."""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return "—"
    try:
        return f"{float(value):,.2f} €".replace(",", "X").replace(".", ",").replace("X", ".")
    except (TypeError, ValueError):
        return "—"


def _section_kurzlage(summary: dict, data_quality: dict | None) -> str:
    """Kurzlage: deterministisch aus red/yellow/green-Listen + Datenqualität."""
    red = _as_list(summary.get("red_checks"))
    yellow = _as_list(summary.get("yellow_checks"))
    green = _as_list(summary.get("green_checks"))

    labels = _CHECK_LABELS
    red_names = ", ".join(str(labels.get(c, c)) for c in red) if red else "keine"
    yellow_names = ", ".join(str(labels.get(c, c)) for c in yellow) if yellow else "keine"
    green_names = ", ".join(str(labels.get(c, c)) for c in green) if green else "keine"

    dq = _as_dict(data_quality)
    dq_status = dq.get("status")
    dq_text = "ok" if dq_status in (None, "", "ok") else _safe_str(dq_status)

    lines = [
        f"- Rote Punkte: {red_names}.",
        f"- Gelbe Punkte: {yellow_names}.",
        f"- Grüne Punkte: {green_names}.",
        f"- Datenqualität: {dq_text}.",
    ]
    return "\n".join(lines)


def _section_datenqualitaet(data_quality: dict | None) -> str:
    """Datenqualität: Status + Issues (aus dem Faktenpaket, nie erfunden)."""
    dq = _as_dict(data_quality)
    status = dq.get("status")
    if status in (None, "", "ok"):
        return "Datenqualität: ok."
    issues = _as_list(dq.get("issues"))
    if issues:
        return "Datenqualität: " + _safe_str(status) + " — " + "; ".join(_safe_str(i) for i in issues) + "."
    return "Datenqualität: " + _safe_str(status) + "."


def _section_entscheidungsrelevante_punkte(summary: dict) -> str:
    """Entscheidungsrelevante Punkte: max. 3 Positionsvorschläge (1:1 aus
    deterministic_summary.position_actions), keine Neukaufideen (0 zulässig)."""
    actions = _as_list(summary.get("position_actions"))[:3]
    if not actions:
        return "Keine entscheidungsrelevanten Punkte (keine Positionsvorschläge)."
    blocks = []
    for action in actions:
        if not isinstance(action, dict):
            continue
        act = _safe_str(action.get("action"))
        isin = _safe_str(action.get("isin"))
        name = _safe_str(action.get("name"))
        reason = _safe_str(action.get("reason"))
        counter = _safe_str(action.get("gegenargument", action.get("risiko")))
        if not counter:
            # Defensiver Fallback: jede Aktion braucht ein Gegenargument/Risiko
            # (verify major "Option ohne Gegenargument/Risiko"). Fallback ist
            # deterministisch und nummernfrei, passend zum Aktionstyp.
            counter = _ACTION_COUNTER_FALLBACKS.get(act, "Die Marktentwicklung kann vom erwarteten Verlauf abweichen.")
        header = "Position " + name if name else "Position"
        body = f"Option: {act} — {header} ({isin})."
        if reason:
            body += " Begründung: " + _strip_final_period(reason)
        if counter:
            body += " Gegenargument/Risiko: " + _strip_final_period(counter)
        blocks.append(body + ".")
    if not blocks:
        return "Keine entscheidungsrelevanten Punkte (keine Positionsvorschläge)."
    return "\n".join(_safe_str(x) for x in [f"- {b}" for b in blocks])


def _section_strategie_abgleich(summary: dict, strategy_thresholds_pct: dict) -> str:
    """Strategie-Abgleich: Ampel (7 Kategorien) + Strategie-Grenzwerte.

    Die Ampel wird 1:1 aus ``deterministic_summary.traffic_lights`` übernommen
    (keine achte Kategorie, keine Verschiebung). Die Grenzwerte kommen aus
    ``strategy_thresholds_pct`` (bereits Prozentwerte, 1 Dezimalstelle).
    """
    lights = _as_dict(summary.get("traffic_lights"))
    lines = []
    for cat in TRAFFIC_LIGHT_CATEGORIES:
        light = lights.get(cat)
        label = _CATEGORY_LABELS.get(cat, cat)
        if not isinstance(light, dict):
            lines.append(f"- {label}: nicht bestimmbar.")
            continue
        status = _safe_str(light.get("status"))
        status_label = _STATUS_LABELS.get(status, status or "unbekannt")
        reason = _safe_str(light.get("reason"))
        if reason:
            lines.append(f"- {label}: {status_label} — {reason}")
        else:
            lines.append(f"- {label}: {status_label}.")
    return "\n".join(lines)


def _section_news_veraenderungen(facts_package: dict) -> str:
    """Relevante News & Veränderungen: News-Titel, Portfolio-/Strategie-Diff.

    Die drei GLM-Review-Findings (echter Lauf 2026-08-19) sind hier hart
    verankert:

    1. Portfolio-/Transaktionsänderungen werden NUR gerendert, wenn das
       Faktenpaket sie tatsächlich belegt: added/removed/changed als
       nicht-leere Listen bzw. added_count/removed_count > 0. Reine
       Werte-Bewegungen unter den Diff-Schwellen (``changed`` leer) und
       0/0-Counts werden nicht als Veränderung gemeldet — nie erfundene
       Positionen oder Counts.
    2. Transaktions-Zahlen ("X neu, Y entfernt") erscheinen nur bei
       added_count/removed_count > 0 (belastbare Counts), nie bei 0/0.
    3. Strategieänderung wird RICHTUNGSSPEZIFISCH gerendert: added_keys
       als "Hinzugekommen", removed_keys als "Entfernt", changed_keys als
       "Geändert" — removed_keys werden nie als hinzugefügt dargestellt.
    """
    news = _as_list(facts_package.get("news"))
    lines = []
    if news:
        lines.append("News:")
        for n in news:
            if not isinstance(n, dict):
                continue
            title = _safe_str(n.get("title"))
            if title:
                lines.append(f"- {title}")
    changes = _as_dict(facts_package.get("changes"))
    if changes.get("has_previous") is True:
        positions = _as_dict(changes.get("positions"))
        added = _as_list(positions.get("added"))
        removed = _as_list(positions.get("removed"))
        changed = _as_list(positions.get("changed"))
        txns = _as_dict(changes.get("transactions"))
        added_tx = txns.get("added_count") or 0
        removed_tx = txns.get("removed_count") or 0
        # Nur echte Belege rendern: nicht-leere Positions-Listen und Counts > 0.
        # Leere Listen (keine Delta-Felder im Faktenpaket) erzeugen keine
        # Hinzugekommen/Entfernt/Geändert-Zeilen (GLM-Finding 1).
        if added or removed or changed or added_tx or removed_tx:
            lines.append("Veränderungen:")
            if added:
                lines.append(f"- Hinzugekommen: {', '.join(_safe_str(p.get('name') or p.get('isin') or '?') for p in added)}.")
            if removed:
                lines.append(f"- Entfernt: {', '.join(_safe_str(p.get('name') or p.get('isin') or '?') for p in removed)}.")
            if changed:
                lines.append(f"- Geändert: {', '.join(_safe_str(p.get('name') or p.get('isin') or '?') for p in changed)}.")
            # Transaktions-Counts nur bei belegten Counts > 0 (GLM-Finding 2).
            if added_tx or removed_tx:
                parts = []
                if added_tx:
                    parts.append(f"{added_tx} neu")
                if removed_tx:
                    parts.append(f"{removed_tx} entfernt")
                lines.append("- Transaktionen: " + ", ".join(parts) + ".")
    strategy_diff = facts_package.get("strategy_diff")
    if isinstance(strategy_diff, dict) and strategy_diff.get("has_changed"):
        # Richtungsspezifisch: removed_keys gehören zu "Entfernt", nie zu
        # "Hinzugekommen" (GLM-Finding 3 — removed_keys wurden zuvor als
        # hinzugefügt dargestellt).
        changed_keys = _as_list(strategy_diff.get("changed_keys"))
        added_keys = _as_list(strategy_diff.get("added_keys"))
        removed_keys = _as_list(strategy_diff.get("removed_keys"))
        if changed_keys or added_keys or removed_keys:
            lines.append("Strategieänderung:")
            if added_keys:
                lines.append("- Hinzugekommen: " + ", ".join(sorted(set(added_keys))) + ".")
            if removed_keys:
                lines.append("- Entfernt: " + ", ".join(sorted(set(removed_keys))) + ".")
            if changed_keys:
                lines.append("- Geändert: " + ", ".join(sorted(set(changed_keys))) + ".")
    if not lines:
        return "Keine relevanten News oder Veränderungen."
    return "\n".join(lines)


def _section_empfehlung(summary: dict) -> str:
    """Empfehlung: deterministisches BUY/SELL/WATCH-Label 1:1."""
    rec = _as_dict(summary.get("recommendation"))
    label = rec.get("label")
    reason = _safe_str(rec.get("reason"))
    if label not in ("BUY", "SELL", "WATCH"):
        return "Empfehlung: nicht bestimmbar."
    if reason:
        return f"{label} — {reason}"
    return f"{label}"


# --- Phase 5: Signal-Sektionen (kurzer Output) -------------------------------
# Sell-/Reduce-Signale fuer bestehende Satellites + max. 3 Watchlist-Signale.
# Jede Signal-Sektion traegt den Fundamentaldaten-Disclaimer (die Signale
# basieren nie auf Fundamentaldaten, nur auf den 4 deterministischen
# Dimensionen). Keine lange Portfoliotabelle, keine pauschale SELL-
# Empfehlung, keine persoenlichen Thesen als Blocker.


def _signal_line(signal: dict, with_score: bool = False) -> str:
    """Eine Signal-Zeile: ``ISIN — Signal — Grund`` (+ Score bei Watchlist)."""
    isin = _safe_str(signal.get("isin"))
    name = _safe_str(signal.get("name"))
    label = _SIGNAL_LABELS.get(_safe_str(signal.get("signal")), _safe_str(signal.get("signal")))
    reason = _safe_str(signal.get("reason"))
    subject = f"{name} ({isin})" if name and name != isin else isin
    line = f"{subject} — {label}"
    score = signal.get("score")
    if with_score and isinstance(score, (int, float)) and not isinstance(score, bool):
        dims = _as_dict(signal.get("dimensions"))
        pos = sum(1 for v in dims.values() if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0)
        neg = sum(1 for v in dims.values() if isinstance(v, (int, float)) and not isinstance(v, bool) and v < 0)
        line += f" — Score {score:+.0f} ({pos}+/{neg}-)"
    if reason:
        line += " — " + _strip_final_period(reason)
    return line + "."


def _dimensions_line(signal: dict) -> str:
    """Dimensionen-Zeile: ``Dimensionen: D1/D2/D3/D4 — …`` (nur nicht-None)."""
    dims = _as_dict(signal.get("dimensions"))
    parts = []
    for key, label in _DIMENSION_LABELS.items():
        value = dims.get(key)
        if value is not None and not isinstance(value, bool):
            parts.append(f"{label} {value:+d}")
    if not parts:
        return ""
    return "Dimensionen: " + ", ".join(parts) + "."


def _section_sell_reduce_signals(summary: dict) -> str:
    """Sell-/Reduce-Signale (bestehende Satellites): max. 3, je 1 Zeile.

    Quelle ist ``deterministic_summary.satellite_sell_signals`` (nur
    SELL/REDUCE fuer bestehende Satellite-Holdings; Core-ETFs und
    SUSE/Legacy sind ausgeschlossen). Keine Core-ETFs, keine SUSE.
    """
    signals = _as_list(summary.get("satellite_sell_signals"))
    if not signals:
        return "Keine Sell-/Reduce-Signale.\n\n" + FUNDAMENTALS_DISCLAIMER
    lines = [_signal_line(s) for s in signals[:3] if isinstance(s, dict)]
    if not lines:
        return "Keine Sell-/Reduce-Signale.\n\n" + FUNDAMENTALS_DISCLAIMER
    return "\n".join(f"- {line}" for line in lines) + "\n\n" + FUNDAMENTALS_DISCLAIMER


def _section_watchlist_signals(summary: dict) -> str:
    """Watchlist-Signale: max. 3, je 2 Zeilen (ISIN — Signal — Score, D1-D4).

    Quelle ist ``deterministic_summary.watchlist_signals``. SUSE/LU2722255754
    (illiquide Legacy) erscheint sichtbar als NO SIGNAL (kein BUY/SELL).
    Reine NO-SIGNAL-Auswahlen zeigen den Abschluss-Text.
    """
    signals = _as_list(summary.get("watchlist_signals"))
    if not signals:
        return "Keine Watchlist-Signale (NO SIGNAL für alle Positionen).\n\n" + FUNDAMENTALS_DISCLAIMER
    blocks = []
    for signal in signals[:3]:
        if not isinstance(signal, dict):
            continue
        line1 = _signal_line(signal, with_score=True)
        line2 = _dimensions_line(signal)
        blocks.append(f"- {line1}" + (f"\n  {line2}" if line2 else ""))
    if not blocks:
        return "Keine Watchlist-Signale (NO SIGNAL für alle Positionen).\n\n" + FUNDAMENTALS_DISCLAIMER
    return "\n".join(blocks) + "\n\n" + FUNDAMENTALS_DISCLAIMER


def _section_naechster_schritt(summary: dict) -> str:
    """Naechster Schritt: deterministisch aus den Signalen (keine Thesen).

    - SELL/REDUCE auf bestehenden Satellites -> "SUSE/Legacy ignorieren,
      SELL prüfen, ggf. manuell ausführen."
    - BUY auf der Watchlist -> "Watchlist-Position prüfen, ggf. manuell kaufen."
    - Sonst -> "Nächste Woche neuer Lauf, keine Aktion erforderlich."
    """
    sell = _as_list(summary.get("satellite_sell_signals"))
    watch = _as_list(summary.get("watchlist_signals"))
    if any(isinstance(s, dict) and s.get("signal") in ("SELL", "REDUCE") for s in sell):
        return "SUSE/Legacy ignorieren, SELL prüfen, ggf. manuell ausführen."
    if any(isinstance(s, dict) and s.get("signal") == "BUY" for s in watch):
        return "Watchlist-Position prüfen, ggf. manuell kaufen."
    return "Nächste Woche neuer Lauf, keine Aktion erforderlich."


def render_final_briefing(facts_package: dict, mode: str = "monday") -> str:
    """Deterministisches finales Briefing in den kurzen Sektionen (Phase 5):

    ## Kurzlage
    ## Datenqualität
    ## Sell-/Reduce-Signale (bestehende Satellites)
    ## Watchlist-Signale
    ## Nächster Schritt

    ``facts_package``: Output von ``facts.build_facts_package`` (kann auch ein
    leeres/minimales Dict sein — dann greift der leere Fakten-Fallback). Der
    Modus beeinflusst ausschließlich die Kopfzeile (lesbarer Label wie in
    ``render_markdown.MODE_LABELS``), nie die Fakten.
    """
    package = _as_dict(facts_package)
    summary = _as_dict(package.get("deterministic_summary"))
    data_quality = package.get("data_quality")

    # Modus-Label für die Kopfzeile (deutsch, ohne technisches mode-Token).
    mode_labels = {"monday": "Montag", "friday": "Freitag", "monthly": "Monatsrückblick"}
    label = mode_labels.get(mode, mode)

    sections = [
        ("Kurzlage", _section_kurzlage(summary, data_quality)),
        ("Datenqualität", _section_datenqualitaet(data_quality)),
        ("Sell-/Reduce-Signale (bestehende Satellites)", _section_sell_reduce_signals(summary)),
        ("Watchlist-Signale", _section_watchlist_signals(summary)),
        ("Nächster Schritt", _section_naechster_schritt(summary)),
    ]
    body = "\n\n".join(f"## {name}\n{content}" for name, content in sections)
    return f"# Portfolio-Briefing — {label}\n\n{body}\n"


if __name__ == "__main__":
    from scripts import analyze, facts, sc_bridge

    _portfolio, _transactions = sc_bridge.load_mock()
    _strategy = analyze.load_strategy()
    _analysis = analyze.analyze_portfolio(_portfolio, _transactions, _strategy)
    _package = facts.build_facts_package(
        _portfolio, _transactions, _analysis, news=[], strategy=_strategy, mode="monday"
    )
    print(render_final_briefing(_package, mode="monday"))
