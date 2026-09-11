"""Orchestrator: pull → check → fund → news → brief → sanity → render → send.

Aufruf:
    .venv/bin/python scripts/run_briefing.py monday [--dry-run]

Strikte CLI: unbekannte Flags/Modes → Fehler mit Usage, nie stiller Live-Run
(Tippfehler-Schutz, z.B. ``--dryrun`` statt ``--dry-run``).

Live:
- Phase A pull via headless ``opencode run`` (Default-Agent, Projekt-MCP
  ``scalable``) → data/portfolio.json, data/watchlist.json, ...
- Fallback-Kette lt. Plan §2: 1) MCP-Pull; 2) sc-Pull (P3-Hook);
  3) letzter Snapshot aus data/*.json mit Alterswarnung + "MCP re-auth / sc login"-Ping.
- Jeder Fehlerpfad → Telegram-Alert (kurz, handlungsorientiert) + Exit 1, nie stumm.

Dry-Run:
- KEIN MCP-Pull, KEIN Telegram, KEIN echter LLM-Call. Fixtures aus
  tests/mock_data/, schreibt das Artefakt reports/{date}-{mode}-dryrun.md, Exit 0.
"""

from __future__ import annotations

import argparse
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
PIPELINE_PATH = REPO_ROOT / "config" / "pipeline.yaml"

_FIXTURE_EURUSD = 1.08  # Dry-Run-FX für EUR-Normalisierung der Fixtures

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


class AlertExit(Exception):
    """Kontrollierter Abbruch, der zwingend einen Alert auslöst."""


# ---------------------------------------------------------------- Phase A/B

def _pull_phase_a() -> None:
    """Phase A: headless MCP-Pull (Default-Agent, Projekt-MCP scalable)."""
    DATA_DIR.mkdir(exist_ok=True)
    cmd = ["opencode", "run", PULL_PROMPT]
    print("[phase A] MCP-Pull via opencode run ...")
    subprocess.run(cmd, cwd=str(REPO_ROOT), check=True, timeout=300)


def _pull_phase_b() -> bool:
    """sc-Pull-Fallback — zählt als P3 (noch NICHT implementiert).

    Klar markierter Hook: liefert True, wenn ein frischer Pull erfolgreich war.
    Bis P3 ist dies eine bewusste Leerstelle.
    """
    print("[phase B] sc-Pull-Fallback ist P3 — aktuell nicht implementiert.")
    return False


def _fetch_eurusd() -> float | None:
    """EURUSD-Kurs für FX-Normalisierung (fail-open, config-getrieben)."""
    return fundamentals.fetch_eurusd(str(PIPELINE_PATH))


def _fx_note(snap: data_mod.Snapshot, eurusd: float | None) -> str:
    """FX-Befund für den Bericht: genutzte Feldnamen + Entscheidung (find. 4).

    Per-Position-EUR-Felder (value_eur/valuation/currentValue/marketValue/value)
    werden bevorgezogen, wenn die Overview-Prüfung sie zeigte. Sonst Umrechnung
    Nicht-EUR über EURUSD (config/yfinance, fail-open); ohne Kurs → Position
    aus total ausgeschlossen (kein falscher EUR-Wert).
    """
    non_eur = [h for h in snap.holdings if h.get("currency") and h["currency"] != "EUR"]
    converted = [h for h in non_eur if h.get("fx_applied")]
    excluded = [h for h in non_eur if h.get("value_eur") is None]
    parts = ["Per-Position-EUR-Felder bevorzugt (Overview); Nicht-EUR per EURUSD"]
    if eurusd is not None:
        parts.append(f"EURUSD={eurusd:.2f}")
    else:
        parts.append("EURUSD unbekannt (fail-open-Schluss)")
    if converted:
        parts.append(f"{len(converted)} USD-Position umgerechnet")
    if excluded:
        parts.append(f"{len(excluded)} Nicht-EUR-Position ohne Wert ausgeschlossen")
    return "; ".join(parts)


# ---------------------------------------------------------------- Fixtures (Dry-Run)

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
    snap = data_mod.load_snapshot(MOCK_DIR, eurusd=_FIXTURE_EURUSD)
    strategy = checks.load_strategy(str(STRATEGY_PATH))
    data_mod.apply_strategy_classification(snap, strategy)
    signals = checks.run_checks(snap.holdings, strategy)
    _, core_ratio, sat_ratio = checks.calculate_ratios(snap.holdings)
    tickers, isins = _collect_asset_refs(snap)
    mcp_news = snap.news
    # Dry-Run: keine echten RSS-/yfinance-Calls.
    news_items = mcp_news + news_mod.fetch_rss([], tickers, isins)
    return {
        "holdings": snap.holdings,
        "watchlist": snap.watchlist,
        "quotes": snap.quotes,
        "news": news_items,
        "fundamentals": _load_fixture_fundamentals(),
        "signals": [vars(s) for s in signals],
        "core_ratio": core_ratio,
        "satellite_ratio": sat_ratio,
        "total_value_eur": snap.total_value_eur,
        "cash_eur": snap.cash_eur,
        "captured_at": snap.captured_at,
        "suggestions": _load_fixture_suggestion(),
        "fx_note": _fx_note(snap, _FIXTURE_EURUSD),
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


# ---------------------------------------------------------------- Live-Daten

def _snapshot_age_hours() -> float | None:
    """Datenalter aus Datei-Metadaten (mtime) von data/portfolio.json."""
    p = DATA_DIR / "portfolio.json"
    if not p.exists():
        return None
    mtime = datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc)
    return (datetime.now(timezone.utc) - mtime).total_seconds() / 3600.0


def _has_snapshot() -> bool:
    return (DATA_DIR / "portfolio.json").exists() and (DATA_DIR / "watchlist.json").exists()


def _build_common(snap: data_mod.Snapshot, strategy: dict, eurusd: float | None) -> dict:
    """Baut das gemeinsame Datendikt (Normalisierung + Checks + Fund + News)."""
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
        "fx_note": _fx_note(snap, eurusd),
        "issues": [{"severity": i.severity, "message": i.message} for i in snap.issues],
    }


def _acquire_live_data(dry_run: bool) -> dict:
    """Fallback-Kette: 1) MCP-Pull, 2) sc-Pull (P3), 3) letzter Snapshot."""
    pull_ok = False
    try:
        _pull_phase_a()
        pull_ok = _has_snapshot()
    except Exception as exc:  # noqa: BLE001 - Pull-Fehler → nächste Stufe
        print(f"[phase A] MCP-Pull fehlgeschlagen: {exc}")

    if not pull_ok and _pull_phase_b():
        pull_ok = _has_snapshot()

    if not pull_ok:
        # Stufe 3: letzter Snapshot mit Alterswarnung + Re-Auth-Ping.
        if not _has_snapshot():
            raise AlertExit(
                "Portfolio-Briefing fehlgeschlagen: kein MCP-/sc-Pull und kein letzter "
                "Snapshot. Bitte MCP re-auth / sc login prüfen."
            )
        age = _snapshot_age_hours()
        age_note = f" (Alter ~{age:.0f} h)" if age is not None else ""
        print(f"[phase C] Pull fehlgeschlagen — nutze letzten Snapshot{age_note}")
        _send_alert(
            f"Portfolio-Briefing: MCP re-auth / sc login bitte. Pull fehlgeschlagen, "
            f"nutze letzten Snapshot{age_note}.",
            dry_run,
        )

    eurusd = _fetch_eurusd()
    snap = data_mod.load_snapshot(DATA_DIR, eurusd=eurusd)
    strategy = checks.load_strategy(str(STRATEGY_PATH))
    data = _build_common(snap, strategy, eurusd)
    data["snapshot_age_hours"] = _snapshot_age_hours()
    return data


# ---------------------------------------------------------------- Alert

def _send_alert(message: str, dry_run: bool) -> bool:
    """Sendet einen kurzen handlungsorientierten Alert (Telegram mode 'alert').

    Dry-Run: nur Log, kein Telegram. Best-Effort — Sendefehler blockieren den
    eigentlichen Fehlerpfad nicht, sind aber nie stumm.
    """
    if dry_run:
        print(f"[alert-skipped dry-run] {message}")
        return False
    try:
        from scripts import send_telegram

        return send_telegram.send_briefing(message, "alert")
    except Exception as exc:  # noqa: BLE001 - Alert nie stumm
        print(f"[alert] Sendefehler: {exc}")
        return False


# ---------------------------------------------------------------- Run

def run(mode: str, dry_run: bool) -> int:
    if dry_run:
        print("[dry-run] Fixtures statt Live-Daten; kein MCP/Telegram/LLM.")
        data = _load_fixtures()
    else:
        try:
            data = _acquire_live_data(dry_run=False)
        except AlertExit as exc:
            _send_alert(str(exc), dry_run=False)
            return 1

    # Snapshot-SeverityCheck VOR dem LLM-Call (kein sinnloser LLM-Call bei
    # kaputtem Snapshot / kein Halluzinations-Futter).
    fatal = [i for i in data["issues"] if i["severity"] == "error"]
    if fatal:
        msg = f"Portfolio-Briefing: {len(fatal)} Datenfehler — kein Briefing."
        for f in fatal:
            msg += f"\n - {f['message']}"
        print(msg)
        _send_alert(msg, dry_run)
        return 1

    # brief-Phase: genau 1 LLM-Call (config-determiniert) — nur live.
    if not dry_run:
        print("[phase brief] LLM-Call ...")
        try:
            data["suggestions"] = brief.generate_suggestions(data)
        except Exception as exc:  # noqa: BLE001 - fail-closed
            msg = "Portfolio-Briefing: LLM-Call fehlgeschlagen — kein Briefing."
            print(f"{msg} {exc}")
            _send_alert(msg, dry_run)
            return 1

    # Sanity gegen die LLM-Ausgabe (Dry-Run: Fixture-Suggestion).
    suggestions = data.get("suggestions") or ""
    violations = sanity.check_sanity(suggestions, data)
    if violations:
        msg = "Portfolio-Briefing: Sanity verletzt — kein Briefing-Versand."
        for v in violations:
            msg += f"\n - {v}"
        print(msg)
        if not dry_run:
            _send_alert(msg, dry_run)
            return 1
        # Dry-Run: Sanity-Verletzungen melden, aber Artefakt trotzdem erzeugen.

    text = render.render_briefing(data)
    REPORT_DIR.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    artifact = REPORT_DIR / f"{stamp}-{mode}-{'dryrun' if dry_run else 'briefing'}.md"
    artifact.write_text(text, encoding="utf-8")
    print(f"[artefakt] {artifact}")

    if dry_run:
        return 0

    ok = send_telegram_send(text, mode)
    if not ok:
        _send_alert("Portfolio-Briefing: Telegram-Versand fehlgeschlagen — kein Briefing zugestellt.", False)
        return 1
    return 0


def send_telegram_send(text: str, mode: str) -> bool:
    """Sendet das Briefing via send_telegram (Fail-closed)."""
    from scripts import send_telegram  # lokaler Import vermeidet Zirkularität

    return send_telegram.send_briefing(text, mode)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="run_briefing.py",
        description="Portfolio-Briefing-Pipeline (Mo).",
    )
    parser.add_argument("mode", nargs="?", default="monday",
                        choices=["monday", "friday", "monthly"],
                        help="Briefing-Modus")
    parser.add_argument("--dry-run", action="store_true", help="Dry-Run ohne externe Calls")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])
    return run(args.mode, args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
