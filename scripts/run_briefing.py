"""Orchestrator for portfolio briefing pipeline."""
from __future__ import annotations

import argparse
import logging
import sys
import traceback
from datetime import datetime
from pathlib import Path

from scripts import analyze, filter_news, llm_briefing, render_markdown, sc_bridge, send_telegram, verify


ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = ROOT / "logs"
LOG_FILE = LOG_DIR / "briefing.log"


def _setup_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler(sys.stdout)],
    )


def _already_run_today(mode: str) -> bool:
    date = datetime.now().strftime("%Y-%m-%d")
    vault = Path.home() / "docs" / "notizen" / "portfolio-briefings"
    return (vault / f"{date}-{mode}.md").exists()


def _alert(message: str) -> None:
    logging.error(message)
    try:
        send_telegram.send_briefing(f"⚠️ portfolio-briefing Fehler\n\n{message}", "alert")
    except Exception:
        pass


def run(mode: str, dry_run: bool = False) -> int:
    _setup_logging()
    logging.info(f"Starting briefing run: mode={mode} dry_run={dry_run}")

    if mode not in {"monday", "friday", "monthly"}:
        logging.error(f"Invalid mode: {mode}")
        return 1

    if not dry_run and _already_run_today(mode):
        logging.info("Briefing already exists for today+mode. Skipping.")
        return 0

    try:
        portfolio, transactions = sc_bridge.update_config()
        logging.info("sc_bridge completed")
    except Exception as e:
        _alert(f"sc_bridge failed: {e}\n{traceback.format_exc()}")
        return 1

    try:
        strategy = analyze.load_strategy()
        analysis = analyze.analyze_portfolio(portfolio, transactions, strategy)
        logging.info("analyze completed")
    except Exception as e:
        _alert(f"analyze failed: {e}\n{traceback.format_exc()}")
        return 1

    try:
        news = filter_news.fetch_and_filter_news(portfolio)
        logging.info(f"filter_news completed: {len(news)} candidates")
    except Exception as e:
        _alert(f"filter_news failed: {e}\n{traceback.format_exc()}")
        return 1

    try:
        if dry_run:
            briefing = "Briefing-Generierung fehlgeschlagen: Dry-Run ohne LLM."
        else:
            briefing = llm_briefing.generate_briefing(portfolio, analysis, news, strategy, mode=mode)
        logging.info("llm_briefing completed")
    except Exception as e:
        _alert(f"llm_briefing failed: {e}\n{traceback.format_exc()}")
        return 1

    try:
        warnings = verify.verify_briefing(briefing, analysis, news, portfolio)
        if warnings:
            logging.warning(f"verify warnings: {warnings}")
    except Exception as e:
        logging.warning(f"verify failed: {e}")
        warnings = []

    date = datetime.now().strftime("%Y-%m-%d")
    if warnings:
        briefing = briefing + "\n\n---\n\nVerify-Warnungen:\n" + "\n".join(f"- {w}" for w in warnings)

    try:
        markdown = render_markdown.render(briefing, mode, date)
        logging.info("render completed")
    except Exception as e:
        _alert(f"render failed: {e}\n{traceback.format_exc()}")
        return 1

    try:
        if dry_run:
            path = Path.home() / "docs" / "notizen" / "portfolio-briefings" / f"{date}-{mode}.md"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(markdown, encoding="utf-8")
            logging.info(f"Dry-run archived to {path}")
        else:
            send_telegram.send_briefing(markdown, mode)
            logging.info("send completed")
    except Exception as e:
        _alert(f"send failed: {e}\n{traceback.format_exc()}")
        return 1

    logging.info("Briefing run completed successfully")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Portfolio briefing runner")
    parser.add_argument("mode", choices=["monday", "friday", "monthly"], default="monday", nargs="?")
    parser.add_argument("--dry-run", action="store_true", help="Run without LLM and Telegram")
    args = parser.parse_args()
    sys.exit(run(args.mode, dry_run=args.dry_run))


if __name__ == "__main__":
    main()
