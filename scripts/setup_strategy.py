"""Deterministischer Setup-Flow: 15 Fragen -> answers.yaml -> Review -> strategy.yaml.

Ersetzt den LLM-YAML-Generator (config/prompts/setup_generate.txt) vollstaendig:
- Das LLM moderiert nur noch (setup_system.txt) und erstellt eine Antwort-
  Zusammenfassung als Entwurf; es setzt KEINE Fachwerte und es gibt KEINE
  Defaults. Fehlende/unklare analyserelevante Antworten sind ein harter
  Setup-Fehler (kein Raten, kein LLM-Fallback).
- Persistenz: config/setup/answers.yaml (roh) und config/setup/answers.reviewed.yaml
  (bestaetigt, Review-Status/Datum). Beide sind gitignored.
- Validierung: gegen config/strategy.schema.yaml (Single Source of Truth).
  Unbekannte Felder schlagen fehl (fail-closed).
- Emission: config/strategy.yaml atomar mit Review-Backup nach
  config/setup/backup/strategy-<ts>.yaml; nur Felder mit Mapping-Status
  analyserelevant|dokumentationsrelevant werden emittiert (bewusst informative
  Felder bleiben in answers.reviewed.yaml + Doku). strategy/strategy-version.txt
  (Hash + Datum, lokal, nicht versioniert) wird aktualisiert.
"""
from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import yaml

from scripts import analyze

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
SETUP_DIR = CONFIG_DIR / "setup"
BACKUP_DIR = SETUP_DIR / "backup"
ANSWERS_PATH = SETUP_DIR / "answers.yaml"
ANSWERS_REVIEWED_PATH = SETUP_DIR / "answers.reviewed.yaml"
STRATEGY_PATH = CONFIG_DIR / "strategy.yaml"
VERSION_PATH = ROOT / "strategy" / "strategy-version.txt"

# Mapping-Status, die in strategy.yaml emittiert werden (Plan §5[5]).
_EMITTED_MAPPINGS = {"analyserelevant", "dokumentationsrelevant"}

# Klassifikations-Antworten (Plan P3): Antwortpfad holdings.classification.<ISIN>
# wird auf das P1-Schema holdings_classification.isins.<ISIN> gemappt. "holdings"
# ist keine Schema-Sektion — der Adapter erkennt den Praefix explizit.
_CLASSIFICATION_PREFIX = "holdings.classification."
_CLASSIFICATION_CATEGORIES = ("core", "satellite", "legacy", "unknown")


class SetupError(Exception):
    """Setup-/Validierungsfehler (fail-closed, kein LLM-Default)."""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def today_str() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _load_yaml(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        raise SetupError(f"{path} ist kein YAML-Objekt")
    return data


def _write_yaml(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)


# --- Schema-Helfer (aus analyze übernommen, gleiche Quelle) ------------------


def _schema_sections(schema: dict) -> dict:
    sections = schema.get("sections", {}) if isinstance(schema, dict) else {}
    return sections if isinstance(sections, dict) else {}


def _all_schema_fields(schema: dict) -> list[tuple[str, str, dict]]:
    """Alle (section, field, spec) aus dem Schema, inkl. verschachtelter dict-Felder.

    Felder unter einem ``dynamic: true``-Container (z.B.
    holdings_classification.isins.category) werden mit ``"dynamic"`` als
    section-Vorsilbe markiert — sie sind nur pro dynamischem Schlüssel
    Pflicht, nicht als einzelne Setup-Antwort (die Sektion ist optional).
    """
    result: list[tuple[str, str, dict]] = []
    for section, section_spec in _schema_sections(schema).items():
        if not isinstance(section_spec, dict):
            continue
        fields = section_spec.get("fields")
        if not isinstance(fields, dict):
            continue
        for field, spec in fields.items():
            if not isinstance(spec, dict):
                continue
            result.append((section, field, spec))
            if spec.get("type") == "dict":
                nested = spec.get("fields")
                if isinstance(nested, dict):
                    for nf, nspec in nested.items():
                        if isinstance(nspec, dict):
                            if spec.get("dynamic") is True:
                                result.append((f"{section}.{field}.dynamic", nf, nspec))
                            else:
                                result.append((f"{section}.{field}", nf, nspec))
    return result


# --- Answers: Validierung gegen das Schema -----------------------------------


def _validate_answers_structure(answers: dict, schema: dict) -> list[str]:
    """Struktur-Validierung der Antworten gegen das Schema (fail-closed).

    Jede Antwort muss ein Feld "value" tragen. Pflichtfelder mit Mapping
    analyserelevant muessen beantwortet sein; dokumentationsrelevante
    Pflichtfelder duerfen fehlen, wenn sie als "bewusst nicht beantwortet"
    markiert sind (explizit, nicht still). Unbekannte Felder schlagen fehl.
    """
    errors: list[str] = []
    if not isinstance(answers, dict) or "answers" not in answers:
        return ["answers.yaml muss ein Objekt mit 'answers' sein"]
    answer_map = answers.get("answers", {})
    if not isinstance(answer_map, dict):
        return ["answers.answers muss ein Objekt sein"]

    known = {(s, f) for s, f, _ in _all_schema_fields(schema)}
    for path, entry in answer_map.items():
        if not isinstance(entry, dict) or "value" not in entry:
            errors.append(f"Antwort '{path}' muss ein Objekt mit 'value' sein")
            continue
        if path.startswith(_CLASSIFICATION_PREFIX):
            # Klassifikations-Antworten (Plan P3): value = {category, confirmed, source}.
            # Fail-closed: category muss aus dem P1-Enum stammen (keine Heuristik,
            # keine automatische Klassifikation); unbekannte Keys schlagen fehl.
            value = entry["value"]
            if not isinstance(value, dict):
                errors.append(
                    f"Antwort '{path}' muss ein Dict mit category/confirmed/source sein"
                )
                continue
            unknown_keys = set(value) - {"category", "confirmed", "source"}
            if unknown_keys:
                errors.append(
                    f"Antwort '{path}' enthaelt unbekannte Felder: {sorted(unknown_keys)} "
                    "(nur category, confirmed, source erlaubt)"
                )
            category = value.get("category")
            if category not in _CLASSIFICATION_CATEGORIES:
                errors.append(
                    f"Antwort '{path}': category muss einer der Werte sein: "
                    f"{sorted(_CLASSIFICATION_CATEGORIES)}"
                )
            continue
        parts = path.split(".")
        key = (parts[0], parts[1]) if len(parts) >= 2 else (path, "")
        if key not in known and parts[0] not in {s for s, _, _ in _all_schema_fields(schema)}:
            errors.append(f"Unbekanntes Antwort-Feld '{path}' (nicht im Strategie-Schema)")

    # Analyserelevante Pflichtfelder: nur die Blatt-Felder des Schemas muessen
    # beantwortet sein (dict-Container wie portfolio.rebalancing sind Sammlungen,
    # keine eigenen Antworten; dynamische Felder unter einem optionalen
    # Container wie holdings_classification.isins sind keine Pflicht-Antworten).
    for section, field, spec in _all_schema_fields(schema):
        if ".dynamic" in section:
            continue  # dynamische Felder: nur pro ISIN-Eintrag Pflicht, nicht global
        if spec.get("mapping") != "analyserelevant":
            continue
        if spec.get("type") == "dict":
            continue  # Container: keine eigene Antwort, nur die Blatt-Felder
        entry = answer_map.get(f"{section}.{field}")
        if entry is None or not isinstance(entry, dict) or entry.get("value") is None:
            errors.append(
                f"Analyserelevante Antwort fehlt oder ist unbeantwortet: {section}.{field} "
                f"(kein LLM-Default moeglich — Setup bricht ab)"
            )
    return errors


def _validate_emitted_strategy(strategy: dict) -> None:
    """Emitierte strategy.yaml gegen das Schema validieren (fail-closed)."""
    validation = analyze.validate_strategy(strategy)
    if not validation["valid"]:
        raise SetupError("Emitierte strategy.yaml ungueltig: " + "; ".join(validation["errors"]))


# --- Emission ----------------------------------------------------------------


def _emit_classification_block(answer_map: dict) -> dict | None:
    """holdings_classification.isins.<ISIN> aus den Klassifikations-Antworten.

    Nur explizit bestaetigte Klassifikationen (confirmed: true) werden
    emittiert — keine Heuristik, keine automatische Klassifikation.
    category wird fail-closed gegen das P1-Enum geprueft (unbekannte Werte
    sind bereits in _validate_answers_structure blockiert; hier als
    Double-Check fuer direkte _emit_dict_from_answers-Aufrufer).

    Rueckgabe: {"isins": {ISIN: {category, confirmed, source?}}} wenn
    mindestens eine ISIN bestaetigt ist, sonst None (Block fehlt — das
    emitierte strategy.yaml bleibt abwaertskompatibel).
    """
    isins: dict = {}
    for path, entry in answer_map.items():
        if not path.startswith(_CLASSIFICATION_PREFIX) or not isinstance(entry, dict):
            continue
        isin = path[len(_CLASSIFICATION_PREFIX):]
        if not isin:
            continue
        value = entry.get("value")
        if not isinstance(value, dict) or value.get("confirmed") is not True:
            continue  # nur explizit bestaetigte Klassifikationen
        category = value.get("category")
        if category not in _CLASSIFICATION_CATEGORIES:
            raise SetupError(
                f"Antwort '{path}': category muss einer der Werte sein: "
                f"{sorted(_CLASSIFICATION_CATEGORIES)}"
            )
        item = {"category": category, "confirmed": True}
        source = value.get("source")
        if isinstance(source, str) and source:
            item["source"] = source
        isins[isin] = item
    if not isins:
        return None
    return {"isins": isins}


def _emit_dict_from_answers(answers: dict, schema: dict) -> dict:
    """Emitiertes Strategie-Dict: nur Felder mit Mapping analyserelevant|dokumentationsrelevant.

    Bewusst informative Felder (z.B. external_provision, personal_context,
    check_frequency) werden NICHT in strategy.yaml geschrieben — sie bleiben
    in answers.reviewed.yaml und erscheinen in der Doku.
    """
    strategy: dict = {}
    answer_map = answers.get("answers", {})
    for section, field, spec in _all_schema_fields(schema):
        if spec.get("mapping") not in _EMITTED_MAPPINGS:
            continue
        entry = answer_map.get(f"{section}.{field}")
        if entry is None or not isinstance(entry, dict) or entry.get("value") is None:
            continue
        value = entry["value"]
        if spec.get("type") == "dict":
            # dict-Container (z.B. portfolio.rebalancing): Blatt-Felder
            # (method, threshold_pct) werden separat emittiert — der
            # Container selbst ist keine eigene Antwort.
            continue
        if "." in section:  # verschachteltes dict-Feld (z.B. portfolio.rebalancing.threshold_pct)
            # section='portfolio.rebalancing', field='threshold_pct' -> portfolio.rebalancing.threshold_pct
            parts = section.split(".")
            parent = parts[0]
            target = strategy.setdefault(parent, {})
            for part in parts[1:]:
                target = target.setdefault(part, {})
            target[field] = value
            continue
        strategy.setdefault(section, {})
        strategy[section][field] = value

    # P3: holdings_classification aus den Klassifikations-Antworten emittieren
    # (Antwortpfad holdings.classification.<ISIN> -> P1-Schema). Nur wenn
    # mindestens eine ISIN bestaetigt ist; sonst bleibt der Block weg
    # (Abwaertskompatibilitaet).
    classification = _emit_classification_block(answer_map)
    if classification is not None:
        strategy["holdings_classification"] = classification
    return strategy


def _backup_strategy() -> Path | None:
    """Bestehende strategy.yaml nach config/setup/backup/strategy-<ts>.yaml kopieren."""
    if not STRATEGY_PATH.exists():
        return None
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    target = BACKUP_DIR / f"strategy-{ts}.yaml"
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copy2(STRATEGY_PATH, target)
    return target


def _write_atomic(path: Path, data: dict | str) -> None:
    """Atomar schreiben: Temp-Datei + os.replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".tmp-setup-", suffix=".tmp")
    try:
        with open(fd, "w", encoding="utf-8") as f:
            if isinstance(data, str):
                f.write(data)
            else:
                yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)
            f.flush()
        import os

        os.replace(tmp_name, path)
    except BaseException:
        try:
            import os

            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _write_version_file(strategy: dict, version: object) -> None:
    """strategy/strategy-version.txt: Hash + Datum (keine Strategiewerte)."""
    content = (
        f"version: {version}\n"
        f"hash: {analyze.strategy_hash(strategy)}\n"
        f"generated_at: {now_iso()}\n"
    )
    _write_atomic(VERSION_PATH, content)


def emit(answers_reviewed_path: Path | None = None) -> dict:
    """Emission: answers.reviewed.yaml -> config/strategy.yaml (+ Backup, Version).

    Fail-closed: keine Review-Version -> SetupError; ungueltige Antworten ->
    SetupError; ungueltige emitierte Strategie -> SetupError (nichts geschrieben).
    Rueckgabe: {"strategy", "backup_path", "version"}.
    """
    if answers_reviewed_path is None:
        answers_reviewed_path = ANSWERS_REVIEWED_PATH
    if not answers_reviewed_path.exists():
        raise SetupError(
            f"{answers_reviewed_path} fehlt — zuerst Review/Bestaetigung ausfuehren"
        )
    try:
        answers = _load_yaml(answers_reviewed_path)
    except OSError as e:
        raise SetupError(f"answers.reviewed.yaml nicht lesbar: {e}") from e
    schema = analyze.load_strategy_schema()
    errors = _validate_answers_structure(answers, schema)
    if errors:
        raise SetupError("; ".join(errors))

    strategy = _emit_dict_from_answers(answers, schema)
    # Version hochzaehlen (bestehend oder Start bei 1).
    current_version = 1
    if STRATEGY_PATH.exists():
        try:
            existing = yaml.safe_load(STRATEGY_PATH.read_text(encoding="utf-8")) or {}
            current_version = int(existing.get("meta", {}).get("version", 0)) + 1
        except (TypeError, ValueError, yaml.YAMLError):
            current_version = 1
    strategy.setdefault("meta", {})
    strategy["meta"]["version"] = current_version
    if "created" not in strategy.get("meta", {}):
        strategy["meta"]["created"] = today_str()
    strategy["meta"]["last_reviewed"] = today_str()

    _validate_emitted_strategy(strategy)

    backup_path = _backup_strategy()
    _write_atomic(STRATEGY_PATH, strategy)
    _write_version_file(strategy, current_version)
    return {
        "strategy": strategy,
        "backup_path": str(backup_path) if backup_path else None,
        "version": current_version,
    }


def _load_answers_snapshot(answers: dict) -> str:
    """Kompakte Antwort-Zusammenfassung (deterministisch, keine LLM-Interpretation)."""
    lines = []
    for path in sorted(answers.get("answers", {}).keys()):
        entry = answers.get("answers", {}).get(path)
        if isinstance(entry, dict) and entry.get("value") is not None:
            lines.append(f"- {path}: {entry['value']!r}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Deterministischer Strategie-Setup-Flow")
    parser.add_argument("--emit", action="store_true", help="strategy.yaml aus answers.reviewed.yaml emittieren")
    parser.add_argument(
        "--validate", action="store_true", help="answers.reviewed.yaml gegen das Schema validieren"
    )
    parser.add_argument(
        "--init-answers",
        action="store_true",
        help="Leere answers.yaml (Struktur aus dem Schema) anlegen — danach manuell befuellen",
    )
    args = parser.parse_args(argv)

    try:
        schema = analyze.load_strategy_schema()
        if args.init_answers:
            if ANSWERS_PATH.exists():
                raise SetupError(f"{ANSWERS_PATH} existiert bereits — nicht ueberschreiben")
            answers: dict = {"answers": {}, "meta": {"created": now_iso(), "source": "sokratischer Dialog"}}
            _write_yaml(ANSWERS_PATH, answers)
            print(f"answers.yaml angelegt: {ANSWERS_PATH} (bitte manuell aus dem Dialog befuellen)")
            return 0

        if args.validate:
            if not ANSWERS_REVIEWED_PATH.exists():
                raise SetupError(f"{ANSWERS_REVIEWED_PATH} fehlt")
            answers = _load_yaml(ANSWERS_REVIEWED_PATH)
            errors = _validate_answers_structure(answers, schema)
            if errors:
                raise SetupError("; ".join(errors))
            print(f"Validierung OK: {ANSWERS_REVIEWED_PATH}")
            return 0

        if args.emit:
            result = emit()
            print(
                f"Emission OK: {STRATEGY_PATH} (version={result['version']}, "
                f"backup={result['backup_path'] or 'keiner'})"
            )
            return 0

        # Ohne Aktion: Schema + Antwort-Struktur anzeigen (Doku-Hilfe).
        for section, field, spec in _all_schema_fields(schema):
            print(
                f"{section}.{field}: {spec.get('type')} "
                f"[{spec.get('mapping')}] required={spec.get('required')}"
            )
        return 0
    except (SetupError, analyze.StrategyValidationError) as e:
        print(f"Setup-Fehler: {e}", file=sys.stderr)
        return 1
    except OSError as e:
        print(f"Setup-Fehler (Dateisystem): {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
