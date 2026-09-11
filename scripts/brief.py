"""Genau 1 LLM-Call zur Vorschlagsliste.

Config-determinierter Client (Default neuralwatt/deepseek-v4-flash), Key
NEURALWATT_API_KEY aus ~/.config/automation/config.env via automation-core-
Konvention. Der Funktionsname wird NIE selbst gelesen/ausgegeben/committet.

Output: Vorschlagsliste VERKAUFEN/REDUZIEREN/KAUF/HALT, je 1-2 Sätze Begründung
+ Bestätigungsfrage. „Keine Aktion nötig" ist explizit ein gutes Ergebnis.
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml
from dotenv import load_dotenv

try:
    import openai
except Exception:  # noqa: BLE001 - pragma: no cover
    openai = None  # type: ignore[assignment]

ENV_PATH = Path.home() / ".config" / "automation" / "config.env"
DEFAULT_CONFIG_PATH = Path("config") / "pipeline.yaml"
DEFAULT_MODEL = "neuralwatt/deepseek-v4-flash"
DEFAULT_BASE_URL = "https://api.neuralwatt.com/v1"

ACTIONS = ("VERKAUFEN", "REDUZIEREN", "KAUF", "HALT")


def load_llm_config(path: str | Path | None = None) -> dict:
    """Lädt config/pipeline.yaml (model/base_url). Fallback auf Defaults."""
    cfg: dict = {
        "model": DEFAULT_MODEL,
        "base_url": DEFAULT_BASE_URL,
    }
    p = Path(path) if path else DEFAULT_CONFIG_PATH
    if p.exists():
        loaded = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        llm = loaded.get("llm") or loaded
        cfg["model"] = llm.get("model", cfg["model"])
        cfg["base_url"] = llm.get("base_url", cfg["base_url"])
    return cfg


def _fact_package(data: dict) -> str:
    """Baut das deterministische Faktenpaket für den Prompt (kompakt)."""
    parts: list[str] = []
    parts.append("PORTFOLIO:")
    for h in data.get("holdings", []):
        parts.append(
            f"- {h.get('name')} ({h.get('isin')}) {h.get('category','?')} "
            f"{h.get('value_eur',0):.0f} EUR {h.get('quantity',0):.2f}"
        )
    parts.append("WATCHLIST:")
    for w in data.get("watchlist", []):
        parts.append(f"- {w.get('name')} ({w.get('isin')}) [{w.get('security_type')}]")
    parts.append("FUNDAMENTALS:")
    for f in data.get("fundamentals", []):
        parts.append(
            f"- {f.get('ticker')}: fwdPE={f.get('forward_pe')} revG={f.get('revenue_growth')} "
            f"earnG={f.get('earnings_growth')} d/e={f.get('debt_to_equity')} "
            f"52w={f.get('position_52w')}"
        )
    parts.append("NEWS:")
    for n in data.get("news", [])[:12]:
        parts.append(f"- [{n.get('source')}] {n.get('title')}")
    parts.append("SIGNALE:")
    for s in data.get("signals", []):
        parts.append(f"- [{s.get('severity')}] {s.get('message')}")
    parts.append(
        f"RATIOS: core={data.get('core_ratio')}% sat={data.get('satellite_ratio')}% "
        f"total={data.get('total_value_eur',0):.0f} EUR cash={data.get('cash_eur',0):.0f} EUR"
    )
    return "\n".join(parts)


PROMPT_TEMPLATE = """Du analysierst ein Depot-Portfolio. Formuliere konkrete Vorschläge.

Regeln:
- Nenne pro Vorschlag GENAU EINE Aktion aus: {actions}
- Jeder Vorschlag: 1-2 Sätze Begründung OHNE neue Kennzahlen (nur aus den
  gegebenen Daten) + abschließende Bestätigungsfrage an den Nutzer.
- Nutze ausschließlich ISINs/Ticker, die im Faktenpaket vorkommen.
- Wenn nichts zu tun ist, schreibe ausdrücklich: "Keine Aktion nötig."

FAKTENPAKET:
{facts}
"""


def build_prompt(data: dict) -> str:
    return PROMPT_TEMPLATE.format(actions="/".join(ACTIONS), facts=_fact_package(data))


def generate_suggestions(data: dict, config: dict | None = None) -> str:
    """Führt den genau einen LLM-Call aus und liefert den Vorschlagstext."""
    if openai is None:  # pragma: no cover
        raise RuntimeError("openai nicht verfügbar")
    cfg = config or load_llm_config()
    if ENV_PATH.exists():
        load_dotenv(ENV_PATH, override=True)
    api_key = os.getenv("NEURALWATT_API_KEY")
    if not api_key:
        raise RuntimeError("NEURALWATT_API_KEY not set")
    client = openai.OpenAI(api_key=api_key, base_url=cfg["base_url"])
    response = client.chat.completions.create(
        model=cfg["model"],
        messages=[
            {"role": "system", "content": "Du bist ein konservativer Portfolio-Analyst."},
            {"role": "user", "content": build_prompt(data)},
        ],
        temperature=0.3,
        max_tokens=600,
    )
    return (response.choices[0].message.content or "").strip()
