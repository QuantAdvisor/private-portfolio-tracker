"""Diagnose-Report per E-Mail (Server-Cronjob) - DB-Gesundheitscheck.

Laeuft NACH dem vollen Batch-Stack (siehe run_pipeline.py im Root-Ordner),
VOR dem eigentlichen Portfolio-Report (etf_tracker_send_report.py). Eigene,
erste Mail - Nutzerwunsch 2026-08-17: "der diagnose report soll als erste
mail raus dann der rest getrennt".

Prueft genau die Fehlerklassen, die in der PoC-Session 2026-08-16/17
tatsaechlich reale Bugs waren (siehe portfolio_intelligence.design_log):
  1. Pipeline-Frische je Tabelle - faengt "Phase X wurde nicht neu
     gerechnet"-Bugs (genau das ist heute zweimal passiert: Preis-Fix ohne
     Downstream-Rerun, phase10 vor statt nach phase12/13).
  2. Neue Kurs-Anomalien (v_dashboard_price_anomaly) - faengt Bad Ticks wie
     den 2025-10-24-Fall.
  3. NormRt-Konsistenz (Budget = Real * Auslastung) - Formel-Regression.
  4. MCTR-Euler-Konsistenz (Summe CTR = TE_Portfolio) - Formel-Regression.
  5. Offene design_log-Eintraege (status='open') - Erinnerung an bekannte
     Luecken (aktuell: 41-Tage-Datenlimit auf Mandats-Ebene).

Kein Ersatz fuer den Portfolio-Performance-Report (TWR/Fachkonzept-Kennzahlen
- separates Projekt des Nutzers in generate_report.py). Rein technischer
Zustand der Datenbank.
"""

from __future__ import annotations

import logging
import sys
import traceback
from datetime import date, datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PLATFORM_DIR = SCRIPT_DIR.parent / "portfolio-intelligence-platform"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.FileHandler(SCRIPT_DIR / "diagnostic_report.log"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)

from dotenv import load_dotenv
load_dotenv(SCRIPT_DIR / ".env")

import db_utils
from mail_utils import send_mail

# Ab dieser Abweichung (Tage) gilt eine Tabelle als "hinterher" - toleriert
# normale Wochenend-/Feiertagsluecken, faengt aber einen ausgebliebenen
# Pipeline-Schritt zuverlaessig ab.
FRESHNESS_LAG_TOLERANCE_DAYS = 3


def _check_pipeline_freshness() -> tuple[str, list[dict]]:
    """MAX(Datum) je Kernquelle - alles muss nah beieinander liegen. Fing
    heute genau den Bug, dass account_twr/total_twr nach phase12/13 auf
    altem Datum haengen blieben, waehrend mandate/account/total weiterliefen."""
    df = db_utils.query_df(
        """
        SELECT 'asset_daily_return' AS source, MAX(return_date) AS max_date
            FROM portfolio_intelligence.asset_daily_return
        UNION ALL
        SELECT 'entity_daily_valuation (mandate/account/total)', MAX(valuation_date)
            FROM portfolio_intelligence.entity_daily_valuation
        UNION ALL
        SELECT 'twr_daily_total (Gesamtportfolio, transaktionsbasiert)', MAX(valuation_date)
            FROM portfolio_intelligence.twr_daily_total
        UNION ALL
        SELECT 'tracking_error_rolling (mandate/account/total)', MAX(calc_date)
            FROM portfolio_intelligence.tracking_error_rolling WHERE entity_type IN ('mandate','account','total')
        UNION ALL
        SELECT 'tracking_error_rolling (account_twr/total_twr)', MAX(calc_date)
            FROM portfolio_intelligence.tracking_error_rolling WHERE entity_type IN ('account_twr','total_twr')
        UNION ALL
        SELECT 'mctr_snapshot', MAX(calc_date) FROM portfolio_intelligence.mctr_snapshot
        """
    )
    max_overall = df["max_date"].max()
    df["lag_days"] = df["max_date"].apply(lambda d: (max_overall - d).days if d is not None else None)
    status = "ERROR" if df["lag_days"].apply(lambda x: x is None or x > FRESHNESS_LAG_TOLERANCE_DAYS).any() else "OK"
    return status, df.to_dict("records")


def _check_new_price_anomalies() -> tuple[str, list[dict]]:
    """v_dashboard_price_anomaly-Treffer der letzten 7 Tage - alte, bereits
    bekannte Ausreisser (z.B. COVID-Crash) sind normal und werden nicht
    jedes Mal neu gemeldet, nur frische."""
    df = db_utils.query_df(
        """
        SELECT legacy_isin, name, return_date, daily_return_pct, z_score
        FROM portfolio_intelligence.v_dashboard_price_anomaly
        WHERE return_date >= CURRENT_DATE - INTERVAL '7 days'
        ORDER BY return_date DESC
        """
    )
    status = "WARN" if not df.empty else "OK"
    return status, df.to_dict("records")


def _check_normrt_consistency() -> tuple[str, list[dict]]:
    df = db_utils.query_df(
        """
        SELECT entity_type, entity_id, benchmark_id, window_days,
               norm_return_budget, norm_return_real, utilization_pct,
               ABS(norm_return_budget - norm_return_real * utilization_pct / 100.0) AS diff
        FROM portfolio_intelligence.tracking_error_rolling
        WHERE norm_return_budget IS NOT NULL AND norm_return_real IS NOT NULL
          AND utilization_pct IS NOT NULL
          AND calc_date = (SELECT MAX(calc_date) FROM portfolio_intelligence.tracking_error_rolling)
        ORDER BY diff DESC
        LIMIT 5
        """
    )
    if df.empty:
        return "OK", []
    status = "ERROR" if float(df["diff"].max()) > 0.05 else "OK"
    return status, df.to_dict("records")


def _check_mctr_euler_consistency() -> tuple[str, list[dict]]:
    df = db_utils.query_df(
        """
        SELECT calc_date, benchmark_id, window_days,
               SUM(ctr_pct_pa) AS sum_ctr, MAX(portfolio_te_pct_pa) AS te_p,
               ABS(SUM(ctr_pct_pa) - MAX(portfolio_te_pct_pa)) AS diff
        FROM portfolio_intelligence.mctr_snapshot
        GROUP BY calc_date, benchmark_id, window_days
        ORDER BY diff DESC
        """
    )
    if df.empty:
        return "WARN", []
    status = "ERROR" if float(df["diff"].max()) > 0.05 else "OK"
    return status, df.to_dict("records")


def _check_open_design_log() -> tuple[str, list[dict]]:
    df = db_utils.query_df(
        """
        SELECT logged_date, area, title, decision
        FROM portfolio_intelligence.design_log
        WHERE status = 'open'
        ORDER BY logged_date DESC
        """
    )
    return "INFO", df.to_dict("records")


def _fmt_table(rows: list[dict]) -> str:
    if not rows:
        return "<p style='color:#666'>Keine Zeilen.</p>"
    cols = list(rows[0].keys())
    head = "".join(f"<th style='text-align:left;padding:4px 8px;border-bottom:1px solid #ccc'>{c}</th>" for c in cols)
    body_rows = []
    for r in rows[:20]:
        cells = "".join(f"<td style='padding:4px 8px;border-bottom:1px solid #eee'>{r[c]}</td>" for c in cols)
        body_rows.append(f"<tr>{cells}</tr>")
    more = f"<p style='color:#666'>... und {len(rows) - 20} weitere Zeilen.</p>" if len(rows) > 20 else ""
    return f"<table style='border-collapse:collapse;font-size:13px'><tr>{head}</tr>{''.join(body_rows)}</table>{more}"


_STATUS_COLOR = {"OK": "#2e7d32", "WARN": "#e65100", "ERROR": "#c62828", "INFO": "#555"}


def build_report_html() -> tuple[str, str]:
    """Returns (html, overall_status)."""
    checks = [
        ("Pipeline-Frische", _check_pipeline_freshness()),
        ("Neue Kurs-Anomalien (letzte 7 Tage)", _check_new_price_anomalies()),
        ("NormRt-Konsistenz (Budget = Real x Auslastung)", _check_normrt_consistency()),
        ("MCTR-Euler-Konsistenz (Summe CTR = TE_Portfolio)", _check_mctr_euler_consistency()),
        ("Offene Design-Log-Punkte", _check_open_design_log()),
    ]

    severity_order = {"ERROR": 0, "WARN": 1, "INFO": 2, "OK": 3}
    overall = min((s for _, (s, _) in checks), key=lambda s: severity_order[s])

    sections = []
    for title, (status, rows) in checks:
        color = _STATUS_COLOR[status]
        sections.append(
            f"<h3 style='margin-bottom:4px'>{title} "
            f"<span style='color:{color};font-size:13px'>[{status}]</span></h3>"
            f"{_fmt_table(rows)}"
        )

    html = f"""
    <html><body style="font-family:Arial,sans-serif;color:#222">
    <h2>Diagnose-Report {date.today().isoformat()}</h2>
    <p>Automatischer DB-Gesundheitscheck nach dem Batch-Lauf (portfolio_intelligence-Stack).
    Kein Performance-Report - siehe separate Mail dafuer.</p>
    {''.join(sections)}
    </body></html>
    """
    return html, overall


def main() -> None:
    run_time = datetime.now()
    log.info("=== Diagnose-Report %s ===", date.today())

    try:
        html, overall = build_report_html()
        subject = f"[{overall}] Diagnose-Report {run_time.strftime('%d.%m.%Y')}"
    except Exception:
        err = traceback.format_exc()
        log.error("Fehler bei Diagnose-Report-Generierung:\n%s", err)
        send_mail(
            subject=f"[FEHLER] Diagnose-Report {run_time.strftime('%d.%m.%Y')}",
            html_body=f"<pre style='color:red;font-size:12px'>{err}</pre>",
        )
        sys.exit(1)

    send_mail(subject, html)
    log.info("=== Fertig: %s ===", overall)


if __name__ == "__main__":
    main()
