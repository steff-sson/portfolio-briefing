"""Deterministische Strategiedoku: strategy/strategy.md aus strategy.yaml + answers.reviewed.yaml.

Die menschenlesbare Governance-Strategie wird aus den bestaetigten Antworten
(answers.reviewed.yaml) und der emittierten strategy.yaml deterministisch
generiert. Werte in der Doku == Werte in strategy.yaml (Doku-SSoT).
Kein LLM, keine Secrets. Die Doku wird lokal generiert und **nicht versioniert**
(persoenliche Werte, gitignored). Öffentliche Vorlage:
`config/strategy.example.yaml`.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

from scripts import analyze

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
STRATEGY_PATH = CONFIG_DIR / "strategy.yaml"
ANSWERS_REVIEWED_PATH = CONFIG_DIR / "setup" / "answers.reviewed.yaml"
DOC_PATH = ROOT / "strategy" / "strategy.md"

# Deutsche Sektornamen für die Doku (nur Doku-Lesart, keine Fachwerte).
_SECTOR_LABELS = {
    "technology": "Technologie",
    "ai": "Künstliche Intelligenz",
    "energy": "Energie",
    "fossil_fuels": "Fossile Brennstoffe",
    "defense": "Rüstung",
}

# Doku-Lesart der Klassifikations-Kategorien (Fachwerte bleiben unverändert).
_CATEGORY_LABELS = {
    "core": "Core",
    "satellite": "Satellite",
    "legacy": "Legacy",
    "unknown": "Unknown",
}

_NO_CLASSIFICATION_HINT = (
    "Keine Klassifikation hinterlegt (Fallback auf etf_lookup + Live-Daten)."
)


def _load_yaml(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data if isinstance(data, dict) else {}


def _answer_value(answer_map: dict, path: str):
    entry = answer_map.get(path)
    if isinstance(entry, dict):
        return entry.get("value")
    return None


def _fmt(value) -> str:
    if isinstance(value, list):
        return ", ".join(str(v) for v in value)
    if isinstance(value, bool):
        return "ja" if value else "nein"
    if value is None:
        return "—"
    return str(value)


def _sector_label(value) -> str:
    if isinstance(value, list):
        return ", ".join(_SECTOR_LABELS.get(str(v), str(v)) for v in value)
    if value is None:
        return "—"
    return _SECTOR_LABELS.get(str(value), str(value))


def render_strategy_doc() -> str:
    """Generiert die menschenlesbare Governance-Strategie (deterministisch)."""
    strategy = _load_yaml(STRATEGY_PATH)
    answers = _load_yaml(ANSWERS_REVIEWED_PATH) if ANSWERS_REVIEWED_PATH.exists() else {}
    answer_map = answers.get("answers", {}) if isinstance(answers, dict) else {}

    portfolio = strategy.get("portfolio", {})
    limits = strategy.get("satellite_limits", {})
    investor = strategy.get("investor", {})
    sectors = strategy.get("sectors", {})
    meta = strategy.get("meta", {})
    review_schedule = strategy.get("review_schedule", {})
    alerts = strategy.get("alerts", {})

    lines: list[str] = []
    lines.append("---")
    lines.append("tags: [portfolio-briefing, strategy, governance]")
    lines.append("created: 2026-08-19")
    lines.append("status: active")
    lines.append("---")
    lines.append("")
    lines.append("# Anlagestrategie (Governance)")
    lines.append("")
    lines.append(
        "> Diese Doku wird deterministisch aus `config/setup/answers.reviewed.yaml` und "
        "`config/strategy.yaml` generiert (`scripts/render_strategy_doc.py`). "
        "Sie enthaelt keine Anlageberatung — sie dokumentiert die bestaetigte Selbstreflexion."
    )
    lines.append("")
    lines.append("## 1. Bestätigte Strategiewerte")
    lines.append("")
    lines.append("| Bereich | Wert |")
    lines.append("|---|---|")
    lines.append(f"| Anlagehorizont | {_fmt(_answer_value(answer_map, 'investor.horizon') or investor.get('horizon', ''))} |")
    lines.append(f"| Anlagezweck | {_fmt(_answer_value(answer_map, 'investor.purpose') or investor.get('purpose', ''))} |")
    lines.append(f"| Einkommensquelle | {_fmt(_answer_value(answer_map, 'investor.income_source') or investor.get('income_source', ''))} |")
    lines.append(f"| Sparrate | {_fmt(_answer_value(answer_map, 'investor.monthly_savings_eur') or investor.get('monthly_savings_eur', ''))} EUR/Monat |")
    lines.append(f"| Erfahrung Einzelaktien | {_fmt(_answer_value(answer_map, 'investor.experience_years') or investor.get('experience_years', ''))} Jahre |")
    lines.append(f"| Hebel | {_fmt(_answer_value(answer_map, 'investor.leverage') or investor.get('leverage', ''))} |")
    lines.append(f"| Check-Frequenz | {_fmt(_answer_value(answer_map, 'investor.check_frequency') or investor.get('check_frequency', ''))} |")
    lines.append(f"| Core-Zielquote | {_fmt(portfolio.get('core_pct'))}% |")
    lines.append(f"| Satellite-Zielquote | {_fmt(portfolio.get('satellite_pct'))}% |")
    lines.append(f"| Core-Beschreibung | {_fmt(portfolio.get('core_description'))} |")
    rebalancing = portfolio.get("rebalancing", {})
    lines.append(f"| Rebalancing | Methode {_fmt(rebalancing.get('method'))}, Toleranz {_fmt(rebalancing.get('threshold_pct'))}pp |")
    lines.append(f"| Ziel-Positionsgröße | {_fmt(limits.get('target_position_pct'))}% |")
    lines.append(f"| Warn-Positionsgröße | {_fmt(limits.get('warn_position_pct'))}% |")
    lines.append(f"| Max. Positionsgröße | {_fmt(limits.get('max_position_pct'))}% |")
    lines.append(f"| Max. Sektorkonzentration | {_fmt(limits.get('max_sector_pct'))}% |")
    lines.append(f"| Max. Anzahl Positionen | {_fmt(limits.get('max_positions'))} |")
    lines.append(f"| Max. Jahresumschlag | {_fmt(limits.get('max_turnover_annual_pct'))}% |")
    lines.append(f"| Max. Trades pro Quartal | {_fmt(limits.get('max_trades_per_quarter'))} |")
    lines.append(f"| Bevorzugte Sektoren | {_sector_label(sectors.get('preferred'))} |")
    lines.append(f"| Ausgeschlossene Sektoren | {_sector_label(sectors.get('excluded'))} |")
    lines.append(f"| Thesis-Pflicht | {'ja' if strategy.get('thesis', {}).get('required') else 'nein'} |")
    lines.append(f"| Cooling-Off | {_fmt(meta.get('cooling_off_days'))} Tage |")
    lines.append(f"| Thesen-Ablauffrist | {_fmt(alerts.get('on_thesis_expiring_soon_days'))} Tage |")
    lines.append("")
    lines.append("## 2. Persönliche Faktoren (bewusst informativ)")
    lines.append("")
    provision = _answer_value(answer_map, "investor.external_provision")
    personal_context = _answer_value(answer_map, "investor.personal_context")
    if provision:
        lines.append("- **Externe Vorsorgebausteine (nicht im Depot):**")
        for p in provision:
            if isinstance(p, dict):
                lines.append(
                    f"  - {p.get('name', '?')}: {p.get('amount_eur', '?')} EUR/{p.get('frequency', '?')} "
                    f"(Rolle: {p.get('role', '?')})"
                )
    if personal_context:
        lines.append(f"- **Persönlicher Kontext (Frage 15):** {_fmt(personal_context)}")
    if not provision and not personal_context:
        lines.append("_Keine Angaben._")
    lines.append("")
    lines.append("## 3. Governance & Review-Rhythmus")
    lines.append("")
    lines.append(f"- **Version:** {_fmt(meta.get('version'))} (erzeugt {_fmt(meta.get('last_reviewed'))})")
    lines.append(f"- **Quartalsweises Strategie-Review:** {'ja' if review_schedule.get('quarterly_strategy_review') else 'nein'}")
    lines.append(f"- **Jährliches Voll-Review:** {'ja' if review_schedule.get('annual_full_review') else 'nein'}")
    triggers = review_schedule.get("triggers") or []
    lines.append(f"- **Review-Trigger:** {', '.join(str(t) for t in triggers) if triggers else 'keine'}")
    lines.append(f"- **Cooling-Off:** {_fmt(meta.get('cooling_off_days'))} Tage bei Strategieänderungen")
    lines.append(f"- **Strategie-Hash:** `{analyze.strategy_hash(strategy)}` (SHA-256, in `strategy/strategy-version.txt`)")
    lines.append("")
    lines.append("## 4. Abgrenzung")
    lines.append("")
    lines.append(
        "- **Technische Schnittstelle** (Schema, Validierung, Hash/Diff): "
        "`docs/strategy-interface.md` (ehemals `docs/strategy.md`)."
    )
    lines.append(
        "- **Maschinenlesbare Strategie:** `config/strategy.yaml` (gitignored, aus "
        "`config/setup/answers.reviewed.yaml` per `scripts/setup_strategy.py --emit` erzeugt)."
    )
    lines.append(
        "- **Bewusst informative Felder** (externe Vorsorgebausteine, persönlicher Kontext, "
        "Check-Frequenz) fließen in keine Checks — sie sind nur hier und in "
        "`answers.reviewed.yaml` dokumentiert."
    )
    lines.append(
        "- **Unbewertete Positionen** (z.B. SUSE / LU2722255754 mit `valuation: null`): "
        "bleiben erhalten, werden nie mit 0 bewertet und erscheinen in der Datenqualität "
        "als `incomplete` (siehe `docs/strategy-interface.md`, §8)."
    )
    lines.append("")
    lines.append("## 5. Offene Annahmen")
    lines.append("")
    lines.append(
        "- Alle Werte stammen aus dem sokratischen Dialog (2026-08-14) und den "
        "geklärten Punkten (max. 5 Trades/Quartal). Es gibt keine LLM-Defaults und "
        "keine „sinnvollen Mappings“ — fehlende/unklare analyserelevante Antworten "
        "blockieren den Setup hart."
    )
    lines.append("")
    lines.append("## 6. Holdings-Klassifikation")
    lines.append("")
    classification = strategy.get("holdings_classification", {})
    isins = classification.get("isins", {}) if isinstance(classification, dict) else {}
    if not isinstance(isins, dict) or not isins:
        lines.append(f"_{_NO_CLASSIFICATION_HINT}_")
    else:
        lines.append("| ISIN | Name | Kategorie | Bestätigt | Quelle |")
        lines.append("|---|---|---|---|---|")
        lookup = analyze.load_etf_lookup()
        for isin, entry in sorted(isins.items()):
            if not isinstance(entry, dict):
                continue
            name = ""
            if isinstance(lookup.get(isin), dict):
                name = str(lookup.get(isin, {}).get("name") or "")
            lines.append(
                f"| {isin} | {name or '—'} | "
                f"{_CATEGORY_LABELS.get(str(entry.get('category')), str(entry.get('category') or '—'))} | "
                f"{_fmt(entry.get('confirmed'))} | {_fmt(entry.get('source'))} |"
            )
        lines.append("")
        lines.append(
            "Nur in `strategy.yaml` hinterlegte Klassifikationen (Strategie-Intent) "
            "erscheinen hier; nicht klassifizierte Holdings gelten zur Laufzeit als "
            "`unknown` (Fallback-Kette: Rohdaten-Kategorie → etf_lookup → unknown)."
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Strategiedoku deterministisch generieren")
    parser.add_argument("--write", action="store_true", help="strategy/strategy.md schreiben (lokal generiert, nicht versioniert)")
    args = parser.parse_args(argv)
    doc = render_strategy_doc()
    if args.write:
        DOC_PATH.parent.mkdir(parents=True, exist_ok=True)
        DOC_PATH.write_text(doc, encoding="utf-8")
        print(f"Strategiedoku geschrieben: {DOC_PATH}")
    else:
        print(doc)
    return 0


if __name__ == "__main__":
    sys.exit(main())
