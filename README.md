# portfolio-briefing

Wöchentliche/monatliche Portfolio-Briefings per RSS-Filter, LLM-Generierung mit Review-Gate und Telegram-Alert.

## Pipeline (Phase 4)

```
deterministic first → DeepSeek Draft → Python verify → GLM Review → (optionale 1× DeepSeek-Revision) → final gate → Telegram/Vault
```

1. **Deterministic first** (`sc_bridge` → `analyze` → `filter_news` → `facts.build_facts_package`): Portfolio, Checks (Grenzwerte, Drift, Turnover, Thesis-Deadlines, Trades/Quartal), gefilterte News, `deterministic_summary` (einzige erlaubte Zahlenquelle) plus Ampel (7 Kategorien), Gesamt-Empfehlung (BUY/SELL/WATCH) und Top-3-Positionsvorschläge — alles deterministisch, kein LLM, keine Secrets.
2. **DeepSeek Draft** (`llm_briefing.generate_draft`, Modell `deepseek-v4-flash`): strukturierter Markdown-Entwurf mit festen Sektionen (`## Kurzlage`, `## Datenqualität`, `## Entscheidungsrelevante Punkte`, `## Strategie-Abgleich`, `## Relevante News & Veränderungen`) + abschließender `## Empfehlung` (Label 1:1 aus `deterministic_summary.recommendation`).
3. **Python verify** (`verify.verify_draft`): deterministische Prüfung gegen das Faktenpaket — alle Sektionen vorhanden, Zahlen nur aus `deterministic_summary` (1:1), Ticker/ISIN nur aus dem Portfolio, News-Referenz, Ampel 1:1, Empfehlungs-Label 1:1, Positionsvorschläge (max. 3, konkrete ISIN), Neukaufideen (max. 2, mind. 2 unabhängige Quellen). Erkennt auch LLM-Fehlerstrings (wirft `LLMError`).
4. **GLM Review** (`llm_review.review_draft`, Modell `glm-5.2`): striktes JSON-Review (`findings` mit `severity` ∈ critical|major|minor|info, `overall_verdict` ∈ pass|revise|block).
5. **Optionale eine DeepSeek-Revision** (`llm_revise.revise_draft`): nur bei `overall_verdict=revise` **und** ausschließlich nicht-kritischen Findings; maximal 1 Revision (`MAX_REVISIONS`), danach erneutes verify + Review.
6. **Final gate** (`verify.final_gate`): blockt Versand bei critical/major Findings (verify ODER review), bei `overall_verdict=block`, `revise` nach ausgeschöpfter Revision, fehlendem/ungültigem Verdict. `minor`/`info` blockieren nie.
7. **Telegram/Vault** (`render_markdown` + `send_telegram`): Archiv als `{date}-{mode}.md` (status `active`), Telegram-Zusammenfassung.

## Setup

```bash
cd ~/github/portfolio-briefing
python3 -m venv .venv
.venv/bin/pip install -e ~/github/automation-core
.venv/bin/pip install -e ".[dev]"
```

## scalable.capital Login / Live-First-Daten

Produktiver Lauf läuft ausschließlich über den scalable.capital-CLI (`sc login` erforderlich), fail-closed ohne Mock-/Seed-Fallback:

```
snapshot.load_previous() → sc_bridge.refresh_from_sc() → snapshot.capture() → diff.diff_snapshots()
```

- **Persistenz nur über das Snapshot-Modul:** `config/snapshot.current.json` (rolling) + `config/snapshots/archive/` (Historie), atomar geschrieben. Kein `update_config`.
- **Snapshot-Diff:** `diff.diff_snapshots(previous, current)` liefert `changes` für das Faktenpaket; der erste Lauf hat keinen Vorgänger (`has_previous: false`).
- **Kein Mock im produktiven Lauf:** fehlendes `sc`, non-zero Exit, leere Holdings → Fehler, kein Versand.
- **Mock nur im Dry-Run:** `sc_bridge.load_mock()` (`tests/mock_data/`), explizit, nirgends automatisch.
- Frühere Seed-Dateien `config/portfolio.json`/`config/transactions.json` sind entfernt (gitignored); der erste produktive Lauf erzeugt den ersten Snapshot.

### sc-Session-Lifecycle (belegte Grenzen)

Offiziell bestätigt durch den scalable-cli Maintainer ([Issue #5](https://github.com/ScalableCapital/scalable-cli/issues/5), [Repo](https://github.com/ScalableCapital/scalable-cli)):

- **Refresh-Token:** bis zu 7 Tage Lebensdauer.
- **Idle-Timeout:** 24h ohne Nutzung.
- Das CLI refresht die Session bei Nutzung automatisch — bei Nutzung mind. 1×/24h bleibt sie bis zu 7 Tage aktiv; danach ist ein **neues interaktives `sc login`** erforderlich.
- Login ist human-oriented (OAuth-Device-Flow): **kein dokumentierter non-interactive-/Cron-Login** — die Pipeline führt `sc login` nie selbst aus.
- **Auth-Check:** `sc whoami --json` (de-facto, kein `sc health`). `healthcheck.py` nutzt ihn als Keepalive und erkennt `no_session`, `REFRESH_RELOGIN_REQUIRED` und `secret_storage_unavailable` differenziert (handlungsorientierte Alerts).
- `session_backend=file`: wird nur dokumentierend geprüft, **nie automatisch überschrieben**.

## Config/Secrets

Nicht-sensitive Pipeline-Defaults: `config/pipeline.example.yaml` (nur dokumentierend, wird nicht geladen). Anlagestrategie: `config/strategy.yaml` (gitignored, read-only Input, deterministisch aus `config/setup/answers.reviewed.yaml` erzeugt) — technische Schnittstelle (Schema-SSoT `config/strategy.schema.yaml`, Validierung, Hash/Diff, Initiallauf): `docs/strategy-interface.md`; menschenlesbare Governance-Strategie: `strategy/strategy.md` (deterministisch generiert, **lokal, nicht versioniert** — öffentliche Vorlage: `config/strategy.example.yaml`).

Secrets nur in `~/.config/automation/config.env` (Namen, keine Werte):

```env
NEURALWATT_API_KEY=...
TELEGRAM_TOKEN=...
TELEGRAM_CHAT_ID=...
```

## Dry-Run (Mock-only)

```bash
.venv/bin/python scripts/run_briefing.py monday --dry-run
```

Überspringt alle LLM-Calls und Telegram; Daten ausschließlich aus `sc_bridge.load_mock()` — kein `sc`-Aufruf, kein Snapshot-/Diff-Schreiben, keine Live-Config. Schreibt `{date}-{mode}-dryrun.md` (status `draft`, Suffix blockt keinen realen Lauf am selben Tag).

## Erstlauf / Seed-Migration (offene Schritte)

1. `sc login` ausführen (interaktiv, OAuth-Device-Flow — kein non-interactive-/Cron-Login; siehe Session-Lifecycle oben).
2. Ersten produktiven Lauf starten (`.venv/bin/python scripts/run_briefing.py monday`), damit `config/snapshot.current.json` entsteht (Seed-Migration).
3. Briefing-Cron-Jobs unten sind noch nicht aktiviert (offener Schritt); der Healthcheck ist bereits täglich 06:00 aktiv.

## Strategie-Setup (deterministischer Flow)

```bash
# 1. Antworten aus dem sokratischen Dialog persistieren (config/setup/answers.yaml)
# 2. Review-Version bestätigen (config/setup/answers.reviewed.yaml)
.venv/bin/python scripts/setup_strategy.py --validate     # gegen config/strategy.schema.yaml
.venv/bin/python scripts/setup_strategy.py --emit         # strategy.yaml + Backup + Version
.venv/bin/python scripts/render_strategy_doc.py --write   # strategy/strategy.md generieren
```

Kein LLM-YAML-Generator, keine Defaults: fehlende/unklare analyserelevante
Antworten brechen den Setup hart ab (fail-closed). `strategy.yaml` wird atomar
mit Review-Backup (`config/setup/backup/`) geschrieben; `strategy/strategy-version.txt`
(Hash + Datum) wird aktualisiert.

## Fail-closed

Jeder Fehler (API-Key fehlt, API-Error, leere LLM-Antwort, ungültiges Review-JSON, fehlende Felder, unbekannte severity/verdict, unerwarteter verify-Fehler, blockiertes Gate) → **kein Versand, keine Vault-Briefing-Datei**, nur ein kurzer Alert via Telegram (mode `alert`), Exit-Code 1. Ein Re-Run am selben Tag ist möglich.

## Cron-Setup (Healthcheck aktiv, Briefings offen)

Der Healthcheck ist über die zentrale Crontab (`~/github/automation-core/crontab.txt`, Source of Truth, installiert als User-Crontab) täglich 06:00 aktiv. Die Briefing-Läufe sind noch nicht aktiviert:

```bash
# Montag 08:00
0 8 * * 1 /home/stef/github/portfolio-briefing/.venv/bin/python /home/stef/github/portfolio-briefing/scripts/run_briefing.py monday
# Freitag 18:00
0 18 * * 5 /home/stef/github/portfolio-briefing/.venv/bin/python /home/stef/github/portfolio-briefing/scripts/run_briefing.py friday
# Monatlich 1. 08:30
30 8 1 * * /home/stef/github/portfolio-briefing/.venv/bin/python /home/stef/github/portfolio-briefing/scripts/run_briefing.py monthly
# Healthcheck 1× täglich 06:00 — hält sc-Session aktiv (sc whoami --json), vor Montag-Lauf 08:00
# Aktiv via automation-core/crontab.txt: cd /home/stef/github/portfolio-briefing && .venv/bin/python scripts/healthcheck.py
```

> Healthcheck hält die sc-Session aktiv durch `sc whoami --json` (Idle-Timeout 24h, Refresh-Token bis zu 7 Tage — danach interaktives `sc login`, siehe [sc-Session-Lifecycle](#sc-session-lifecycle-belegte-grenzen)).

## Bekannte Grenzen

- Kein Phase-5-plus: Revise-Loop ist auf genau 1 Revision begrenzt; bleibt das Review `revise`, blockt das Gate (kein zweiter Versuch, kein „force send").
- `verify` prüft Zahlen/Ticker/ISIN deterministisch gegen das Faktenpaket — stilistische oder semantische Qualität beurteilt nur das GLM-Review.
- LLM-Ausfälle sind fail-closed: ein partiell fehlgeschlagener Lauf erzeugt keinen Teildraft im Vault, sondern nur einen Alert.
- `healthcheck.py` und `check_position.py` sind separate Werkzeuge (nicht Teil der Briefing-Pipeline): kein Mock-Fallback. `check_position.py` liest das Portfolio aus dem Live-Snapshot; `healthcheck.py` meldet fehlende Live-Config als roten Status und prüft die sc-Session per `sc whoami --json` (Auth-Probe/Keepalive, kein Broker-Datenabruf, keine Secrets im Alert).
