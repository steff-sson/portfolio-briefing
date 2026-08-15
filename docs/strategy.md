---
tags: [portfolio-briefing, strategy, docs]
created: 2026-08-14
status: active
---

# Strategie-Schnittstelle — technische Dokumentation

Dieses Dokument beschreibt ausschließlich die **technische Schnittstelle** zwischen
der Briefing-Pipeline und der Anlagestrategie. Es enthält keine konkreten
Strategiewerte, keine Governance-Regeln und keine Anlageberatung.

Strategische Inhalte (Welche Werte gelten?) werden in einer **separaten Session**
entwickelt und in `config/strategy.yaml` abgelegt. Dieses Dokument beantwortet nur:
Wo liegt die Strategie, welches Schema wird erwartet, welche Felder liest die
Pipeline, wie wird validiert und wie werden Änderungen erkannt.

## 1. Dateiablage

- **Persönliche Strategie:** `config/strategy.yaml` — wird von der Pipeline geladen.
- **Gitignored:** `config/strategy.yaml` ist in `.gitignore` (persönliche Strategie,
  darf nie committet werden). Ebenso gitignored: `config/snapshot.current.json` und
  `config/snapshots/` (enthalten `strategy_content` als JSON).
- **Strukturelle Vorlage:** `config/strategy.example.yaml` — zeigt das erwartete
  Schema mit Beispielwerten. Dient als Template für `strategy.yaml`, ist selbst
  aber keine Strategie.

## 2. Strategie als read-only Input

Die Pipeline behandelt `config/strategy.yaml` als **read-only Input**:

- Sie wird **nicht erzeugt** und **nicht modifiziert** — Strategieentwicklung
  erfolgt in einer separaten Session (außerhalb dieses Projekts).
- Sie wird bei jedem produktiven Lauf neu geladen und schema-validiert
  (`analyze.load_strategy()` → `analyze.validate_strategy()`).
- Sie fließt unverändert ins Faktenpaket (`facts.build_facts_package`, Feld
  `strategy`) und wird als `strategy_thresholds_pct` (nur Prozent-Grenzwerte)
  an LLM-Kontexte gegeben. **Roh-Strategieinhalte werden nie in Prompts gesendet.**

## 3. Erwartetes YAML-Schema

Das Schema ist **pipeline-fixed** (in `scripts/analyze.py`, `STRATEGY_SCHEMA`):
Es definiert, welche Top-Level-Blöcke existieren dürfen und welche required sind.
Es legt keine Werte fest — die Werte kommen ausschließlich aus `strategy.yaml`.
Unbekannte Felder werden **ignoriert** (permissive), nur required-Blöcke und
Validierungsregeln greifen.

| Top-Level-Block | Required | Felder (bekannt) | Typen |
|---|---|---|---|
| `meta` | nein | `version`, `created`, `last_reviewed`, `next_review`, `cooling_off_days` | int / date (`YYYY-MM-DD`) |
| `investor` | nein | `horizon`, `income_source`, `purpose`, `risk_profile` | str |
| `portfolio` | **ja** | `core_pct`, `satellite_pct`, `core_description`, `rebalancing` | siehe unten |
| `satellite_limits` | **ja** | `max_position_pct`, `max_sector_pct`, `max_positions`, `max_turnover_annual_pct`, `max_trades_per_quarter` | siehe unten |
| `sectors` | nein | `preferred`, `excluded`, `notes` | list[str] / str |
| `regions` | nein | `core`, `satellite_restriction` | str |
| `thesis` | nein | `required`, `template` | bool / str |
| `alerts` | nein | frei (kein festes Feldschema) | bool / int / float |
| `review_schedule` | nein | `quarterly_strategy_review`, `annual_full_review`, `triggers` | bool / list[str] |

### Felder im Detail

```yaml
meta:
  version: 1                    # int
  created: "YYYY-MM-DD"         # date
  last_reviewed: "YYYY-MM-DD"   # date
  next_review: "YYYY-MM-DD"     # date
  cooling_off_days: 30          # int

investor:
  horizon: ">15y"               # str
  income_source: "monthly_savings"  # str
  purpose: "retirement"         # str
  risk_profile: "moderate"      # str

portfolio:                      # REQUIRED
  core_pct: 75.0                # int|float — Prozent
  satellite_pct: 25.0           # int|float — Prozent
  core_description: "..."       # str
  rebalancing:                  # dict (optional)
    method: "threshold"         # str
    threshold_pct: 5.0          # int|float — Prozentpunkte

satellite_limits:               # REQUIRED
  max_position_pct: 5.0         # int|float — Prozent
  max_sector_pct: 15.0          # int|float — Prozent
  max_positions: 10             # int
  max_turnover_annual_pct: 30.0 # int|float — Prozent
  max_trades_per_quarter: 3     # int

sectors:                        # optional
  preferred: ["technology"]     # list[str]
  excluded: ["tobacco"]         # list[str]
  notes: ""                     # str

regions:                        # optional
  core: "global"                # str
  satellite_restriction: "none" # str

thesis:                         # optional
  required: true                # bool
  template: "thesis_template.md"  # str

alerts:                         # optional, freies Feldschema
  on_thesis_expiring_soon_days: 30  # int — Tage

review_schedule:                # optional
  quarterly_strategy_review: true   # bool
  annual_full_review: true          # bool
  triggers: ["life_event"]          # list[str]
```

Die strukturelle Vorlage mit Kommentaren zu jedem Feld ist
`config/strategy.example.yaml`.

## 4. Welche Felder die Pipeline liest

Die Pipeline liest Strategie-Grenzwerte ausschließlich über
`analyze.py`-Helfer (`_portfolio_cfg`, `_rebalancing_cfg`, `_satellite_limits`,
`_alerts`) und rechnet Prozentwerte intern auf Ratio um (`75.0` → `0.75`).
Fehlende/ungültige Grenzwerte ergeben `0.0` → der jeweilige Check wird **rot**
(fail-closed: keine erfundenen Zahlen).

| Feld | Gelesen von | Zweck |
|---|---|---|
| `portfolio.core_pct` | `calculate_core_satellite`, `calculate_drift` | Zielquote Core/Satellite (Toleranzband ± `rebalancing.threshold_pct`) |
| `portfolio.rebalancing.threshold_pct` | `calculate_core_satellite`, `calculate_drift` | Toleranzband / Drift-Schwelle (Prozentpunkte) |
| `satellite_limits.max_sector_pct` | `calculate_sector_concentration` | Sektor-Obergrenze |
| `satellite_limits.max_position_pct` | `calculate_single_position_max` | Einzelpositions-Obergrenze |
| `satellite_limits.max_turnover_annual_pct` | `calculate_turnover` | Jahresumschlag-Obergrenze |
| `alerts.on_thesis_expiring_soon_days` | `check_thesis_deadlines` | Ablauffrist für Thesen (Tage) |

Zusätzlich extrahiert `facts._strategy_thresholds_pct` die Prozent-Grenzwerte
(`core_pct`, `satellite_pct`, `threshold_pct`, `max_position_pct`,
`max_sector_pct`, `max_turnover_annual_pct`) als `strategy_thresholds_pct` ins
Faktenpaket — die einzige erlaubte Zahlenquelle für das LLM (verify prüft Zahlen
gegen diese Allowlist). Die übrigen Blöcke (`meta`, `investor`, `sectors`,
`regions`, `thesis`, `review_schedule`, restliche `alerts`) sind informativ und
lösen keine Checks aus.

## 5. Validierung

Vor jeder Verwendung wird `strategy.yaml` schema-validiert
(`analyze.validate_strategy`, `STRATEGY_SCHEMA`):

1. `portfolio` und `satellite_limits` müssen existieren (required — ohne sie
   können keine Checks laufen).
2. `core_pct + satellite_pct == 100`.
3. `max_position_pct <= max_sector_pct`.
4. `max_positions >= 1`.
5. Typ-Prüfung: Prozentwerte `int|float`, `max_positions` `int`, Listen sind Listen.

Ergebnis: `{"valid": True, "errors": []}` oder `{"valid": False, "errors": [str, ...]}`.

**Fail-closed:** Ungültige Strategie → `StrategyValidationError` → kein Versand,
keine Vault-Datei, nur Alert. Ebenso fail-closed: fehlende `config/strategy.yaml`
(`FileNotFoundError`).

## 6. Hash / Diff-Verhalten

Änderungen an der Strategie werden deterministisch erkannt, ohne Inhalte zu
speichern oder an LLM-Kontexte zu geben:

- **Kanonischer Hash** (`analyze.strategy_hash`): SHA-256 des kanonisch
  serialisierten Strategy-Dicts (`json.dumps(..., sort_keys=True,
  ensure_ascii=False)`). Einweg — erlaubt Änderungserkennung ohne Speicherung
  von Inhalten. Gleiche Strategie → gleicher Hash; jede Änderung → anderer Hash.
- **Snapshot**: `snapshot.build_snapshot`/`capture` speichern pro Lauf
  `strategy_hash` und `strategy_content` (geparste Strategie als JSON) im
  gitignored Snapshot (`config/snapshot.current.json` + `config/snapshots/`).
  Snapshot-Inhalte werden nie geloggt; `strategy_content` dient nur dem
  lokalen Feld-Level-Diff beim nächsten Lauf.
- **Feld-Level-Diff** (`diff.diff_strategy`): vergleicht Vorgänger-Strategie
  (aus `previous["strategy_content"]`) mit der aktuellen auf Top-Level- und
  Feldebene (rekursiv, z. B. `rebalancing`). Ausgabe enthält **nur Feld-Pfade,
  keine Werte**:

  ```json
  {
    "has_changed": true,
    "has_previous": true,
    "changed_keys": ["portfolio.core_pct"],
    "added_keys": [],
    "removed_keys": [],
    "hash_previous": "…",
    "hash_current": "…"
  }
  ```

  `diff.diff_snapshots` reicht das Ergebnis als `changes["strategy"]` weiter;
  `facts.build_facts_package` nimmt es als `strategy_diff` ins Faktenpaket auf
  (LLM-sicher, da wertfrei).

## 7. Initiallauf

Beim ersten Lauf existiert kein Vorgänger-Snapshot (`previous=None`):

- `diff_strategy`: `has_previous=False`, `has_changed=False` (kein Vergleich möglich).
- `strategy_diff.has_changed=False` → Trigger `has_strategy_change=False`.
- Das Briefing weist den Erstlauf aus ("Erstlauf, kein Vorgänger-Snapshot").
- Abwärtskompatibel: alte Snapshots ohne `strategy_content` werden wie
  `previous=None` behandelt.

## 8. Umgang mit Strategieänderung

Wird `strategy.yaml` gegenüber dem Vorgänger-Snapshot geändert:

1. `diff_strategy` liefert `has_changed=True` plus geänderte/hinzugefügte/
   entfernte Feld-Pfade (`changed_keys`, `added_keys`, `removed_keys`).
2. Das Faktenpaket enthält `strategy_diff`; der Trigger
   `triggers.has_strategy_change` wird gesetzt (höchste Priorität unter den
   Optionen-Triggern).
3. Die Sektion **Strategie-Abgleich** thematisiert die Änderung; abhängig von
   den Triggern werden Optionen (halten/reduzieren/aufstocken) mit Begründung
   und Gegenargument generiert.
4. Der nächste Lauf vergleicht gegen den neuen Stand — die Änderung wird nur
   einmal als solche berichtet.

## 9. Abgrenzung / Nicht dokumentiert

- **Nicht dokumentiert hier:** konkrete Strategiewerte, Governance-Regeln
  (Cooling-off, Review-Trigger), der dialogische Entwicklungsprozess,
  Anlageberatung. Diese Themen gehören in die Strategie-Session bzw. in
  `config/strategy.yaml` (persönlich, gitignored).
- **Strategieentwicklung** erfolgt in einer separaten Session; dieses
  Implementierungsprojekt erzeugt oder verändert keine Strategie.
- Die strategische Single Source of Truth für Werte ist die Strategie-Session;
  `docs/strategy.md` ist ausschließlich die technische Schnittstellen-Doku.
