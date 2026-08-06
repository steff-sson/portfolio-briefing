# AGENTS.md — portfolio-briefing

## Projektbeschreibung

Wöchentliche/monatliche Portfolio-Briefings per RSS-Filter, LLM-Zusammenfassung und Telegram-Alert.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -e ~/github/automation-core
.venv/bin/pip install -e ".[dev]"
```

## `sc login`

Für echte Portfolio-Daten muss `sc login` ausgeführt sein. Ist `sc` nicht installiert, greifen alle Scripts auf `tests/mock_data/` zurück.

## Cron-Setup

```bash
0 8 * * 1 /home/stef/github/portfolio-briefing/.venv/bin/python scripts/run_briefing.py monday
0 18 * * 5 /home/stef/github/portfolio-briefing/.venv/bin/python scripts/run_briefing.py friday
30 8 1 * * /home/stef/github/portfolio-briefing/.venv/bin/python scripts/run_briefing.py monthly
*/30 * * * * /home/stef/github/portfolio-briefing/.venv/bin/python scripts/healthcheck.py
```

## Config-Env-Variablen

In `~/.config/automation/config.env`:

- `NEURALWATT_API_KEY` — für LLM-Briefing (`glm-5.2` via NeuralWatt)
- `TELEGRAM_TOKEN` — Bot-Token für Alerts
- `TELEGRAM_CHAT_ID` — Ziel-Chat für Alerts
