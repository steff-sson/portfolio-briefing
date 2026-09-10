"""Interaktiver Holding-Klassifikations-Flow (Strategiesitzung, Plan Phase 1).

Laeuft als OpenCode-Session (nicht als Pipeline-Automation): liest den
aktuellen Snapshot (``config/snapshot.current.json``), zeigt alle Holdings
mit ISIN/Name/Wert/Anteil/Datenqualitaet, verlangt pro Holding eine
User-Klassifikation (core/satellite/legacy/unknown) und schreibt die
bestaetigten Antworten in den bestehenden answers.reviewed.yaml-Mechanismus
(Antwortpfad ``holdings.classification.<ISIN>``, ausgewertet von
``setup_strategy.py --validate/--emit``).

Prinzipien:
- Keine harte Kodierung: Vorschlaege werden aus dem Snapshot abgeleitet
  (Roh-``category``, Name "ETF", bestehende etf_lookup-Kategorie,
  ILLIQUID_LEGACY_ISINS), der User bestaetigt oder ueberschreibt jede
  Klassifikation.
- Fail-closed: unbekannte Eingaben werden abgewiesen, leere Pflicht-
  Eingaben fuehren zum Abbruch (kein Teil-Schreiben).
- Abwaertskompatibel: bereits bestaetigte Klassifikationen (confirmed: true)
  in answers.reviewed.yaml werden als Default-Vorschlag angeboten und im
  Flow als "bereits klassifiziert" markiert.
- Keine neuen Datenmodell-Felder: Sparplan-Erfassung wird bewusst NICHT
  implementiert (kein Schema-/Flow-Support ohne Datenmodell-Ausweitung).
- Schreibzugriff nur auf config/setup/answers.reviewed.yaml (gitignored,
  persoenliche Strategie) — niemals auf strategy.yaml.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

from scripts import snapshot

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
SETUP_DIR = CONFIG_DIR / "setup"
ANSWERS_REVIEWED_PATH = SETUP_DIR / "answers.reviewed.yaml"
CURRENT_PATH = snapshot.CURRENT_PATH

# Kategorien-Enum identisch zur Antwort-Validierung in setup_strategy.py.
CLASSIFICATION_CATEGORIES = ("core", "satellite", "legacy", "unknown")

# Legacy/illiquide ISINs (analyze.ILLIQUID_LEGACY_ISINS — nur hier lokal
# gespiegelt fuer den Vorschlag, keine Klassifikations-Entscheidung).
_ILLIQUID_LEGACY_ISINS = {"LU2722255754"}

# Antwort-Praefix identisch zu setup_strategy._CLASSIFICATION_PREFIX.
_CLASSIFICATION_PREFIX = "holdings.classification."

# User-Eingaben fuer "Bestaetigen" im continue-Dialog (J/a/j = ja).
_CONTINUE_YES = {"j", "ja", "y", "yes", ""}


class ClassifyError(Exception):
    """Abbruch/Fehler im Klassifikations-Flow (fail-closed)."""


def today_str() -> str:
    return datetime.now(timezone.utc).date().isoformat()


# --- Laden -------------------------------------------------------------------


def load_snapshot(path: Path | None = None) -> dict | None:
    """Snapshot laden (aktueller Stand; optional expliziter Pfad).

    Nutzt ausschliesslich das Snapshot-Modul (fail-closed, kein Mock-Fallback):
    fehlender/korrupter Snapshot oder unerwartete Portfolio-Struktur -> None.
    """
    return snapshot.read_snapshot(path or CURRENT_PATH)


def load_etf_lookup(path: Path | None = None) -> dict:
    """config/etf_lookup.json lesen (Vorschlags-Grundlage, fehlend -> leer)."""
    try:
        with open(path or (CONFIG_DIR / "etf_lookup.json"), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def load_reviewed_answers(path: Path | None = None) -> dict:
    """Bereits bestaetigte Klassifikations-Antworten (fehlend -> leer)."""
    path = path or ANSWERS_REVIEWED_PATH
    if not path.exists():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return {}
    if not isinstance(data, dict):
        return {}
    return data


def load_strategy(path: Path | None = None) -> dict:
    """config/strategy.yaml lesen (nur fuer Anzeige/Ziele, ungueltig -> leer)."""
    path = path or (CONFIG_DIR / "strategy.yaml")
    if not path.exists():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return {}
    return data if isinstance(data, dict) else {}


# --- Vorschlag + Anzeige ------------------------------------------------------


def _holding_value(h: dict) -> float:
    raw = h.get("value_eur", h.get("value", 0)) or 0
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 0.0


def _data_quality(h: dict) -> str:
    """Datenqualitaet je Holding: "incomplete" wenn kein EUR-Wert, sonst "ok"."""
    if _holding_value(h) <= 0:
        return "incomplete"
    return "ok"


def _category_suggestion(holding: dict, lookup: dict) -> str:
    """Vorschlag (NUR Vorschlag): Roh-category -> etf_lookup -> Name-Heuristik.

    Keine Klassifikations-Entscheidung: der User bestaetigt oder ueberschreibt
    jede Kategorie. Fehlender Vorschlag -> "unknown" (fail-closed).
    """
    raw = holding.get("category")
    if isinstance(raw, str) and raw in CLASSIFICATION_CATEGORIES and raw != "unknown":
        return raw
    isin = str(holding.get("isin") or "")
    if isin in _ILLIQUID_LEGACY_ISINS:
        return "legacy"
    if isinstance(lookup.get(isin), dict):
        category = lookup.get(isin, {}).get("category")
        if isinstance(category, str) and category in CLASSIFICATION_CATEGORIES:
            return category
    name = str(holding.get("name") or "").lower()
    if "etf" in name or "ucits" in name:
        return "core"
    return "unknown"


def _confirmed_category(holding: dict, reviewed: dict) -> str | None:
    """Bereits bestaetigte Kategorie aus answers.reviewed.yaml (confirmed: true)."""
    entry = reviewed.get("answers", {}).get(_CLASSIFICATION_PREFIX + str(holding.get("isin") or ""))
    if not isinstance(entry, dict):
        return None
    value = entry.get("value")
    if not isinstance(value, dict) or value.get("confirmed") is not True:
        return None
    category = value.get("category")
    if isinstance(category, str) and category in CLASSIFICATION_CATEGORIES:
        return category
    return None


def format_holdings_table(holdings: list) -> list[str]:
    """Holdings-Tabelle: ISIN | Name | Wert (EUR) | Anteil (%) | Datenqualitaet.

    Gibt formatierte Zeilen zurueck (deterministisch, testbar ohne Input()).
    """
    total = sum(_holding_value(h) for h in holdings)
    lines = [
        "ISIN | Name | Wert (EUR) | Anteil (%) | Datenqualitaet",
        "---|---|---|---|---",
    ]
    for h in holdings:
        value = _holding_value(h)
        share = f"{value / total * 100:.1f}" if total else "—"
        value_txt = f"{value:,.2f}".replace(",", " ") if value > 0 else "—"
        name = h.get("name") or h.get("isin") or "—"
        lines.append(
            f"{h.get('isin') or '—'} | {name} | {value_txt} | {share} | {_data_quality(h)}"
        )
    return lines


def format_summary(holdings: list, strategy: dict) -> list[str]:
    """Kompakte Strategie- und Portfolio-Zusammenfassung (Anzeige, keine Werte)."""
    total = sum(_holding_value(h) for h in holdings)
    evaluated = sum(1 for h in holdings if _holding_value(h) > 0)
    lines = [
        f"Holdings: {len(holdings)} (davon bewertet: {evaluated})",
        f"Gesamtwert (Summe bewerteter Holdings): {total:,.2f} EUR".replace(",", " "),
    ]
    portfolio = strategy.get("portfolio", {}) if isinstance(strategy, dict) else {}
    if portfolio:
        core = portfolio.get("core_pct")
        sat = portfolio.get("satellite_pct")
        lines.append(f"Core-Ziel: {core}% | Satellite-Ziel: {sat}%")
    limits = strategy.get("satellite_limits", {}) if isinstance(strategy, dict) else {}
    if limits:
        lines.append(
            "Satellite-Limits: max_position "
            f"{limits.get('max_position_pct')}% | max_sector {limits.get('max_sector_pct')}%"
        )
    return lines


# --- Eingabe + Persistenz ------------------------------------------------------


def _ask_category(holding: dict, suggestion: str, confirmed: str | None) -> str:
    """Interaktive Klassifikations-Eingabe fuer eine Holding.

    Zeigt ISIN/Name/Wert/Anteil/Datenqualitaet, den Vorschlag (oder
    "bereits klassifiziert als <category>") und verlangt eine Auswahl
    [1] Core [2] Satellite [3] Legacy [4] unknown. Unbekannte Eingaben
    werden fail-closed abgewiesen (Schleife, keine stillen Defaults).
    """
    isin = holding.get("isin") or "?"
    name = holding.get("name") or isin
    value = _holding_value(holding)
    value_txt = f"{value:,.2f} EUR".replace(",", " ") if value > 0 else "unbewertet"
    print(f"\n{isin} — {name}")
    print(f"  Wert: {value_txt} | Datenqualitaet: {_data_quality(holding)}")
    if confirmed is not None:
        print(f"  Bereits klassifiziert: {confirmed} (aus answers.reviewed.yaml)")
        print("  [Return] beibehalten  [1-4] neu klassifizieren")
        prompt = "> "
    else:
        print(f"  Vorschlag: {suggestion}")
        print("  Klassifizieren: [1] Core  [2] Satellite  [3] Legacy  [4] unknown")
        prompt = "> "
    while True:
        choice = input(prompt).strip().lower()
        if confirmed is not None and choice in ("",):
            return confirmed
        if choice in ("1", "core"):
            return "core"
        if choice in ("2", "satellite"):
            return "satellite"
        if choice in ("3", "legacy"):
            return "legacy"
        if choice in ("4", "unknown"):
            return "unknown"
        print(f"  Ungueltige Eingabe '{choice}' — bitte 1-4 waehlen.")


def _ask_continue() -> bool:
    """Uebernahme-Bestaetigung: Abbruch bei explizitem 'n' (kein Teil-Schreiben)."""
    answer = input("Klassifikationen in answers.reviewed.yaml schreiben? [J/n] > ").strip().lower()
    return answer in _CONTINUE_YES


def build_classification_entries(holdings: list, categories: dict[str, str]) -> dict[str, dict]:
    """Antwort-Eintraege holdings.classification.<ISIN> bauen (serialisierbar).

    ``categories``: ISIN -> Kategorie. Liefert pro Holding einen Eintrag
    {value: {category, confirmed: true, source: "user"}, status: answered,
    date, source: review} — exakt das Format, das setup_strategy.py
    _validate_answers_structure/_emit_classification_block erwartet.
    """
    entries: dict[str, dict] = {}
    for h in holdings:
        isin = h.get("isin")
        if not isin or isin not in categories:
            continue
        entries[_CLASSIFICATION_PREFIX + isin] = {
            "value": {"category": categories[isin], "confirmed": True, "source": "user"},
            "status": "answered",
            "date": today_str(),
            "source": "review",
        }
    return entries


def merge_into_reviewed(reviewed: dict, new_entries: dict[str, dict]) -> dict:
    """Neue Klassifikations-Antworten in answers.reviewed.yaml eintragen.

    Bestehende (auch Nicht-Klassifikations-) Antworten bleiben unveraendert;
    nur die Klassifikations-Antworten werden ueberschrieben/ergaenzt.
    """
    result = dict(reviewed)
    answers = dict(reviewed.get("answers", {}))
    answers.update(new_entries)
    result["answers"] = answers
    meta = dict(reviewed.get("meta", {}))
    meta.setdefault("created", today_str())
    meta["reviewed"] = True
    meta["reviewed_at"] = today_str()
    meta["reviewer"] = "stef"
    meta.setdefault("source", "sokratischer Dialog + Holding-Klassifikation")
    result["meta"] = meta
    return result


def write_reviewed(path: Path, reviewed: dict) -> None:
    """answers.reviewed.yaml atomar schreiben (gitignored, persoenliche Strategie)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            yaml.safe_dump(reviewed, f, allow_unicode=True, sort_keys=False)
            f.flush()
        import os

        os.replace(tmp, path)
    except BaseException:
        try:
            import os

            os.unlink(tmp)
        except OSError:
            pass
        raise


# --- Flow ----------------------------------------------------------------------


def run_flow(
    holdings: list,
    *,
    lookup: dict | None = None,
    reviewed: dict | None = None,
    strategy: dict | None = None,
    answers_path: Path | None = None,
) -> dict:
    """Interaktiver Klassifikations-Flow fuer die gegebenen Holdings.

    Gibt {"written": bool, "entries": {ISIN: category}, "path": str} zurueck.
    """
    lookup = lookup if lookup is not None else load_etf_lookup()
    reviewed = reviewed if reviewed is not None else load_reviewed_answers()
    strategy = strategy if strategy is not None else load_strategy()

    print("\n=== HOLDING-KLASSIFIKATION (Strategiesitzung) ===")
    print("\n".join(format_summary(holdings, strategy)))
    print("\nAKTUELLES PORTFOLIO (Snapshot):")
    print("\n".join(format_holdings_table(holdings)))

    categories: dict[str, str] = {}
    for index, h in enumerate(holdings, start=1):
        isin = h.get("isin")
        if not isin:
            print(f"\nWarnung: Holding {index} ohne ISIN wird uebersprungen.")
            continue
        suggestion = _category_suggestion(h, lookup)
        confirmed = _confirmed_category(h, reviewed)
        print(f"\nPosition {index}/{len(holdings)}:")
        category = _ask_category(h, suggestion, confirmed)
        categories[isin] = category

    if not categories:
        print("\nKeine klassifizierbaren Holdings (keine ISINs). Abbruch.")
        return {"written": False, "entries": {}, "path": ""}

    print("\n=== UEBERNAHME ===")
    for isin, category in categories.items():
        print(f"  {isin}: {category}")
    if not _ask_continue():
        print("Abgebrochen — nichts geschrieben.")
        return {"written": False, "entries": categories, "path": ""}

    entries = build_classification_entries(holdings, categories)
    merged = merge_into_reviewed(reviewed, entries)
    path = answers_path or ANSWERS_REVIEWED_PATH
    write_reviewed(path, merged)
    print(f"\n{len(entries)} Klassifikations-Antworten geschrieben: {path}")
    print("Naechster Schritt: setup_strategy.py --validate und --emit ausfuehren.")
    return {"written": True, "entries": categories, "path": str(path)}


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="Interaktive Holding-Klassifikation (Strategiesitzung, Phase 1)"
    )
    parser.add_argument(
        "--snapshot",
        help="Alternativer Snapshot-Pfad (default: config/snapshot.current.json)",
    )
    parser.add_argument(
        "--answers",
        help="Ziel-answers.reviewed.yaml (default: config/setup/answers.reviewed.yaml)",
    )
    args = parser.parse_args(argv)

    try:
        data = load_snapshot(args.snapshot)
        if data is None:
            raise ClassifyError(
                f"Snapshot nicht lesbar: {args.snapshot or CURRENT_PATH} "
                "(kein Mock-Fallback — zuerst produktiven Lauf/snapshot.current.json erzeugen)"
            )
        portfolio = data.get("portfolio")
        if not isinstance(portfolio, dict):
            raise ClassifyError("Snapshot enthaelt kein Portfolio-Objekt")
        holdings = portfolio.get("holdings")
        if not isinstance(holdings, list) or not holdings:
            raise ClassifyError("Snapshot enthaelt keine Holdings")
        result = run_flow(
            holdings,
            answers_path=Path(args.answers) if args.answers else None,
        )
        return 0 if result["written"] else 1
    except (ClassifyError, OSError) as e:
        print(f"Klassifikation abgebrochen: {e}", file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        print("\nKlassifikation abgebrochen (Interrupt) — nichts geschrieben.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
