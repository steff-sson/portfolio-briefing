# portfolio-briefing

Wöchentliche/monatliche Portfolio-Briefings per RSS-Filter, LLM-Zusammenfassung und Telegram-Alert.

## Setup

```bash
cd ~/github/portfolio-briefing
python3 -m venv .venv
.venv/bin/pip install -e ~/github/automation-core
.venv/bin/pip install -e ".[dev]"
```

## scalable.capital Login

Für echte Daten: `sc login` vor dem ersten Lauf. Wenn `sc` nicht installiert ist, greifen alle Scripts auf `tests/mock_data/` zurück.

## Config/Secrets

Secrets in `~/.config/automation/config.env`:

```env
NEURALWATT_API_KEY=...
TELEGRAM_TOKEN=...
TELEGRAM_CHAT_ID=...
```

## Cron-Setup

```bash
# Montag 08:00
0 8 * * 1 /home/stef/github/portfolio-briefing/.venv/bin/python /home/stef/github/portfolio-briefing/scripts/run_briefing.py monday
# Freitag 18:00
0 18 * * 5 /home/stef/github/portfolio-briefing/.venv/bin/python /home/stef/github/portfolio-briefing/scripts/run_briefing.py friday
# Monatlich 1. 08:30
30 8 1 * * /home/stef/github/portfolio-briefing/.venv/bin/python /home/stef/github/portfolio-briefing/scripts/run_briefing.py monthly
```

## Run

```bash
# Produktion
.venv/bin/python scripts/run_briefing.py monday

# Testlauf mit Mock-Daten (keine API-Calls)
.venv/bin/python scripts/run_briefing.py monday --dry-run
```
