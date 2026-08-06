"""Send Telegram summary and archive full briefing in vault."""
from __future__ import annotations

import os
import re
from datetime import datetime
from pathlib import Path

import httpx
from dotenv import load_dotenv


ENV_PATH = Path.home() / ".config" / "automation" / "config.env"
VAULT_DIR = Path.home() / "docs" / "notizen" / "portfolio-briefings"
TELEGRAM_MAX_LEN = 4096


def _load_env() -> None:
    if ENV_PATH.exists():
        load_dotenv(ENV_PATH, override=True)


def _archive(markdown: str, mode: str) -> Path:
    VAULT_DIR.mkdir(parents=True, exist_ok=True)
    date = datetime.now().strftime("%Y-%m-%d")
    path = VAULT_DIR / f"{date}-{mode}.md"
    path.write_text(markdown, encoding="utf-8")
    return path


def _send_telegram(token: str, chat_id: str, text: str) -> bool:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
    try:
        with httpx.Client(timeout=30) as client:
            resp = client.post(url, json=payload)
            resp.raise_for_status()
        return True
    except Exception:
        return False


def send_briefing(markdown: str, mode: str) -> bool:
    """Send shortened version to Telegram and archive full version in vault."""
    _load_env()
    token = os.getenv("TELEGRAM_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    archived = _archive(markdown, mode)

    if not token or not chat_id:
        print(f"Telegram skipped; archived to {archived}")
        return False

    # Strip frontmatter for Telegram
    text = re.sub(r"^---\n.*?---\n", "", markdown, flags=re.DOTALL).strip()
    if len(text) > TELEGRAM_MAX_LEN:
        text = text[: TELEGRAM_MAX_LEN - 20] + "\n\n[... gekürzt]"

    if not _send_telegram(token, chat_id, text):
        print(f"Telegram send failed; archived to {archived}")
        return False
    return True


if __name__ == "__main__":
    ok = send_briefing("# Test\n\nBriefing.", "monday")
    print(f"Sent: {ok}")
