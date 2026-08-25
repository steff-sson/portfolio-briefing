"""Post-LLM verification: numbers, tickers, news references + send gate."""
from __future__ import annotations

import re
from pathlib import Path

from scripts.llm_briefing import LLMError

ROOT = Path(__file__).resolve().parent.parent

# Feste Output-Sektionen des kurzen Renderer-Contracts (Phase 5,
# final_briefing.render_final_briefing): die Abschnittspruefung richtet sich
# exakt an diesen Vertrag aus (## Kurzlage, ## Datenqualität,
# ## Sell-/Reduce-Signale (bestehende Satellites), ## Watchlist-Signale,
# ## Nächster Schritt).
SELL_SIGNALS_SECTION = "## Sell-/Reduce-Signale (bestehende Satellites)"
WATCHLIST_SIGNALS_SECTION = "## Watchlist-Signale"
NEXT_STEP_SECTION = "## Nächster Schritt"

# Alle Sektionen des kurzen Output-Formats in fester Reihenfolge.
DRAFT_SECTIONS = [
    "## Kurzlage",
    "## Datenqualität",
    SELL_SIGNALS_SECTION,
    WATCHLIST_SIGNALS_SECTION,
    NEXT_STEP_SECTION,
]

# Fruehere lange 5-Sektionen-Struktur (Plan Phase 2.1) — seit Phase 5 vom
# kurzen Renderer-Contract abgeloest; nur noch fuer Kontextpruefungen relevant.
_LEGACY_SECTIONS = [
    "## Entscheidungsrelevante Punkte",
    "## Strategie-Abgleich",
    "## Relevante News & Veränderungen",
]

# Fundamentaldaten-Disclaimer (Phase 5): Muss in jeder Signal-Sektion stehen.
# Signale stammen nie aus Fundamentaldaten (Umsatz/Gewinn/Cashflow/
# Verschuldung/Bewertung sind nicht automatisch verfuegbar und fliessen
# nicht in ein Signal) — nur aus Strategie-Fit, Portfolio-Fit,
# 7-Tage-RSS-News und sc-Kursen. Identisch zu final_briefing.FUNDAMENTALS_DISCLAIMER
# (ohne Import-Zyklus bewusst als Konstante dupliziert — fachlicher Contract).
# Case-insensitive geprueft: der Marker ist ein Substring des Renderer-Texts,
# die Pruefung vergleicht beide Seiten in Kleinschreibung (Umlaute,
# Gross-/Kleinschreibung spielen keine Rolle).
FUNDAMENTALS_DISCLAIMER = (
    "Fundamentaldaten (Umsatz, Gewinn, Cashflow, Verschuldung, Bewertung) "
    "nicht automatisch verfügbar"
)

# Abschliessende Gesamt-Empfehlungs-Sektion (Plan §6a): exakt ein Label
# BUY|SELL|WATCH, 1:1 aus deterministic_summary.recommendation.
RECOMMENDATION_SECTION = "## Empfehlung"
RECOMMENDATION_LABELS = ("BUY", "SELL", "WATCH")

# Sektionen fuer die Optionen-/Status-Kontextpruefung (Plan Phase 2.4/2.6).
_OPTIONS_SECTION = "## Entscheidungsrelevante Punkte"
_KURZLAGE_SECTION = "## Kurzlage"

# Optionen-Contract (Plan Phase 2.4/2.8): deterministische Marker.
# Optionen-Marker: halten/reduzieren/aufstocken (Wortgrenzen).
_OPTION_TERMS = ("halten", "reduzieren", "aufstocken")
# Begruendungs-Marker pro Option.
_REASON_MARKERS = ("begründung", "begruendung", "weil", "deshalb", "daher")
# Gegenargument-/Risiko-Marker pro Option.
_COUNTER_MARKERS = ("gegenargument", "gegenargumente", "risiko", "risiken")
# Imperative Kauf-/Verkaufsanweisung — critical, auch innerhalb der Sektion.
_IMPERATIVE_TERMS = ("kaufen sie", "verkaufen sie", "kauf sie", "verkauf sie")
# Handlungsempfehlungen — critical ausserhalb der Optionen-Sektion.
_RECOMMENDATION_TERMS = (
    "sie sollten",
    "empfehle",
    "empfehlen",
    "kaufen sie",
    "verkaufen sie",
    "kauf sie",
    "verkauf sie",
)

# Prozent-Grenzwerte aus facts.strategy_thresholds_pct, die das LLM
# referenzieren darf (z.B. "Core-Ziel 75%") — bereits in Prozent.
_STRATEGY_THRESHOLD_KEYS = (
    "core_pct",
    "satellite_pct",
    "threshold_pct",
    "max_position_pct",
    "max_sector_pct",
    "max_turnover_annual_pct",
)

# Defense-in-Depth: bekannte LLM-Fehlerstrings duerfen nie als Draft durchgehen.
ERROR_MARKERS = ("fehlgeschlagen", "API-Key", "Empty response")

# Konformitaets-Phrasen (case-insensitive geprueft) — nur in der KURZLAGE-
# Sektion zulaessig: "Alle Grenzen eingehalten"/"Ruhige Woche" nur ohne rote
# und gelbe Checks, "Kein Handlungsbedarf" zusaetzlich nur ohne jeden
# Optionen-Trigger (STATUS-REGELN in config/prompts/{mode}.txt).
CONFORMITY_MARKERS = ("alle grenzen eingehalten", "ruhige woche", "kein handlungsbedarf")

# Stil-Gates (nicht-lockernde Zusatzpruefung, Plan Stil-Fix): rohe bekannte
# snake_case-Checknamen und verbotene englische Fachbegriffe duerfen nie im
# finalen Markdown stehen. Deutsche Lesarten laut STIL-Regeln in
# config/prompts/{mode}.txt:
#   core_satellite -> Core-/Satelliten-Aufteilung
#   drift -> Drift            (daher "drift" NUR klein geprueft)
#   sector_concentration -> Sektorkonzentration
#   single_position -> Einzelposition
#   thesis_deadlines -> Thesen-Fristen
#   turnover -> Umschlag      ("Turnover"/"Turnover-Ratio" blockiert)
# Verbotene Fachbegriffe: "MVP" (-> aktuelle Ausbaustufe), "Zeitfenster-Logik".
_RAW_CHECK_NAMES_CI = (
    "core_satellite",
    "sector_concentration",
    "single_position",
    "thesis_deadlines",
    "turnover",
)
_RAW_CHECK_NAMES_CS = ("drift",)
_FORBIDDEN_STYLE_TERMS_CI = ("mvp", "zeitfenster-logik")


def _finding(severity: str, issue: str, evidence: str, correction: str) -> dict:
    """Build a finding dict matching the review output contract."""
    return {
        "severity": severity,
        "issue": issue,
        "evidence": evidence,
        "correction": correction,
    }


# --- Briefing-Schnittstelle (Plan §6a): Ampel-/Empfehlungs-Pruefung -----------

# Deutsche Lesarten der sieben Kategorien (wie im Draft-Prompt STIL).
_CATEGORY_GERMAN = {
    "core_satellite": "Core-/Satelliten-Aufteilung",
    "sector_concentration": "Sektorkonzentration",
    "single_position": "Einzelposition",
    "thesis_deadlines": "Thesen-Fristen",
    "turnover": "Umschlag",
    "trades_per_quarter": "Trades/Quartal",
    "data_quality": "Datenqualität",
}

# Erlaubte Aktionen der Positionsvorschlaege.
_POSITION_ACTIONS = ("aufstocken", "reduzieren", "verkaufen")


def _traffic_lights(facts_package: dict) -> dict:
    lights = facts_package.get("deterministic_summary", {}).get("traffic_lights")
    return lights if isinstance(lights, dict) else {}


def _recommendation(facts_package: dict) -> dict:
    rec = facts_package.get("deterministic_summary", {}).get("recommendation")
    return rec if isinstance(rec, dict) else {}


def _position_actions(facts_package: dict) -> list:
    actions = facts_package.get("deterministic_summary", {}).get("position_actions")
    return actions if isinstance(actions, list) else []


def _non_excluded_action_signals(facts_package: dict, labels: tuple) -> list:
    """Signal-Objekte mit Signal-Label in ``labels``, ohne excluded-Marker.

    Gleiche Ausschlussregel wie der Renderer (final_briefing
    _section_naechster_schritt/_section_sell_reduce_signals): excluded=True
    markiert Core-ETFs/SUSE-Legacy — die zaehlen nie als Handlungssignal.
    """
    summary = facts_package.get("deterministic_summary", {})
    result: list[dict] = []
    for signal in summary.get("satellite_sell_signals", []) if isinstance(summary.get("satellite_sell_signals"), list) else []:
        if isinstance(signal, dict) and not signal.get("excluded") and signal.get("signal") in labels:
            result.append(signal)
    for signal in summary.get("watchlist_signals", []) if isinstance(summary.get("watchlist_signals"), list) else []:
        if isinstance(signal, dict) and not signal.get("excluded") and signal.get("signal") in labels:
            result.append(signal)
    return result


def _naechster_schritt_handlungsbedarf(facts_package: dict) -> bool:
    """Konkrete Handlungssignale fuer die no-action-Regel (Phase 5)?

    Deckt sich 1:1 mit dem Renderer-Contract (final_briefing
    _section_naechster_schritt): SELL/REDUCE auf bestehenden Satellites,
    BUY auf der Watchlist oder nicht-leere position_actions. Nur diese
    Signale erzeugen konkreten Handlungstext — rote/gelbe Checks allein
    sind keine Handlungsempfehlung und blocken die no-action-Phrase nicht.
    """
    if _position_actions(facts_package):
        return True
    if _non_excluded_action_signals(facts_package, ("SELL", "REDUCE", "BUY")):
        return True
    return False


def _extract_recommendation_label(text: str) -> str | None:
    """Genau ein Label (BUY|SELL|WATCH) in der '## Empfehlung'-Sektion."""
    section = _extract_section(text, RECOMMENDATION_SECTION)
    if section is None:
        return None
    found = [label for label in RECOMMENDATION_LABELS if re.search(rf"\b{label}\b", section)]
    if len(found) == 1:
        return found[0]
    return None


def _verify_traffic_lights(facts_package: dict, text: str, findings: list[dict]) -> None:
    """Ampel 1:1: jede Kategorie mit Status im Faktenpaket; das LLM darf die
    Ampel weder erfinden noch verschieben (keine 8. Kategorie, keine gelb-
    statt-rot-Liste). Die Ampel-Labels stehen deterministisch fest.

    Ohne Ampel-Daten im Faktenpaket (z.B. Orchestrator-Test-Mocks mit leerer
    Analyse) wird die Pruefung uebersprungen — die Empfehlungs-Sektion bleibt
    dann ebenfalls ungeprueft (kein deterministisches Label vorhanden).
    """
    lights = _traffic_lights(facts_package)
    if not lights:
        return
    for cat, light in lights.items():
        if not isinstance(light, dict) or light.get("status") not in ("green", "yellow", "red"):
            findings.append(
                _finding(
                    "critical",
                    f"Ampel-Kategorie '{cat}' ungueltig",
                    f"traffic_lights[{cat}] = {light!r}, Status muss green|yellow|red sein",
                    "Ampel deterministisch aus dem Faktenpaket uebernehmen",
                )
            )


def _verify_recommendation(facts_package: dict, text: str, findings: list[dict]) -> None:
    """Gesamt-Empfehlung (Plan §6a): exakt ein Label in '## Empfehlung', 1:1
    mit dem deterministischen Label. Imperative Kauf-/Verkaufsanweisungen im
    Fliesstext bleiben verboten (bestehender Contract).

    Seit Phase 5 (kurzer Renderer-Contract, final_briefing.render_final_briefing)
    existiert KEINE '## Empfehlung'-Sektion mehr im finalen Text — der kurze
    Output enthält keine pauschale Gesamt-Empfehlung (test_no_pauschale_
    seLL_recommendation). Die Sektion wird nur noch als Pflicht geprueft, wenn
    der Text sie tatsaechlich enthaelt (Legacy-Drafts/Tests mit
    '## Empfehlung'); ein Text ohne die Sektion ist kein Verstoss.

    Ohne deterministisches Label (leeres Faktenpaket in Test-Mocks) wird die
    Sektion nicht geprueft.
    """
    expected = _recommendation(facts_package).get("label")
    if expected is None:
        return
    if RECOMMENDATION_SECTION not in text:
        return
    label = _extract_recommendation_label(text)
    if label is None:
        findings.append(
            _finding(
                "critical",
                f"Fehlende oder mehrdeutige Gesamt-Empfehlung in '{RECOMMENDATION_SECTION}'",
                f"Draft enthaelt nicht genau ein Label aus {', '.join(RECOMMENDATION_LABELS)}",
                f"Sektion '{RECOMMENDATION_SECTION}' mit exakt einem Label BUY|SELL|WATCH ergaenzen",
            )
        )
        return
    if label != expected:
        findings.append(
            _finding(
                "critical",
                f"Gesamt-Empfehlung '{label}' weicht vom deterministischen Label ab",
                f"deterministic_summary.recommendation.label = {expected}, Draft nennt {label}",
                f"Label '{expected}' 1:1 uebernehmen (deterministisch abgeleitet, nicht LLM-gewaehlt)",
            )
        )


def _verify_position_actions(facts_package: dict, text: str, findings: list[dict]) -> None:
    """Top-3-Positionsvorschlaege (Plan §6a): Aktion + ISIN 1:1 aus dem
    Faktenpaket; keine bloesse HOLD-Ausgabe; konkrete ISIN Pflicht."""
    actions = _position_actions(facts_package)
    if not actions:
        return
    if len(actions) > 3:
        findings.append(
            _finding(
                "critical",
                "Mehr als 3 Positionsvorschlaege",
                f"position_actions enthaelt {len(actions)} Vorschlaege",
                "Maximal 3 positionsbezogene Aenderungsvorschlaege ausgeben",
            )
        )
    for action in actions:
        if not isinstance(action, dict):
            continue
        act = action.get("action")
        isin = action.get("isin", "")
        if act not in _POSITION_ACTIONS:
            findings.append(
                _finding(
                    "critical",
                    f"Unzulaessige Aktion '{act}'",
                    f"position_actions[].action = {act!r}",
                    f"Erlaubte Aktionen: {', '.join(_POSITION_ACTIONS)}",
                )
            )
        if not isin or not re.fullmatch(r"[A-Z]{2}[A-Z0-9]{9}\d", str(isin)):
            findings.append(
                _finding(
                    "critical",
                    "Positionsvorschlag ohne gueltige ISIN",
                    f"position_actions[] = {action!r}",
                    "Konkrete ISIN im Positionsvorschlag nennen",
                )
            )


def _verify_glm_ideas(facts_package: dict, text: str, findings: list[dict]) -> None:
    """Neukaufideen (Plan §6a): max. 2, als Idee kenntlich, mit Investmentthese
    UND Risiko-Skizze sowie mind. 2 unabhaengigen Quellen; bei Unsicherheit
    keine Idee.

    Erkennung ist NICHT auf das Wort "Idee" angewiesen: eine konkrete
    Neukaufidee liegt vor, wenn (a) ein Neukauf-/Kaufideen-/Idee-Marker, (b) eine
    ISIN, die NICHT im Portfolio liegt (unbekanntes Wertpapier) oder (c) eine
    Neukauf-Struktur ("Neu-Kandidat", "Kaufkandidat", "Neu-Investment") in der
    Sektion "Entscheidungsrelevante Punkte" auftaucht. Erlaubte Texte ohne
    Neukauf-Bezug (z.B. "Thesen-Idee", "keine Idee") werden nicht blockiert.
    """
    ideas_section = _extract_section(text, "## Entscheidungsrelevante Punkte") or ""
    if not ideas_section.strip():
        return

    # Starke Marker (Neukauf-/Kauf-Kontext), die keine generische "Idee" sind.
    strong_markers = re.findall(r"(?i)(neukauf|kaufidee|kaufkandidat|neu-kandidat|neu-investment)", ideas_section)
    # ISINs in der Sektion, die NICHT im Portfolio liegen (unbekannte Wertpapiere).
    portfolio_isins = {
        str(h.get("isin", ""))
        for h in facts_package.get("portfolio", {}).get("holdings", [])
        if isinstance(h, dict) and h.get("isin")
    }
    unknown_isins = sorted(i for i in _extract_isins(ideas_section) if i not in portfolio_isins)
    # Generisches "Idee" nur in Neukauf-Naehe (Kontext: Idee + These/Quellen/ISIN).
    idea_word = re.search(r"(?i)\bidee\b", ideas_section)

    has_idea = bool(strong_markers or unknown_isins or (idea_word and (strong_markers or unknown_isins)))
    if not has_idea:
        return

    # Mind. 2 unabhaengige Quellen (Yahoo Finance zaehlt nicht automatisch).
    news = facts_package.get("news", [])
    if not isinstance(news, list):
        news = []
    eval_result = _news_independence(news)
    if not eval_result["independent"]:
        findings.append(
            _finding(
                "major",
                "Neukaufidee ohne unabhaengige Quellenbasis",
                f"News-Publisher: {eval_result['publishers']}, erfordert mind. {eval_result['min_sources']}",
                "Neukaufidee nur mit mind. 2 unabhaengigen Publishern ausgeben oder entfernen",
            )
        )

    # Ideen-Blocks zaehlen: explizite Ideen-Marker ODER unbekannte ISINs (je ISIN eine Idee).
    idea_blocks = re.findall(r"(?im)^\s*[-*]\s*(?:Neukauf-?Idee|Kaufidee|Idee|Kaufkandidat)\s*\d*\s*:", ideas_section)
    idea_count = max(len(idea_blocks), len(unknown_isins))
    if idea_count > 2:
        findings.append(
            _finding(
                "critical",
                "Mehr als 2 Neukaufideen",
                f"{idea_count} Ideen-Blocks/ISINs in der Sektion",
                "Maximal 2 Neukaufideen (als Idee kenntlich) ausgeben",
            )
        )

    # Pro Idee: Investmentthese (These/Begründung) UND Risiko-Skizze (Risiko/
    # Gegenargument/Gefahr) erforderlich — pro Absatz geprueft, damit eine
    # These einer anderen Idee nicht fuer diese zaehlt.
    paragraphs = _paragraphs(ideas_section)
    idea_paragraphs = [
        p
        for p in paragraphs
        if re.search(r"(?i)(neukauf|kaufidee|kaufkandidat|neu-kandidat|neu-investment|\bidee\b)", p)
        or _extract_isins(p)
    ]
    for paragraph in idea_paragraphs:
        has_thesis = _has_marker(paragraph, _REASON_MARKERS) or re.search(r"(?i)\bthese\b", paragraph)
        has_risk = _has_marker(paragraph, _COUNTER_MARKERS) or re.search(r"(?i)gefahr", paragraph)
        if not has_thesis:
            findings.append(
                _finding(
                    "major",
                    "Neukaufidee ohne Investmentthese",
                    f"Ideen-Block ohne These/Begründung: {paragraph[:120]!r}",
                    "Pro Neukaufidee eine Investmentthese nennen",
                )
            )
        if not has_risk:
            findings.append(
                _finding(
                    "major",
                    "Neukaufidee ohne Risiko-Skizze",
                    f"Ideen-Block ohne Risiko/Gegenargument/Gefahr: {paragraph[:120]!r}",
                    "Pro Neukaufidee eine kompakte Risiko-Skizze (Risiko/Gegenargument/Gefahr) nennen",
                )
            )


def _news_independence(news: list) -> dict:
    """Quellen-/Duplikatpruefung (Plan §6a): unabhaengig = mind. 2 verschiedene
    Publisher (Yahoo Finance zaehlt nicht automatisch als unabhaengig)."""
    from scripts.filter_news import evaluate_news_independence

    return evaluate_news_independence(news)


def _summary_watchlist_scores(summary: dict) -> list[float]:
    """Score-Ganzzahlen der Watchlist-/Sell-Signale (Phase 5, Zahlen-Allowlist).

    Nur die deterministischen Score-Ganzzahlen der Signal-Objekte (keine
    Fundamentaldaten). Die Renderer-Sektion zeigt Scores als "+2"/"-1" — der
    Draft darf genau diese Ganzzahlen referenzieren.
    """
    values: list[float] = []
    for key in ("watchlist_signals", "satellite_sell_signals"):
        for signal in summary.get(key, []) if isinstance(summary.get(key), list) else []:
            if not isinstance(signal, dict):
                continue
            score = signal.get("score")
            if isinstance(score, (int, float)) and not isinstance(score, bool):
                values.append(float(score))
    return values


def _verify_signal_sections(facts_package: dict, text: str, findings: list[dict]) -> None:
    """Phase 5: kurze Signal-Sektionen + Fundamentaldaten-Disclaimer.

    Prueft, ob alle Sektionen des kurzen Renderer-Contracts (DRAFT_SECTIONS:
    ``## Kurzlage``, ``## Datenqualität``, ``## Sell-/Reduce-Signale (bestehende
    Satellites)``, ``## Watchlist-Signale``, ``## Nächster Schritt``) im Text
    vorhanden sind und ob die beiden Signal-Sektionen den Fundamentaldaten-
    Disclaimer tragen (blockt, wenn fehlt — fail-closed). Naechster-Schritt-
    Sektion wird nur auf Vorhandensein geprueft (determinierter Text ohne
    Zahlen).
    """
    for section in DRAFT_SECTIONS:
        if not re.search(rf"^{re.escape(section)}$", text, re.MULTILINE):
            findings.append(
                _finding(
                    "critical",
                    f"Fehlende Sektion {section}",
                    f"Draft enthaelt keine Zeile '{section}'",
                    f"Sektion {section} als eigene Markdown-Ueberschrift ergaenzen",
                )
            )
    # Fundamentaldaten-Disclaimer nur in den Signal-Sektionen (nicht naechster
    # Schritt) — case-insensitive geprueft (Umlaute, Gross-/Kleinschreibung).
    # Der Renderer haengt den Disclaimer ohne nachfolgenden Punkt an die
    # Signal-Zeilen an; die Pruefung bleibt ein Substring-Match.
    disclaimer_lower = FUNDAMENTALS_DISCLAIMER.lower()
    for section in (SELL_SIGNALS_SECTION, WATCHLIST_SIGNALS_SECTION):
        content = _section_content(_extract_section(text, section)) or ""
        if disclaimer_lower not in content.lower():
            findings.append(
                _finding(
                    "critical",
                    f"Fundamentaldaten-Disclaimer fehlt in {section}",
                    "Signal-Sektion ohne Hinweis auf fehlende Fundamentaldaten",
                    "Hinweis ergaenzen: Fundamentaldaten (Umsatz, Gewinn, Cashflow, Verschuldung, Bewertung) sind nicht automatisch verfügbar und fließen nicht in das Signal ein",
                )
            )


def _extract_numbers(text: str) -> list[float]:
    """Alle Prozentzahlen im Text (Punkt ODER Komma als Dezimaltrenner).

    Post-Live-Fix P0.3: Komma-Dezimalen (z.B. "24,8%") werden mitgeprueft,
    statt die Zahlen-Pruefung zu umgehen. Als float normalisiert.
    """
    matches = re.findall(r"(\d+(?:[.,]\d+)?)\s*%", text)
    return [float(m.replace(",", ".")) for m in matches]


def _extract_tickers(text: str) -> set[str]:
    common = {
        "LLM", "API", "HTTP", "USD", "EUR", "ETF", "MVP", "RSS", "Q4", "AI", "OK",
        "USA", "IPO", "CEO", "GDP", "CPI", "EPS", "NASDAQ", "NYSE", "CNBC", "DAX",
        "S&P", "MSCI", "FTSE",
        # Gesamt-Empfehlungs-Labels (Plan §6a) — keine Ticker.
        "BUY", "SELL", "WATCH",
        # Deterministische Signal-Labels (Phase 5, final_briefing._SIGNAL_LABELS):
        # "NO SIGNAL" (rendered z.B. als "Keine Watchlist-Signale (NO SIGNAL
        # für alle Positionen).") ist ein Signal-Status, kein Ticker.
        "NO", "SIGNAL",
        # Weitere Signal-Labels (final_briefing._SIGNAL_LABELS): AVOID/REDUCE
        # sind Signal-Status gerenderter Sell-/Watchlist-Signale, keine Ticker.
        "AVOID", "REDUCE",
        # SUSE SE (LU2722255754, illiquide Legacy-Position): gerenderte
        # Signal-/Legacy-Texte nennen "SUSE" als Holdingnamen, kein Ticker.
        "SUSE",
        # Finanz-/Rechtsform-Token — keine Ticker (geschlossene Blocklist).
        "SE", "ISIN", "WKN", "AG", "KG", "SA", "NV", "BV",
        "PLC", "LTD", "INC", "CORP", "CO",
    }
    candidates = set(re.findall(r"\b[A-Z]{2,5}(?:[-\.]?[A-Z]+)?\b", text))
    return candidates - common


def _extract_isins(text: str) -> set[str]:
    return set(re.findall(r"[A-Z]{2}[A-Z0-9]{9}\d", text))


def _portfolio_name_words(holdings: list) -> set[str]:
    """Tokens aus echten Holding-Namen (Allowlist für den Ticker-Check).

    Live-Fehler: Holdings stehen im Portfolio nur über ISINs (kein Ticker-
    Feld). Echte Namens-Bestandteile wie 'SRI' (iShares MSCI World SRI),
    'IMI' (iShares Core MSCI Emerging Markets IMI) oder 'ADR' (BioNTech ADR)
    sahen für ``_extract_tickers`` wie erfundene Ticker aus und blockierten
    den Versand als major. Diese Tokens sind Teil eines echten Portfolio-
    Holdingnamens und werden freigegeben, wenn die zugehörige Holding über
    eine Portfolio-ISIN vorhanden ist. Unbekannte Ticker/ISINs, die weder
    Portfolio-Ticker/ISIN noch Bestandteil eines Holdingnamens oder eines
    referenzierten News-Titels sind, bleiben major (fail-closed).
    """
    words: set[str] = set()
    for h in holdings:
        if not isinstance(h, dict):
            continue
        name = h.get("name")
        if not name or not h.get("isin"):
            continue  # nur Holdings mit Portfolio-ISIN zählen als echte Bestände
        words.update(word.upper() for word in re.findall(r"[A-Za-z]{2,}", str(name)))
    return words


def _summary_numbers_pct(summary: dict) -> list[float]:
    """All percentage-representable numbers of deterministic_summary (as %)."""
    values: list[float] = []
    for key in ("core_ratio", "max_position_weight", "max_sector_ratio", "drift", "turnover_ratio"):
        value = summary.get(key)
        if isinstance(value, (int, float)):
            values.append(round(value * 100, 1))
    return values


def _strategy_thresholds_pct(strategy_thresholds: dict) -> list[float]:
    """Strategy limit percentages from the facts package (already in %, e.g. 75.0).

    Non-positive values are skipped: without real thresholds the allowlist
    stays empty and unknown numbers remain critical (fail-closed).
    """
    if not isinstance(strategy_thresholds, dict):
        return []
    values: list[float] = []
    for key in _STRATEGY_THRESHOLD_KEYS:
        value = strategy_thresholds.get(key)
        if isinstance(value, (int, float)) and value > 0:
            values.append(round(value, 1))
    return values


def _raw_check_name_violations(text: str) -> list[str]:
    """Rohe bekannte snake_case-Checknamen im Text (Original-Schreibweise).

    "drift" wird nur in Kleinschreibung geprueft — "Drift" ist die erlaubte
    deutsche Lesart. Die uebrigen Namen sind unverwechselbare technische
    Bezeichner (case-insensitive, z.B. auch "Core_Satellite").
    """
    found: list[str] = []
    for name in _RAW_CHECK_NAMES_CI:
        match = re.search(rf"\b{re.escape(name)}\b", text, re.IGNORECASE)
        if match:
            found.append(match.group(0))
    for name in _RAW_CHECK_NAMES_CS:
        match = re.search(rf"\b{re.escape(name)}\b", text)
        if match:
            found.append(match.group(0))
    return found


def _forbidden_style_terms(text: str) -> list[str]:
    """Verbotene englische Fachbegriffe im Text (case-insensitive)."""
    found: list[str] = []
    for term in _FORBIDDEN_STYLE_TERMS_CI:
        match = re.search(rf"\b{re.escape(term)}\b", text, re.IGNORECASE)
        if match:
            found.append(match.group(0))
    return found


def _extract_section(text: str, heading: str) -> str | None:
    """Exakter Sektions-Substring (Ueberschrift bis zur naechsten '## ').

    Kein Substring-Match: nur eine eigene Zeile '## <Sektion>' zaehlt
    (identisch zur DRAFT_SECTIONS-Pruefung). Fehlt die Sektion, wird
    None zurueckgegeben.
    """
    match = re.search(rf"^{re.escape(heading)}$", text, re.MULTILINE)
    if match is None:
        return None
    nxt = re.search(r"^## ", text[match.end():], re.MULTILINE)
    end = match.end() + nxt.start() if nxt else len(text)
    return text[match.start():end]


def _section_content(section: str | None) -> str | None:
    """Sektionsinhalt ohne Ueberschriftszeile (None wenn Sektion fehlt)."""
    if section is None:
        return None
    newline = section.find("\n")
    if newline == -1:
        return ""
    return section[newline + 1:]


def _has_relevant_changes(changes: object) -> bool:
    """Relevante Portfolio-/Transaktions-Veränderungen seit dem letzten Lauf.

    Ein Diff zaehlt nur bei vorhandenem Vorgaenger (has_previous) und wenn
    sich tatsaechlich etwas geaendert hat: added/removed/changed Positionen,
    Gesamtwert-Delta ungleich 0 oder neue/entfernte Transaktionen. Ohne
    Vorgaenger (Erstlauf) werden keine erfundenen Aenderungen gemeldet.
    """
    if not isinstance(changes, dict):
        return False
    if not changes.get("has_previous"):
        return False
    positions = changes.get("positions")
    if isinstance(positions, dict) and (
        positions.get("added") or positions.get("removed") or positions.get("changed")
    ):
        return True
    totals = changes.get("totals")
    if isinstance(totals, dict) and totals.get("delta_eur") not in (None, 0):
        return True
    txns = changes.get("transactions")
    if isinstance(txns, dict) and (txns.get("added_count") or txns.get("removed_count")):
        return True
    return False


def compute_triggers(facts_package: dict) -> dict:
    """Deterministische Trigger-Flags fuer den Optionen-Contract (Plan Phase 3).

    Bevorzugt das vom Faktenpaket mitgelieferte ``triggers``-Feld
    (facts.build_facts_package -> facts.compute_triggers); faellt bei
    fehlendem/unvollstaendigem Feld defensiv auf dieselbe deterministische
    Logik aus den Fakten zurueck, damit verify/Review/Revise unabhaengig vom
    Feld stabil arbeiten (z.B. in Tests oder vor Integration der Fakten-Lane).

    Datenqualitaet hat Vorrang: bei ``data_quality.status != "ok"`` werden
    Portfolio-Grenzverletzungen nicht als Trigger gewertet — Optionen werden
    dann nicht generiert (Randfall 'Datenqualitaet unzureichend'). Eine
    Strategieaenderung bleibt davon unberuehrt ein eigener Trigger.
    """
    explicit = facts_package.get("triggers")
    if isinstance(explicit, dict) and "has_any_trigger" in explicit:
        return {
            "has_boundary_violation": bool(explicit.get("has_boundary_violation", False)),
            "has_relevant_changes": bool(explicit.get("has_relevant_changes", False)),
            "has_thesis_news": bool(explicit.get("has_thesis_news", False)),
            "has_strategy_change": bool(explicit.get("has_strategy_change", False)),
            "has_any_trigger": bool(explicit.get("has_any_trigger", False)),
        }
    summary = facts_package.get("deterministic_summary", {}) or {}
    data_quality = facts_package.get("data_quality") or {}
    data_quality_ok = data_quality.get("status") in (None, "", "ok")
    has_boundary_violation = bool(
        data_quality_ok and (summary.get("red_checks") or summary.get("yellow_checks"))
    )
    has_relevant_changes = _has_relevant_changes(facts_package.get("changes"))
    news = facts_package.get("news") or []
    has_thesis_news = any(isinstance(n, dict) and n.get("thesis_relevant") for n in news)
    strategy_diff = facts_package.get("strategy_diff")
    if strategy_diff is None and isinstance(facts_package.get("changes"), dict):
        strategy_diff = facts_package["changes"].get("strategy")
    has_strategy_change = bool(
        isinstance(strategy_diff, dict) and strategy_diff.get("has_changed", False)
    )
    has_any_trigger = any(
        (has_boundary_violation, has_relevant_changes, has_thesis_news, has_strategy_change)
    )
    return {
        "has_boundary_violation": has_boundary_violation,
        "has_relevant_changes": has_relevant_changes,
        "has_thesis_news": has_thesis_news,
        "has_strategy_change": has_strategy_change,
        "has_any_trigger": has_any_trigger,
    }


def _find_option_terms(section_content: str) -> list[str]:
    """Optionen-Marker (halten/reduzieren/aufstocken) im Sektionsinhalt."""
    found: list[str] = []
    for term in _OPTION_TERMS:
        if re.search(rf"\b{re.escape(term)}\b", section_content, re.IGNORECASE):
            found.append(term)
    return found


def _paragraphs(content: str) -> list[str]:
    """Sektionsinhalt in Absaetze teilen (durch Leerzeilen getrennt).

    Basis der Pro-Option-Pruefung (Post-Live-Fix P0.3): Begruendung und
    Gegenargument muessen im selben Absatz wie die Option stehen — ein
    Marker an anderer Stelle der Sektion zaehlt nicht fuer diese Option.
    """
    return [p.strip() for p in re.split(r"\n\s*\n", content) if p.strip()]


def _has_marker(content: str, markers: tuple[str, ...]) -> bool:
    """Mindestens ein Marker (Wortgrenze, case-insensitive) im Text."""
    return any(
        re.search(rf"\b{re.escape(marker)}\b", content, re.IGNORECASE)
        for marker in markers
    )


def verify_draft(facts_package: dict, draft: str) -> list[dict]:
    """Stage-3 gate: deterministic draft checks against the facts package.

    Returns a list of findings (contract like review findings). Raises
    LLMError if the draft looks like an LLM error string. Does not mutate
    any input.
    """
    findings: list[dict] = []
    text = draft

    # 0. LLM-Fehlerstring-Erkennung (Defense-in-Depth, Plan §4.4)
    for marker in ERROR_MARKERS:
        if marker in text:
            raise LLMError(f"Draft sieht nach LLM-Fehlertext aus (Marker '{marker}').")

    # 1. Sektionen — zeilenverankert und exakt (^## ...$ mit re.MULTILINE), kein
    #    Substring-Match. Nur eine eigene Zeile "## <Sektion>" zaehlt als vorhanden;
    #    Varianten mit Doppelpunkt, Fettdruck oder Inline-Zusatz
    #    ("## Kurzlage: ...", "## **Kurzlage**") gelten als fehlend.
    for section in DRAFT_SECTIONS:
        if not re.search(rf"^{re.escape(section)}$", text, re.MULTILINE):
            findings.append(
                _finding(
                    "critical",
                    f"Fehlende Sektion {section}",
                    f"Draft enthaelt keine Zeile '{section}'",
                    f"Sektion {section} als eigene Markdown-Ueberschrift ergaenzen",
                )
            )

    # 1a. Briefing-Schnittstelle (Plan §6a): Ampeln (7 Kategorien), Gesamt-
    #     Empfehlung (## Empfehlung, 1:1), Positionsvorschlaege, Neukaufideen.
    _verify_traffic_lights(facts_package, text, findings)
    _verify_recommendation(facts_package, text, findings)
    _verify_position_actions(facts_package, text, findings)
    _verify_glm_ideas(facts_package, text, findings)
    # 1b. Phase 5: kurze Signal-Sektionen + Fundamentaldaten-Disclaimer.
    _verify_signal_sections(facts_package, text, findings)

    # 2. Status-Konformitaet — nur in der KURZLAGE-Sektion (Plan Phase 2.6):
    #    Konformitaetsphrasen ("Alle Grenzen eingehalten"/"Ruhige Woche"/
    #    "Kein Handlungsbedarf") sind nur ohne rote/gelbe Checks erlaubt;
    #    "Kein Handlungsbedarf" zusaetzlich nur ohne jeden Optionen-Trigger.
    summary = facts_package.get("deterministic_summary", {})
    red_checks = summary.get("red_checks") or []
    yellow_checks = summary.get("yellow_checks") or []
    kurzlage = _section_content(_extract_section(text, _KURZLAGE_SECTION)) or ""
    kurzlage_lower = kurzlage.lower()
    found_markers = [m for m in CONFORMITY_MARKERS if m in kurzlage_lower]
    if found_markers and (red_checks or yellow_checks):
        findings.append(
            _finding(
                "critical",
                "Konformitaetsphrase trotz roter/gelber Checks",
                f"red_checks={red_checks}, yellow_checks={yellow_checks}, Kurzlage nennt Konformitaetsphrase",
                "Rote/gelbe Checks namentlich benennen, Konformitaetsphrase entfernen",
            )
        )
    elif "kein handlungsbedarf" in kurzlage_lower and compute_triggers(facts_package)["has_any_trigger"]:
        findings.append(
            _finding(
                "critical",
                "Konformitaetsphrase 'Kein Handlungsbedarf' trotz Trigger",
                f"Kurzlage nennt 'Kein Handlungsbedarf', Trigger: {compute_triggers(facts_package)}",
                "'Kein Handlungsbedarf' nur ohne jeden Optionen-Trigger verwenden",
            )
        )

    # 2a. Naechster-Schritt-Konformitaet (Phase 5): "Keine Aktion erforderlich"
    #     in der "## Naechster Schritt"-Sektion blockt als critical, wenn das
    #     Faktenpaket KONKRETE Handlungssignale ausweist (deterministischer
    #     Renderer-Contract final_briefing._section_naechster_schritt):
    #     - nicht-excluded SELL/REDUCE in satellite_sell_signals,
    #     - BUY in watchlist_signals,
    #     - nicht-leere position_actions.
    #     Rote/gelbe Checks ALLEIN sind KEINE konkrete Handlungsempfehlung —
    #     sie blocken die no-action-Phrase nicht (nur die Kurzlage-
    #     Konformitaetsphrase in Schritt 2 reagiert auf rote/gelbe Checks).
    #     Der Renderer wuerde in diesen Faellen SELL/REDUCE/BUY/Vorschlags-
    #     Text erzeugen — ein Draft mit no-action-Phrase trotzdem ist ein
    #     Widerspruch (fail-closed).
    naechster = _section_content(_extract_section(text, NEXT_STEP_SECTION)) or ""
    naechster_lower = naechster.lower()
    if "keine aktion erforderlich" in naechster_lower and _naechster_schritt_handlungsbedarf(
        facts_package
    ):
        findings.append(
            _finding(
                "critical",
                "'Keine Aktion erforderlich' trotz Handlungsbedarf",
                f"position_actions={len(_position_actions(facts_package))}, "
                f"sell_signals={_non_excluded_action_signals(facts_package, ('SELL', 'REDUCE'))}, "
                f"buy_signals={_non_excluded_action_signals(facts_package, ('BUY',))}",
                "Nächster Schritt konkret adressieren (SELL/REDUCE/BUY/Vorschlag)",
            )
        )

    # 3. Stil-Gates (nicht-lockernd): rohe snake_case-Checknamen blocken als
    #    critical (technische Bezeichner duerfen nie im Markdown stehen),
    #    verbotene Fachbegriffe als major. Deutsche Lesarten sind erlaubt
    #    (z.B. "Drift", "Core-/Satelliten-Aufteilung", "Umschlag").
    for token in _raw_check_name_violations(text):
        findings.append(
            _finding(
                "critical",
                f"Roher Checkname '{token}' im Briefing",
                f"Draft enthaelt den technischen Bezeichner '{token}'",
                "Deutsche Lesart verwenden (z.B. 'Core-/Satelliten-Aufteilung' statt 'core_satellite')",
            )
        )
    for token in _forbidden_style_terms(text):
        findings.append(
            _finding(
                "major",
                f"Verbotener Stil-Begriff '{token}' im Briefing",
                f"Draft enthaelt '{token}'",
                "Durch verstaendliche deutsche Formulierung ersetzen (z.B. 'aktuelle Ausbaustufe' statt 'MVP')",
            )
        )

    # 4. Zahlen nur aus deterministic_summary + strategy_thresholds_pct
    #    (Post-Live-Fix P0.3: 1:1-Match auf 1 Dezimalstelle — identisch zur
    #    ZULÄSSIGE-ZAHLEN-Liste im Draft-Prompt. Eine fruehere ±0.5pp-Toleranz
    #    liess abweichende Werte durch; abweichende Zahlen blocken jetzt
    #    critical, egal wie nah sie an einem erlaubten Wert liegen).
    thresholds = facts_package.get("strategy_thresholds_pct", {})
    allowed = _summary_numbers_pct(summary) + _strategy_thresholds_pct(thresholds)
    allowed += _summary_watchlist_scores(summary)  # Phase 5: Signal-Score-Ganzzahlen
    allowed_formatted = sorted({f"{value:.1f}" for value in allowed})
    for num in _extract_numbers(text):
        if f"{num:.1f}" not in allowed_formatted:
            findings.append(
                _finding(
                    "critical",
                    f"Zahl {num}% passt nicht 1:1 zu deterministic_summary",
                    f"Draft nennt {num}%, erlaubt (1:1): {', '.join(allowed_formatted)}",
                    "Zahl unveraendert (identischer Wert und Schreibweise, 1 Dezimalstelle) aus der ZULÄSSIGE-ZAHLEN-Liste uebernehmen oder entfernen",
                )
            )

    # 5. Ticker/ISIN im Portfolio
    holdings = facts_package.get("portfolio", {}).get("holdings", [])
    portfolio_tickers = {h.get("ticker", "") for h in holdings if h.get("ticker")}
    portfolio_isins = {h.get("isin", "") for h in holdings if h.get("isin")}
    # Signal-ISINs (Watchlist-Kandidaten + Satellite-SELLs) sind legitim im
    # Draft — 1:1 aus deterministic_summary, per Renderer (final_briefing
    # _section_watchlist_signals/_section_sell_reduce_signals) gerendert.
    signal_isins = {
        str(s.get("isin", ""))
        for key in ("watchlist_signals", "satellite_sell_signals")
        for s in (summary.get(key, []) if isinstance(summary.get(key), list) else [])
        if isinstance(s, dict) and s.get("isin")
    }
    # Tokens aus echten Holding-Namen (Allowlist): 'SRI'/'IMI'/'ADR' etc. sind
    # Bestandteil eines Portfolio-Holdingnamens, keine erfundenen Ticker.
    holding_name_words = _portfolio_name_words(holdings)
    # News-Titel aus dem Faktenpaket (Bestand + gezielte Neukauf-Recherche):
    # deren Woerter/Tokens sind referenzierte Titel, keine erfundenen Ticker.
    news_titles = {
        str(n.get("title", ""))
        for n in facts_package.get("news", [])
        if isinstance(n, dict) and n.get("title")
    }
    news_words = set()
    for title in news_titles:
        news_words.update(word.upper() for word in re.findall(r"[A-Za-z]{2,}", title))
    for ticker in _extract_tickers(text):
        if ticker in news_words or ticker in holding_name_words:
            continue  # Teil eines referenzierten News-Titels oder echten Holdingnamens
        if ticker not in portfolio_tickers and ticker not in portfolio_isins:
            findings.append(
                _finding(
                    "major",
                    f"Ticker/ISIN {ticker} nicht im Portfolio",
                    f"Draft erwaehnt {ticker}, Portfolio kennt ihn nicht",
                    f"{ticker} entfernen oder durch Portfolio-Bestand ersetzen",
                )
            )
    for isin in _extract_isins(text):
        if isin in portfolio_isins or isin in signal_isins:
            continue
        findings.append(
            _finding(
                "info",
                f"ISIN nicht gefunden: {isin}",
                f"Draft erwaehnt {isin}, Portfolio/Signale kennen ihn nicht",
                f"{isin} pruefen oder aus Portfolio-Daten ergaenzen",
            )
        )

    # 6. News-Referenz (minor — blockiert Versand nicht)
    news = facts_package.get("news", [])
    mentioned_titles = {entry["title"] for entry in news if entry.get("title")}
    if mentioned_titles and not any(t.lower() in text.lower() for t in mentioned_titles):
        findings.append(
            _finding(
                "minor",
                "Keine der gefilterten News im Draft referenziert",
                f"{len(mentioned_titles)} News im Faktenpaket, keine referenziert",
                "Mindestens eine News referenzieren",
            )
        )

    # 7. Optionen-Contract (Plan Phase 2.4/2.8 + Post-Live-Fix P0.3):
    #    Optionen nur bei deterministischen Triggern; pro Option(s-Block)
    #    Begruendung + Gegenargument/Risiko im SELBEN Absatz (Blank-Zeile-
    #    getrennt) — ein Marker einer anderen Option zaehlt nicht; imperative
    #    Kauf-/Verkaufsanweisung bleibt critical; sonstige Empfehlungen
    #    ausserhalb der Optionen-Sektion bleiben critical.
    triggers = compute_triggers(facts_package)
    options_section = _extract_section(text, _OPTIONS_SECTION)
    options_content = _section_content(options_section) or ""
    option_terms = _find_option_terms(options_content)
    if option_terms and not triggers["has_any_trigger"]:
        findings.append(
            _finding(
                "major",
                "Option ohne Trigger",
                f"Optionen {', '.join(option_terms)} im Draft, Trigger: {triggers}",
                "Optionen nur bei Triggern generieren oder Sektion auf 'keine entscheidungsrelevanten Punkte' setzen",
            )
        )
    for paragraph in _paragraphs(options_content):
        terms = _find_option_terms(paragraph)
        if not terms:
            continue
        if not _has_marker(paragraph, _REASON_MARKERS):
            findings.append(
                _finding(
                    "major",
                    "Option ohne Begründung",
                    f"Options-Block {', '.join(terms)} ohne Begründung im Absatz",
                    "Pro Option eine Begründung (Evidenz aus dem Faktenpaket) im selben Block ergänzen",
                )
            )
        if not _has_marker(paragraph, _COUNTER_MARKERS):
            findings.append(
                _finding(
                    "major",
                    "Option ohne Gegenargument/Risiko",
                    f"Options-Block {', '.join(terms)} ohne Gegenargument/Risiko im Absatz",
                    "Pro Option ein konkretes Gegenargument/Risiko im selben Block ergänzen",
                )
            )
    # Imperative Kauf-/Verkaufsanweisung — critical, auch innerhalb der Sektion.
    for term in _IMPERATIVE_TERMS:
        if re.search(rf"\b{re.escape(term)}\b", text, re.IGNORECASE):
            findings.append(
                _finding(
                    "critical",
                    f"Imperative Kauf-/Verkaufsanweisung '{term}'",
                    f"Draft enthaelt die imperative Anweisung '{term}'",
                    "Optionen-Raum aufspannen (halten/reduzieren/aufstocken mit Begründung und Gegenargument), keine finale Anweisung",
                )
            )
            break
    # Sonstige Empfehlungen ausserhalb der Optionen-Sektion — critical.
    outside = text
    if options_section is not None:
        outside = text.replace(options_section, "", 1)
    for term in _RECOMMENDATION_TERMS:
        if re.search(rf"\b{re.escape(term)}\b", outside, re.IGNORECASE):
            findings.append(
                _finding(
                    "critical",
                    f"Handlungsempfehlung außerhalb der Optionen-Sektion ('{term}')",
                    f"Empfehlung '{term}' ausserhalb von '{_OPTIONS_SECTION}'",
                    "Empfehlung entfernen oder als Option (halten/reduzieren/aufstocken) mit Begründung und Gegenargument in der Sektion formulieren",
                )
            )
            break

    return findings


class GateDecision:
    """Verdict of the send gate (stage 6)."""

    def __init__(self, allow_send: bool, reason: str):
        self.allow_send = allow_send
        self.reason = reason

    def __repr__(self) -> str:
        return f"GateDecision(allow_send={self.allow_send}, reason={self.reason!r})"

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, GateDecision):
            return NotImplemented
        return self.allow_send == other.allow_send and self.reason == other.reason


def final_gate(draft_verification: list[dict], review: dict) -> GateDecision:
    """Stage-6 send gate: combine deterministic findings + LLM review.

    Fail-closed: any critical/major finding (verify or review) blocks the
    send; ``overall_verdict`` ``block`` blocks as well. ``revise`` blocks
    here because the orchestrator's revision loop (max MAX_REVISIONS) has
    already run — a verdict still ``revise`` at this point is unresolved.
    ``info``/``minor`` never block. Does not mutate any input.
    """
    blocking = [
        f for f in draft_verification if f.get("severity") in ("critical", "major")
    ]
    review_findings = review.get("findings", []) if isinstance(review, dict) else []
    blocking += [f for f in review_findings if f.get("severity") in ("critical", "major")]

    if blocking:
        reasons = ", ".join(f.get("issue", "?") for f in blocking[:3])
        return GateDecision(False, f"Blockierende Findings: {reasons}")

    verdict = review.get("overall_verdict") if isinstance(review, dict) else None
    if verdict not in ("pass", "revise", "block"):
        return GateDecision(False, "overall_verdict fehlt oder ungueltig")
    if verdict == "revise":
        return GateDecision(False, "overall_verdict=revise (nach MAX_REVISIONS ungeloest)")
    if verdict == "block":
        return GateDecision(False, "overall_verdict=block")
    return GateDecision(True, "pass")


def verify_briefing(
    briefing_markdown: str,
    analysis: dict,
    news: list,
    portfolio: dict,
) -> list[str]:
    """Return list of warnings (empty if OK)."""
    warnings: list[str] = []
    text = briefing_markdown

    # 1. Number plausibility
    reported_numbers = _extract_numbers(text)
    positions = analysis.get("checks", {}).get("positions", {}).get("positions", [])
    portfolio_weights = [round(p["weight"] * 100, 1) for p in positions]
    for num in reported_numbers:
        if num > 99 and not any(num - 1 <= w <= num + 1 for w in portfolio_weights):
            warnings.append(f"Zahl {num}% im Briefing passt nicht zu Portfolio-Weights")
            break

    # 2. Ticker/ISIN existence
    mentioned_tickers = _extract_tickers(text)
    mentioned_isins = _extract_isins(text)
    portfolio_tickers = set()
    portfolio_isins = set()
    for h in portfolio.get("holdings", []):
        if h.get("ticker"):
            portfolio_tickers.add(h.get("ticker", ""))
        portfolio_isins.add(h.get("isin", ""))
    holding_name_words = _portfolio_name_words(portfolio.get("holdings", []))
    for ticker in mentioned_tickers:
        if ticker in holding_name_words:
            continue  # Bestandteil eines echten Portfolio-Holdingnamens
        if ticker not in portfolio_tickers and ticker not in portfolio_isins:
            warnings.append(f"Ticker/ISIN {ticker} im Briefing nicht im Portfolio")
    # ISINs: Portfolio-ISINs ok; unbekannte ISINs sind non-blocking
    # Datenqualitaets-Hinweise (konsistent zu verify_draft Schritt 5, info).
    for isin in mentioned_isins:
        if isin not in portfolio_isins:
            warnings.append(f"ISIN nicht gefunden: {isin}")

    # 3. News references
    mentioned_titles = {entry["title"] for entry in news if entry.get("title")}
    referenced_titles = {t for t in mentioned_titles if t.lower() in text.lower()}
    if mentioned_titles and not referenced_titles:
        warnings.append("Keine der gefilterten News im Briefing referenziert")

    return warnings


if __name__ == "__main__":
    sample = "Apple (AAPL) ist mit 24.8% im Portfolio. Reuters meldet..."
    portfolio = {"holdings": [{"ticker": "AAPL", "isin": "US0378331005"}]}
    analysis = {"checks": {"positions": {"positions": [{"weight": 0.248}]}}}
    news = [{"title": "Reuters meldet"}]
    print(verify_briefing(sample, analysis, news, portfolio))
