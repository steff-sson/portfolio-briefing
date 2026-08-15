# AGENTS.md — portfolio-briefing

## Projektbeschreibung

Wöchentliche/monatliche Portfolio-Briefings per RSS-Filter, LLM-Generierung mit Review-Gate und Telegram-Alert.

## Pipeline (Phase 4)

```
deterministic first → DeepSeek Draft → Python verify → GLM Review → (optionale 1× DeepSeek-Revision) → final gate → Telegram/Vault
```

- **Deterministic first:** `sc_bridge` → `analyze` → `filter_news` → `facts.build_facts_package` (inkl. `deterministic_summary`, der einzigen erlaubten Zahlenquelle).
- **Live-First-Daten:** produktiver Lauf via `snapshot.load_previous()` → `sc_bridge.refresh_from_sc()` (fail-closed, kein Mock) → `snapshot.capture()` → `diff.diff_snapshots()`; Persistenz nur über das Snapshot-Modul (`config/snapshot.current.json` + `config/snapshots/archive/`), kein `update_config`. Mock (`load_mock()`) nur im Dry-Run.
- **DeepSeek Draft:** `llm_briefing.generate_draft` (`deepseek-v4-flash`), feste Sektionen (Kurzlage, Datenqualität, Entscheidungsrelevante Punkte, Strategie-Abgleich, Relevante News & Veränderungen).
- **Python verify:** `verify.verify_draft` — alle 5 Sektionen, Zahlen (vs. deterministic_summary, ±0.5pp), Ticker/ISIN (vs. Portfolio), News-Referenz; erkennt LLM-Fehlerstrings.
- **GLM Review:** `llm_review.review_draft` (`glm-5.2`), striktes JSON (findings + overall_verdict pass|revise|block).
- **Optionale 1× DeepSeek-Revision:** `llm_revise.revise_draft` nur bei `revise` und ausschließlich nicht-kritischen Findings; max 1 Revision (`MAX_REVISIONS`), danach erneutes verify + Review.
- **Final gate:** `verify.final_gate` — blockt bei critical/major (verify ODER review), `block`, `revise` nach max Revision, fehlendem/ungültigem Verdict. minor/info blocken nie.
- **Versand:** `render_markdown` (Vault, status `active`) + `send_telegram` (mode `alert` archiviert nie).

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -e ~/github/automation-core
.venv/bin/pip install -e ".[dev]"
```

## `sc login` / Erstlauf

Für echte Daten muss `sc login` ausgeführt sein. Produktiver Lauf ist fail-closed ohne Mock-Fallback; der erste Lauf erzeugt den ersten Snapshot (Seed-Migration). Seed-Dateien `config/portfolio.json`/`config/transactions.json` sind entfernt.

## sc-Session-Betrieb

Session-Lifecycle des scalable.capital-CLI — offiziell bestätigt durch den Maintainer ([Issue #5](https://github.com/ScalableCapital/scalable-cli/issues/5), [Repo](https://github.com/ScalableCapital/scalable-cli)):

- **Refresh-Token-Lebensdauer:** bis zu 7 Tage — danach ist immer ein neues interaktives `sc login` erforderlich.
- **Idle-Timeout:** 24h ohne Nutzung — die Session verfällt nach einem Tag Inaktivität.
- **Automatischer Refresh:** das CLI refresht die Session bei jeder Nutzung automatisch; bei Nutzung mind. 1×/24h bleibt sie bis zu 7 Tage aktiv.
- **Login ist interaktiv (OAuth-Device-Flow):** human-oriented — es gibt **keinen dokumentierten non-interactive-/Cron-Login**. `sc login` wird ausschließlich vom User ausgeführt, nie von der Pipeline/Automation.
- **Auth-Check:** `sc whoami --json` ist der de-facto Auth-Check (kein dedizierter `sc health`-Befehl). `healthcheck.py` führt ihn aus und hält die Session damit aktiv.
- **Fehlerklassen:** `no_session` / `REFRESH_RELOGIN_REQUIRED` → `sc login` erforderlich; `secret_storage_unavailable` → System-/Keyring-Prüfung. `healthcheck.py` differenziert die Status; `sc_bridge.refresh_from_sc` wirft dafür spezifische Exceptions (handlungsorientierte Alerts).
- **`session_backend=file`:** wird von diesem Projekt nur dokumentierend geprüft, **niemals automatisch überschrieben** (keine Änderung der sc-Konfiguration).

## Cron-Setup (offener Schritt — noch nicht aktiviert)

```bash
0 8 * * 1 /home/stef/github/portfolio-briefing/.venv/bin/python scripts/run_briefing.py monday
0 18 * * 5 /home/stef/github/portfolio-briefing/.venv/bin/python scripts/run_briefing.py friday
30 8 1 * * /home/stef/github/portfolio-briefing/.venv/bin/python scripts/run_briefing.py monthly
# Healthcheck 1× täglich 06:00 — hält sc-Session aktiv (sc whoami --json), vor Montag-Lauf 08:00
0 6 * * * /home/stef/github/portfolio-briefing/.venv/bin/python scripts/healthcheck.py
```

## Testlauf (Mock-only, ohne API-Calls)

```bash
.venv/bin/python scripts/run_briefing.py monday --dry-run
```

Dry-Run nutzt ausschließlich `sc_bridge.load_mock()` (`tests/mock_data/`) — kein sc-Call, kein Snapshot-/Diff-Schreiben, kein Telegram; schreibt `{date}-{mode}-dryrun.md` (status `draft`).

## Fail-closed

Jeder Fehler → kein Versand, keine Vault-Briefing-Datei, nur kurzer Alert (Telegram mode `alert`), Exit-Code 1. Re-Run am selben Tag möglich.

## Config-Env-Variablen (nur Namen)

In `~/.config/automation/config.env`:

- `NEURALWATT_API_KEY` — für alle LLM-Calls (Draft `deepseek-v4-flash`, Review `glm-5.2`, Revise `deepseek-v4-flash` via NeuralWatt)
- `TELEGRAM_TOKEN` — Bot-Token für Alerts
- `TELEGRAM_CHAT_ID` — Ziel-Chat für Alerts

Nicht-sensitive Pipeline-Defaults: `config/pipeline.example.yaml` (nur dokumentierend, wird nicht geladen).
