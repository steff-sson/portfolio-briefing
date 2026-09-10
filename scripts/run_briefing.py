"""Orchestrator for portfolio briefing pipeline (1-Call-Architektur).

Datenbeschaffung: Dry-Run ausschliesslich ueber sc_bridge.load_mock() (kein
sc-Aufruf, kein Snapshot-/Diff-Schreiben, kein Telegram-Alert bei Fehlern).
Produktiver Lauf: snapshot.load_previous() -> sc_bridge.refresh_from_sc()
(fail-closed, kein Mock) -> capture_staged (schreibt nur pending, Baseline
unangetastet) -> diff.diff_snapshots(previous, staged) -> erst danach
analyze/filter/facts/LLM. Erst nach final_gate + Render wird staged via
promote_staged zur Baseline; jeder Fehlerpfad davor verwirft staged
(discard_staged) — fehlgeschlagene Laeufe schreiben die Baseline nie fort.
Persistenz nur ueber das Snapshot-Modul (kein update_config).

1-Call-Architektur (Plan 1-Call-Briefing) + begrenzter Revisions-Loop
(Phase C2, Plan briefing-revision-loop): Faktenpaket -> EIN fachlicher
LLM-Initial-Draft (llm_briefing.generate_draft, deepseek-v4-flash) ->
verify_draft (deterministisch, 1:1 gegen das Faktenpaket) -> final_gate
(verification-only, fail-closed bei critical/major). Blockiert das Gate,
folgen hoechstens MAX_LLM_ATTEMPTS-1 Revisionen (llm_briefing.revise_draft,
gleiche LLM-Quelle, unveraendertes Faktenpaket + strukturierte blockierende
Findings) mit erneutem verify/final_gate — maximal MAX_LLM_ATTEMPTS
LLM-Calls insgesamt, keine Endlosschleife. Bei PASS laeuft der bestehende
final_gate-/Render-/Versand-Pfad weiter.

Phase D (Fallback, Plan briefing-revision-loop §6): Endet der LLM-Loop
ohne validen Draft (Versuchslimit erschoepft ODER harter LLM-/Verify-Fehler),
wird bei valider Datenbasis (Datenqualitaet != implausible, Faktenpaket ok)
statt des technischen Alerts ein deterministisches Faktenbriefing gerendert
(scripts/fallback_briefing.build_fallback_briefing — kein LLM-Call, keine
weitere Revision, keine Endlosschleife) und durch dieselben Sicherheitschecks
(verify_draft + final_gate) gefuehrt. Nur ein gueltiger Fallback durchlaeuft
den bestehenden Render-/Promote-/Versand-Pfad (status active, Briefing-Modus
— kein mode=alert); ein ungueltiger Fallback oder eine kaputte Datenbasis
bleiben beim technischen Alert (kein Versand, keine Promotion). Status/Alert
werden ohne Secrets geloggt; die Fallback-Briefing-Datei ist im Vault klar
als Faktenbriefing markiert. Die alte Humanize-/Review-/Revise-Stufe bleibt
entfernt (nur revise.txt). Der Dry-Run-Pfad nutzt unveraendert
_dry_run_placeholder (kein LLM-Call, keine Revision)."""
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
    fallback_briefing,
    filter_news,
    llm_briefing,
    render_markdown,
    sc_bridge,
    send_telegram,
    snapshot,
    telegram_inbound,
    verify,
)

ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = ROOT / "logs"
LOG_FILE = LOG_DIR / "briefing.log"
VAULT_DIR = Path.home() / "docs" / "notizen" / "portfolio-briefings"

# Dry-Run-Platzhalter statt Fehlerstring: explizit als Dry-Run markiert,
# damit kein Fehlertext als Briefing durch die Pipeline laeuft. Sektionen
# entsprechen exakt dem 1-Call-Contract (verify.DRAFT_SECTIONS).
def _dry_run_placeholder(facts_package: dict | None) -> str:
    """Dry-Run-Platzhalter statt Fehlerstring: explizit als Dry-Run markiert,
    damit kein Fehlertext als Briefing durch die Pipeline laeuft. Sektionen
    entsprechen exakt dem 1-Call-Contract (verify.DRAFT_SECTIONS) inkl.
    Pflicht-Empfehlung (## Empfehlung, deterministisches Label aus dem
    Faktenpaket) und der Pflicht-Signalsektionen. Der Fundamentaldaten-
    Disclaimer ist hart verankert.
    """
    label = "WATCH"
    reason = "Dry-Run, keine deterministische Bewertung."
    if facts_package is not None:
        rec = facts_package.get("deterministic_summary", {}).get("recommendation", {})
        if isinstance(rec, dict) and rec.get("label") in ("BUY", "SELL", "WATCH"):
            label = rec["label"]
            reason = f"Dry-Run — deterministisches Label: {rec.get('reason', label)}."
    sell_section = "Keine Sell-/Reduce-Signale.\n\nFundamentaldaten (Umsatz, Gewinn, Cashflow, Verschuldung, Bewertung) nicht automatisch verfügbar"
    watch_section = "Keine Watchlist-Signale (NO SIGNAL für alle Positionen).\n\nFundamentaldaten (Umsatz, Gewinn, Cashflow, Verschuldung, Bewertung) nicht automatisch verfügbar"
    if (
        facts_package is not None
        and verify._naechster_schritt_handlungsbedarf(facts_package)
    ):
        next_section = "Handlungsbedarf: nächste Schritte gemäß Empfehlung umsetzen — Details siehe Empfehlung."
    else:
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
        "## Empfehlung\n"
        f"{label} — {reason}\n\n"
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


def _blocking_findings(verification: list[dict]) -> list[dict]:
    """Blockierende Verify-Findings (critical/major) — gleiche Regel wie final_gate.

    Nur diese Findings gehen strukturiert an die Revision
    (llm_briefing.revise_draft): info/minor blocken nie und werden nicht
    revidiert (1:1 zur final_gate-Semantik in verify.py).
    """
    return [f for f in verification if f.get("severity") in ("critical", "major")]


def _data_basis_valid(facts_package: dict) -> bool:
    """Valide Datenbasis fuer den Fallback (Phase D, Plan §6.2)?

    Der Fallback setzt eine valide Datenbasis voraus: Analyse und Strategie
    sind an dieser Stelle bereits erfolgreich gelaufen (sonst waere Stufe 1
    abgebrochen). Was hier noch entscheidet, ist die Datenqualitaet:
    ``implausible`` (negativer Wert, absurder Gesamtwert-Sprung) markiert die
    Datenbasis als kaputt -> kein Fallback, technischer Alert. Die Status
    ``stale``/``incomplete`` blocken den Fallback nicht (der Lauf war schon
    vorher nicht abgebrochen; die Fakten tragen den Status transparent).
    Ohne deterministisches Summary gibt es keine autoritativen Fakten ->
    ebenfalls kein Fallback.
    """
    if not isinstance(facts_package, dict):
        return False
    data_quality = facts_package.get("data_quality")
    if isinstance(data_quality, dict) and data_quality.get("status") == "implausible":
        return False
    summary = facts_package.get("deterministic_summary")
    if not isinstance(summary, dict):
        return False
    return True


def _build_fallback_draft(facts_package: dict, reason: str) -> str | None:
    """Deterministisches Faktenbriefing bauen (Phase D) — kein LLM-Call.

    Nur bei valider Datenbasis (``_data_basis_valid``). Das Rendering selbst
    ist deterministisch; der Aufrufer fuehrt das Ergebnis trotzdem durch
    verify_draft + final_gate (fail-closed). Liefert None, wenn kein Fallback
    moeglich ist — dann wurde bereits ein technischer Alert geloggt/versendet
    und der Lauf muss mit return 1 enden (kein Versand, keine Promotion).
    """
    if not _data_basis_valid(facts_package):
        _alert(
            "Briefing blockiert: Datenbasis nicht valide — kein Fallback, "
            "kein Versand (technischer Fehler)."
        )
        logging.error("fallback skipped: Datenbasis nicht valide (%s)", reason)
        return None
    try:
        draft = fallback_briefing.build_fallback_briefing(facts_package)
    except Exception as e:
        # Fallback-Rendering-Fehler ist ein technischer Fehler: kurzer Alert
        # ohne Secrets, kein Versand.
        _alert(f"Briefing blockiert: Fallback-Rendering fehlgeschlagen (kein Versand).")
        logging.error("fallback render failed (%s): %s", reason, e)
        return None
    logging.info("fallback briefing rendered (kein LLM-Call): %s", reason)
    return draft


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
            # P7: Offene Punkte aus dem Telegram-Rückkanal laden (untrusted
            # user input, status=open). Im Dry-Run wird der geladene Kontext
            # NICHT als resolved markiert (kein Persistenz-Nebenwirkung);
            # gepollt wird hier nie — das macht nur telegram_inbound.pull_and_ack
            # (P6/P8, Cron bleibt unangetastet).
            open_points = telegram_inbound.load_open_points()
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
                open_points=open_points,
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

        # Stufe 2-4 (Phase C2 + Phase D, Plan briefing-revision-loop §3.1/§6):
        # Draft -> verify -> final_gate mit begrenztem Revisions-Loop. Der
        # Initial-Draft kommt vom einzigen fachlichen LLM-Call
        # (llm_briefing.generate_draft, deepseek-v4-flash, NUR aus dem
        # Faktenpaket). Blockiert das Gate mit critical/major-Findings, wird
        # der Draft mit dem UNVERAENDERTEN Faktenpaket und den strukturierten
        # blockierenden Findings revidiert (llm_briefing.revise_draft, gleiche
        # Quelle, revise.txt) und erneut verifiziert. Maximal MAX_LLM_ATTEMPTS
        # LLM-Calls insgesamt (Initial + hoechstens MAX_LLM_ATTEMPTS-1
        # Revisionen) — keine Endlosschleife. Endet der Loop ohne validen
        # Draft (Versuchslimit erschoepft oder harter LLM-/Verify-Fehler),
        # dockt Phase D an: bei valider Datenbasis wird nach dem Loop der
        # deterministische Fallback gerendert (kein LLM-Call) und durch
        # verify_draft + final_gate gefuehrt — nur ein gueltiger Fallback
        # laeuft den Render-/Promote-/Versand-Pfad weiter. Dry-Run bleibt
        # netzwerkfrei: Platzhalter _dry_run_placeholder (kein LLM-Call,
        # keine Revision).
        verification: list[dict] = []
        draft: str | None = None
        attempts = 0
        # Dry-Run hat kein LLM-Budget: ein einziger Platzhalter-Versuch, nie
        # eine Revision (kein Netzwerk im Dry-Run; Platzhalter ist deterministisch).
        max_attempts = 1 if dry_run else llm_briefing.MAX_LLM_ATTEMPTS
        # Grund, warum der LLM-Pfad ohne validen Draft endete (None = PASS).
        # Nur bei gesetztem Grund wird nach dem Loop der Fallback versucht.
        fallback_reason: str | None = None
        while fallback_reason is None:
            # --- LLM-Call: Initial-Draft (Versuch 1) oder Revision ---
            try:
                if draft is None:
                    if dry_run:
                        draft = _dry_run_placeholder(facts_package)
                    else:
                        draft = llm_briefing.generate_draft(facts_package, mode=mode)
                        logging.info("briefing draft generated (LLM-Versuch 1)")
                else:
                    # Nur blockierende Findings (critical/major, gleiche Regel
                    # wie final_gate) strukturiert an die Revision uebergeben.
                    draft = llm_briefing.revise_draft(
                        facts_package, draft, _blocking_findings(verification), mode=mode
                    )
                    logging.info(f"briefing draft revised (LLM-Versuch {attempts + 1})")
                attempts += 1
            except llm_briefing.LLMError as e:
                # Harte LLM-/API-Fehler (Timeout, leere Antwort, fehlender
                # Key): fail-closed, kein weiterer LLM-Versuch. Der Lauf bricht
                # hier NICHT sofort ab — Phase D entscheidet nach dem Loop
                # ueber den deterministischen Fallback (nur bei valider
                # Datenbasis, sonst technischer Alert). Fehlertext nur loggen
                # (ohne Secrets), kein Alert in diesem Schritt.
                fallback_reason = (
                    f"draft failed (fail-closed): {e}"
                    if draft is None
                    else f"revision failed (fail-closed): {e}"
                )
                logging.error("%s", fallback_reason)
                break
            except Exception as e:
                fallback_reason = (
                    f"draft failed: {e}" if draft is None else f"revision failed: {e}"
                )
                logging.error("%s", fallback_reason)
                break

            # --- Deterministische Draft-Verifikation des aktuellen Versuchs
            # (1:1 gegen das Faktenpaket: Zahlen, Ticker/ISIN, News, Sektionen,
            # Empfehlungs-Label). ---
            try:
                verification = verify.verify_draft(facts_package, draft)
                if verification:
                    logging.warning(
                        f"verify_draft findings (Versuch {attempts}): "
                        f"{[f['issue'] for f in verification]}"
                    )
            except llm_briefing.LLMError as e:
                # Draft sieht nach LLM-Fehlertext aus -> kein weiterer Versuch
                # mit diesem Draft; Phase D entscheidet ueber den Fallback.
                fallback_reason = f"verify failed (fail-closed): {e}"
                logging.error("%s", fallback_reason)
                break
            except Exception as e:
                # Unerwarteter Verify-Fehler: fail-closed, kein Versand direkt
                # hier — Phase D entscheidet ueber den Fallback.
                fallback_reason = f"verify failed: {e}"
                logging.error("%s", fallback_reason)
                break

            # --- Versand-Gate (verification-only, fail-closed bei
            # critical/major-Findings; info/minor blocken nie). ---
            gate = verify.final_gate(verification)
            if gate.allow_send:
                logging.info(f"final_gate bestanden (Versuch {attempts}/{max_attempts})")
                break
            if attempts >= max_attempts:
                # Versuchslimit erschoepft: endgueltiger FAIL des LLM-Pfads.
                # Phase D entscheidet nach dem Loop ueber den Fallback (nur bei
                # valider Datenbasis, sonst technischer Alert). Kein Alert hier.
                fallback_reason = (
                    f"final_gate nach {max_attempts} LLM-Versuchen blockiert: {gate.reason}"
                )
                logging.warning("%s", fallback_reason)
                break
            logging.info(
                f"final_gate blockiert (Versuch {attempts}/{max_attempts}): {gate.reason}"
            )

        # --- Phase D (Plan briefing-revision-loop §6): endete der LLM-Loop
        # ohne validen Draft, wird bei valider Datenbasis das deterministische
        # Faktenbriefing gerendert (fallback_briefing.build_fallback_briefing —
        # kein LLM-Call, keine weitere Revision) und durch DIESELBEN
        # Sicherheitschecks gefuehrt wie ein LLM-Draft (verify_draft +
        # final_gate, fail-closed). Erst ein gueltiger Fallback laeuft den
        # bestehenden Render-/Promote-/Versand-Pfad weiter (status active,
        # Briefing-Modus mode — kein mode=alert); ein ungueltiger Fallback
        # oder eine kaputte Datenbasis bleiben beim technischen Alert
        # (kein Versand, keine Promotion).
        if fallback_reason is not None:
            draft = _build_fallback_draft(facts_package, fallback_reason)
            if draft is None:
                return 1
            try:
                verification = verify.verify_draft(facts_package, draft)
            except llm_briefing.LLMError as e:
                _alert(f"Briefing blockiert: Fallback-Verify fehlgeschlagen (fail-closed): {e}")
                return 1
            except Exception as e:
                _alert(f"Briefing blockiert: Fallback-Verify fehlgeschlagen: {e}")
                return 1
            gate = verify.final_gate(verification)
            if not gate.allow_send:
                _alert(
                    "Briefing blockiert: Fallback-Briefing ungueltig "
                    f"(final_gate: {gate.reason})"
                )
                return 1
            logging.info("fallback briefing used and verified (%s)", fallback_reason)

        # Defensive: ab hier existiert immer ein Draft — entweder final_gate-
        # PASS des LLM-Pfads oder ein verifizierter Fallback (beide Pfade haben
        # bei Fehlern vorher mit return 1 abgebrochen).
        assert draft is not None, "Draft fehlt vor Render (kein PASS, kein Fallback)"

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

        # P7-Lebenszyklus: erst NACH final_gate + Render + erfolgreichem
        # Versand werden die eingespeisten offenen Punkte resolved. Jeder
        # Fehlerpfad davor (facts/LLM/verify/gate/render/send) kehrt frueher
        # zurueck — die Punkte bleiben open und fliessen ins naechste
        # Briefing. Fehler beim Markieren duerfen den bereits erfolgreichen
        # Versand nicht blockieren, werden aber sauber geloggt.
        if not dry_run and open_points:
            for point in open_points:
                message_id = point.get("message_id")
                if not isinstance(message_id, int):
                    continue
                try:
                    telegram_inbound.mark_resolved(message_id)
                    logging.info("open point resolved (message_id=%s)", message_id)
                except Exception:
                    # Nie den Versand rueckwirkend blockieren — nur loggen.
                    logging.exception(
                        "open point mark_resolved failed (message_id=%s) — Punkt bleibt offen",
                        message_id,
                    )

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
