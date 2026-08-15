"""Orchestrator for portfolio briefing pipeline (Phase 4, two-stage + revise loop).

Datenbeschaffung: Dry-Run ausschliesslich ueber sc_bridge.load_mock() (kein
sc-Aufruf, kein Snapshot-/Diff-Schreiben, kein Telegram-Alert bei Fehlern).
Produktiver Lauf: snapshot.load_previous() -> sc_bridge.refresh_from_sc()
(fail-closed, kein Mock) -> capture_staged (schreibt nur pending, Baseline
unangetastet) -> diff.diff_snapshots(previous, staged) -> erst danach
analyze/filter/facts/LLM. Erst nach final_gate + Render wird staged via
promote_staged zur Baseline; jeder Fehlerpfad davor verwirft staged
(discard_staged) — fehlgeschlagene Laeufe schreiben die Baseline nie fort.
Persistenz nur ueber das Snapshot-Modul (kein update_config).

Deterministisches Faktenpaket → DeepSeek-Draft → verify_draft → glm-5.2-
Review → (bedingt: deepseek-v4-flash-Revision, maximal MAX_REVISIONS) →
final_gate → Versand. overall_verdict `block`, kritische Findings,
ungueltiges Review oder wiederholte Verify-Fehler blockieren den Versand
fail-closed (nur Alert, keine Vault-Datei).
"""
from __future__ import annotations

import argparse
import logging
import sys
import traceback
from datetime import datetime
from pathlib import Path

from scripts import (
    analyze,
    diff,
    facts,
    filter_news,
    llm_briefing,
    llm_review,
    llm_revise,
    render_markdown,
    sc_bridge,
    send_telegram,
    snapshot,
    verify,
)

ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = ROOT / "logs"
LOG_FILE = LOG_DIR / "briefing.log"
VAULT_DIR = Path.home() / "docs" / "notizen" / "portfolio-briefings"

# Maximal eine Revision (Plan §6.1: max_revisions: 1) — kein Endlos-Loop.
MAX_REVISIONS = 1

# Dry-Run-Platzhalter statt Fehlerstring: explizit als Dry-Run markiert,
# damit kein Fehlertext als Briefing durch die Pipeline laeuft. Sektionen
# entsprechen exakt dem 5-Sektionen-Output-Contract (verify.DRAFT_SECTIONS).
DRY_RUN_PLACEHOLDER = (
    "## Kurzlage\n"
    "Dry-Run — kein LLM-Call.\n\n"
    "## Datenqualität\n"
    "—\n\n"
    "## Entscheidungsrelevante Punkte\n"
    "—\n\n"
    "## Strategie-Abgleich\n"
    "—\n\n"
    "## Relevante News & Veränderungen\n"
    "—"
)


def _setup_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler(sys.stdout)],
    )


def _already_run_today(mode: str) -> bool:
    date = datetime.now().strftime("%Y-%m-%d")
    return (VAULT_DIR / f"{date}-{mode}.md").exists()


# Dry-Run-Modus: _alert() sendet in diesem Fall keinen Telegram-Alert (nur Logging),
# damit Dry-Run-Fehler keine externen Calls/Secrets-Risiken ausloesen.
_DRY_RUN_ACTIVE = False


# Handlungsorientierte Alerts fuer differenzierte sc-Auth-Fehler: die
# Fehlerklasse wird benannt, die Aktion klar adressiert. Kein non-interactive-
# Login (sc login ist human-oriented, OAuth-Device-Flow).
_SC_AUTH_ALERTS: dict[type, str] = {
    sc_bridge.ScSessionExpiredError: (
        "sc-Session abgelaufen (no_session) — interaktives `sc login` erforderlich "
        "(kein non-interactive-/Cron-Login; CLI refresht nur bei Nutzung)."
    ),
    sc_bridge.ScReloginRequiredError: (
        "sc-Session erfordert Re-Login (REFRESH_RELOGIN_REQUIRED) — interaktives "
        "`sc login` erforderlich (kein non-interactive-/Cron-Login)."
    ),
    sc_bridge.ScSecretStorageError: (
        "sc Secret Storage nicht verfuegbar (secret_storage_unavailable) — "
        "System-/Keyring-Pruefung erforderlich."
    ),
}


def _alert(message: str) -> None:
    logging.error(message)
    if _DRY_RUN_ACTIVE:
        # Dry-Run: Fehler nur loggen — kein Telegram-Alert.
        return
    try:
        # Alert-Modus: send_telegram archiviert in diesem Fall nichts im Vault.
        send_telegram.send_briefing(f"⚠️ portfolio-briefing Fehler\n\n{message}", "alert")
    except Exception:
        pass


def run(mode: str, dry_run: bool = False) -> int:
    global _DRY_RUN_ACTIVE
    _DRY_RUN_ACTIVE = dry_run
    _setup_logging()
    logging.info(f"Starting briefing run: mode={mode} dry_run={dry_run}")

    if mode not in {"monday", "friday", "monthly"}:
        logging.error(f"Invalid mode: {mode}")
        return 1

    if not dry_run and _already_run_today(mode):
        logging.info("Briefing already exists for today+mode. Skipping.")
        return 0

    # Cleanup: alter staged-Snapshot aus einem gecrashten Lauf verwerfen
    # (idempotent, fail-closed). Ein frischer Lauf startet immer ohne staged-Stand.
    if not dry_run:
        snapshot.discard_staged()

    # Staged-Snapshot-Lifecycle: capture_staged schreibt den Live-Stand nur als
    # pending (config/snapshot.staged.json); die Baseline
    # (snapshot.current.json) bleibt bis zur finalen Promotion unangetastet.
    # Erst nach final_gate + erfolgreichem Render wird promoted (vor Versand).
    # Jeder Fehlerpfad danach (verify/review/revise/gate/render/send) verwirft
    # staged ueber das finally unten — ein fehlgeschlagener Lauf schreibt die
    # Baseline nie fort.
    staged_promoted = False
    try:
        # Stufe 1: deterministisches Faktenpaket
        try:
            if dry_run:
                # Dry-Run: ausschliesslich Mock-Daten als Datenquelle — kein
                # sc-Aufruf, kein Snapshot-/Diff-Schreiben, keine Live-Config.
                portfolio, transactions = sc_bridge.load_mock()
                changes = None
                strategy = analyze.load_strategy()  # validiert intern (fail-closed)
                data_quality = analyze.assess_data_quality(portfolio, None)
            else:
                # Produktiver Lauf: erst letzten erfolgreichen Snapshot laden,
                # dann Strategie laden (schema-validiert, fail-closed bei
                # ungueltig), dann frischer Live-Abruf (fail-closed, kein Mock).
                previous = snapshot.load_previous()
                strategy = analyze.load_strategy()  # validiert intern (fail-closed)
                portfolio, transactions = sc_bridge.refresh_from_sc()
                # Datenqualitaet VOR den Portfolio-Checks pruefen (stale/
                # incomplete/implausible): kein Abbruch, aber die roten Befunde
                # werden im Faktenpaket supprimiert (nur "data_quality" als
                # roter Punkt).
                data_quality = analyze.assess_data_quality(portfolio, previous)
                # Live-Stand zunaechst NUR staged persistieren (inkl. Strategie-
                # Hash/Content), dann Diff gegen den Vorgaenger berechnen —
                # erst danach analyze/filter/facts. Die Baseline bleibt bis
                # final_gate + Render unveraendert.
                staged = snapshot.capture_staged(
                    portfolio, transactions, mode=mode, sc_meta={"source": "sc"}, strategy=strategy
                )
                changes = diff.diff_snapshots(previous, staged["snapshot"])
                logging.info(f"snapshot staged (previous: {previous is not None})")
            analysis = analyze.analyze_portfolio(portfolio, transactions, strategy)
            if dry_run:
                # Dry-Run: vollstaendig netzwerkfrei — kein RSS-/News-Fetch.
                news: list = []
            else:
                news = filter_news.fetch_and_filter_news(portfolio)
            facts_package = facts.build_facts_package(
                portfolio,
                transactions,
                analysis,
                news,
                strategy,
                mode=mode,
                changes=changes,
                data_quality=data_quality,
            )
            logging.info("facts package built")
        except Exception as e:
            alert = _SC_AUTH_ALERTS.get(type(e))
            if alert is not None:
                # Differenzierter sc-Auth-Fehler: handlungsorientierter Alert statt
                # generischem "facts failed" + Traceback-Rauschen.
                _alert(alert)
            else:
                _alert(f"facts failed: {e}\n{traceback.format_exc()}")
            return 1

        # Stufe 2: DeepSeek-Draft (im Dry-Run Platzhalter, kein LLM-Call)
        try:
            if dry_run:
                draft = DRY_RUN_PLACEHOLDER
            else:
                draft = llm_briefing.generate_draft(facts_package, mode=mode)
            logging.info("draft completed")
        except llm_briefing.LLMError as e:
            # Fail-closed: LLM-Fehler duerfen nie als Briefing versendet werden.
            _alert(f"draft failed (fail-closed): {e}\n{traceback.format_exc()}")
            return 1
        except Exception as e:
            _alert(f"draft failed: {e}\n{traceback.format_exc()}")
            return 1

        # Stufe 3: deterministische Draft-Verifikation
        verification: list[dict] = []
        try:
            verification = verify.verify_draft(facts_package, draft)
            if verification:
                logging.warning(f"verify_draft findings: {[f['issue'] for f in verification]}")
        except llm_briefing.LLMError as e:
            _alert(f"verify failed (fail-closed): {e}\n{traceback.format_exc()}")
            return 1
        except Exception as e:
            # Unerwarteter Verify-Fehler: fail-closed, kein Versand, keine Vault-Datei.
            _alert(f"verify failed: {e}\n{traceback.format_exc()}")
            return 1

        # Stufe 4/5: LLM-Review + bedingter Revision-Loop (max MAX_REVISIONS).
        # pass -> unveraendert zum final_gate; revise (ohne kritische Findings) ->
        # deepseek-v4-flash-Revision -> verify_draft erneut -> erneutes Review.
        # block/kritisch/MAX erreicht/ungueltig -> final_gate entscheidet fail-closed.
        if dry_run:
            review = {"findings": [], "overall_verdict": "pass"}
        else:
            revision_count = 0
            while True:
                try:
                    review = llm_review.review_draft(facts_package, draft)
                    logging.info("review completed")
                except llm_review.LLMError as e:
                    _alert(f"review failed (fail-closed): {e}\n{traceback.format_exc()}")
                    return 1
                except Exception as e:
                    _alert(f"review failed: {e}\n{traceback.format_exc()}")
                    return 1

                verdict = review.get("overall_verdict")
                has_critical = any(
                    f.get("severity") == "critical" for f in review.get("findings", [])
                )

                if verdict == "pass":
                    break  # unveraendert weiter zum final_gate

                if verdict == "revise" and not has_critical and revision_count < MAX_REVISIONS:
                    try:
                        draft = llm_revise.revise_draft(facts_package, draft, review)
                    except llm_revise.LLMError as e:
                        _alert(f"revise failed (fail-closed): {e}\n{traceback.format_exc()}")
                        return 1
                    except Exception as e:
                        _alert(f"revise failed: {e}\n{traceback.format_exc()}")
                        return 1
                    revision_count += 1
                    logging.info(f"revise completed ({revision_count}/{MAX_REVISIONS})")
                    # Stufe 3 erneut: revidierten Draft deterministisch verifizieren
                    try:
                        verification = verify.verify_draft(facts_package, draft)
                        if verification:
                            logging.warning(f"verify_draft findings (nach Revision): {[f['issue'] for f in verification]}")
                    except llm_briefing.LLMError as e:
                        _alert(f"verify failed (fail-closed): {e}\n{traceback.format_exc()}")
                        return 1
                    except Exception as e:
                        _alert(f"verify failed: {e}\n{traceback.format_exc()}")
                        return 1
                    continue  # revidierten Draft erneut reviewen (bounded durch MAX_REVISIONS)

                # block / kritische Findings / MAX_REVISIONS erreicht: final_gate blockt.
                break

        # Stufe 6: Versand-Gate (nach evtl. Revision entscheidet final_gate)
        gate = verify.final_gate(verification, review)
        if not gate.allow_send:
            # Kein traceback.format_exc(): hier ist kein Exception-Kontext aktiv.
            _alert(f"Briefing blockiert (final_gate): {gate.reason}")
            return 1
        logging.info(f"final_gate: {gate.reason}")

        date = datetime.now().strftime("%Y-%m-%d")
        try:
            # Status kommt explizit vom Orchestrator (kein Fehlertext-Heuristik-Fallback).
            status = "draft" if dry_run else "active"
            markdown = render_markdown.render(draft, mode, date, status=status)
            logging.info("render completed")
        except Exception as e:
            _alert(f"render failed: {e}\n{traceback.format_exc()}")
            return 1

        if not dry_run:
            # final_gate bestanden + Render ok: staged wird zur neuen Baseline
            # (Vorgaenger archiviert), BEVOR versendet wird. Fehlgeschlagene
            # Laeufe bis hierhin haben staged bereits verworfen (finally unten).
            snapshot.promote_staged()
            staged_promoted = True
            logging.info("staged snapshot promoted to baseline")

        try:
            if dry_run:
                # Suffix-Strategie: Dry-Run-Datei blockt keinen realen Lauf am selben Tag.
                path = VAULT_DIR / f"{date}-{mode}-dryrun.md"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(markdown, encoding="utf-8")
                logging.info(f"Dry-run archived to {path}")
            else:
                if not send_telegram.send_briefing(markdown, mode):
                    # send_briefing=False: Versand fehlgeschlagen (dort bereits
                    # diagnostisch geloggt). Fail-closed: kurzer Alert, Exit 1.
                    # Keine weitere Briefing-Datei — die Vault-Archivierung hat
                    # send_briefing intern uebernommen (mode != alert).
                    _alert("send failed: send_briefing returned False")
                    return 1
                logging.info("send completed")
        except Exception as e:
            _alert(f"send failed: {e}\n{traceback.format_exc()}")
            return 1

        logging.info("Briefing run completed successfully")
        return 0
    finally:
        # Fail-closed: ausser bei erfolgreicher Promotion wird der staged-Stand
        # in jedem Pfad verworfen (return 1, Exception, Gate-Block). Dry-Run
        # schreibt nie staged und loescht hier auch nichts.
        if not dry_run and not staged_promoted:
            snapshot.discard_staged()


def main() -> None:
    parser = argparse.ArgumentParser(description="Portfolio briefing runner")
    parser.add_argument("mode", choices=["monday", "friday", "monthly"], default="monday", nargs="?")
    parser.add_argument("--dry-run", action="store_true", help="Run without LLM and Telegram")
    args = parser.parse_args()
    sys.exit(run(args.mode, dry_run=args.dry_run))


if __name__ == "__main__":
    main()
