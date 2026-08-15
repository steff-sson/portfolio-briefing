"""Fokussierte Tests: Telegram-Token-Leak-Praevention + Alert-Versandpfad.

Keine echten Telegram-/API-Aufrufe: httpx.Client wird mit MockTransport
gepatcht. Kein Token wird in Logs oder Fehlerpfaden ausgegeben.
"""
from __future__ import annotations

import json
import logging
import re

import httpx
import pytest

from scripts import send_telegram

# Fake-Token im echten Telegram-Format (bot<id>:<secret>), damit die
# Redaktions-Regex greift. Niemals ein echtes Secret hier.
FAKE_TOKEN = "1234567890:AAHfakefakefakefakefakefakefakefakefakefake"
FAKE_CHAT_ID = "-1001234567890"
TOKEN_URL = f"https://api.telegram.org/bot{FAKE_TOKEN}/sendMessage"

BRIEFING = "---\ntags: [test]\n---\n\n# Titel\n\nKurztext."
ALERT_TEXT = "⚠️ portfolio-briefing Fehler\n\nfacts failed: boom"


def _patch_env(monkeypatch, tmp_path) -> None:
    """Isoliert ENV (kein echter config.env) und Vault-Verzeichnis."""
    monkeypatch.setattr(send_telegram, "ENV_PATH", tmp_path / "no.env")
    monkeypatch.setenv("TELEGRAM_TOKEN", FAKE_TOKEN)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", FAKE_CHAT_ID)
    monkeypatch.setattr(send_telegram, "VAULT_DIR", tmp_path / "vault")


def _mock_client(monkeypatch, handler) -> httpx.MockTransport:
    transport = httpx.MockTransport(handler)
    real_client = httpx.Client  # vor dem Patch einfrieren (sonst Rekursion)
    monkeypatch.setattr(
        httpx, "Client", lambda *args, **kwargs: real_client(*args, transport=transport, **kwargs)
    )
    return transport


def _ok_handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"ok": True}, request=request)


def _long_briefing(n_sections: int = 8) -> str:
    """Briefing > 4096 Zeichen: Preamble + n Sektionen an ``## ``-Grenzen.

    Jede Sektion ist ~900 Zeichen lang — klein genug fuer einen Chunk,
    gross genug, dass das Gesamtdokument die Telegram-Grenze sprengt.
    """
    sections = "".join(
        f"## Sektion {i}\n\n" + "Absatz " * 150 + "\n" for i in range(n_sections)
    )
    return f"---\ntags: [test]\n---\n\n# Portfolio-Briefing\n\n{sections}"


def _strip_frontmatter(markdown: str) -> str:
    """Erwarteter Telegram-Text: wie in send_briefing, ohne Frontmatter."""
    return re.sub(r"^---\n.*?---\n", "", markdown, flags=re.DOTALL).strip()


# --- Leak-Praevention in Log-/Fehlerpfaden ---------------------------------


def test_success_send_logs_no_token_or_telegram_url(caplog, monkeypatch, tmp_path):
    """Erfolgreicher Versand: keine INFO-HTTP-Zeile, kein Token/URL in Logs."""
    _patch_env(monkeypatch, tmp_path)
    _mock_client(monkeypatch, _ok_handler)
    caplog.set_level(logging.INFO)

    assert send_telegram.send_briefing(BRIEFING, "monday") is True

    assert FAKE_TOKEN not in caplog.text
    assert "api.telegram.org" not in caplog.text
    assert "HTTP Request" not in caplog.text  # httpx-INFO-Logs sind unterdrueckt


def test_redaction_filter_redacts_token_in_httpx_args(caplog, monkeypatch, tmp_path):
    """Redaktions-Filter: selbst eine INFO-Zeile mit URL-Argument bleibt tokenfrei."""
    _patch_env(monkeypatch, tmp_path)  # Filter wird beim Import installiert
    caplog.set_level(logging.INFO, logger="httpx")

    logging.getLogger("httpx").info("HTTP Request: %s %s", "POST", httpx.URL(TOKEN_URL))

    assert FAKE_TOKEN not in caplog.text
    assert "bot1234567890" not in caplog.text
    assert "bot[REDACTED]" in caplog.text


def test_http_error_is_diagnosable_and_sanitized(caplog, monkeypatch, tmp_path):
    """HTTP-Fehler (400): Status diagnostizierbar, aber nie URL/Token im Log."""
    _patch_env(monkeypatch, tmp_path)
    payloads: list[dict] = []

    def _bad_request(request: httpx.Request) -> httpx.Response:
        payloads.append(json.loads(request.content))
        return httpx.Response(400, request=request)

    _mock_client(monkeypatch, _bad_request)
    caplog.set_level(logging.ERROR, logger="scripts.send_telegram")

    assert send_telegram.send_briefing(ALERT_TEXT, "alert") is False

    assert "400" in caplog.text  # technischer Fehler ist diagnostizierbar
    assert FAKE_TOKEN not in caplog.text
    assert "api.telegram.org" not in caplog.text
    assert "bot1234567890" not in caplog.text


def test_exception_with_url_in_message_is_sanitized(caplog, monkeypatch, tmp_path):
    """Generische Exception, deren Message eine Bot-URL enthaelt: wird redigiert."""
    _patch_env(monkeypatch, tmp_path)

    def _explode(request: httpx.Request) -> httpx.Response:
        raise RuntimeError(f"connection boom {TOKEN_URL}")

    _mock_client(monkeypatch, _explode)
    caplog.set_level(logging.ERROR, logger="scripts.send_telegram")

    assert send_telegram.send_briefing(ALERT_TEXT, "alert") is False

    assert "connection boom" in caplog.text  # Fehlerursache bleibt erkennbar
    assert FAKE_TOKEN not in caplog.text
    assert "bot1234567890" not in caplog.text  # Bot-ID/Secret redigiert
    assert "bot[REDACTED]" in caplog.text


# --- Alert-Versandpfad -----------------------------------------------------


def test_alert_mode_is_plain_text_and_never_archives(monkeypatch, tmp_path):
    """Alert: kein parse_mode (kein fehleranfaelliges Markdown), keine Vault-Datei."""
    _patch_env(monkeypatch, tmp_path)
    payloads: list[dict] = []

    def _capture(request: httpx.Request) -> httpx.Response:
        payloads.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True}, request=request)

    _mock_client(monkeypatch, _capture)

    assert send_telegram.send_briefing(ALERT_TEXT, "alert") is True

    assert len(payloads) == 1
    assert "parse_mode" not in payloads[0]  # Plain-Text statt Legacy-Markdown
    assert payloads[0]["text"] == ALERT_TEXT
    assert not (tmp_path / "vault").exists()  # Alert archiviert nie im Vault


def test_normal_mode_keeps_markdown_and_archives(monkeypatch, tmp_path):
    """Normales Briefing: Markdown bleibt, Frontmatter wird gestrippt, archiviert."""
    _patch_env(monkeypatch, tmp_path)
    payloads: list[dict] = []

    def _capture(request: httpx.Request) -> httpx.Response:
        payloads.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True}, request=request)

    _mock_client(monkeypatch, _capture)

    assert send_telegram.send_briefing(BRIEFING, "monday") is True

    assert len(payloads) == 1
    assert payloads[0]["parse_mode"] == "Markdown"
    assert "---" not in payloads[0]["text"]  # Frontmatter nicht im Telegram-Text
    archived = list((tmp_path / "vault").glob("*.md"))
    assert len(archived) == 1  # normales Briefing wird archiviert


def test_alert_text_with_embedded_bot_url_is_sanitized_before_send(monkeypatch, tmp_path):
    """Defensiv: Bot-URL im Alert-Text darf nie als Telegram-Text ankommen."""
    _patch_env(monkeypatch, tmp_path)
    payloads: list[dict] = []

    def _capture(request: httpx.Request) -> httpx.Response:
        payloads.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True}, request=request)

    _mock_client(monkeypatch, _capture)
    leaky_alert = f"Fehler: {TOKEN_URL}"

    assert send_telegram.send_briefing(leaky_alert, "alert") is True

    assert FAKE_TOKEN not in payloads[0]["text"]
    assert "bot[REDACTED]" in payloads[0]["text"]


def test_missing_credentials_skips_without_request(monkeypatch, tmp_path):
    """Ohne Credentials: kein Request, normales Briefing wird trotzdem archiviert."""
    monkeypatch.setattr(send_telegram, "ENV_PATH", tmp_path / "no.env")
    monkeypatch.setattr(send_telegram, "VAULT_DIR", tmp_path / "vault")
    monkeypatch.delenv("TELEGRAM_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)

    assert send_telegram.send_briefing(BRIEFING, "monday") is False
    assert len(list((tmp_path / "vault").glob("*.md"))) == 1


@pytest.mark.parametrize("mode", ["alert", "healthcheck"])
def test_notification_modes_never_use_markdown_parse_mode(monkeypatch, tmp_path, mode):
    """Alerts UND Healthchecks: robustes Plain-Text-Format statt Legacy-Markdown."""
    _patch_env(monkeypatch, tmp_path)
    payloads: list[dict] = []

    def _capture(request: httpx.Request) -> httpx.Response:
        payloads.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True}, request=request)

    _mock_client(monkeypatch, _capture)

    assert send_telegram.send_briefing(ALERT_TEXT, mode) is True
    assert "parse_mode" not in payloads[0]


# --- Markdown-400-Fallback -------------------------------------------------


def test_markdown_400_falls_back_to_plain_text(caplog, monkeypatch, tmp_path):
    """400 bei Markdown: Body diagnostisch geloggt, genau einmal Plain-Text-Retry -> True."""
    _patch_env(monkeypatch, tmp_path)
    payloads: list[dict] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        payloads.append(payload)
        if "parse_mode" in payload:
            return httpx.Response(
                400,
                json={
                    "ok": False,
                    "error_code": 400,
                    "description": "Bad Request: can't parse entities",
                },
                request=request,
            )
        return httpx.Response(200, json={"ok": True}, request=request)

    _mock_client(monkeypatch, _handler)
    caplog.set_level(logging.ERROR, logger="scripts.send_telegram")

    assert send_telegram.send_briefing(BRIEFING, "monday") is True

    assert len(payloads) == 2  # Original + genau ein Fallback, keine Schleife
    assert payloads[0]["parse_mode"] == "Markdown"
    assert "parse_mode" not in payloads[1]  # Retry als Plain-Text
    assert "can't parse entities" in caplog.text  # Diagnose-Body geloggt
    assert FAKE_TOKEN not in caplog.text
    assert "api.telegram.org" not in caplog.text


def test_fallback_also_fails_returns_false_without_loop(caplog, monkeypatch, tmp_path):
    """400 + Fallback scheitert ebenfalls: False, genau 2 Requests, kein Endlos-Loop."""
    _patch_env(monkeypatch, tmp_path)
    payloads: list[dict] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        payloads.append(json.loads(request.content))
        return httpx.Response(
            400, json={"ok": False, "description": "Bad Request"}, request=request
        )

    _mock_client(monkeypatch, _handler)
    caplog.set_level(logging.ERROR, logger="scripts.send_telegram")

    assert send_telegram.send_briefing(BRIEFING, "monday") is False

    assert len(payloads) == 2  # Original + genau ein Fallback, kein Loop
    assert "parse_mode" in payloads[0]
    assert "parse_mode" not in payloads[1]
    assert FAKE_TOKEN not in caplog.text


def test_400_response_body_is_sanitized(caplog, monkeypatch, tmp_path):
    """400-Body mit Bot-URL: wird vor dem Loggen sanitisiert (kein Token-Leak)."""
    _patch_env(monkeypatch, tmp_path)
    payloads: list[dict] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        payloads.append(json.loads(request.content))
        return httpx.Response(
            400,
            json={"ok": False, "description": f"Bad Request: {TOKEN_URL}"},
            request=request,
        )

    _mock_client(monkeypatch, _handler)
    caplog.set_level(logging.ERROR, logger="scripts.send_telegram")

    # Fallback schlaegt ebenfalls fehl -> False, aber der Diagnose-Body
    # ist trotzdem sanitisiert im Log.
    assert send_telegram.send_briefing(BRIEFING, "monday") is False

    assert FAKE_TOKEN not in caplog.text
    assert "bot1234567890" not in caplog.text
    assert "bot[REDACTED]" in caplog.text
    assert len(payloads) == 2


def test_alert_400_has_no_fallback_retry(monkeypatch, tmp_path):
    """Alert (Plain-Text): 400 -> kein Fallback-Retry, genau 1 Request."""
    _patch_env(monkeypatch, tmp_path)
    payloads: list[dict] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        payloads.append(json.loads(request.content))
        return httpx.Response(
            400, json={"ok": False, "description": "Bad Request"}, request=request
        )

    _mock_client(monkeypatch, _handler)

    assert send_telegram.send_briefing(ALERT_TEXT, "alert") is False

    assert len(payloads) == 1  # Alerts bleiben Plain-Text und werden nie wiederholt
    assert "parse_mode" not in payloads[0]


# --- Volltext-Versand / Split-Logik (Phase 5) ------------------------------


def test_send_briefing_splits_long_text(monkeypatch, tmp_path):
    """Text >4096: mehrere Nachrichten an Sektionsgrenzen, jede <=4096."""
    _patch_env(monkeypatch, tmp_path)
    payloads: list[dict] = []

    def _capture(request: httpx.Request) -> httpx.Response:
        payloads.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True}, request=request)

    _mock_client(monkeypatch, _capture)

    long = _long_briefing()
    assert send_telegram.send_briefing(long, "monday") is True

    assert len(payloads) > 1
    assert all(len(p["text"]) <= send_telegram.TELEGRAM_MAX_LEN for p in payloads)
    # Chunks rekonstruieren den vollstaendigen (frontmatter-freien) Text
    assert "".join(p["text"] for p in payloads) == _strip_frontmatter(long)
    # Split an Sektionsgrenzen: jeder Chunk nach dem ersten beginnt mit "## "
    assert payloads[0]["text"].startswith("# Portfolio-Briefing")
    assert all(p["text"].startswith("## Sektion") for p in payloads[1:])


def test_send_briefing_short_text_single_message(monkeypatch, tmp_path):
    """Text <=4096: genau eine Nachricht, kein Split."""
    _patch_env(monkeypatch, tmp_path)
    payloads: list[dict] = []

    def _capture(request: httpx.Request) -> httpx.Response:
        payloads.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True}, request=request)

    _mock_client(monkeypatch, _capture)

    assert send_telegram.send_briefing(BRIEFING, "monday") is True

    assert len(payloads) == 1
    assert payloads[0]["text"] == _strip_frontmatter(BRIEFING)


def test_send_briefing_section_longer_than_max(monkeypatch, tmp_path):
    """Einzelne Sektion >4096: hartes Split, alle Chunks <=4096, rekonstruierbar."""
    _patch_env(monkeypatch, tmp_path)
    payloads: list[dict] = []

    def _capture(request: httpx.Request) -> httpx.Response:
        payloads.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True}, request=request)

    _mock_client(monkeypatch, _capture)

    section = "## Mega-Sektion\n\n" + "Wort " * 3000  # ~15000 Zeichen, keine Newlines
    markdown = f"---\ntags: [test]\n---\n\n# Titel\n\n{section}"
    assert send_telegram.send_briefing(markdown, "monday") is True

    assert len(payloads) > 1
    assert all(len(p["text"]) <= send_telegram.TELEGRAM_MAX_LEN for p in payloads)
    assert "".join(p["text"] for p in payloads) == _strip_frontmatter(markdown)


def test_archive_unchanged(monkeypatch, tmp_path):
    """Vault-Archiv bleibt ungeteilt und unveraendert (vollstaendiges Markdown)."""
    _patch_env(monkeypatch, tmp_path)
    payloads: list[dict] = []

    def _capture(request: httpx.Request) -> httpx.Response:
        payloads.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True}, request=request)

    _mock_client(monkeypatch, _capture)

    long = _long_briefing()
    assert send_telegram.send_briefing(long, "monday") is True
    assert len(payloads) > 1  # Versand wurde gesplittet

    archived = list((tmp_path / "vault").glob("*.md"))
    assert len(archived) == 1
    assert archived[0].read_text(encoding="utf-8") == long  # ungeteilt, unveraendert


def test_send_briefing_chunk_failure_returns_false_but_sends_remaining(
    caplog, monkeypatch, tmp_path
):
    """Fehler bei einem Chunk: Rest wird trotzdem versucht, Rueckgabe False."""
    _patch_env(monkeypatch, tmp_path)
    payloads: list[dict] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        payloads.append(payload)
        if "## Sektion 3" in payload["text"]:
            return httpx.Response(500, json={"ok": False}, request=request)
        return httpx.Response(200, json={"ok": True}, request=request)

    _mock_client(monkeypatch, _handler)
    caplog.set_level(logging.ERROR, logger="scripts.send_telegram")

    assert send_telegram.send_briefing(_long_briefing(), "monday") is False

    # Alle Chunks wurden versucht — kein Abbruch nach dem Fehler.
    assert len(payloads) == 3
    assert "## Sektion 3" in payloads[1]["text"]  # fehlgeschlagener Chunk (HTTP 500)
    assert "## Sektion 6" in payloads[2]["text"]  # Rest wurde trotzdem gesendet
    assert FAKE_TOKEN not in caplog.text
