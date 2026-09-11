"""Orchestrator: pull → check → fund → news → brief → sanity → render → send.

Aufruf:
    .venv/bin/python scripts/run_briefing.py monday [--dry-run]

Live:
- Phase A pull via headless ``opencode run`` (Default-Agent, Projekt-MCP
  ``scalable``) → schreibt data/portfolio.json, data/watchlist.json,
  data/quotes.json, data/news.json.
- send_telegram liefert an Telegram.

Dry-Run:
- KEIN MCP-Pull, KEIN Telegram, KEIN echter LLM-Call. Nutzt Fixtures aus
  tests/mock_data/ + vorgegebene LLM-Ausgabe und schreibt das gerenderte
  Briefing als Artefakt nach reports/{date}-{mode}-dryrun.md.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from scripts import brief, checks, fundamentals, render, sanity
from scripts import data as data_mod
from scripts import news as news_mod

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"
MOCK_DIR = REPO_ROOT / "tests" / "mock_data"
REPORT_DIR = REPO_ROOT / "reports"
STRATEGY_PATH = REPO_ROOT / "config" / "strategy.yaml"

# Guardrail (hart): ausschließlich Read-Calls der Liste in diesem Pull-Prompt.
READ_TOOLS = [
    "scalable_get_portfolio_holdings",
    "scalable_list_watchlist_items",
    "scalable_get_portfolio_cash_breakdown",
    "scalable_get_security_quote",
    "scalable_get_security_chart",
    "scalable_get_security_news",
    "scalable_list_portfolio_transactions",
    "scalable_get_account_profile",
]

PULL_PROMPT = (
    "Du bist ein read-only Datensammler. Nutze AUSSCHLIESSLICH diese "
    "Scalable-Lese-Tools: " + ", ".join(READ_TOOLS) + ". "
    "Lade Portfolio-Holdings, Watchlist, Cash-Breakdown, Kurse/Charts und News "
    "und schreibe die Rohdaten als JSON nach data/portfolio.json, "
    "data/watchlist.json, data/quotes.json, data/news.json. "
    "FÜHRE KEINE Order-/Schreib-Operationen aus. Keine Antwort benötigt."
)


def _pull_phase_a() -> None:
    """Phase A: headless MCP-Pull (Default-Agent, Projekt-MCP scalable)."""
    DATA_DIR.mkdir(exist_ok=True)
    cmd = ["opencode", "run", PULL_PROMPT]
    print("[phase A] MCP-Pull via opencode run ...")
    subprocess.run(cmd, cwd=str(REPO_ROOT), check=True, timeout=300)


def _collect_asset_refs(snap: data_mod.Snapshot) -> tuple[set[str], set[str]]:
    tickers = {str(h.get("ticker")) for h in snap.holdings if h.get("ticker")}
    tickers |= {str(w.get("ticker")) for w in snap.watchlist if w.get("ticker")}
    isins = {h["isin"] for h in snap.holdings if h.get("isin")}
    isins |= {w["isin"] for w in snap.watchlist if w.get("isin")}
    return tickers, isins


def _ticker_list(snap: data_mod.Snapshot) -> list[dict]:
    items: list[dict] = []
    seen: set[str] = set()
    for h in snap.holdings:
        t = h.get("ticker")
        if t and t not in seen:
            seen.add(t)
            items.append({"ticker": t, "isin": h.get("isin")})
    for w in snap.watchlist:
        t = w.get("ticker")
        if t and t not in seen:
            seen.add(t)
            items.append({"ticker": t, "isin": w.get("isin")})
    return items


def _load_fixtures() -> dict:
    """Baut das Dry-Run-Datendikt aus Fixtures (kein Netz, kein LLM)."""
    snap = data_mod.load_snapshot(MOCK_DIR)
    strategy = checks.load_strategy(str(STRATEGY_PATH))
    data_mod.apply_strategy_classification(snap, strategy)
    signals = checks.run_checks(snap.holdings, strategy)
    _, core_ratio, sat_ratio = checks.calculate_ratios(snap.holdings)
    tickers, isins = _collect_asset_refs(snap)
    mcp_news = snap.news
    # Dry-Run: keine echten RSS-/yfinance-Calls.
    news_items = mcp_news + news_mod.fetch_rss([], tickers, isins)
    funds = _load_fixture_fundamentals()
    suggestions = _load_fixture_suggestion()
    return {
        "holdings": snap.holdings,
        "watchlist": snap.watchlist,
        "quotes": snap.quotes,
        "news": news_items,
        "fundamentals": funds,
        "signals": [vars(s) for s in signals],
        "core_ratio": core_ratio,
        "satellite_ratio": sat_ratio,
        "total_value_eur": snap.total_value_eur,
        "cash_eur": snap.cash_eur,
        "captured_at": snap.captured_at,
        "suggestions": suggestions,
        "issues": [{"severity": i.severity, "message": i.message} for i in snap.issues],
    }


def _load_fixture_fundamentals() -> list[dict]:
    p = MOCK_DIR / "fundamentals.json"
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return []


def _load_fixture_suggestion() -> str:
    p = MOCK_DIR / "suggestion.txt"
    if p.exists():
        return p.read_text(encoding="utf-8").strip()
    return "Keine Aktion nötig."


def _build_live_data(snap: data_mod.Snapshot, strategy: dict) -> dict:
    """Baut das Live-Datendikt (echte MCP-Daten)."""
    data_mod.apply_strategy_classification(snap, strategy)
    signals = checks.run_checks(snap.holdings, strategy)
    _, core_ratio, sat_ratio = checks.calculate_ratios(snap.holdings)
    tickers, isins = _collect_asset_refs(snap)
    feeds = news_mod.load_feeds(REPO_ROOT / "config" / "feeds.json")
    funds = fundamentals.fetch_fundamentals(_ticker_list(snap))
    news_items = news_mod.collect_news(snap.news, feeds, tickers, isins)
    return {
        "holdings": snap.holdings,
        "watchlist": snap.watchlist,
        "quotes": snap.quotes,
        "news": news_items,
        "fundamentals": [vars(f) for f in funds],
        "signals": [vars(s) for s in signals],
        "core_ratio": core_ratio,
        "satellite_ratio": sat_ratio,
        "total_value_eur": snap.total_value_eur,
        "cash_eur": snap.cash_eur,
        "captured_at": snap.captured_at,
        "issues": [{"severity": i.severity, "message": i.message} for i in snap.issues],
    }


def run(mode: str, dry_run: bool) -> int:
    if dry_run:
        print("[dry-run] Fixtures statt Live-Daten; kein MCP/Telegram/LLM.")
        data = _load_fixtures()
    else:
        _pull_phase_a()
        snap = data_mod.load_snapshot(DATA_DIR)
        strategy = checks.load_strategy(str(STRATEGY_PATH))
        data = _build_live_data(snap, strategy)
        # brief-Phase: genau 1 LLM-Call (config-determiniert).
        data["suggestions"] = brief.generate_suggestions(data)

    fatal = [i for i in data["issues"] if i["severity"] == "error"]
    if fatal:
        print(f"[fail-closed] {len(fatal)} Datenfehler — kein Briefing.")
        for f in fatal:
            print(" -", f["message"])
        return 1

    # Sanity gegen die LLM-Ausgabe (im Dry-Run: Fixture-Suggestion).
    suggestions = data.get("suggestions") or ""
    violations = sanity.check_sanity(suggestions, data)
    if violations:
        print("[sanity] Verletzungen:")
        for v in violations:
            print(" -", v)
        if not dry_run:
            print("[fail-closed] Sanity verletzt — kein Versand.")
            return 1

    text = render.render_briefing(data)
    REPORT_DIR.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    artifact = REPORT_DIR / f"{stamp}-{mode}-{'dryrun' if dry_run else 'briefing'}.md"
    artifact.write_text(text, encoding="utf-8")
    print(f"[artefakt] {artifact}")

    if dry_run:
        return 0

    ok = send_telegram_send(text, mode)
    return 0 if ok else 1


def send_telegram_send(text: str, mode: str) -> bool:
    """Sendet das Briefing via send_telegram (Fail-closed)."""
    from scripts import send_telegram  # lokaler Import vermeidet Zirkularität

    return send_telegram.send_briefing(text, mode)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    args = [a for a in argv if not a.startswith("--")]
    dry_run = "--dry-run" in argv
    mode = args[0] if args else "monday"
    return run(mode, dry_run)


if __name__ == "__main__":
    sys.exit(main())
