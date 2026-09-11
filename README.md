# portfolio-briefing

Wöchentliches Telegram-Briefing (Mo 07:00, live): Portfolio + Watchlist +
Fundamentaldaten + News gegen deine `strategy.yaml`, mit **konkreten Kauf-/
Verkaufsvorschlägen als Bestätigungsfrage**. Du entscheidest — Orders legst du
selbst in Scalable an. KISS: genau **1 LLM-Call** pro Briefing.

## Pipeline

```
Phase A — Daten         headless `opencode run` (Default-Agent + Projekt-MCP `scalable`,
                        READ_TOOLS-Guardrail) → data/*.json (gitignored)
                        stdout-JSON-Handover: Agent gibt fenced JSON (Rohschema) via
                        stdout zurück; run_briefing.py schreibt data/*.json
                        deterministisch+atomar selbst.
Phase B — Deterministik scripts/data.py, checks.py, fundamentals.py, news.py (Python,
                        cron-sicher, testbar)
Phase C — Ausgabe       scripts/brief.py (1 LLM-Call) → sanity.py → render.py → send_telegram.py
```

`run_briefing.py` orchestriert `pull → check → fund → news → brief → sanity → render → send`.

## Auth / Login (Scalable MCP)

Voraussetzung: **„Agentic Investing"** in der Scalable-Web-App aktiviert
(Profil → Sicherheit). Dann:

```bash
cd ~/github/portfolio-briefing && opencode mcp auth scalable
```

Browser öffnet sich, 2FA übernimmt den Rest.

**Hinweis für SSH/Remote-Clients:** Der OAuth-Redirect landet auf `127.0.0.1`
des Clients. Zustellen der Callback-URL auf dem OpenCode-Host per `curl`, dann
schließt der Flow ab.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -e ~/github/automation-core
.venv/bin/pip install -e ".[dev]"
```

## Dry-Run + Testgate

```bash
# Dry-Run: Fixtures aus tests/mock_data/, keine externen Calls
.venv/bin/python scripts/run_briefing.py monday --dry-run

# Testgate (muss grün sein)
.venv/bin/python -m pytest tests/ -q
.venv/bin/ruff check scripts/ tests/
```

## Ops / Cron

Live: Mo 07:00
(`0 7 * * 1  cd ~/github/portfolio-briefing && .venv/bin/python scripts/run_briefing.py monday`).
SSoT-Konvention: `~/crontab.txt` ist die Single Source of Truth (nie Symlink),
Änderungen über `make sync-cron` + `make install-cron` in
`~/github/automation-core` — nicht direkt runterladen. Telegram-Alert nur bei
echtem Handlungsbedarf — keine täglichen Vault-Notes.

## Fail-closed

Jeder Fehler → **kein Versand**, kein Vault-Archiv, nur ein kurzer Alert
(Telegram mode `alert`), Exit-Code 1. Re-Run am selben Tag möglich.

## Persönliche Daten (nie committen)

gitignored: `data/`, `reports/`, `config/strategy.yaml`, `config/pipeline.yaml`.
Im Repo nur Fixtures mit fiktiven Werten (keine PII, keine echten
ISIN-Mengen-Bestände).
