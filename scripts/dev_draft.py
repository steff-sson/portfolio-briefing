"""Shadow validator for prompt development (kein Pipeline-Nebenwirkungspfad).

Baut das deterministische Faktenpaket genau wie der Orchestrator (mode
"monday"), ruft generate_draft EINMAL auf, laesst verify.verify_draft laufen
und druckt jedes Finding + Gate-Ergebnis. KEINE Snapshot-Writes
(capture_staged/discard/promote), kein Telegram, kein render_markdown, keine
Datei-Writes.

Nutzung:
    .venv/bin/python scripts/dev_draft.py --mock   # Mock-Daten, kein Netzwerk
    .venv/bin/python scripts/dev_draft.py          # LIVE-Daten (sc + News), echter LLM-Call
"""
from __future__ import annotations

import argparse
import sys

from scripts import analyze, facts, filter_news, llm_briefing, sc_bridge, snapshot, verify


def _build_facts_package(mock: bool) -> dict:
    """Faktenpaket analog Orchestrator-Reihenfolge (run_briefing.run, monday)."""
    if mock:
        portfolio, transactions = sc_bridge.load_mock()
        watchlist = sc_bridge.load_mock_watchlist()
        changes = None
        previous = None
        data_quality = analyze.assess_data_quality(portfolio, None)
        news: list = []
        current_captured_at = None
    else:
        previous = snapshot.load_previous()
        portfolio, transactions = sc_bridge.refresh_from_sc()
        data_quality = analyze.assess_data_quality(portfolio, previous)
        watchlist = sc_bridge.fetch_watchlist_from_sc()
        news = filter_news.fetch_and_filter_news(portfolio)
        idea_news = filter_news.fetch_news_for_unlisted_ideas(portfolio)
        seen = {str(n.get("title", "")) for n in news if isinstance(n, dict)}
        news = news + [n for n in idea_news if isinstance(n, dict) and str(n.get("title", "")) not in seen]
        changes = None
        current_captured_at = None
    strategy = analyze.load_strategy()
    analysis = analyze.analyze_portfolio(portfolio, transactions, strategy)
    return facts.build_facts_package(
        portfolio,
        transactions,
        analysis,
        news,
        strategy,
        mode="monday",
        changes=changes,
        data_quality=data_quality,
        previous_snapshot=previous if not mock else None,
        current_captured_at=current_captured_at,
        watchlist=watchlist,
    )


def main() -> int:
    """Dev-Validator: Faktenpaket -> 1 LLM-Call -> verify -> Findings + Gate."""
    parser = argparse.ArgumentParser(description="Shadow validator for prompt development")
    parser.add_argument("--mock", action="store_true", help="Mock-Daten statt Live-sc/News")
    args = parser.parse_args()

    facts_package = _build_facts_package(args.mock)
    draft = llm_briefing.generate_draft(facts_package, mode="monday")
    verification = verify.verify_draft(facts_package, draft)

    for finding in verification:
        print(f"[{finding.get('severity', '?')}] {finding.get('issue', '?')}")
        print(f"    Evidence: {finding.get('evidence', '')}")
    if any(f.get("severity") in ("critical", "major") for f in verification):
        print("GATE WÜRDE BLOCKIEREN")
        return 1
    print("VERIFY PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
