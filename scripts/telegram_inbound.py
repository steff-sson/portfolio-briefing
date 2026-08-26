"""Telegram-Inbound: getUpdates-Pull, Chat-Whitelist, Canned-Ack, Open-Point-Persistenz.

Kein Daemon, kein Long-Polling-Betrieb (Default ``timeout=0``): der Pull laeuft
einmalig (ad-hoc oder vor dem Briefing via Cron). Nur die konfigurierte
``TELEGRAM_CHAT_ID`` wird akzeptiert; fremde Chats werden verworfen (geloggt
ohne Text-Inhalt, kein Ack). Antworten erhalten eine statische Canned-Ack —
kein LLM, kein Echo des untrusted Texts.

Persistenz (alle gitignored via ``config/setup/``):
- ``config/setup/open_points.json`` — offene Punkte (JSON-Liste, atomar)
- ``config/setup/telegram.offset`` — letzter bestaetigter Offset (atomar)
- ``config/setup/telegram.lock`` — flock-Lock gegen konkurrierende Pulls

Reihenfolge (Plan P6): getUpdates → Whitelist/Parse → Ack → Persist → Offset.
Bei Ack-/Persistenz-Fehler bleibt der Offset unveraendert (Idempotenz: der
naechste Pull holt die Nachricht erneut; ``message_id`` verhindert Duplikate).

Offset-Korruption: safer Reset statt fail-closed-Stop. Pull ohne Offset liefert
alle Updates seit dem Telegram-Speicherfenster; der message_id-Dedupe in
``persist_open_point`` verhindert doppelte Open Points, ein erneuter Ack ist
harmlos, und der Offset heilt sich nach erfolgreichem Lauf selbst. So geht
keine Nachricht verloren (fail-safe); ein strikter Stop wuerde Updates bis zum
manuellen Eingriff festhalten.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from scripts import send_telegram

# send_telegram installiert den Bot-Token-Redaction-Filter auf die
# httpx/httpcore-Logger; hier explizit sicherstellen (idempotent).
send_telegram._install_redaction_filter()

logger = logging.getLogger(__name__)

ACK_TEXT = "Erhalten. Offener Punkt wird ins nächste Briefing aufgenommen."
RETENTION_DAYS = 30

ROOT = Path(__file__).resolve().parent.parent
SETUP_DIR = ROOT / "config" / "setup"
OPEN_POINTS_PATH = SETUP_DIR / "open_points.json"
OFFSET_PATH = SETUP_DIR / "telegram.offset"
LOCK_PATH = SETUP_DIR / "telegram.lock"

API_BASE = "https://api.telegram.org"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _write_atomic(path: Path, content: str) -> Path:
    """Atomar schreiben: Temp-Datei im Zielverzeichnis + os.replace.

    Wie in ``snapshot.py``/``setup_strategy.py``: fsync vor replace, Temp-Datei
    wird bei Fehlern aufgeraeumt.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".tmp-telegram-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
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


def _load_all_points(path: Path | None = None) -> list[dict]:
    """Alle Eintraege (open/consumed/resolved); korrupt/fehlend -> [].

    Wirft nie: korrupte Dateien werden wie "leer" behandelt (kein Crash),
    damit der naechste erfolgreiche Lauf die Datei atomar neu schreibt.
    """
    path = path or OPEN_POINTS_PATH
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(raw, list):
        return []
    return [p for p in raw if isinstance(p, dict)]


def load_open_points(path: Path | None = None) -> list[dict]:
    """Liefert nur Eintraege mit status ``open`` (P7-Einspeisung)."""
    path = path or OPEN_POINTS_PATH
    return [p for p in _load_all_points(path) if p.get("status") == "open"]


def persist_open_point(point: dict, path: Path | None = None) -> None:
    """Haengt einen Open Point atomar an — idempotent via ``message_id``.

    Existiert bereits ein Eintrag mit gleicher ``message_id`` (auch
    ``resolved``), wird nichts geschrieben: wiederholte Pulls (Offset-Korruption,
    fehlgeschlagener Offset-Advance) erzeugen keine Duplikate. Format wie Plan:
    ``{message_id, chat_id, text, received_at, status: "open", briefing_date: None}``.
    """
    path = path or OPEN_POINTS_PATH
    points = _load_all_points(path)
    message_id = point.get("message_id")
    if message_id is not None and any(p.get("message_id") == message_id for p in points):
        return
    entry = {
        "message_id": message_id,
        "chat_id": str(point.get("chat_id")),
        "text": point.get("text"),
        "received_at": point.get("received_at"),
        "status": "open",
        "briefing_date": None,
    }
    points.append(entry)
    _write_atomic(path, json.dumps(points, indent=2, ensure_ascii=False))


def mark_resolved(message_id: int, path: Path | None = None) -> None:
    """Setzt status auf ``resolved`` fuer alle Eintraege mit ``message_id`` (P7)."""
    path = path or OPEN_POINTS_PATH
    points = _load_all_points(path)
    changed = False
    for p in points:
        if p.get("message_id") == message_id and p.get("status") != "resolved":
            p["status"] = "resolved"
            changed = True
    if changed:
        _write_atomic(path, json.dumps(points, indent=2, ensure_ascii=False))


def advance_offset(offset: int, path: Path | None = None) -> None:
    """Persistiert den Offset atomar — nur nach erfolgreichem Ack + Persist."""
    path = path or OFFSET_PATH
    _write_atomic(path, str(offset))


def _load_offset(path: Path | None = None) -> int | None:
    """None bei fehlendem/korruptem Offset -> Pull ohne Offset (safer Reset).

    Begruendung: Pull ohne Offset liefert alle Updates seit dem Telegram-
    Speicherfenster (fail-safe, nichts geht verloren). Duplikate sind durch
    message_id-Dedupe in ``persist_open_point`` ausgeschlossen, ein erneuter
    Ack ist harmlos, und der Offset heilt sich nach erfolgreichem Lauf selbst.
    """
    path = path or OFFSET_PATH
    if not path.exists():
        return None
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except ValueError:
        logger.warning(
            "telegram.offset korrupt (%s) — Pull ohne Offset (safer Reset); "
            "Duplikat-Schutz via message_id aktiv, keine Updates verloren",
            path,
        )
        return None
    except OSError as exc:
        logger.warning("telegram.offset nicht lesbar (%s): %s", path, exc)
        return None


@contextmanager
def _acquire_lock(path: Path | None = None) -> Iterator[bool]:
    """Non-blocking flock: True = Lock gehalten, False = belegt (Skip).

    Bei belegtem Lock: Skip mit Hinweis, kein Warten, kein Blockieren.
    Die Lock-Datei bleibt bestehen (idempotent); flock wird beim Prozess-Exit
    automatisch freigegeben.
    """
    path = path or LOCK_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as lock_file:
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)


def _whitelist_check(chat_id: int | str) -> bool:
    """Nur die konfigurierte ``TELEGRAM_CHAT_ID`` wird akzeptiert.

    Str/int-sicher: beide Seiten werden auf ``str`` normalisiert und getrimmt
    (Telegram liefert private Chats als int, Supergruppen als str).
    """
    configured = os.getenv("TELEGRAM_CHAT_ID")
    if not configured:
        logger.warning("TELEGRAM_CHAT_ID nicht gesetzt — keine Chats akzeptiert")
        return False
    return str(configured).strip() == str(chat_id).strip()


def _received_at(message: dict) -> str:
    """Empfangszeit aus Telegram ``message.date`` (UTC-ISO), Fallback now."""
    try:
        timestamp = int(message["date"])
    except (KeyError, TypeError, ValueError):
        return _now_iso()
    try:
        return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat(timespec="seconds")
    except (OverflowError, OSError, ValueError):
        return _now_iso()


def _parse_message(update: dict) -> dict | None:
    """Extrahiert ``{message_id, update_id, chat_id, text, received_at}``.

    None bei fehlender/korrupter Struktur, Nicht-Text-Nachrichten oder fremden
    Chats. Fremde Chats werden verworfen und geloggt — ohne Text-Inhalt, nur
    Chat-ID + update_id, kein Ack.
    """
    if not isinstance(update, dict):
        return None
    update_id = update.get("update_id")
    if not isinstance(update_id, int):
        return None
    message = update.get("message")
    if not isinstance(message, dict):
        return None
    chat = message.get("chat")
    if not isinstance(chat, dict):
        return None
    text = message.get("text")
    if not isinstance(text, str) or not text.strip():
        logger.info("Telegram inbound: Nicht-Text-Nachricht ignoriert (update_id=%s)", update_id)
        return None
    message_id = message.get("message_id")
    if not isinstance(message_id, int):
        return None
    chat_id = chat.get("id")
    if not isinstance(chat_id, (int, str)):
        return None
    if not _whitelist_check(chat_id):
        logger.info(
            "Telegram inbound: fremde Chat-ID verworfen (chat_id=%s, update_id=%s)",
            chat_id,
            update_id,
        )
        return None
    return {
        "message_id": message_id,
        "update_id": update_id,
        "chat_id": chat_id,
        "text": text,
        "received_at": _received_at(message),
    }


def pull_updates(offset: int | None = None, timeout: int = 0) -> list[dict]:
    """Holt Updates via getUpdates — kein Daemon, kein Long-Polling-Betrieb.

    ``timeout=0`` (Default) = Short-Polling. Bei Fehlern: ``[]`` und nur
    sanitisierte Fehlermeldung — nie Token oder URL in Logs. Der Offset wird
    hier bewusst NICHT fortgeschrieben (das macht erst ``pull_and_ack`` nach
    erfolgreichem Ack + Persist).
    """
    send_telegram._load_env()
    token = os.getenv("TELEGRAM_TOKEN")
    if not token:
        logger.warning("TELEGRAM_TOKEN nicht gesetzt — getUpdates übersprungen")
        return []
    params: dict[str, int] = {}
    if offset is not None:
        params["offset"] = offset
    if timeout:
        params["timeout"] = timeout
    url = f"{API_BASE}/bot{token}/getUpdates"
    try:
        with httpx.Client(timeout=timeout + 10 if timeout else 30) as client:
            with send_telegram._suppress_http_request_logs():
                response = client.get(url, params=params)
            response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        # status/reason nur — nie die Request-URL (enthaelt den Token).
        logger.error(
            "Telegram getUpdates failed: HTTP %s %s",
            exc.response.status_code,
            exc.response.reason_phrase,
        )
        return []
    except httpx.RequestError as exc:
        logger.error("Telegram getUpdates failed: %s", send_telegram._sanitize(str(exc)))
        return []
    except OSError as exc:
        logger.error("Telegram getUpdates failed: %s", send_telegram._sanitize(str(exc)))
        return []
    try:
        data = response.json()
    except ValueError:
        logger.error("Telegram getUpdates failed: Antwort kein JSON")
        return []
    if not isinstance(data, dict) or data.get("ok") is not True:
        description = ""
        if isinstance(data, dict):
            description = send_telegram._sanitize(str(data.get("description", "")))
        logger.error("Telegram getUpdates failed: ok=false (%s)", description)
        return []
    result = data.get("result")
    return result if isinstance(result, list) else []


def _send_canned_ack(chat_id: int | str, message_id: int) -> bool:
    """Statische Canned-Ack via bestehender Outbound-Sendlogik.

    Kein LLM, kein Echo des untrusted Texts. ``send_telegram._send_telegram``
    uebernimmt Token-Redaction, HTTP-Suppression und Fehlerbehandlung.
    """
    send_telegram._load_env()
    token = os.getenv("TELEGRAM_TOKEN")
    if not token:
        logger.warning("TELEGRAM_TOKEN nicht gesetzt — Canned-Ack nicht gesendet")
        return False
    ok = send_telegram._send_telegram(token, str(chat_id), ACK_TEXT, None)
    if ok:
        logger.info("Canned-Ack gesendet (chat_id=%s, message_id=%s)", chat_id, message_id)
    return ok


def _cleanup_resolved(points: list[dict], days: int = RETENTION_DAYS) -> list[dict]:
    """Entfernt resolved-Eintraege, deren ``received_at`` aelter als ``days`` ist.

    Konservativ: Eintraege mit unparsbarem Datum bleiben erhalten.
    """
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=days)
    kept: list[dict] = []
    for point in points:
        if point.get("status") == "resolved":
            received = point.get("received_at")
            try:
                timestamp = datetime.fromisoformat(str(received))
                if timestamp.tzinfo is None:
                    timestamp = timestamp.replace(tzinfo=timezone.utc)
            except (TypeError, ValueError):
                timestamp = None
            if timestamp is not None and timestamp < cutoff:
                continue  # alt genug: loeschen
        kept.append(point)
    return kept


def pull_and_ack() -> dict:
    """Top-Level: Lock → getUpdates → Whitelist/Parse → Ack → Persist → Offset.

    Reihenfolge strikt (Plan P6): Der Offset wird erst nach erfolgreichem
    Ack + Persistenz fortgeschrieben. Bei Ack-/Persistenz-Fehler bleibt der
    Offset unveraendert — der naechste Pull holt die Nachricht erneut
    (Idempotenz via message_id-Dedupe). Bei belegtem Lock: Skip ohne Schreiben.

    Rueckgabe: ``{"pulled", "accepted", "acked", "skipped", "lock_busy"}``
    (``skipped``: Duplikat, Offset-Korruptions-Update oder Ack-/Persist-Fehler).

    Advance-Semantik (strikte Plan-Lesart): Der Offset wird NUR dann
    fortgeschrieben, wenn der komplette Batch fehlerfrei verarbeitet wurde
    (``failures == 0``). Bei jedem Ack-/Persistenz-Fehler bleibt der Offset
    unveraendert — der naechste Pull holt alles erneut; bereits persistierte
    Punkte werden via ``message_id`` dedupliziert (kein Doppel-Open-Point,
    kein Re-Ack). Verworfenes (fremde Chats, Nicht-Text, Duplikate) zaehlt als
    erledigt und wird vom Advance uebersprungen (sonst Endlosschleife).
    Leerer Pull: kein Schreiben (Idempotenz).
    """
    with _acquire_lock() as locked:
        if not locked:
            logger.info("Telegram inbound: Lock belegt (telegram.lock) — Lauf übersprungen")
            return {"pulled": 0, "accepted": 0, "acked": 0, "skipped": 0, "lock_busy": True}

        offset = _load_offset()
        updates = pull_updates(offset=offset)
        existing_ids = {p.get("message_id") for p in _load_all_points()}
        accepted: list[dict] = []
        skipped = 0
        for update in updates:
            parsed = _parse_message(update)
            if parsed is None:
                continue
            if parsed["message_id"] in existing_ids:
                skipped += 1
                continue
            accepted.append(parsed)

        acked = 0
        failures = 0
        for point in accepted:
            if not _send_canned_ack(point["chat_id"], point["message_id"]):
                failures += 1
                continue
            acked += 1
            try:
                persist_open_point(point)
            except OSError as exc:
                logger.error(
                    "Open-Point-Persistenz fehlgeschlagen (message_id=%s): %s",
                    point["message_id"],
                    exc,
                )
                failures += 1

        if updates and not failures:
            try:
                points = _cleanup_resolved(_load_all_points())
                _write_atomic(OPEN_POINTS_PATH, json.dumps(points, indent=2, ensure_ascii=False))
                update_ids: list[int] = [u["update_id"] for u in updates if isinstance(u.get("update_id"), int)]
                max_update = max(update_ids) if update_ids else None
                if max_update is not None:
                    advance_offset(max_update + 1)
            except OSError as exc:
                logger.error("Telegram inbound: Persistenz beim Advance fehlgeschlagen: %s", exc)
                failures += 1
        if failures:
            skipped += failures

    return {
        "pulled": len(updates),
        "accepted": len(accepted),
        "acked": acked,
        "skipped": skipped,
        "lock_busy": False,
    }


if __name__ == "__main__":
    print(pull_and_ack())
