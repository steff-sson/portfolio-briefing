# Plan: Briefing-Qualität — 1 LLM-Call mit Mandat

> Stand: 2026-08-26. Auslöser: Briefing-Output ist Maschinensprache (siehe
> `notizen/portfolio-briefings/2026-08-26-monday.md`) — keine Laien-Sprache,
> keine Ideen, keine Empfehlung. Ursache: 4 LLM-Stufen mit Verbots-Prompt
> (Humanizer darf nichts erklären/bewerten) statt 1 Call mit Mandat.
>
> Geprüft durch @oracle (2026-08-26): GO-WITH-FIXUPS — alle 5 Fixups
> eingearbeitet (Reaktivierung `generate_draft`, Sektions-Contract,
> `final_briefing.py`-Löschung, `q4_tax_context.txt`, AGENTS.md-Sync).

## Ziel

Layman-lesbares Briefing mit einfachen Erklärungen, Ideen und klarer
Empfehlung — bei deterministisch abgesicherter Zahlenbasis (verify + Gate).

## Zielarchitektur

```
Deterministische Fakten (build_facts_package)
        ↓
   1 LLM-Call (generate_draft): Layman-Briefing NUR aus Faktenpaket
        ↓
   verify.py: Zahlen/Ticker/News/Sektionen 1:1 gegen Faktenpaket
        ↓
   Final Gate (fail-closed) → Telegram/Vault
```

Modell: `deepseek-v4-flash` via NeuralWatt (wie bisheriger Draft-Pfad).

## Sektions-Contract (neu, bindend für Prompt UND verify)

Sechs Pflichtsektionen in dieser Reihenfolge:

1. `## Kurzlage` — Was ist passiert? (Ampeln + Top-Befunde erklärt)
2. `## Datenqualität` — Datenlage und was sie bedeutet
3. `## Sell-/Reduce-Signale (bestehende Satellites)` — unverändert aus Signalen
4. `## Watchlist-Signale` — unverändert aus Signalen
5. `## Empfehlung` — BUY/SELL/WATCH-Label 1:1 aus `deterministic_summary.recommendation`
   + Begründung/Counterargument aus den Signal-Dimensionen (keine neuen Fakten)
6. `## Nächster Schritt` — konkreter Handlungshinweis aus den Signalen

Basis: bestehende `DRAFT_SECTIONS` in verify.py + wiederaktivierte
`RECOMMENDATION_SECTION`-Prüfung (beides existiert bereits als Legacy).
Neu ist nur die Erklärungs-/Empfehlungs-Prosa AUS den Signalen — verify
prüft Label/Zahlen 1:1, Prosa bleibt frei (siehe Risiko).

## Schritte

### Phase 0 — Sicherung
1. Uncommitteten Stand (Humanizer P1–P5) committen:
   `wip: humanizer P1-P5 (wird durch 1-call-Architektur ersetzt)`

### Phase 1 — Architektur-Umbau
2. Neuer Prompt `config/prompts/briefing.txt` (ersetzt monday/friday/monthly/humanize):
   - Input: Faktenpaket-JSON inkl. `position_actions`, `watchlist_signals`, News
   - Mandat: Layman-Sprache, pro Befund „Was heißt das für mich?",
     Ideen/Empfehlung aus den deterministischen Signalen ableiten
   - Harte Regeln: Zahlen/ISINs/Labels nur aus dem Paket; keine neuen Fakten;
     keine Imperative ohne Signal-Basis; Sektions-Contract oben
3. `llm_briefing.py`: Prompt-Umschaltung auf `briefing.txt`;
   `generate_draft` (llm_briefing.py:212) wird wieder aktiv genutzt.
   `q4_tax_context.txt` BLEIBT und wird weiterhin im Zeitraum Okt–Dez an
   `briefing.txt` angehängt (bestehender `_load_prompt`-Mechanismus)
4. `run_briefing.py`: Flow = Facts → `generate_draft(facts_package)` →
   `verify_draft` → `final_gate(verification)` → Versand. Entfernen:
   Humanize-Stufe, Review-Stufe, Revise-Loop (inkl. tot Re-Render,
   run_briefing.py:480-486), `_filter_review_findings`, `MAX_REVISIONS`,
   Aufruf von `render_final_briefing` (run_briefing.py:379) und
   `render_verify_paragraph` (run_briefing.py:525)
5. Löschen: `scripts/llm_humanize.py`, `scripts/llm_review.py`,
   `scripts/llm_revise.py`, **`scripts/final_briefing.py`** (komplett —
   enthält ausschließlich Renderer-Funktionen `_section_*`/`_signal_*`/
   `_format_*`, keine Kalkulation; Fakten-/Signalkalkul lebt bereits in
   `facts.py`/`analyze.py`); Prompts `humanize.txt`, `review.txt`,
   `revise.txt`, `monday.txt`, `friday.txt`, `monthly.txt`

### Phase 2 — Politur
6. Doppel-Titel-Bug: entfällt automatisch (LLM-Body ohne Titel;
   `render_markdown` setzt den einzigen Titel)
7. `verify.py`: Legacy-Checks für gelöschte Sektionen raus;
   `DRAFT_SECTIONS` auf neuen Contract (inkl. `## Empfehlung`) setzen;
   Kern bleibt: Zahlen 1:1 vs. `deterministic_summary`, Ticker/ISIN vs.
   Portfolio, News-Referenz, Fehlerstrings, Empfehlungs-Label
8. `verify.final_gate`: Signatur auf verification-only; critical/major
   blockt weiter fail-closed
9. **AGENTS.md synchronisieren:** Pipeline-Block auf 1-Call-Architektur
   ändern (aktuell noch „Draft → verify → GLM Review → Revision"),
   Sektions-Contract dokumentieren

### Phase 3 — Tests + Verifikation
10. Löschen: `tests/test_llm_humanize.py`, `tests/test_llm_review.py`,
    `tests/test_llm_revise.py`, `tests/test_final_briefing.py`.
    Anpassen: `test_orchestrator.py`, `test_run_briefing.py`,
    `test_llm_briefing.py`, `test_verify_gate.py`
11. `.venv/bin/python -m pytest tests/ -q` grün
12. Dry-Run (Mock-only) → kontrollierter Live-Lauf → Output-Review durch
    Stefan (GO/NO-GO)

## Bewusst außerhalb des Scopes

- Cron-Aktivierung (separater Schritt nach Output-GO)
- RSS-Feed-Erweiterung (31 Feeds), Monthly-Feinschliff

## Risiken & Gegenmaßnahmen

| Risiko | Gegenmaßnahme |
|---|---|
| GLM-Review fällt als semantische Sicherheitsstufe weg; Prosa-Empfehlungen werden nicht inhaltlich geprüft | Striktes `verify.py` (Label/Zahlen/ISIN 1:1, max. Ideen-Anzahl, Quellenpflicht) + fail-closed Gate. Restrisiko: sprachliche Übertreibung in Prosa ohne Zahlenbasis — wird beim Output-Review bewertet |
| Neue Prompt-Qualität schlechter als erwartet | Ein-Zeilen-Umschaltung auf stärkeres Modell, erneuter Live-Lauf |

## Erwartetes Ergebnis

~600 Zeilen Code weniger, 4 Testfiles weniger, 1 LLM-Call/Lauf statt bis zu 4.
Briefing in 2 Minuten lesbar: einfache Sprache, Ideen, klare Empfehlung,
Zahlen abgesichert.
