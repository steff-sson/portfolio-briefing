# portfolio-briefing

Wöchentliche/monatliche Portfolio-Briefings per RSS-Filter, LLM-Generierung mit Review-Gate und Telegram-Alert.

## Pipeline (Phase 4)

```
deterministic first → DeepSeek Draft → Python verify → GLM Review → (optionale 1× DeepSeek-Revision) → final gate → Telegram/Vault
```

1. **Deterministic first** (`sc_bridge` → `analyze` → `filter_news` → `facts.build_facts_package`): Portfolio, Checks (Grenzwerte, Drift, Turnover, Thesis-Deadlines), gefilterte News und `deterministic_summary` — die einzige erlaubte Zahlenquelle für das LLM. Kein LLM, keine Secrets.
2. **DeepSeek Draft** (`llm_briefing.generate_draft`, Modell `deepseek-v4-flash`): strukturierter Markdown-Entwurf mit festen Sektionen (`## Kurzlage`, `## Datenqualität`, `## Entscheidungsrelevante Punkte`, `## Strategie-Abgleich`, `## Relevante News & Veränderungen`).
3. **Python verify** (`verify.verify_draft`): deterministische Prüfung gegen das Faktenpaket — alle 5 Sektionen vorhanden, Zahlen nur aus `deterministic_summary` (±0.5pp), Ticker/ISIN nur aus dem Portfolio, News-Referenz. Erkennt auch LLM-Fehlerstrings (wirft `LLMError`).
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

## Config/Secrets

Nicht-sensitive Pipeline-Defaults: `config/pipeline.example.yaml` (nur dokumentierend, wird nicht geladen). Anlagestrategie: `config/strategy.yaml` (gitignored, read-only Input, Vorlage `config/strategy.example.yaml`) — technische Schnittstelle (Schema, Validierung, Hash/Diff, Initiallauf): `docs/strategy.md`.

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

1. `sc login` ausführen.
2. Ersten produktiven Lauf starten (`.venv/bin/python scripts/run_briefing.py monday`), damit `config/snapshot.current.json` entsteht (Seed-Migration).
3. Cron-Jobs unten sind noch nicht aktiviert (offener Schritt).

## Fail-closed

Jeder Fehler (API-Key fehlt, API-Error, leere LLM-Antwort, ungültiges Review-JSON, fehlende Felder, unbekannte severity/verdict, unerwarteter verify-Fehler, blockiertes Gate) → **kein Versand, keine Vault-Briefing-Datei**, nur ein kurzer Alert via Telegram (mode `alert`), Exit-Code 1. Ein Re-Run am selben Tag ist möglich.

## Cron-Setup (offener Schritt — noch nicht aktiviert)

```bash
# Montag 08:00
0 8 * * 1 /home/stef/github/portfolio-briefing/.venv/bin/python /home/stef/github/portfolio-briefing/scripts/run_briefing.py monday
# Freitag 18:00
0 18 * * 5 /home/stef/github/portfolio-briefing/.venv/bin/python /home/stef/github/portfolio-briefing/scripts/run_briefing.py friday
# Monatlich 1. 08:30
30 8 1 * * /home/stef/github/portfolio-briefing/.venv/bin/python /home/stef/github/portfolio-briefing/scripts/run_briefing.py monthly
# Healthcheck (ohne Live-Abfrage)
*/30 * * * * /home/stef/github/portfolio-briefing/.venv/bin/python /home/stef/github/portfolio-briefing/scripts/healthcheck.py
```

## Bekannte Grenzen

- Kein Phase-5-plus: Revise-Loop ist auf genau 1 Revision begrenzt; bleibt das Review `revise`, blockt das Gate (kein zweiter Versuch, kein „force send").
- `verify` prüft Zahlen/Ticker/ISIN deterministisch gegen das Faktenpaket — stilistische oder semantische Qualität beurteilt nur das GLM-Review.
- LLM-Ausfälle sind fail-closed: ein partiell fehlgeschlagener Lauf erzeugt keinen Teildraft im Vault, sondern nur einen Alert.
- `healthcheck.py` und `check_position.py` sind separate Werkzeuge (nicht Teil der Briefing-Pipeline): live-first, keine automatischen `sc`-/API-Calls, kein Mock-Fallback. `check_position.py` liest das Portfolio aus dem Live-Snapshot; `healthcheck.py` meldet fehlende Live-Config als roten Status.
