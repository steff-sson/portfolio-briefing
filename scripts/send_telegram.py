"""Send Telegram summary and archive full briefing in vault."""
from __future__ import annotations

import logging
import os
import re
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

import httpx
from dotenv import load_dotenv

ENV_PATH = Path.home() / ".config" / "automation" / "config.env"
VAULT_DIR = Path.home() / "docs" / "notizen" / "portfolio-briefings"
TELEGRAM_MAX_LEN = 4096
RETAINED_BRIEFINGS = 4

# Exakte Briefing-Dateinamen (echte Briefings). Nur diese zaehlen fuer die
# Retention: Dry-Run-Dateien (*-dryrun.md), Healthchecks und andere Dateien
# bleiben unangetastet.
_BRIEFING_FILE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}-(?:monday|friday|monthly)\.md$")

# Legacy-Markdown nur fuer echte Briefings. Alerts/Healthchecks werden als
# Plain-Text gesendet: Telegram-Markdown-Fehler (400) duerfen keinen Alert blocken.
_BRIEFING_MODES = {"monday", "friday", "monthly"}

# Telegram-Bot-Token in URLs: `bot<id>:<secret>`. Nur das Muster, nie der Wert.
_TOKEN_IN_URL_RE = re.compile(r"(bot)\d{6,}:[A-Za-z0-9_-]{20,}")
_REDACTED = r"\1[REDACTED]"

logger = logging.getLogger(__name__)


class _BotTokenRedactionFilter(logging.Filter):
    """Redigiert Bot-Token in HTTP-Client-Logs (httpx/httpcore).

    Nur Redaktion, keine Level-Aenderung: schuetzt ohne globale Nebenwirkung
    fuer andere httpx-Nutzer (z.B. OpenAI-Client der LLM-Scripts). Gilt fuer
    msg und formatierte Args (httpx loggt die URL als Argument).
    """

    def filter(self, record: logging.LogRecord) -> bool:
        changed = False
        if isinstance(record.msg, str) and _TOKEN_IN_URL_RE.search(record.msg):
            record.msg = _TOKEN_IN_URL_RE.sub(_REDACTED, record.msg)
            changed = True
        if isinstance(record.args, tuple):
            new_args = list(record.args)
            for i, arg in enumerate(new_args):
                text = str(arg)
                if _TOKEN_IN_URL_RE.search(text):
                    new_args[i] = _TOKEN_IN_URL_RE.sub(_REDACTED, text)
                    changed = True
            if changed:
                record.args = tuple(new_args)
        elif isinstance(record.args, dict):
            new_args = dict(record.args)
            for key, value in new_args.items():
                text = str(value)
                if _TOKEN_IN_URL_RE.search(text):
                    new_args[key] = _TOKEN_IN_URL_RE.sub(_REDACTED, text)
                    changed = True
            if changed:
                record.args = new_args
        return True


def _install_redaction_filter() -> None:
    for name in ("httpx", "httpcore"):
        http_logger = logging.getLogger(name)
        if not any(isinstance(f, _BotTokenRedactionFilter) for f in http_logger.filters):
            http_logger.addFilter(_BotTokenRedactionFilter())


_install_redaction_filter()


@contextmanager
def _suppress_http_request_logs() -> Iterator[None]:
    """Daempft httpx/httpcore-INFO-Logs NUR fuer die Telegram-Anfrage.

    httpx loggt sonst jede Request-URL (inkl. Bot-Token) auf INFO. Das Level
    wird lokal gesetzt und danach zurueckgesetzt — kein globaler Effekt.
    """
    loggers = [logging.getLogger(name) for name in ("httpx", "httpcore")]
    previous = [lg.level for lg in loggers]
    for lg in loggers:
        lg.setLevel(logging.WARNING)
    try:
        yield
    finally:
        for lg, level in zip(loggers, previous):
            lg.setLevel(level)


def _sanitize(text: str) -> str:
    """Entfernt Bot-Token aus beliebigen Strings (Logs, Fehlermeldungen)."""
    return _TOKEN_IN_URL_RE.sub(_REDACTED, text)


def _load_env() -> None:
    if ENV_PATH.exists():
        load_dotenv(ENV_PATH, override=True)


def _archive(markdown: str, mode: str) -> Path:
    VAULT_DIR.mkdir(parents=True, exist_ok=True)
    date = datetime.now().strftime("%Y-%m-%d")
    path = VAULT_DIR / f"{date}-{mode}.md"
    path.write_text(markdown, encoding="utf-8")
    return path


def _retain_briefings(keep: int = RETAINED_BRIEFINGS) -> list[Path]:
    """Loescht aeltere Briefings, so dass nur die `keep` neuesten bleiben.

    Nur Dateien mit exaktem Briefing-Namen (``YYYY-MM-DD-monday.md``,
    ``YYYY-MM-DD-friday.md``, ``YYYY-MM-DD-monthly.md``) zaehlen. Andere
    Dateien (``*-dryrun.md``, Healthchecks, Readmes ...) bleiben erhalten.
    Zurueckgegeben werden die geloeschten Dateien (Testbarkeit).
    """
    VAULT_DIR.mkdir(parents=True, exist_ok=True)
    briefing_files = sorted(
        (p for p in VAULT_DIR.iterdir() if p.is_file() and _BRIEFING_FILE_RE.match(p.name)),
        key=lambda p: p.name,
    )
    removed: list[Path] = []
    for path in briefing_files[:-keep] if keep > 0 else briefing_files:
        path.unlink()
        removed.append(path)
    return removed


def _send_telegram(token: str, chat_id: str, text: str, parse_mode: str | None) -> bool:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": text}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    try:
        with httpx.Client(timeout=30) as client:
            with _suppress_http_request_logs():
                resp = client.post(url, json=payload)
            resp.raise_for_status()
        return True
    except httpx.HTTPStatusError as e:
        # str(e) enthaelt die volle Request-URL — hier NIE loggen.
        status = e.response.status_code
        if status == 400 and parse_mode:
            # 400 bei Markdown: Telegram lehnt Legacy-Markdown haeufig ab.
            # Body/description sanitisiert diagnostisch loggen, dann genau
            # EINMAL als Plain-Text ohne parse_mode wiederholen. Kein Loop:
            # der Retry laeuft mit parse_mode=None und faellt hier nicht rein.
            body = e.response.content.decode("utf-8", errors="replace")
            logger.error(
                "Telegram send failed: HTTP %s %s (body: %s)",
                status,
                e.response.reason_phrase,
                _sanitize(body),
            )
            return _send_telegram(token, chat_id, text, None)
        logger.error("Telegram send failed: HTTP %s %s", status, e.response.reason_phrase)
        return False
    except httpx.RequestError as e:
        logger.error("Telegram send failed: %s", _sanitize(str(e)))
        return False
    except Exception as e:
        logger.error("Telegram send failed: %s", _sanitize(str(e)))
        return False


def _split_into_chunks(text: str, max_len: int) -> list[str]:
    """Teilt Text in Telegram-konforme Chunks (jeder <= max_len).

    Bevorzugt Sektionsgrenzen (``## ``): Sektionen werden so lange
    gebuendelt, bis der naechste Abschnitt den Chunk ueberlaufen liesse.
    Eine einzelne Sektion > max_len wird hart (zeilenbewusst) gesplittet.
    Text <= max_len wird unveraendert als ein Chunk zurueckgegeben.
    Chunks rekonstruieren den Eingabetext exakt (``"".join(chunks)``).
    """
    if len(text) <= max_len:
        return [text]
    # An Zeilenanfaengen von "## "-Sektionen trennen; die Sektionsgrenze
    # (Newline + Header) bleibt vollstaendig im jeweiligen Element erhalten.
    sections = re.split(r"(?m)^(?=## )", text)
    sections = [s for s in sections if s]  # leeres Element bei Text, der mit "## " beginnt
    chunks: list[str] = []
    current = ""
    for section in sections:
        if current and len(current) + len(section) <= max_len:
            current += section
            continue
        if current:
            chunks.append(current)
            current = ""
        if len(section) <= max_len:
            current = section
        else:
            chunks.extend(_hard_split(section, max_len))
    if current:
        chunks.append(current)
    return chunks


def _hard_split(text: str, max_len: int) -> list[str]:
    """Hartes Split einer ueberlangen Einzelsektion (zeilenbewusst).

    Schneidet bevorzugt an der letzten Newline innerhalb von max_len
    (keine Zeile wird mitten im Wort zerissen), sonst exakt bei max_len.
    Kein Chunk ueberschreitet max_len; der Gesamttext bleibt rekonstruierbar.
    """
    chunks: list[str] = []
    rest = text
    while len(rest) > max_len:
        cut = rest.rfind("\n", 1, max_len)
        if cut == -1:
            cut = max_len
        chunks.append(rest[:cut])
        rest = rest[cut:]
    chunks.append(rest)
    return chunks


def send_briefing(markdown: str, mode: str) -> bool:
    """Send full briefing to Telegram and archive full version in vault.

    Bei > TELEGRAM_MAX_LEN Zeichen wird der (frontmatter-freie) Text in
    mehrere Nachrichten gesplittet — bevorzugt an Sektionsgrenzen (``## ``),
    harter Fallback bei ueberlangen Einzelsektionen. Das Vault-Archiv
    bleibt immer ungeteilt und unveraendert (vollstaendiges Markdown).

    Mode ``alert`` (Orchestrator-Fehlerpfad) wird nur gesendet, nie
    archiviert — Fehler-Alerts duerfen keine Briefing-Datei im Vault
    anlegen. Alerts laufen ohne parse_mode (Plain-Text), damit kein
    Legacy-Markdown-Fehler den Alert blockt.
    """
    _load_env()
    token = os.getenv("TELEGRAM_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    archived: Path | None = None
    if mode != "alert":
        archived = _archive(markdown, mode)
        # Retention: nach erfolgreichem Archivieren nur die 4 neuesten
        # echten Briefings behalten. Dry-Run-Dateien/Healthchecks bleiben.
        _retain_briefings()

    if not token or not chat_id:
        print(f"Telegram skipped; archived to {archived}")
        return False

    # Strip frontmatter for Telegram
    text = re.sub(r"^---\n.*?---\n", "", markdown, flags=re.DOTALL).strip()

    # Defensiv: selbst wenn eine Fehlermeldung eine Bot-URL enthaelt, darf sie
    # nie als Alerttext ankommen.
    text = _sanitize(text)

    # Volltext-Versand: bei >4096 Zeichen an Sektionsgrenzen splitten,
    # harter Fallback bei ueberlangen Einzelsektionen (_split_into_chunks).
    parse_mode = "Markdown" if mode in _BRIEFING_MODES else None
    chunks = _split_into_chunks(text, TELEGRAM_MAX_LEN)
    ok = True
    for chunk in chunks:
        if not _send_telegram(token, chat_id, chunk, parse_mode):
            ok = False
            # Rest nicht abbrechen: weitere Chunks trotzdem versuchen.
    if not ok:
        print(f"Telegram send failed; archived to {archived}")
        return False
    return True


if __name__ == "__main__":
    ok = send_briefing("# Test\n\nBriefing.", "monday")
    print(f"Sent: {ok}")
