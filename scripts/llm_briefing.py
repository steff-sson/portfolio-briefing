"""Generate LLM briefing via NeuralWatt API (OpenAI-compatible)."""
from __future__ import annotations

import json
import os
import time
from datetime import datetime
from pathlib import Path

import openai
from dotenv import load_dotenv


CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"
PROMPTS_DIR = CONFIG_DIR / "prompts"
ENV_PATH = Path.home() / ".config" / "automation" / "config.env"


def _load_env() -> None:
    if ENV_PATH.exists():
        load_dotenv(ENV_PATH, override=True)


def _load_prompt(mode: str, portfolio: dict, analysis: dict, news: list, strategy: dict) -> str:
    prompt_file = PROMPTS_DIR / f"{mode}.txt"
    if not prompt_file.exists():
        prompt_file = PROMPTS_DIR / "monday.txt"
    template = prompt_file.read_text(encoding="utf-8")
    if 10 <= datetime.now().month <= 12:
        tax_file = PROMPTS_DIR / "q4_tax_context.txt"
        if tax_file.exists():
            template = template + "\n\n" + tax_file.read_text(encoding="utf-8")
    return template.format(
        portfolio=json.dumps(portfolio, ensure_ascii=False, indent=2),
        analysis=json.dumps(analysis, ensure_ascii=False, indent=2),
        news=json.dumps(news, ensure_ascii=False, indent=2),
        strategy=json.dumps(strategy, ensure_ascii=False, indent=2),
    )


def _get_client() -> openai.OpenAI:
    _load_env()
    api_key = os.getenv("NEURALWATT_API_KEY")
    if not api_key:
        raise RuntimeError("NEURALWATT_API_KEY not set")
    return openai.OpenAI(api_key=api_key, base_url="https://api.neuralwatt.com/v1")


def generate_briefing(
    portfolio: dict,
    analysis: dict,
    news: list,
    strategy: dict,
    mode: str = "monday",
) -> str:
    """Single-stage LLM filter+composition."""
    prompt = _load_prompt(mode, portfolio, analysis, news, strategy)
    try:
        client = _get_client()
    except RuntimeError:
        return "Briefing-Generierung fehlgeschlagen: API-Key fehlt."

    last_error = None
    for attempt in range(2):
        try:
            response = client.chat.completions.create(
                model="glm-5.2",
                messages=[
                    {"role": "system", "content": "Du bist ein präziser Portfoliobeobachter."},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.3,
                max_tokens=4096,
                timeout=60,
            )
            content = response.choices[0].message.content
            if not content:
                raise ValueError("Empty response from LLM")
            return content
        except Exception as e:
            last_error = e
            if attempt < 1:
                time.sleep(2 ** attempt)
            continue
    return f"Briefing-Generierung fehlgeschlagen: {last_error}"


if __name__ == "__main__":
    from scripts import analyze, sc_bridge

    portfolio = sc_bridge.get_portfolio()
    transactions = sc_bridge.get_transactions()
    strategy = analyze.load_strategy()
    analysis = analyze.analyze_portfolio(portfolio, transactions, strategy)
    news = []  # standalone test
    briefing = generate_briefing(portfolio, analysis, news, strategy)
    print(briefing)
