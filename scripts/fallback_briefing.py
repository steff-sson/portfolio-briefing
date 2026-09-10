"""Deterministisches Faktenbriefing (Fallback, Phase D — kein LLM).

Wenn der Revisions-Loop (Phase C) nach maximal ``MAX_LLM_ATTEMPTS`` Versuchen
keinen validen Draft erzeugt, rendert ``build_fallback_briefing`` aus dem
Faktenpaket ein korrektes, deterministisches Briefing im bestehenden
6-Sektionen-Contract (verify.DRAFT_SECTIONS) — verstaendlich und kompakt.
Kein LLM-Aufruf, keine Prosa-Interpretation: alle Zahlen, Kategorien, Ampeln,
Empfehlung und der Naechste Schritt stammen ausschliesslich aus autoritativen
Paket-Fakten (``deterministic_summary``/``strategy_thresholds_pct``/``data_quality``).
Der Fallback ist klar als "Faktenbriefing — LLM-Text nicht verfügbar/validiert"
markiert, ohne den Status der Fakten zu verfaelschen.

Sicherheitsprinzip (verify-1:1): Der Text darf keine Prozentzahl enthalten,
die nicht 1:1 in der gemeinsamen Zahlen-Allowlist
(``verify.build_allowed_numbers``) steht — genau die Regel, die
``verify.verify_draft`` auch gegen LLM-Drafts anwendet. Freie Texte aus dem
Faktenpaket (Ampel-Reasons, Signal-Gruende) werden nur dann woertlich
uebernommen, wenn ihre Prozentzahlen allowlist-konform sind; sonst faellt die
Zeile auf den Status ohne Zahlen zurueck. Der Aufrufer fuehrt das Ergebnis
trotzdem durch verify_draft + final_gate (fail-closed): ein ungueltiger
Fallback wird nie versendet.
"""
from __future__ import annotations

import re

from scripts import verify

# Marker, der das Briefing eindeutig als deterministischen Fallback kennzeichnet
# (kein LLM-Text). Steht am Anfang der Kurzlage.
FALLBACK_MARKER = "Faktenbriefing — LLM-Text nicht verfügbar/validiert"

# Deterministische Reihenfolge der Ampel-Kategorien (7, wie im Contract).
_TRAFFIC_LIGHT_ORDER = (
    "core_satellite",
    "sector_concentration",
    "single_position",
    "thesis_deadlines",
    "turnover",
    "trades_per_quarter",
    "data_quality",
)

# Deutsche Lesarten — identisch zu verify._CATEGORY_GERMAN (keine rohen
# snake_case-Bezeichner im Markdown, Stil-Gate).
_CATEGORY_GERMAN = {
    "core_satellite": "Core-/Satelliten-Aufteilung",
    "sector_concentration": "Sektorkonzentration",
    "single_position": "Einzelposition",
    "thesis_deadlines": "Thesen-Fristen",
    "turnover": "Umschlag",
    "trades_per_quarter": "Trades/Quartal",
    "data_quality": "Datenqualität",
}

_STATUS_GERMAN = {"green": "grün", "yellow": "gelb", "red": "rot"}

_POSITION_STATUS_GERMAN = {
    "rot": "rot",
    "gelb": "gelb",
    "gruen": "grün",
    "green": "grün",
    "yellow": "gelb",
    "red": "rot",
    "ok": "ok",
    "unbewertet": "unbewertet",
}

_CATEGORY_SHORT = {
    "core": "Core",
    "satellite": "Satellite",
    "legacy": "Legacy",
    "unknown": "unbekannt",
}

_DATA_QUALITY_STATUS_GERMAN = {
    "ok": "ok",
    "stale": "veraltet",
    "incomplete": "unvollständig",
    "implausible": "unplausibel",
}

# Fundamentaldaten-Disclaimer (identisch zum verify-Contract; wird von
# verify.verify_draft case-insensitiv als Substring geprueft).
_FUNDAMENTALS_NOTE = (
    "Fundamentaldaten (Umsatz, Gewinn, Cashflow, Verschuldung, Bewertung) "
    "nicht automatisch verfügbar und fließen nicht in das Signal ein."
)

# Option-2-Klarstellung (Laiensicht): Watchlist-Kandidaten sind nur
# Beobachtung/Review. Aus den vorliegenden Daten (Kurse, News, Fit) laesst
# sich keine belastbare Gewinn-/Kurs-Prognose ableiten; fuer eine fundierte
# Kauf-/Verkaufsentscheidung fehlen Fundamentaldaten. Die Formulierung nutzt
# bewusst "Gewinn- oder Kurs-Prognose" (Bindestrich), damit die reine
# Prognose-Aussage nicht als Prognose-Versprechen missverstanden wird.
_WATCHLIST_OBSERVATION_NOTE = (
    "Beobachtung/Review: Diese Watchlist-Kandidaten sind ausschließlich "
    "Beobachtung und Review (Strategie-Fit, Portfolio-Fit, News). Aus den "
    "vorliegenden Daten (Kurse, News, Fit) lässt sich keine belastbare Gewinn- "
    "oder Kurs-Prognose ableiten; für eine fundierte Kauf-/Verkaufsentscheidung "
    "wären Fundamentaldaten (Umsatz, Gewinn, Bewertung) nötig, die aktuell nicht "
    "vorliegen. Das ist keine Kauf-Empfehlung und keine Gewinn-Prognose."
)

# Signale, die eine akute Handlungsadresse erzeugen (identisch zur
# no-action-Regel in verify).
_ACUTE_SIGNAL_LABELS = ("SELL", "REDUCE", "BUY")

_ACTION_GERMAN = {"aufstocken": "Aufstocken", "reduzieren": "Reduzieren", "verkaufen": "Verkaufen"}


def _summary(facts_package: dict) -> dict:
    summary = facts_package.get("deterministic_summary")
    return summary if isinstance(summary, dict) else {}


def _eur(value: object) -> str:
    """Ganzzahliger Euro-Betrag mit deutschem Tausendertrenner (keine %-Zahl)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "–"
    return f"{int(round(value)):,}".replace(",", ".") + " €"


def _percent_ok(text: str, allowed_formatted: set[str], allowed_exact_2: set[str]) -> bool:
    """Sind alle Prozentzahlen in ``text`` 1:1 in der Allowlist (wie verify)?

    Gleiche Pruefregel wie verify.verify_draft Schritt 4: Werte >= 1.0 mit 1
    Dezimalstelle, Werte < 1.0 zusaetzlich mit 2 Dezimalstellen. Enthaelt der
    Text keine Prozentzahl, ist er immer ok.
    """
    for raw in re.findall(r"(\d+(?:[.,]\d+)?)\s*%", text):
        num = float(raw.replace(",", "."))
        if f"{num:.1f}" not in allowed_formatted and not (
            num < 1.0 and f"{num:.2f}" in allowed_exact_2
        ):
            return False
    return True


def _embed(text: object, allowed_formatted: set[str], allowed_exact_2: set[str]) -> str:
    """Freien Fakten-Text nur uebernehmen, wenn keine nicht-allowlistete %-Zahl drinsteht."""
    if not isinstance(text, str) or not text.strip():
        return ""
    if _percent_ok(text, allowed_formatted, allowed_exact_2):
        return text.strip()
    return ""


def _allowed_sets(facts_package: dict) -> tuple[set[str], set[str]]:
    values = verify.build_allowed_numbers(facts_package)
    return (
        {f"{value:.1f}" for value in values},
        {f"{value:.2f}" for value in values if value < 1.0},
    )


# --- Sektionen -----------------------------------------------------------------


def _section_kurzlage(facts_package: dict, allowed_fmt: set[str], allowed_2: set[str]) -> str:
    summary = _summary(facts_package)
    thresholds = facts_package.get("strategy_thresholds_pct") if isinstance(facts_package, dict) else {}
    if not isinstance(thresholds, dict):
        thresholds = {}
    lights = summary.get("traffic_lights")
    lights = lights if isinstance(lights, dict) else {}
    lines: list[str] = [FALLBACK_MARKER + ". Deterministisch aus autoritativen Fakten; Status unverändert."]

    # Direkte Ein-Satz-Zusammenfassung VOR allen Aufzaehlungspunkten
    # (1:1 aus deterministic_summary.recommendation, keine neuen Fakten).
    rec = summary.get("recommendation")
    if isinstance(rec, dict) and rec.get("label") in verify.RECOMMENDATION_LABELS:
        reason = rec.get("reason")
        reason_txt = f" {reason}" if isinstance(reason, str) and reason.strip() else ""
        lines.append(f"Gesamturteil: {rec['label']}.{reason_txt}")
    else:
        lines.append("Gesamturteil: keine deterministische Empfehlung (Analyse-Daten fehlen).")

    total = summary.get("total_value_eur")
    if isinstance(total, (int, float)) and not isinstance(total, bool) and total > 0:
        count = summary.get("position_count")
        count_txt = f", {int(count)} Positionen" if isinstance(count, (int, float)) else ""
        lines.append(f"Gesamtwert: {_eur(total)}{count_txt}.")

    # Ampelzeilen (Status + Kategorie + reason 1:1 aus traffic_lights).
    # Status als Wort (gruen/gelb/rot) — kein Emoji.
    for key in _TRAFFIC_LIGHT_ORDER:
        light = lights.get(key)
        if not isinstance(light, dict):
            continue
        status = light.get("status")
        status_txt = _STATUS_GERMAN.get(str(status), "unbekannt") if isinstance(status, str) else "unbekannt"
        label = _CATEGORY_GERMAN.get(key, key)
        reason = _embed(light.get("reason"), allowed_fmt, allowed_2)
        lines.append(f"- {label}: {status_txt}" + (f" — {reason}" if reason else ""))

    # Strategie-Ziele/Limits (nur positive Schwellen — Werte 1:1 allowlist-konform).
    goal_bits: list[str] = []
    core_pct = thresholds.get("core_pct")
    satellite_pct = thresholds.get("satellite_pct")
    threshold_pct = thresholds.get("threshold_pct")
    if isinstance(core_pct, (int, float)) and core_pct > 0 and isinstance(satellite_pct, (int, float)) and satellite_pct > 0:
        goal_bits.append(f"Core {core_pct:.1f}% / Satellite {satellite_pct:.1f}%")
    if isinstance(threshold_pct, (int, float)) and threshold_pct > 0:
        goal_bits.append(f"Toleranz {threshold_pct:.1f}%")
    if goal_bits:
        lines.append("- Strategie-Ziel: " + ", ".join(goal_bits) + ".")
    limit_bits: list[str] = []
    max_position_pct = thresholds.get("max_position_pct")
    max_sector_pct = thresholds.get("max_sector_pct")
    max_turnover_pct = thresholds.get("max_turnover_annual_pct")
    if isinstance(max_position_pct, (int, float)) and max_position_pct > 0:
        limit_bits.append(f"Einzelposition {max_position_pct:.1f}%")
    if isinstance(max_sector_pct, (int, float)) and max_sector_pct > 0:
        limit_bits.append(f"Satellite-Sektor {max_sector_pct:.1f}%")
    if isinstance(max_turnover_pct, (int, float)) and max_turnover_pct > 0:
        limit_bits.append(f"Umschlag {max_turnover_pct:.1f}%/Jahr")
    if limit_bits:
        lines.append("- Max. Anteil: " + ", ".join(limit_bits) + ".")

    # Positions-Zeilen als Fliesstext (keine Markdown-Tabelle, keine Pipe).
    detail = summary.get("positions_detail")
    if isinstance(detail, list) and detail:
        lines.append("Positionen:")
        for pos in detail:
            if not isinstance(pos, dict):
                continue
            weight = pos.get("weight")
            if isinstance(weight, (int, float)) and not isinstance(weight, bool) and weight > 0:
                anteil = f"{round(float(weight) * 100, 1):.1f}% des Gesamtportfolios"
            else:
                anteil = "unbewertet (kein Anteil)"
            limit = pos.get("limit_pct")
            if isinstance(limit, (int, float)) and not isinstance(limit, bool) and limit > 0:
                limit_txt = f"max. Anteil {float(limit):.1f}%"
            else:
                limit_txt = "kein Einzelpositionslimit"
            category = str(pos.get("category") or "unknown")
            status = str(pos.get("status") or "")
            lines.append(
                "- {name} ({isin}), Kategorie {cat}: {wert}, {anteil}; {limit}; Status {status}.".format(
                    name=str(pos.get("name") or "–"),
                    isin=str(pos.get("isin") or "–"),
                    cat=_CATEGORY_SHORT.get(category, category),
                    wert=_eur(pos.get("value_eur")),
                    anteil=anteil,
                    limit=limit_txt,
                    status=_POSITION_STATUS_GERMAN.get(status, status or "–"),
                )
            )

    # Sektor-Zeilen — Anteil ausdruecklich AM SATELLITE-UMFANG relativiert,
    # damit ein Wert wie "Unknown 100.0%" nie als Gesamtportfolio-Konzentration
    # wirkt.
    sectors = summary.get("sectors_detail")
    if isinstance(sectors, list) and sectors:
        lines.append("Satellite-Sektoren (Anteil am Satellite-Umfang):")
        for sec in sectors:
            if not isinstance(sec, dict):
                continue
            ratio = sec.get("ratio")
            if isinstance(ratio, (int, float)) and not isinstance(ratio, bool) and ratio > 0:
                anteil = f"{round(float(ratio) * 100, 1):.1f}% der Satellite-Positionen"
            else:
                anteil = "kein Anteil"
            limit = sec.get("limit_pct")
            if isinstance(limit, (int, float)) and not isinstance(limit, bool) and limit > 0:
                limit_txt = f"max. Anteil {float(limit):.1f}%"
            else:
                limit_txt = "kein Sektorlimit"
            status = str(sec.get("status") or "")
            lines.append(
                "- Satellite-Sektor {name}: {wert}, {anteil}; {limit}; Status {status}.".format(
                    name=str(sec.get("name") or "–"),
                    wert=_eur(sec.get("value_eur")),
                    anteil=anteil,
                    limit=limit_txt,
                    status=_POSITION_STATUS_GERMAN.get(status, status or "–"),
                )
            )

    return "## Kurzlage\n" + "\n".join(lines)


def _section_datenqualitaet(facts_package: dict) -> str:
    summary = _summary(facts_package)
    dq = facts_package.get("data_quality") if isinstance(facts_package, dict) else None
    if not isinstance(dq, dict):
        status = summary.get("data_quality_status")
        issues = summary.get("data_quality_issues")
        dq = {"status": status, "issues": issues if isinstance(issues, list) else []}
    status = dq.get("status")
    status_txt = _DATA_QUALITY_STATUS_GERMAN.get(str(status), str(status or "ok")) if isinstance(status, str) else "ok"
    lines = [f"Status: {status_txt}"]
    issues = dq.get("issues")
    if isinstance(issues, list) and issues:
        lines.append("Hinweise:")
        lines.extend(f"- {issue}" for issue in issues if isinstance(issue, str) and issue.strip())
    elif status in (None, "ok"):
        lines.append("Keine bekannten Probleme.")
    return "## Datenqualität\n" + "\n".join(lines)


def _signal_rows(signals: object, allowed_fmt: set[str], allowed_2: set[str]) -> list[str]:
    lines: list[str] = []
    if not isinstance(signals, list):
        return lines
    for signal in signals:
        if not isinstance(signal, dict) or signal.get("excluded"):
            continue
        name = str(signal.get("name") or signal.get("isin") or "?")
        isin = str(signal.get("isin") or "")
        sig = str(signal.get("signal") or "NO SIGNAL")
        score = signal.get("score")
        score_txt = ""
        if isinstance(score, int) and not isinstance(score, bool) and score != 0:
            score_txt = f" (Score {score:+d})"
        reason = _embed(signal.get("reason"), allowed_fmt, allowed_2)
        isin_txt = f" ({isin})" if isin else ""
        line = f"- {name}{isin_txt}: {sig}{score_txt}"
        if reason:
            line += f" — {reason}"
        lines.append(line)
    return lines


def _section_sell_reduce(facts_package: dict, allowed_fmt: set[str], allowed_2: set[str]) -> str:
    signals = _summary(facts_package).get("satellite_sell_signals")
    rows = _signal_rows(signals, allowed_fmt, allowed_2)
    if rows:
        lines: list[str] = rows
    else:
        lines = ["Keine Sell-/Reduce-Signale."]
    lines.append("")
    lines.append(_FUNDAMENTALS_NOTE)
    return verify.SELL_SIGNALS_SECTION + "\n" + "\n".join(lines)


def _section_watchlist(facts_package: dict, allowed_fmt: set[str], allowed_2: set[str]) -> str:
    signals = _summary(facts_package).get("watchlist_signals")
    rows = _signal_rows(signals, allowed_fmt, allowed_2)
    if rows:
        lines = list(rows)
        # Option 2: Kandidaten nur als Beobachtung/Review, ausdruecklich ohne
        # Kauf-Empfehlung/Gewinn-Prognose.
        lines.append("")
        lines.append(_WATCHLIST_OBSERVATION_NOTE)
    else:
        lines = ["Keine Watchlist-Signale (NO SIGNAL für alle Positionen)."]
    # Der Fundamentaldaten-Disclaimer steht genau EINMAL im Briefing (am Ende
    # der Sell-/Reduce-Sektion) und gilt fuer alle Signal-Sektionen — kein
    # Duplikat hier.
    return verify.WATCHLIST_SIGNALS_SECTION + "\n" + "\n".join(lines)


def _section_empfehlung(facts_package: dict) -> str:
    rec = _summary(facts_package).get("recommendation")
    if isinstance(rec, dict) and rec.get("label") in verify.RECOMMENDATION_LABELS:
        label = str(rec["label"])
        reason = rec.get("reason")
        reason_txt = f" — {reason}" if isinstance(reason, str) and reason.strip() else ""
        return f"## Empfehlung\n{label}{reason_txt}"
    return "## Empfehlung\nKeine deterministische Empfehlung (Analyse-Daten fehlen)."


def _section_naechster_schritt(facts_package: dict) -> str:
    """Naechster Schritt: akute Aktionen adressieren, band_review als
    Quartals-Review-Hinweis, sonst no-action-Phrase (gleiche Semantik wie
    verify._naechster_schritt_handlungsbedarf)."""
    summary = _summary(facts_package)
    actions = summary.get("position_actions")
    actions = actions if isinstance(actions, list) else []
    akut = [a for a in actions if isinstance(a, dict) and a.get("category") != "band_review"]
    band_review = [a for a in actions if isinstance(a, dict) and a.get("category") == "band_review"]

    def _action_line(action: dict) -> str:
        name = str(action.get("name") or "?")
        isin = str(action.get("isin") or "")
        act = _ACTION_GERMAN.get(str(action.get("action")), str(action.get("action") or "Anpassen"))
        reason = action.get("reason")
        line = f"- {act} {name} ({isin})"
        if isinstance(reason, str) and reason.strip():
            line += f" — {reason.strip()}"
        if not line.endswith("."):
            line += "."
        return line

    lines: list[str] = [_action_line(a) for a in akut]
    # ISINs, die oben bereits konkret adressiert werden — vermeidet die
    # dreifache Wiederholung derselben Position in Sell-Sektion und
    # Naechster-Schritt.
    mentioned_isins = {
        str(a.get("isin"))
        for a in akut
        if isinstance(a, dict) and a.get("isin")
    }
    for key in ("satellite_sell_signals", "watchlist_signals"):
        signals = summary.get(key)
        if not isinstance(signals, list):
            continue
        for signal in signals:
            if not isinstance(signal, dict) or signal.get("excluded"):
                continue
            if signal.get("signal") not in _ACUTE_SIGNAL_LABELS:
                continue
            isin = str(signal.get("isin") or "")
            if isin and isin in mentioned_isins:
                continue  # Position ist oben bereits konkret adressiert
            name = str(signal.get("name") or signal.get("isin") or "?")
            sig = str(signal.get("signal"))
            lines.append(f"- {sig}-Signal adressieren: {name} ({isin}) — Details in der Signal-Sektion.")
            if isin:
                mentioned_isins.add(isin)

    dq_status = summary.get("data_quality_status")
    if dq_status not in (None, "ok"):
        lines.append("- Datenqualität prüfen und Ursache beheben, bevor Anpassungen erfolgen.")

    if band_review:
        items = "; ".join(
            f"{_ACTION_GERMAN.get(str(a.get('action')), str(a.get('action') or 'Anpassen'))} "
            f"{a.get('name')} ({a.get('isin')})"
            for a in band_review
            if isinstance(a, dict)
        )
        lines.append(f"- Quartals-Review (vierteljährliche Strategiesitzung): {items} — keine akute Aktion.")

    if not lines:
        lines.append("Nächste Woche neuer Lauf, keine Aktion erforderlich.")
    return "## Nächster Schritt\n" + "\n".join(lines)


def build_fallback_briefing(facts_package: dict) -> str:
    """Deterministisches Faktenbriefing aus dem Faktenpaket (kein LLM).

    6 Sektionen (verify.DRAFT_SECTIONS), exakt aus deterministischen Fakten
    gerendert. Alle Prozentzahlen sind 1:1 allowlist-konform (identische Regel
    wie verify.verify_draft) — das Ergebnis muss verify_draft + final_gate
    bestehen; der Aufrufer prueft das fail-closed.
    """
    if not isinstance(facts_package, dict):
        raise ValueError("build_fallback_briefing: facts_package ist kein Dict")
    allowed_fmt, allowed_2 = _allowed_sets(facts_package)
    sections = [
        _section_kurzlage(facts_package, allowed_fmt, allowed_2),
        _section_datenqualitaet(facts_package),
        _section_sell_reduce(facts_package, allowed_fmt, allowed_2),
        _section_watchlist(facts_package, allowed_fmt, allowed_2),
        _section_empfehlung(facts_package),
        _section_naechster_schritt(facts_package),
    ]
    return "\n\n".join(sections)
