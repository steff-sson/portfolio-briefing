# AGENTS.md — portfolio-briefing (KISS-Rewrite v2)

## Projektbeschreibung

Wöchentliches Telegram-Briefing (Mo 07:00), das Portfolio + Watchlist +
Fundamentaldaten + News gegen `strategy.yaml` prüft und **konkrete Kauf-/
Verkaufsvorschläge mit Begründung** formuliert (als Bestätigungsfrage — Orders
legt Stefan selbst in Scalable an). KISS: genau **1 LLM-Call** pro Briefing.

## Pipeline

```
Phase A — Daten (headless opencode run, Default-Agent + Projekt-MCP `scalable`)
  → data/portfolio.json, data/watchlist.json, data/quotes.json, data/news.json
Phase B — Deterministik (Python, cron-sicher, testbar)  scripts/data.py, checks.py,
  fundamentals.py, news.py
Phase C — Ausgabe  scripts/brief.py (1 LLM-Call) → sanity.py → render.py → send_telegram.py
```

Aufruf-Level: `run_briefing.py` orchestriert `pull→check→fund→news→brief→sanity→render→send`.

### Phase A — MCP-Pull (Default-Agent, KEIN eigener Agent)
`cd ~/github/portfolio-briefing && opencode run "<read-only prompt>"`.
Die projektspezifische `opencode.json` liefert den `scalable`-MCP (type remote,
timeout 30000). `run_briefing.py` führt diesen Pull **nur im Live-Modus** aus.

**stdout-JSON-Handover (Option b):** Der headless Pull-Agent gibt NUR einen
fenced JSON-Block (```` ```json `````) mit den 4 Rohdokumenten
(portfolio/watchlist/quotes/news, Rohschema unverändert) via stdout zurück.
`run_briefing.py` parst die Stdout (`_extract_json_block`) und schreibt die 4
Dateien deterministisch + atomar (tmp+`os.replace`) nach `data/`. Kein Verlassen
auf Agent-Schreibrechte/Delegation → der `permissions`-Block für `data/*` in
`opencode.json` ist entfallen. Timeout/Parse-Fehler → bestehende Fail-Chain
(letzter Snapshot + Alert, fail-closed).

**Output-Cap-Hinweis:** Das Gesamt-JSON muss in EINER Agent-Antwort passen.
`quotes.json` ist ein optionales Passthrough-Feld (downstream ungenutzt, nur
`data.py` lädt es); der Pull-Prompt weist an, die grossen Zeitreihen-
`dataPoints`-Arrays dort wegzulassen (isin/timeframe/currency/source/
closingReferencePoint bleiben), damit portfolio/watchlist/news vollständig
durchkommen.

### Guardrail (hart) — ausschließlich Read-Tools
Der Pull-Prompt fordert **nur** diese Read-Calls:

```
scalable_get_portfolio_holdings
scalable_list_watchlist_items
scalable_get_portfolio_cash_breakdown
scalable_get_security_quote
scalable_get_security_chart
scalable_get_security_news
scalable_list_portfolio_transactions
scalable_get_account_profile
```

Diese Write-Tools existieren live und dürfen in **keinem Automationspfad**
auftauchen: `scalable_submit_buy_order`, `scalable_cancel_order`,
`scalable_add_watchlist_item`, `scalable_remove_watchlist_item`,
`scalable_create_price_alert`, `scalable_remove_price_alert`,
`scalable_upsert_savings_plan`, `scalable_remove_savings_plan`.

### Phase B — Deterministik
- `data.py` — Snapshot-Loader + Validatoren (quellenneutral). Toleriert Crypto-ETP-Nullbestände
  (`cryptoHoldings[].etpPositions[]` → `info`-Klasse, nur reale Bestände als Position).
- `checks.py` — Grenzwerte aus `strategy.yaml` (dort satellite- und core-Limits):
  Satellite/Legacy-Sleeve-Prüflinge (Einzelposition > `satellite_limits.max_position_pct`,
  Sektor > `satellite_limits.max_sector_pct`, > `satellite_limits.max_positions`),
  Core/Konzentrations-Check (einzelne Core-Position > `core_limits.max_position_pct`,
  Default 25 %, **warn**, kein Blocker) + Core/Satellite-Drift.
- `fundamentals.py` — yfinance (~6 Felder qualitativ), sleep gegen Rate-Limit, fail-open.
- `news.py` — MCP-News + RSS aus `config/feeds.json`, Ticker/ISIN-Match.
  Rohschema-Mapping (headline→title, Verlags-Fallback).

### Phase C — Ausgabe
- `brief.py` — **genau 1 LLM-Call** (config-determiniert, Default
  `glm-5.3-flash` via `config/pipeline.yaml`), Key `NEURALWATT_API_KEY` aus
  `~/.config/automation/config.env` (automation-core-Konvention, nie selbst ausgeben).
  Ausgabe: Vorschläge `VERKAUFEN/REDUZIEREN/KAUF/HALT` je 1-2 Sätze + Bestätigungsfrage.
  „Keine Aktion nötig" ist explizit ein gutes Ergebnis.
- `sanity.py` — jede Ticker/ISIN/Zahl im Output muss im Input existieren (fail-closed).
- `render.py` — Plain-Text + Ampel-Format C (nur 🟢/🟡/🔴, kein Text-Label).
- `run_briefing.py` — Orchestrator.
- `send_telegram.py` — Telegram + Vault-Archiv (behalten).

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -e ~/github/automation-core
.venv/bin/pip install -e ".[dev]"
```

## Dry-Run (Mock-only, keine externen Calls)

```bash
.venv/bin/python scripts/run_briefing.py monday --dry-run
```

Dry-Run nutzt Fixtures aus `tests/mock_data/` (P0-Struktur) → KEIN MCP-Pull, KEIN
Telegram, KEIN echter LLM-Call. Schreibt das gerenderte Briefing nach
`reports/{date}-{mode}-dryrun.md` und gibt Exit 0 zurück.

## Testgate

```bash
.venv/bin/python -m pytest tests/ -q     # muss vollständig grün sein (~71 Tests)
.venv/bin/ruff check scripts/ tests/      # muss sauber sein
```

Tests decken nur Phase B (checks-Mathe, snapshot-Loader/Validators,
fundamentals-Parsing gemockt, news-Match, sanity, render) — keine Pipeline-/
LLM-Integrationstests.

## Persönliche Daten (nie committen)

- `data/` (Laufzeit-Snapshots), `reports/`, `config/strategy.yaml`,
  `config/pipeline.yaml` sind gitignored.
- Im Repo nur Fixtures mit fiktiven Werten (keine PII, keine echten
  ISIN-Mengen-Bestände; ISINs von Broad-ETFs sind ok).

## Config-Env-Variablen (nur Namen)

In `~/.config/automation/config.env`: `NEURALWATT_API_KEY`, `TELEGRAM_TOKEN`,
`TELEGRAM_CHAT_ID`.

## Fail-closed

Jeder Fehler → kein Versand, kein Vault-Archiv, nur kurzer Alert (Telegram
mode `alert`), Exit-Code 1. Re-Run am selben Tag möglich.

## Cron / Ops

Cron-Eintrag **live** (Mo 07:00):
`0 7 * * 1  cd ~/github/portfolio-briefing && .venv/bin/python scripts/run_briefing.py monday`.
SSoT-Konvention: `~/crontab.txt` ist die Single Source of Truth (nie Symlink);
Änderungen laufen über `make sync-cron` + `make install-cron`, Check via
`make check-cron` in `~/github/automation-core`. Healthcheck-Ping nur bei echtem
Handlungsbedarf — **keine täglichen Vault-Notes**.
