"""Snapshot capture: rolling current file + archived history, atomisches Schreiben.

Phase 1 des Live-First-Refresh-Plans. Bewusst ohne Broker-/LLM-/Telegram-
Abhaengigkeiten: ``capture`` bekommt portfolio/transactions als Argumente.
Keine Secrets: es wird nie Snapshot-Inhalt geloggt, nur Dateipfade/Boolesches.

Layout:
- ``config/snapshot.current.json`` — Rolling-Stand (neuester Snapshot)
- ``config/snapshot.staged.json`` — pending Live-Stand (nur staged, Baseline
  bleibt unangetastet; erst ``promote_staged`` nach erfolgreichem final_gate)
- ``config/snapshots/archive/snapshot-<ts>.json`` — historische Snapshots

Schreibweise: temporaere Datei im Zielverzeichnis + ``os.replace`` (atomar);
bei Fehlern wird die Temp-Datei aufgeraeumt. Korrupte/fehlende Vorgaenger
werden defensiv behandelt (wie "kein Vorgaenger", kein Crash).
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from scripts import analyze

SCHEMA_VERSION = 1

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
CURRENT_PATH = CONFIG_DIR / "snapshot.current.json"
STAGED_PATH = CONFIG_DIR / "snapshot.staged.json"
ARCHIVE_DIR = CONFIG_DIR / "snapshots" / "archive"

_ARCHIVE_PREFIX = "snapshot-"
_ARCHIVE_SUFFIX = ".json"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def build_snapshot(
    portfolio: dict,
    transactions: list,
    mode: str | None = None,
    sc_meta: dict | None = None,
    captured_at: str | None = None,
    strategy: dict | None = None,
) -> dict:
    """Snapshot-Schema bauen — rein, ohne Disk-Zugriff, ohne Input-Mutation.

    Tiefe Kopie via JSON-Roundtrip: garantiert JSON-Serialisierbarkeit
    (TypeError bei Nicht-JSON-Daten) und entkoppelt den Snapshot vom Input.

    ``strategy`` (optional): wird als ``strategy_hash`` (kanonischer SHA-256,
    Einweg) und ``strategy_content`` (JSON-Kopie, nur fuer den lokalen
    Feld-Level-Diff) im Snapshot abgelegt. Beide Felder sind gitignored und
    werden nie geloggt; ``strategy_content`` geht nie in LLM-Prompts. Ohne
    ``strategy`` (Dry-Run/alte Aufrufe) bleiben beide Felder None.
    """
    meta = dict(sc_meta) if sc_meta else {"source": "manual"}
    strategy_hash = None
    strategy_content = None
    if isinstance(strategy, dict):
        strategy_hash = analyze.strategy_hash(strategy)
        strategy_content = json.loads(json.dumps(strategy))
    return {
        "schema_version": SCHEMA_VERSION,
        "captured_at": captured_at or now_iso(),
        "mode": mode,
        "portfolio": json.loads(json.dumps(portfolio)),
        "transactions": json.loads(json.dumps(transactions)),
        "sc_meta": meta,
        "strategy_hash": strategy_hash,
        "strategy_content": strategy_content,
    }


def read_snapshot(path: Path) -> dict | None:
    """Sicherer Leseversuch: fehlend/korrupt/falsche Schema-Version -> None.

    Wirft nie; laesst unbekannte Inhalte unbearbeitet (kein Logging).
    """
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    if raw.get("schema_version") != SCHEMA_VERSION:
        return None
    return raw


def _write_atomic(path: Path, data: dict) -> Path:
    """Atomar schreiben: Temp-Datei im Zielverzeichnis + os.replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".tmp-snapshot-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return path


def _archive_name(previous: dict) -> str:
    ts = None
    try:
        ts = datetime.fromisoformat(str(previous.get("captured_at")))
    except (TypeError, ValueError):
        ts = None
    if ts is None:
        ts = datetime.now(timezone.utc)
    return f"{_ARCHIVE_PREFIX}{ts.strftime('%Y%m%d-%H%M%S')}{_ARCHIVE_SUFFIX}"


def _unique_archive_target(previous: dict) -> Path:
    """Kollisionen (gleiche Sekunde) durch numerisches Suffix vermeiden."""
    base = _archive_name(previous)
    stem = base[: -len(_ARCHIVE_SUFFIX)]
    target = ARCHIVE_DIR / base
    counter = 2
    while target.exists():
        target = ARCHIVE_DIR / f"{stem}-{counter}{_ARCHIVE_SUFFIX}"
        counter += 1
    return target


def archive_previous(current: dict) -> Path | None:
    """Vorgaenger-Snapshot ins Archiv kopieren (idempotenter Name)."""
    if current is None:
        return None
    target = _unique_archive_target(current)
    _write_atomic(target, current)
    return target


def read_latest_archive() -> dict | None:
    """Neuester lesbarer Archiv-Snapshot (Namens-Sortierung = Zeit-Reihenfolge)."""
    if not ARCHIVE_DIR.is_dir():
        return None
    names = sorted(p.name for p in ARCHIVE_DIR.glob(f"{_ARCHIVE_PREFIX}*.json"))
    for name in reversed(names):
        snapshot = read_snapshot(ARCHIVE_DIR / name)
        if snapshot is not None:
            return snapshot
    return None


def load_previous() -> dict | None:
    """Bester verfuegbarer Vorgaenger: aktueller Stand, sonst neuester Archiv-Eintrag.

    Fehlt der aktuelle Stand oder ist er korrupt, faellt der Aufruf auf das
    neueste lesbare Archiv-Archiv zurueck (Rollback-Kette).
    """
    current = read_snapshot(CURRENT_PATH)
    if current is not None:
        return current
    return read_latest_archive()


def capture(
    portfolio: dict,
    transactions: list,
    mode: str | None = None,
    sc_meta: dict | None = None,
    captured_at: str | None = None,
    strategy: dict | None = None,
) -> dict:
    """Snapshot erfassen: Vorgaenger archivieren, neuen Stand atomar schreiben.

    Vorgaenger = Inhalt von ``snapshot.current.json`` vor dem Schreiben
    (fehlend/korrupt -> None, kein Crash). ``strategy`` wird an
    ``build_snapshot`` durchgereicht (Hash + Content, siehe dort).
    Rueckgabe: ``{"snapshot", "previous", "archive_path"}``.
    """
    snapshot = build_snapshot(
        portfolio,
        transactions,
        mode=mode,
        sc_meta=sc_meta,
        captured_at=captured_at,
        strategy=strategy,
    )
    previous = read_snapshot(CURRENT_PATH)
    archive_path = archive_previous(previous) if previous is not None else None
    _write_atomic(CURRENT_PATH, snapshot)
    return {
        "snapshot": snapshot,
        "previous": previous,
        "archive_path": str(archive_path) if archive_path else None,
    }


def capture_staged(
    portfolio: dict,
    transactions: list,
    mode: str | None = None,
    sc_meta: dict | None = None,
    captured_at: str | None = None,
    strategy: dict | None = None,
) -> dict:
    """Live-Stand erfassen, aber NUR staged: die Baseline bleibt unangetastet.

    Schreibt nach ``config/snapshot.staged.json`` (atomar, gitignored) und
    archiviert keinen Vorgaenger. Erst ein erfolgreicher Lauf (final_gate
    bestanden + Render ok) promoted den staged Stand via ``promote_staged``;
    jeder Fehlerpfad verwirft ihn via ``discard_staged``.
    Rueckgabe: ``{"snapshot", "staged_path"}``.
    """
    snapshot_dict = build_snapshot(
        portfolio,
        transactions,
        mode=mode,
        sc_meta=sc_meta,
        captured_at=captured_at,
        strategy=strategy,
    )
    staged_path = _write_atomic(STAGED_PATH, snapshot_dict)
    return {"snapshot": snapshot_dict, "staged_path": str(staged_path)}


def promote_staged() -> dict | None:
    """Staged-Snapshot zur neuen Baseline machen (Vorgaenger archivieren).

    Liest ``config/snapshot.staged.json``, archiviert den aktuellen Stand
    (Vorgaenger), schreibt staged -> current (atomar) und loescht staged.
    Kein staged (fehlend/korrupt) -> None, kein Schreiben.
    Rueckgabe: ``{"snapshot", "previous", "archive_path"}`` oder None.
    """
    staged = read_snapshot(STAGED_PATH)
    if staged is None:
        return None
    previous = read_snapshot(CURRENT_PATH)
    archive_path = archive_previous(previous) if previous is not None else None
    _write_atomic(CURRENT_PATH, staged)
    try:
        STAGED_PATH.unlink()
    except OSError:
        pass
    return {
        "snapshot": staged,
        "previous": previous,
        "archive_path": str(archive_path) if archive_path else None,
    }


def discard_staged() -> bool:
    """Staged-Snapshot verwerfen (Fehlerpfad): Baseline bleibt unangetastet.

    Loescht ``config/snapshot.staged.json`` falls vorhanden.
    Rueckgabe: True wenn geloescht, False wenn nicht vorhanden/nicht loeschbar.
    """
    try:
        STAGED_PATH.unlink()
    except FileNotFoundError:
        return False
    except OSError:
        return False
    return True


if __name__ == "__main__":
    import sys

    try:
        portfolio = json.loads((CONFIG_DIR / "portfolio.json").read_text(encoding="utf-8"))
        transactions = json.loads((CONFIG_DIR / "transactions.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        sys.exit(f"snapshot: Konfiguration nicht lesbar: {exc}")
    mode = sys.argv[1] if len(sys.argv) > 1 else None
    result = capture(portfolio, transactions, mode=mode, sc_meta={"source": "config"})
    print(f"Snapshot gespeichert: {CURRENT_PATH} (previous: {result['previous'] is not None})")
