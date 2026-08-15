"""Snapshot-Tests: Schema, atomares Schreiben/Archivierung, defensive Vorgaenger.

Reine lokale Mockdaten (conftest-Fixtures + Inline-Varianten) — keine
API-/Telegram-/Broker-Aufrufe.
"""
from __future__ import annotations

import json

import pytest

from scripts import snapshot

T1 = "2026-08-13T10:00:00+00:00"
T2 = "2026-08-13T10:05:00+00:00"


@pytest.fixture
def paths(monkeypatch, tmp_path):
    current = tmp_path / "config" / "snapshot.current.json"
    staged = tmp_path / "config" / "snapshot.staged.json"
    archive = tmp_path / "config" / "snapshots" / "archive"
    current.parent.mkdir(parents=True, exist_ok=True)
    archive.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(snapshot, "CURRENT_PATH", current)
    monkeypatch.setattr(snapshot, "STAGED_PATH", staged)
    monkeypatch.setattr(snapshot, "ARCHIVE_DIR", archive)
    return current, archive


def _read(path) -> dict | None:
    return snapshot.read_snapshot(path)


# --- Schema -----------------------------------------------------------------


def test_build_snapshot_schema(portfolio, transactions):
    snap = snapshot.build_snapshot(
        portfolio, transactions, mode="monday", sc_meta={"source": "sc"}, captured_at=T1
    )
    assert snap["schema_version"] == 1
    assert snap["captured_at"] == T1
    assert snap["mode"] == "monday"
    assert snap["portfolio"] == portfolio
    assert snap["transactions"] == transactions
    assert snap["sc_meta"] == {"source": "sc"}


def test_build_snapshot_defaults(portfolio, transactions):
    snap = snapshot.build_snapshot(portfolio, transactions)
    assert snap["mode"] is None
    assert snap["sc_meta"] == {"source": "manual"}
    assert snapshot.now_iso()  # ISO-Format erzeugbar
    # captured_at ist ISO-8601 und parsbar
    from datetime import datetime

    datetime.fromisoformat(snap["captured_at"])


def test_build_snapshot_does_not_mutate_inputs(portfolio, transactions):
    portfolio_copy = json.loads(json.dumps(portfolio))
    transactions_copy = json.loads(json.dumps(transactions))
    sc_meta = {"source": "sc"}
    snap = snapshot.build_snapshot(portfolio, transactions, sc_meta=sc_meta, captured_at=T1)
    # Snapshot nachtraeglich veraendern darf die Inputs nicht beruehren
    snap["portfolio"]["holdings"][0]["value_eur"] = 99999.0
    snap["transactions"].clear()
    snap["sc_meta"]["extra"] = "leak"
    assert portfolio == portfolio_copy
    assert transactions == transactions_copy
    assert sc_meta == {"source": "sc"}


# --- Atomares Schreiben / Archivierung --------------------------------------


def test_capture_first_run_writes_current_no_archive(paths, portfolio, transactions):
    current, archive = paths
    result = snapshot.capture(portfolio, transactions, mode="monday", captured_at=T1)

    assert result["previous"] is None
    assert result["archive_path"] is None
    assert current.exists()
    assert _read(current) == result["snapshot"]
    # Erstlauf: nichts zu archivieren
    assert not archive.exists() or list(archive.glob("*.json")) == []


def test_capture_archives_previous(paths, portfolio, transactions):
    current, archive = paths
    r1 = snapshot.capture(portfolio, transactions, mode="monday", captured_at=T1)
    r2 = snapshot.capture(portfolio, transactions, mode="monday", captured_at=T2)

    archived = list(archive.glob("*.json"))
    assert len(archived) == 1
    assert archived[0].name == "snapshot-20260813-100000.json"
    assert _read(archived[0]) == r1["snapshot"]
    assert _read(current) == r2["snapshot"]
    assert r2["previous"] == r1["snapshot"]
    assert r2["archive_path"] == str(archived[0])


def test_capture_no_temp_leftovers(paths, portfolio, transactions):
    current, _ = paths
    snapshot.capture(portfolio, transactions, captured_at=T1)
    snapshot.capture(portfolio, transactions, captured_at=T2)
    leftovers = [p for p in current.parent.glob(".tmp-snapshot-*")]
    assert leftovers == []


def test_capture_writes_valid_json_roundtrip(paths, portfolio, transactions):
    current, _ = paths
    result = snapshot.capture(portfolio, transactions, captured_at=T1)
    assert json.loads(current.read_text(encoding="utf-8")) == result["snapshot"]


def test_archive_name_collision_gets_suffix(paths, portfolio, transactions):
    current, archive = paths
    snapshot.capture(portfolio, transactions, captured_at=T1)
    snapshot.capture(portfolio, transactions, captured_at=T1)  # -> archiviert V1 als ...-100000.json
    snapshot.capture(portfolio, transactions, captured_at=T1)  # -> Kollision, V2 als ...-100000-2.json

    names = sorted(p.name for p in archive.glob("*.json"))
    assert names == ["snapshot-20260813-100000-2.json", "snapshot-20260813-100000.json"]
    assert len(names) == len(set(names))  # keine Ueberschreibung
    assert _read(current) == snapshot.load_previous()


# --- Defensive Vorgaenger-Behandlung ----------------------------------------


def test_load_previous_missing_returns_none(paths):
    assert snapshot.load_previous() is None


def test_read_snapshot_corrupt_returns_none(paths):
    current, _ = paths
    current.write_text("{kaputt", encoding="utf-8")
    assert snapshot.read_snapshot(current) is None
    assert snapshot.load_previous() is None


def test_read_snapshot_wrong_schema_version_returns_none(paths):
    current, _ = paths
    current.write_text(json.dumps({"schema_version": 999, "portfolio": {}}), encoding="utf-8")
    assert snapshot.read_snapshot(current) is None


def test_capture_with_corrupt_predecessor_keeps_working(paths, portfolio, transactions):
    current, _ = paths
    current.write_text("{defekt", encoding="utf-8")

    result = snapshot.capture(portfolio, transactions, captured_at=T1)

    assert result["previous"] is None  # korrupter Vorgaenger -> wie "kein Vorgaenger"
    assert result["archive_path"] is None
    assert _read(current) == result["snapshot"]  # neuer Stand geschrieben


def test_load_previous_falls_back_to_latest_archive(paths, portfolio, transactions):
    current, _ = paths
    r1 = snapshot.capture(portfolio, transactions, captured_at=T1)
    snapshot.capture(portfolio, transactions, captured_at=T2)  # archiviert r1
    current.write_text("{defekt", encoding="utf-8")

    previous = snapshot.load_previous()
    assert previous == r1["snapshot"]  # Fallback auf Archiv-Eintrag


def test_load_previous_prefers_current_over_corrupt_archive(paths, portfolio, transactions):
    _, archive = paths
    snapshot.capture(portfolio, transactions, captured_at=T1)
    snapshot.capture(portfolio, transactions, captured_at=T2)
    (archive / "snapshot-kaputt.json").write_text("{defekt", encoding="utf-8")

    previous = snapshot.load_previous()
    assert previous is not None
    assert previous["captured_at"] == T2  # gueltiger aktueller Stand schlaegt korruptes Archiv


def test_read_latest_archive_skips_corrupt_newest(paths, portfolio, transactions):
    current, archive = paths
    snapshot.capture(portfolio, transactions, captured_at=T1)
    snapshot.capture(portfolio, transactions, captured_at=T2)
    current.unlink()  # aktueller Stand weg -> Fallback auf Archiv
    (archive / "snapshot-99999999-999999.json").write_text("{defekt", encoding="utf-8")

    previous = snapshot.load_previous()
    assert previous is not None
    assert previous["captured_at"] == T1  # korruptes "neuestes" Archiv uebersprungen, naechstes lesbares genommen


# --- Staged-Snapshot (P0.1: Baseline erst nach erfolgreichem final_gate) -------


def test_capture_staged_writes_staged_file(paths, portfolio, transactions):
    """capture_staged schreibt nur staged — Baseline (current) bleibt unangetastet."""
    current, archive = paths
    result = snapshot.capture_staged(portfolio, transactions, mode="monday", captured_at=T1)

    assert result["snapshot"]["captured_at"] == T1
    assert result["staged_path"] == str(snapshot.STAGED_PATH)
    assert snapshot.STAGED_PATH.exists()
    assert _read(snapshot.STAGED_PATH) == result["snapshot"]
    # Baseline unveraendert: kein current, kein Archiv-Eintrag, kein Vorgaenger
    assert _read(current) is None
    assert snapshot.load_previous() is None
    assert list(archive.glob("*.json")) == []


def test_promote_staged_archives_previous_and_promotes(paths, portfolio, transactions):
    """promote_staged: Vorgaenger wird archiviert, staged -> current, staged geloescht."""
    current, archive = paths
    first = snapshot.capture(portfolio, transactions, mode="monday", captured_at=T1)
    staged = snapshot.capture_staged(portfolio, transactions, mode="monday", captured_at=T2)

    result = snapshot.promote_staged()

    assert result is not None
    assert result["snapshot"] == staged["snapshot"]
    assert result["previous"] == first["snapshot"]
    assert result["archive_path"] == str(archive / "snapshot-20260813-100000.json")
    assert _read(current) == staged["snapshot"]  # Baseline = staged
    assert not snapshot.STAGED_PATH.exists()  # staged geloescht
    assert _read(snapshot.STAGED_PATH) is None
    archived = list(archive.glob("*.json"))
    assert len(archived) == 1
    assert _read(archived[0]) == first["snapshot"]  # Vorgaenger archiviert


def test_promote_staged_no_staged_returns_none(paths):
    """Kein staged (fehlend) -> None, kein Schreiben."""
    current, archive = paths
    assert snapshot.promote_staged() is None
    assert not current.exists()
    assert not list(archive.glob("*.json"))


def test_discard_staged_removes_staged(paths, portfolio, transactions):
    snapshot.capture_staged(portfolio, transactions, captured_at=T1)
    assert snapshot.STAGED_PATH.exists()

    assert snapshot.discard_staged() is True
    assert not snapshot.STAGED_PATH.exists()


def test_discard_staged_no_staged_returns_false(paths):
    assert snapshot.discard_staged() is False


def test_staged_is_atomic(paths, portfolio, transactions):
    """Staged wird atomar geschrieben: kein Temp-Rest, kein korruptes Teil-File."""
    current, _ = paths
    snapshot.capture_staged(portfolio, transactions, captured_at=T1)
    r2 = snapshot.capture_staged(portfolio, transactions, captured_at=T2)

    leftovers = [p for p in snapshot.STAGED_PATH.parent.glob(".tmp-snapshot-*")]
    assert leftovers == []
    assert _read(snapshot.STAGED_PATH) == r2["snapshot"]
    assert not current.exists()  # Baseline unangetastet
