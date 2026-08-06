"""Post-LLM verification: numbers, tickers, news references."""
from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def _extract_numbers(text: str) -> list[float]:
    matches = re.findall(r"(\d+\.?\d*)\s*%", text)
    return [float(m) for m in matches]


def _extract_tickers(text: str) -> set[str]:
    common = {"LLM", "API", "HTTP", "USD", "EUR", "ETF", "MVP", "RSS", "Q4", "AI", "OK", "USA", "IPO", "CEO", "GDP", "CPI", "EPS", "NASDAQ", "NYSE", "CNBC", "DAX", "S&P", "MSCI", "FTSE"}
    candidates = set(re.findall(r"\b[A-Z]{2,5}(?:[-\.]?[A-Z]+)?\b", text))
    return candidates - common


def _extract_isins(text: str) -> set[str]:
    return set(re.findall(r"[A-Z]{2}[A-Z0-9]{9}\d", text))


def verify_briefing(
    briefing_markdown: str,
    analysis: dict,
    news: list,
    portfolio: dict,
) -> list[str]:
    """Return list of warnings (empty if OK)."""
    warnings: list[str] = []
    text = briefing_markdown

    # 1. Number plausibility
    reported_numbers = _extract_numbers(text)
    positions = analysis.get("checks", {}).get("positions", {}).get("positions", [])
    portfolio_weights = [round(p["weight"] * 100, 1) for p in positions]
    for num in reported_numbers:
        if num > 99 and not any(num - 1 <= w <= num + 1 for w in portfolio_weights):
            warnings.append(f"Zahl {num}% im Briefing passt nicht zu Portfolio-Weights")
            break

    # 2. Ticker/ISIN existence
    mentioned_tickers = _extract_tickers(text)
    portfolio_tickers = set()
    portfolio_isins = set()
    for h in portfolio.get("holdings", []):
        if h.get("ticker"):
            portfolio_tickers.add(h.get("ticker", ""))
        portfolio_isins.add(h.get("isin", ""))
    for ticker in mentioned_tickers:
        if ticker not in portfolio_tickers and ticker not in portfolio_isins:
            warnings.append(f"Ticker/ISIN {ticker} im Briefing nicht im Portfolio")

    # 3. News references
    mentioned_titles = {entry["title"] for entry in news if entry.get("title")}
    referenced_titles = {t for t in mentioned_titles if t.lower() in text.lower()}
    if mentioned_titles and not referenced_titles:
        warnings.append("Keine der gefilterten News im Briefing referenziert")

    return warnings


if __name__ == "__main__":
    sample = "Apple (AAPL) ist mit 24.8% im Portfolio. Reuters meldet..."
    portfolio = {"holdings": [{"ticker": "AAPL", "isin": "US0378331005"}]}
    analysis = {"checks": {"positions": {"positions": [{"weight": 0.248}]}}}
    news = [{"title": "Reuters meldet"}]
    print(verify_briefing(sample, analysis, news, portfolio))
