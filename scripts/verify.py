"""Post-LLM verification: numbers, tickers, news references + send gate."""
from __future__ import annotations

import re
from pathlib import Path

from scripts.llm_briefing import LLMError

ROOT = Path(__file__).resolve().parent.parent

# Feste Output-Sektionen des Draft-Contracts (config/prompts/{mode}.txt).
# Entscheidungsorientierte 5-Sektionen-Struktur (Plan Phase 2.1).
DRAFT_SECTIONS = [
    "## Kurzlage",
    "## Datenqualität",
    "## Entscheidungsrelevante Punkte",
    "## Strategie-Abgleich",
    "## Relevante News & Veränderungen",
]

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

# Toleranz in Prozentpunkten gegen deterministic_summary (Plan §4.2).
NUMBER_TOLERANCE_PP = 0.5

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


def _extract_numbers(text: str) -> list[float]:
    matches = re.findall(r"(\d+\.?\d*)\s*%", text)
    return [float(m) for m in matches]


def _extract_tickers(text: str) -> set[str]:
    common = {
        "LLM", "API", "HTTP", "USD", "EUR", "ETF", "MVP", "RSS", "Q4", "AI", "OK",
        "USA", "IPO", "CEO", "GDP", "CPI", "EPS", "NASDAQ", "NYSE", "CNBC", "DAX",
        "S&P", "MSCI", "FTSE",
        # Finanz-/Rechtsform-Token — keine Ticker (geschlossene Blocklist).
        "SE", "ISIN", "WKN", "AG", "KG", "SA", "NV", "BV",
        "PLC", "LTD", "INC", "CORP", "CO",
    }
    candidates = set(re.findall(r"\b[A-Z]{2,5}(?:[-\.]?[A-Z]+)?\b", text))
    return candidates - common


def _extract_isins(text: str) -> set[str]:
    return set(re.findall(r"[A-Z]{2}[A-Z0-9]{9}\d", text))


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

    # 4. Zahlen nur aus deterministic_summary + strategy_thresholds_pct (Toleranz ±0.5pp)
    thresholds = facts_package.get("strategy_thresholds_pct", {})
    allowed = _summary_numbers_pct(summary) + _strategy_thresholds_pct(thresholds)
    for num in _extract_numbers(text):
        if not any(abs(num - value) <= NUMBER_TOLERANCE_PP for value in allowed):
            findings.append(
                _finding(
                    "critical",
                    f"Zahl {num}% passt nicht zu deterministic_summary",
                    f"Draft nennt {num}%, erlaubt: {allowed}",
                    "Zahl aus deterministic_summary uebernehmen oder entfernen",
                )
            )

    # 5. Ticker/ISIN im Portfolio
    holdings = facts_package.get("portfolio", {}).get("holdings", [])
    portfolio_tickers = {h.get("ticker", "") for h in holdings if h.get("ticker")}
    portfolio_isins = {h.get("isin", "") for h in holdings if h.get("isin")}
    for ticker in _extract_tickers(text):
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
        if isin not in portfolio_isins:
            findings.append(
                _finding(
                    "major",
                    f"Ticker/ISIN {isin} nicht im Portfolio",
                    f"Draft erwaehnt {isin}, Portfolio kennt ihn nicht",
                    f"{isin} entfernen oder durch Portfolio-Bestand ersetzen",
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

    # 7. Optionen-Contract (Plan Phase 2.4/2.8): Optionen nur bei
    #    deterministischen Triggern, pro Option Begruendung + Gegenargument/
    #    Risiko; imperative Kauf-/Verkaufsanweisung bleibt critical; sonstige
    #    Empfehlungen ausserhalb der Optionen-Sektion bleiben critical.
    triggers = compute_triggers(facts_package)
    options_section = _extract_section(text, _OPTIONS_SECTION)
    options_content = _section_content(options_section) or ""
    option_terms = _find_option_terms(options_content)
    has_reason = any(
        re.search(rf"\b{re.escape(marker)}\b", options_content, re.IGNORECASE)
        for marker in _REASON_MARKERS
    )
    has_counter = any(
        re.search(rf"\b{re.escape(marker)}\b", options_content, re.IGNORECASE)
        for marker in _COUNTER_MARKERS
    )
    if option_terms and not triggers["has_any_trigger"]:
        findings.append(
            _finding(
                "major",
                "Option ohne Trigger",
                f"Optionen {', '.join(option_terms)} im Draft, Trigger: {triggers}",
                "Optionen nur bei Triggern generieren oder Sektion auf 'keine entscheidungsrelevanten Punkte' setzen",
            )
        )
    if option_terms and not has_reason:
        findings.append(
            _finding(
                "major",
                "Option ohne Begründung",
                f"Optionen {', '.join(option_terms)} ohne Begründung in '{_OPTIONS_SECTION}'",
                "Pro Option eine Begründung (Evidenz aus dem Faktenpaket) ergänzen",
            )
        )
    if option_terms and not has_counter:
        findings.append(
            _finding(
                "major",
                "Option ohne Gegenargument/Risiko",
                f"Optionen {', '.join(option_terms)} ohne Gegenargument/Risiko in '{_OPTIONS_SECTION}'",
                "Pro Option ein konkretes Gegenargument/Risiko ergänzen",
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
    portfolio_tickers = set()
    portfolio_isins = set()
    for h in portfolio.get("holdings", []):
        if h.get("ticker"):
            portfolio_tickers.add(h.get("ticker", ""))
        portfolio_isins.add(h.get("isin", ""))
    for ticker in mentioned_tickers:
        if ticker not in portfolio_tickers and ticker not in portfolio_isins:
            warnings.append(f"Ticker/ISIN {ticker} im Briefing nicht im Portfolio")

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
