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

Deterministisches Faktenpaket → DeepSeek-Draft → final_briefing-Render →
verify_draft → glm-5.2-Review → (bedingt: deepseek-v4-flash-Revision,
maximal MAX_REVISIONS, danach erneuter Render + verify) → final_gate →
Versand. overall_verdict `block`, kritische Findings, ungueltiges Review
oder wiederholte Verify-Fehler blockieren den Versand fail-closed (nur
Alert, keine Vault-Datei). Der produktive Verify-/Review-/Final-Gate-Pfad
prueft den gerenderten Text (Teil 2); der Dry-Run-Pfad nutzt unveraendert
_dry_run_placeholder.
"""
from __future__ import annotations

import argparse
import logging
import re
import sys
import traceback
from datetime import datetime
from pathlib import Path

from scripts import (
    analyze,
    diff,
    facts,
    filter_news,
    final_briefing,
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

# --- GLM-Review-Kontrakt: deterministische Sektionen --------------------------
# Die Sektionen der finalen Briefing-Datei werden von Python deterministisch
# aus dem Faktenpaket gerendert (final_briefing.py). Der Review-Prompt
# (config/prompts/review.txt) verpflichtet GLM, dort KEINE eigenen Status-,
# Zahlen-, Kategorien-, Transaktions- oder Strategieänderungen zu behaupten.
# Trotzdem kann ein bestehendes Review-JSON solche halluzinierten Findings
# enthalten. Der Orchestrator filtert ausschließlich Findings, die klar diese
# deterministischen Sektionen/Fakten betreffen; andere echte Review-Findings
# (unerlaubte Handlungsempfehlungen, ## Kurzlage-Verstöße gegen die
# Zusammenfassung) bleiben fail-closed blockierend.
#
# Der Filter ist bewusst konservativ (Keyword-basiert, kein Textverständnis):
# - Deterministische Sektionen werden nur als SUBJEKT des Findings (issue)
#   gewertet — die bloße Nennung in evidence (z. B. "Kaufanweisung außerhalb
#   von ## Entscheidungsrelevante Punkte") ist eine Ortsangabe, kein
#   Fakten-Finding.
# - Findings, die die ## Kurzlage erwähnen, werden NIE gefiltert: GLM darf die
#   Kurzlage gegen die deterministische Zusammenfassung prüfen.
# - Generische Terme wie status/zahl/rot/position werden bewusst NICHT als
#   Filterkriterium genutzt — sie können in echten Kurzlage-Findings
#   vorkommen und müssen dann fail-closed blockieren.
_DETERMINISTIC_SECTIONS = (
    "Datenqualität",
    "Entscheidungsrelevante Punkte",
    "Strategie-Abgleich",
    "Relevante News & Veränderungen",
    "Empfehlung",
)
# Wortgrenzen: "Handlungsempfehlung" trifft "Empfehlung" NICHT (nur die
# eigenständige Sektions-/Label-Nennung), "Entscheidungsrelevante Punkte"
# nur als vollständige Sektion.
_DETERMINISTIC_SECTION_RE = re.compile(
    r"(?i)\b(" + "|".join(re.escape(s) for s in _DETERMINISTIC_SECTIONS) + r")\b"
)
# Klare deterministische Fakten-Themen (Substring, case-insensitive):
# Transaktionen, Datenqualität, gerenderte Veränderungszeilen (Hinzugekommen/
# Entfernt/Geändert), Ampel-Kategorien. BUY/SELL/WATCH mit Wortgrenzen (das
# deterministische Empfehlungs-Label).
_DETERMINISTIC_FACT_TERMS = (
    "datenqualität",
    "datenqualitaet",
    "transaktion",
    "hinzugekommen",
    "hinzugefügt",
    "hinzugefuegt",
    "entfernt",
    "geändert",
    "geaendert",
    "ampel",
    "kategorie",
    "strategieänderung",
    "strategieänderungen",
    "strategie-aenderung",
    "strategie-änderung",
)
_DETERMINISTIC_LABEL_RE = re.compile(r"(?i)\b(buy|sell|watch)\b")


def _finding_touches_deterministic_sections(finding: dict) -> bool:
    """True, wenn ein Review-Finding klar die deterministischen Sektionen
    bzw. deterministische Fakten betrifft (konservativ, Keyword-basiert).

    Treffer, wenn (a) eine deterministische Sektion als Subjekt (issue)
    genannt ist — ## Datenqualität, ## Entscheidungsrelevante Punkte,
    ## Strategie-Abgleich, ## Relevante News & Veränderungen, ## Empfehlung —
    oder (b) ein deterministischer Faktenterm (Transaktionen, Datenqualität,
    Hinzugekommen/Entfernt/Geändert, Ampel-Kategorie, BUY/SELL/WATCH-Label)
    in issue/evidence/correction vorkommt. Findings, die die ## Kurzlage
    betreffen, werden NIE gefiltert (GLM darf sie gegen die Zusammenfassung
    prüfen); Findings ohne klaren Bezug (z. B. unerlaubte
    Handlungsempfehlungen) bleiben fail-closed blockierend.
    """
    issue = str(finding.get("issue", ""))
    evidence = str(finding.get("evidence", ""))
    correction = str(finding.get("correction", ""))
    haystack = f"{issue} {evidence} {correction}".lower()
    # ## Kurzlage ist der EINZIGE semantische Review-Bereich: solche Findings
    # sind echt (Zusammenfassungs-Abgleich) und dürfen nie gefiltert werden.
    if "kurzlage" in haystack:
        return False
    if _DETERMINISTIC_SECTION_RE.search(issue):
        return True
    if _DETERMINISTIC_LABEL_RE.search(haystack):
        return True
    for term in _DETERMINISTIC_FACT_TERMS:
        if term in haystack:
            return True
    return False


def _filter_review_findings(review: object) -> dict:
    """Filtert klar deterministische Review-Findings aus (defensiv, unverändert
    bei Nicht-Dict/fehlendem findings-Key).

    Nur Findings, die eindeutig die deterministischen Sektionen/Fakten
    betreffen (siehe _finding_touches_deterministic_sections), werden entfernt.
    Echte Review-Findings (unerlaubte Handlungsempfehlungen, ## Kurzlage-
    Verstöße) bleiben erhalten und blockieren weiterhin fail-closed.

    Wurden ALLE Findings als halluziniert gefiltert, hat das overall_verdict
    keine verbleibende Basis mehr: es wird auf "pass" gesetzt, damit ein
    reines Halluzinations-Review den Gate nicht blockiert (kein Verschwenden
    einer Revision). Bleiben echte Findings übrig, bleibt das Verdict
    unangetastet (fail-closed).
    """
    if not isinstance(review, dict):
        return review  # type: ignore[return-value]
    findings = review.get("findings")
    if not isinstance(findings, list):
        return review  # type: ignore[return-value]
    filtered = [f for f in findings if not _finding_touches_deterministic_sections(f)]
    if len(filtered) == len(findings):
        return review  # type: ignore[return-value]
    result = {**review, "findings": filtered}
    if not filtered:
        result["overall_verdict"] = "pass"
    return result

# Dry-Run-Platzhalter statt Fehlerstring: explizit als Dry-Run markiert,
# damit kein Fehlertext als Briefing durch die Pipeline laeuft. Sektionen
# entsprechen exakt dem kurzen Output-Contract (verify.SHORT_SECTIONS).
def _dry_run_placeholder(facts_package: dict | None) -> str:
    """Dry-Run-Platzhalter statt Fehlerstring: explizit als Dry-Run markiert,
    damit kein Fehlertext als Briefing durch die Pipeline laeuft. Sektionen
    entsprechen exakt dem kurzen Output-Contract (Phase 5, verify.SHORT_SECTIONS).
    Die Signal-Sektionen uebernehmen die deterministischen Signale aus dem
    Faktenpaket (final_briefing-Renderer), damit verify auch im Dry-Run
    konsistent bleibt. Der Fundamentaldaten-Disclaimer ist hart verankert.
    """
    label = "WATCH"
    reason = "Dry-Run, keine deterministische Bewertung."
    if facts_package is not None:
        rec = facts_package.get("deterministic_summary", {}).get("recommendation", {})
        if isinstance(rec, dict) and rec.get("label") in ("BUY", "SELL", "WATCH"):
            label = rec["label"]
            reason = f"Dry-Run — deterministisches Label: {rec.get('reason', label)}."
    try:
        summary = facts_package.get("deterministic_summary", {}) if facts_package is not None else {}
        sell_section = final_briefing._section_sell_reduce_signals(summary)
        watch_section = final_briefing._section_watchlist_signals(summary)
        next_section = final_briefing._section_naechster_schritt(summary)
    except Exception:
        sell_section = "Keine Sell-/Reduce-Signale.\n\n" + final_briefing.FUNDAMENTALS_DISCLAIMER
        watch_section = "Keine Watchlist-Signale (NO SIGNAL für alle Positionen).\n\n" + final_briefing.FUNDAMENTALS_DISCLAIMER
        next_section = "Nächste Woche neuer Lauf, keine Aktion erforderlich."
    return (
        "## Kurzlage\n"
        "Dry-Run — kein LLM-Call.\n\n"
        "## Datenqualität\n"
        "—\n\n"
        "## Sell-/Reduce-Signale (bestehende Satellites)\n"
        f"{sell_section}\n\n"
        "## Watchlist-Signale\n"
        f"{watch_section}\n\n"
        "## Nächster Schritt\n"
        f"{next_section}"
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
    previous = None
    staged = None
    try:
        # Stufe 1: deterministisches Faktenpaket
        try:
            if dry_run:
                # Dry-Run: ausschliesslich Mock-Daten als Datenquelle — kein
                # sc-Aufruf, kein Snapshot-/Diff-Schreiben, keine Live-Config.
                portfolio, transactions = sc_bridge.load_mock()
                watchlist = sc_bridge.load_mock_watchlist()
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
                # Watchlist-Abruf (Phase 6 Interface): produktiver Abruf ist
                # fail-closed; eine leere Watchlist ist ein legitimer Zustand.
                watchlist = sc_bridge.fetch_watchlist_from_sc()
            analysis = analyze.analyze_portfolio(portfolio, transactions, strategy)
            if dry_run:
                # Dry-Run: vollstaendig netzwerkfrei — kein RSS-/News-Fetch.
                news: list = []
            else:
                news = filter_news.fetch_and_filter_news(portfolio)
                # Neukaufideen-Basis (Plan §6a): gezielte Recherche fuer
                # unbekannte Wertpapiere ergaenzen die Bestands-News — die
                # Quellen-/Duplikatpruefung bewertet beide gemeinsam.
                idea_news = filter_news.fetch_news_for_unlisted_ideas(portfolio)
                seen = {str(n.get("title", "")) for n in news if isinstance(n, dict)}
                news = news + [n for n in idea_news if isinstance(n, dict) and str(n.get("title", "")) not in seen]
            facts_package = facts.build_facts_package(
                portfolio,
                transactions,
                analysis,
                news,
                strategy,
                mode=mode,
                changes=changes,
                data_quality=data_quality,
                previous_snapshot=previous if not dry_run else None,
                current_captured_at=staged["snapshot"]["captured_at"] if (not dry_run and staged) else None,
                watchlist=watchlist,
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

        # Stufe 2: DeepSeek-Draft (im Dry-Run Platzhalter, kein LLM-Call).
        # Danach folgt IMMER der deterministische Final-Render (Teil 2):
        # der produktive Verify-/Review-/Final-Gate-Pfad prueft den
        # gerenderten Text, nicht den rohen LLM-Draft. Der Dry-Run-Pfad
        # behaelt unveraendert _dry_run_placeholder (kein Render).
        try:
            if dry_run:
                draft = _dry_run_placeholder(facts_package)
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

        # Stufe 2b: deterministischer Final-Render (Teil 2) — der LLM-Draft
        # wird vollstaendig ersetzt: ab hier prueft verify/review/gate den
        # gerenderten Briefing-Text (Kurzlage/Datenqualitaet/Punkte/
        # Strategie/News/Empfehlung) 1:1 aus dem Faktenpaket. Render-Fehler
        # sind fail-closed (kein Versand, keine Vault-Datei). Dry-Run
        # ueberspringt den Render unveraendert (Platzhalter-Pfad).
        if not dry_run:
            try:
                draft = final_briefing.render_final_briefing(facts_package, mode=mode)
                logging.info("final briefing rendered (Teil 2)")
            except Exception as e:
                _alert(f"final render failed: {e}\n{traceback.format_exc()}")
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
        #
        # GLM-Review-Kontrakt (config/prompts/review.txt): die deterministisch
        # gerenderten Sektionen sind KEIN Review-Bereich für Faktenbehauptungen.
        # Liefert GLM trotzdem klar halluzinierte Findings zu diesen Sektionen/
        # Fakten, filtert der Orchestrator genau diese aus — andere echte
        # Review-Findings bleiben fail-closed blockierend.
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

                review = _filter_review_findings(review)
                if (
                    not isinstance(review, dict)
                    or not isinstance(review.get("findings"), list)
                    or "overall_verdict" not in review
                ):
                    # Defensiv: ungueltiges Review nach Filterung fail-closed behandeln.
                    review = {"findings": [], "overall_verdict": "block"}
                if review.get("findings"):
                    kept = [f["issue"] for f in review["findings"]]
                    logging.warning(f"review findings nach Filterung: {kept}")

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
                    # Stufe 2b erneut: nach jeder Revision wird der Draft
                    # erneut deterministisch gerendert (Teil 2) — verify und
                    # Review pruefen immer den gerenderten Text, nie den
                    # rohen LLM-Output. Render-Fehler sind fail-closed.
                    try:
                        draft = final_briefing.render_final_briefing(facts_package, mode=mode)
                        logging.info("final briefing re-rendered after revision (Teil 2)")
                    except Exception as e:
                        _alert(f"final render failed: {e}\n{traceback.format_exc()}")
                        return 1
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
