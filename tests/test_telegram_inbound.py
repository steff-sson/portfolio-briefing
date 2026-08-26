"""Tests für telegram_inbound: Whitelist, Canned-Ack, Offset-Semantik, Persistenz, SecOps.

Keine echten Telegram-/API-Aufrufe: httpx.Client wird mit MockTransport
gepatcht; ENV wird isoliert; Persistenz läuft in tmp_path. Kein Token darf in
Logs oder Fehlerpfaden erscheinen.
"""

from __future__ import annotations

import json
import logging

import httpx

from scripts import send_telegram, telegram_inbound

# Fake-Token im echten Telegram-Format (bot<id>:<secret>), damit die
# Redaktions-Regex greift. Niemals ein echtes Secret hier.
FAKE_TOKEN = "1234567890:AAHfakefakefakefakefakefakefakefakefakefake"
FAKE_CHAT_ID = "-1001234567890"
OTHER_CHAT_ID = "987654321"
TOKEN_URL = f"https://api.telegram.org/bot{FAKE_TOKEN}/getUpdates"

BASE_ENDPOINT = "/bot" + FAKE_TOKEN


def _patch_env(monkeypatch, tmp_path) -> None:
    """Isoliert ENV (kein echter config.env) und Persistenz-Pfade."""
    monkeypatch.setattr(send_telegram, "ENV_PATH", tmp_path / "no.env")
    monkeypatch.setenv("TELEGRAM_TOKEN", FAKE_TOKEN)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", FAKE_CHAT_ID)
    monkeypatch.setattr(telegram_inbound, "OPEN_POINTS_PATH", tmp_path / "setup" / "open_points.json")
    monkeypatch.setattr(telegram_inbound, "OFFSET_PATH", tmp_path / "setup" / "telegram.offset")
    monkeypatch.setattr(telegram_inbound, "LOCK_PATH", tmp_path / "setup" / "telegram.lock")


def _write_setup_file(monkeypatch, tmp_path, name: str, content: str) -> None:
    """Schreibt eine Persistenz-Datei (Elternverzeichnis vorher anlegen)."""
    target = tmp_path / "setup" / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def _mock_client(monkeypatch, handler) -> httpx.MockTransport:
    transport = httpx.MockTransport(handler)
    real_client = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda *args, **kwargs: real_client(*args, transport=transport, **kwargs))
    return transport


def _ok_get_updates(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"ok": True, "result": []}, request=request)


def _text_update(update_id: int, message_id: int, chat_id: str | int, text: str, date: int = 1_700_000_000) -> dict:
    return {
        "update_id": update_id,
        "message": {
            "message_id": message_id,
            "chat": {"id": chat_id},
            "text": text,
            "date": date,
        },
    }


# --- Whitelist ---------------------------------------------------------------


def test_whitelist_accepts_configured_chat(monkeypatch, tmp_path):
    """Konfigurierte Chat-ID (str) wird akzeptiert; int-Form ebenfalls."""
    _patch_env(monkeypatch, tmp_path)
    assert telegram_inbound._whitelist_check(FAKE_CHAT_ID) is True
    assert telegram_inbound._whitelist_check(int(FAKE_CHAT_ID)) is True
    assert telegram_inbound._whitelist_check(" -1001234567890 ") is True  # getrimmt


def test_whitelist_blocks_foreign_chat(monkeypatch, tmp_path, caplog):
    """Fremde Chat-ID: verworfen (None), geloggt ohne Text, kein Ack."""
    _patch_env(monkeypatch, tmp_path)
    caplog.set_level(logging.INFO)
    parsed = telegram_inbound._parse_message(
        _text_update(1, 100, OTHER_CHAT_ID, "SUSE bewerten lassen!", date=1_700_000_000)
    )
    assert parsed is None
    assert "fremde Chat-ID" in caplog.text
    assert "SUSE bewerten lassen" not in caplog.text  # Text nie geloggt
    assert OTHER_CHAT_ID in caplog.text  # nur Chat-ID + update_id


def test_whitelist_blocks_non_text(monkeypatch, tmp_path, caplog):
    """Nicht-Text-Nachrichten (z.B. photo): None, geloggt, kein Ack."""
    _patch_env(monkeypatch, tmp_path)
    caplog.set_level(logging.INFO)
    update = {
        "update_id": 2,
        "message": {
            "message_id": 101,
            "chat": {"id": FAKE_CHAT_ID},
            "photo": [{"file_id": "abc"}],
        },
    }
    assert telegram_inbound._parse_message(update) is None
    assert "Nicht-Text-Nachricht" in caplog.text


# --- Canned-Ack --------------------------------------------------------------


def test_canned_ack_is_static_and_sends(monkeypatch, tmp_path):
    """Canned-Ack: statischer Text, kein User-Text, kein LLM, kein Echo."""
    _patch_env(monkeypatch, tmp_path)
    payloads: list[dict] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        payloads.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True}, request=request)

    _mock_client(monkeypatch, _handler)

    assert telegram_inbound._send_canned_ack(FAKE_CHAT_ID, 100) is True
    assert len(payloads) == 1
    assert payloads[0]["text"] == telegram_inbound.ACK_TEXT
    assert "SUSE" not in payloads[0]["text"]
    assert payloads[0]["chat_id"] == FAKE_CHAT_ID
    assert "parse_mode" not in payloads[0]


# --- Offset-Semantik ---------------------------------------------------------


def _pull_and_ack_with_updates(monkeypatch, tmp_path, updates):
    _patch_env(monkeypatch, tmp_path)
    sent: list[dict] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"/bot{FAKE_TOKEN}/sendMessage":
            sent.append(json.loads(request.content))
            return httpx.Response(200, json={"ok": True}, request=request)
        return httpx.Response(200, json={"ok": True, "result": updates}, request=request)

    _mock_client(monkeypatch, _handler)
    result = telegram_inbound.pull_and_ack()
    return result, sent


def test_offset_advances_after_ack_and_persist(monkeypatch, tmp_path):
    """Offset wird erst nach erfolgreichem Ack + Persist fortgeschrieben."""
    updates = [_text_update(10, 100, FAKE_CHAT_ID, "SUSE bewerten lassen!")]
    result, sent = _pull_and_ack_with_updates(monkeypatch, tmp_path, updates)

    assert result["acked"] == 1
    assert result["accepted"] == 1
    assert result["skipped"] == 0
    assert len(sent) == 1  # genau ein Ack
    assert telegram_inbound.OFFSET_PATH.read_text() == "11"  # max update_id + 1
    points = telegram_inbound.load_open_points()
    assert len(points) == 1
    assert points[0]["message_id"] == 100
    assert points[0]["chat_id"] == FAKE_CHAT_ID
    assert points[0]["text"] == "SUSE bewerten lassen!"
    assert points[0]["status"] == "open"
    assert points[0]["briefing_date"] is None


def test_ack_failure_leaves_offset_unchanged(monkeypatch, tmp_path, caplog):
    """Ack-Fehler: kein Open Point, Offset unverändert (kein Advance)."""
    _patch_env(monkeypatch, tmp_path)
    _write_setup_file(monkeypatch, tmp_path, "telegram.offset", "10")
    updates = [_text_update(10, 100, FAKE_CHAT_ID, "SUSE bewerten lassen!")]

    def _handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"/bot{FAKE_TOKEN}/sendMessage":
            return httpx.Response(500, json={"ok": False}, request=request)
        return httpx.Response(200, json={"ok": True, "result": updates}, request=request)

    _mock_client(monkeypatch, _handler)
    caplog.set_level(logging.ERROR, logger="scripts.send_telegram")

    result = telegram_inbound.pull_and_ack()

    assert result["acked"] == 0
    assert result["skipped"] == 1
    assert telegram_inbound.OFFSET_PATH.read_text() == "10"  # unverändert
    assert telegram_inbound.load_open_points() == []  # nichts persistiert


def test_persist_failure_leaves_offset_unchanged(monkeypatch, tmp_path):
    """Persistenz-Fehler: Offset unverändert (kein Advance), Ack aber gesendet."""
    _patch_env(monkeypatch, tmp_path)
    _write_setup_file(monkeypatch, tmp_path, "telegram.offset", "10")
    updates = [_text_update(10, 100, FAKE_CHAT_ID, "SUSE bewerten lassen!")]

    def _handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"/bot{FAKE_TOKEN}/sendMessage":
            return httpx.Response(200, json={"ok": True}, request=request)
        return httpx.Response(200, json={"ok": True, "result": updates}, request=request)

    _mock_client(monkeypatch, _handler)

    def _boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(telegram_inbound, "persist_open_point", _boom)

    result = telegram_inbound.pull_and_ack()

    assert result["acked"] == 1
    assert result["skipped"] == 1
    assert telegram_inbound.OFFSET_PATH.read_text() == "10"  # unverändert


def test_empty_pull_is_idempotent(monkeypatch, tmp_path):
    """Keine neuen Updates: kein Ack, kein Schreiben (Idempotenz)."""
    _patch_env(monkeypatch, tmp_path)
    _mock_client(monkeypatch, _ok_get_updates)

    result = telegram_inbound.pull_and_ack()

    assert result == {"pulled": 0, "accepted": 0, "acked": 0, "skipped": 0, "lock_busy": False}
    assert not telegram_inbound.OFFSET_PATH.exists()
    assert not telegram_inbound.OPEN_POINTS_PATH.exists()


def test_duplicate_update_is_not_stored_twice(monkeypatch, tmp_path):
    """Wiederholtes Update (Offset-Korruption): kein zweiter Open Point, kein Re-Ack."""
    _patch_env(monkeypatch, tmp_path)
    updates = [_text_update(10, 100, FAKE_CHAT_ID, "SUSE bewerten lassen!")]
    sent: list[dict] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"/bot{FAKE_TOKEN}/sendMessage":
            sent.append(json.loads(request.content))
            return httpx.Response(200, json={"ok": True}, request=request)
        return httpx.Response(200, json={"ok": True, "result": updates}, request=request)

    _mock_client(monkeypatch, _handler)

    result1 = telegram_inbound.pull_and_ack()
    assert result1["acked"] == 1
    assert telegram_inbound.OFFSET_PATH.read_text() == "11"
    assert len(sent) == 1

    # Offset künstlich zurückgesetzt -> gleiche Updates kommen erneut.
    _write_setup_file(monkeypatch, tmp_path, "telegram.offset", "5")
    result2 = telegram_inbound.pull_and_ack()

    assert result2["skipped"] == 1
    assert result2["acked"] == 0
    assert len(sent) == 1  # kein Re-Ack
    assert len(telegram_inbound.load_open_points()) == 1  # kein Duplikat


def test_foreign_chat_update_advances_offset_without_ack(monkeypatch, tmp_path):
    """Fremder Chat wird verworfen, aber der Offset überspringt ihn (kein Loop)."""
    updates = [_text_update(20, 200, OTHER_CHAT_ID, "ignoriere mich")]
    result, sent = _pull_and_ack_with_updates(monkeypatch, tmp_path, updates)

    assert result["accepted"] == 0
    assert result["acked"] == 0
    assert len(sent) == 0
    assert telegram_inbound.OFFSET_PATH.read_text() == "21"
    assert telegram_inbound.load_open_points() == []


# --- Persistenz --------------------------------------------------------------


def test_persist_and_load_roundtrip(monkeypatch, tmp_path):
    """Open Points überleben einen Restart (Laden aus Datei)."""
    _patch_env(monkeypatch, tmp_path)
    point = {
        "message_id": 100,
        "chat_id": FAKE_CHAT_ID,
        "text": "Sektorlimit anpassen",
        "received_at": "2026-08-26T14:30:00+00:00",
    }
    telegram_inbound.persist_open_point(point)

    loaded = telegram_inbound.load_open_points()
    assert len(loaded) == 1
    assert loaded[0]["status"] == "open"
    assert loaded[0]["briefing_date"] is None
    assert loaded[0]["text"] == "Sektorlimit anpassen"

    # mark_resolved entfernt ihn aus load_open_points
    telegram_inbound.mark_resolved(100)
    assert telegram_inbound.load_open_points() == []
    all_points = json.loads(telegram_inbound.OPEN_POINTS_PATH.read_text(encoding="utf-8"))
    assert all_points[0]["status"] == "resolved"


def test_atomic_write_creates_no_temp_leftovers(monkeypatch, tmp_path):
    """Atomare Persistenz: Temp-Dateien werden aufgeräumt, Inhalt korrekt."""
    _patch_env(monkeypatch, tmp_path)
    point = {"message_id": 1, "chat_id": FAKE_CHAT_ID, "text": "x", "received_at": "2026-08-26T00:00:00+00:00"}
    telegram_inbound.persist_open_point(point)
    telegram_inbound.advance_offset(42)

    leftovers = list((tmp_path / "setup").glob(".tmp-telegram-*"))
    assert leftovers == []
    assert telegram_inbound.OFFSET_PATH.read_text() == "42"
    assert json.loads(telegram_inbound.OPEN_POINTS_PATH.read_text(encoding="utf-8"))[0]["text"] == "x"


def test_corrupt_open_points_file_recovers(monkeypatch, tmp_path):
    """Korrupte open_points.json: wie leer behandeln, nächster Write repariert."""
    _patch_env(monkeypatch, tmp_path)
    _write_setup_file(monkeypatch, tmp_path, "open_points.json", "{ kein json")
    assert telegram_inbound.load_open_points() == []

    telegram_inbound.persist_open_point(
        {"message_id": 7, "chat_id": FAKE_CHAT_ID, "text": "neu", "received_at": "2026-08-26T00:00:00+00:00"}
    )
    loaded = telegram_inbound.load_open_points()
    assert len(loaded) == 1
    assert loaded[0]["message_id"] == 7


def test_retention_removes_old_resolved(monkeypatch, tmp_path):
    """Resolved-Einträge älter als 30 Tage werden beim nächsten Lauf entfernt."""
    _patch_env(monkeypatch, tmp_path)
    old = "2020-01-01T00:00:00+00:00"
    fresh = "2026-08-26T00:00:00+00:00"
    telegram_inbound.persist_open_point({"message_id": 1, "chat_id": FAKE_CHAT_ID, "text": "alt", "received_at": old})
    telegram_inbound.persist_open_point(
        {"message_id": 2, "chat_id": FAKE_CHAT_ID, "text": "frisch", "received_at": fresh}
    )
    telegram_inbound.mark_resolved(1)
    telegram_inbound.mark_resolved(2)

    cleaned = telegram_inbound._cleanup_resolved(telegram_inbound._load_all_points())
    assert [p["message_id"] for p in cleaned] == [2]


# --- Lock --------------------------------------------------------------------


def test_lock_blocks_second_concurrent_pull(monkeypatch, tmp_path):
    """Zweiter konkurrierender Lauf: Skip mit Hinweis, kein Request, kein Schreiben."""
    _patch_env(monkeypatch, tmp_path)
    _mock_client(monkeypatch, _ok_get_updates)

    with telegram_inbound._acquire_lock() as held:
        assert held is True
        result = telegram_inbound.pull_and_ack()
        assert result["lock_busy"] is True
        assert result["pulled"] == 0
        assert not telegram_inbound.OFFSET_PATH.exists()


# --- SecOps: Token-Redaction -------------------------------------------------


def test_token_never_in_logs(caplog, monkeypatch, tmp_path):
    """Auch bei HTTP-Fehlern: kein Token, keine API-URL in Logs."""
    _patch_env(monkeypatch, tmp_path)
    caplog.set_level(logging.DEBUG)

    def _explode(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom " + TOKEN_URL, request=request)

    _mock_client(monkeypatch, _explode)

    telegram_inbound.pull_updates(offset=10)

    assert FAKE_TOKEN not in caplog.text
    assert "bot1234567890" not in caplog.text
    assert "bot[REDACTED]" in caplog.text
    assert "1234567890:AAH" not in caplog.text


def test_ok_false_response_is_sanitized(caplog, monkeypatch, tmp_path):
    """ok=false mit Token-URL in description: wird redigiert, nie geloggt."""
    _patch_env(monkeypatch, tmp_path)
    caplog.set_level(logging.ERROR, logger="scripts.telegram_inbound")

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"ok": False, "description": f"Unauthorized: {TOKEN_URL}"},
            request=request,
        )

    _mock_client(monkeypatch, _handler)

    assert telegram_inbound.pull_updates() == []
    assert FAKE_TOKEN not in caplog.text
    assert "bot1234567890" not in caplog.text
    assert "bot[REDACTED]" in caplog.text


def test_corrupt_offset_logs_no_token(monkeypatch, tmp_path, caplog):
    """Korruptes Offset-File: Warnung, safer Reset (Pull ohne Offset), kein Token."""
    _patch_env(monkeypatch, tmp_path)
    caplog.set_level(logging.INFO)
    _write_setup_file(monkeypatch, tmp_path, "telegram.offset", "not-an-int")
    requests_seen: list[httpx.Request] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        requests_seen.append(request)
        return httpx.Response(200, json={"ok": True, "result": []}, request=request)

    _mock_client(monkeypatch, _handler)
    result = telegram_inbound.pull_and_ack()

    assert result["lock_busy"] is False
    assert "korrupt" in caplog.text
    assert requests_seen, "getUpdates-Request muss gesendet worden sein"
    assert "offset" not in requests_seen[0].url.params
    assert FAKE_TOKEN not in caplog.text


# --- P7: kein Usertext/keine Secrets in Logs (untrusted offene Punkte) --------


def test_open_points_content_never_logged_by_pipeline(caplog, monkeypatch, tmp_path):
    """Untrusted User-Text offener Punkte erscheint nie in normalen Logs —
    auch nicht via verify-Findings oder Fehlerpfaden."""
    from scripts import run_briefing

    _patch_env(monkeypatch, tmp_path)
    caplog.set_level(logging.DEBUG)

    secret_text = "GEHEIMER-PUNKT-TOKEN-42"
    point = {
        "message_id": 99,
        "chat_id": FAKE_CHAT_ID,
        "text": secret_text,
        "received_at": "2026-08-26T14:30:00+00:00",
        "status": "open",
        "briefing_date": None,
    }
    telegram_inbound.persist_open_point(point)
    loaded = telegram_inbound.load_open_points()
    assert loaded[0]["text"] == secret_text  # Persistenz funktioniert

    # verify-Findings enthalten keinen User-Text (nur generische Beschreibung).
    from scripts import verify

    facts_package = {
        "open_points": [{"text": secret_text, "untrusted": True}],
        "deterministic_summary": {},
    }
    findings = verify.verify_draft(
        facts_package,
        "## Kurzlage\nOK\n\n## Datenqualität\n—\n\n"
        "## Sell-/Reduce-Signale (bestehende Satellites)\nKeine.\n\n"
        "## Watchlist-Signale\nKeine.\n\n## Empfehlung\nWATCH\n\n## Nächster Schritt\nKeine Aktion.",
    )
    for finding in findings:
        assert secret_text not in str(finding)
    assert secret_text not in caplog.text
