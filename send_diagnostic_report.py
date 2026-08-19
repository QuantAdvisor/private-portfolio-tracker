"""Diagnose-Report per E-Mail (Server-Cronjob) - DB-Gesundheitscheck.

Laeuft NACH dem vollen Batch-Stack (siehe run_pipeline.py), VOR dem
eigentlichen Portfolio-Report (etf_tracker_send_report.py). Eigene, erste
Mail - Nutzerwunsch 2026-08-17: "der diagnose report soll als erste mail
raus dann der rest getrennt".

Prueft genau die Fehlerklassen, die in der PoC-Session 2026-08-16/17
tatsaechlich reale Bugs waren (siehe portfolio_intelligence.design_log):
  1. Pipeline-Frische je Tabelle - faengt "Phase X wurde nicht neu
     gerechnet"-Bugs (genau das ist am 2026-08-17 zweimal passiert:
     Preis-Fix ohne Downstream-Rerun, phase10 vor statt nach phase12/13).
  2. Neue Kurs-Anomalien (v_dashboard_price_anomaly) - faengt Bad Ticks wie
     den 2025-10-24-Fall.
  3. NormRt-Konsistenz (Budget = Real * Auslastung) - Formel-Regression.
  4. MCTR-Euler-Konsistenz (Summe CTR = TE_Portfolio) - Formel-Regression.
  5. Offene design_log-Eintraege (status='open') - Erinnerung an bekannte
     Luecken.

Kein Ersatz fuer den Portfolio-Performance-Report (TWR/Fachkonzept-Kennzahlen
- separates Projekt des Nutzers in generate_report.py). Rein technischer
Zustand der Datenbank - gedacht als taegliches Scan-Dokument, nicht zum
Studieren: Ampel-Zusammenfassung oben, Details darunter, unauffaellige
Karten wenn ein Check gruen ist.

HTML-Design bewusst e-mail-sicher: nur Inline-Styles + Tabellen-Layout
(kein <style>-Block, den z.B. Outlook Desktop haeufig entfernt), System-
Schriftstapel (kein @font-face - E-Mail-Clients laden keine eingebetteten
Fonts zuverlaessig), keine CSS-Variablen (fehlende Unterstuetzung in
aelteren Clients).
"""

from __future__ import annotations

import logging
import sys
import traceback
from datetime import date, datetime, timedelta
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

# Absoluter Freshness-Check gegen CURRENT_DATE: 0 Toleranz-Tage ueber dem
# letzten erwarteten Handelstag (Mo-Fr vor heute) gilt als OK, 1 Tag als WARN
# (deckt einen einzelnen Boersenfeiertag ab, ohne stumm zu bleiben), mehr als
# 1 Tag als ERROR. Bewusst eng - eine lockerere Toleranz (z.B. 3 wie beim
# Quellen-untereinander-Vergleich oben) haette genau den 2026-08-19-Fall
# (Stack haengt 1 Handelstag hinter Yahoo's fehlender Close-Notierung fuer
# europaeische Boersen) wieder unter den Tisch fallen lassen.


def _last_expected_business_day(today: date) -> date:
    """Letzter Mo-Fr vor heute (keine Feiertagsliste - dafuer ist die
    ABSOLUTE_FRESHNESS_TOLERANCE_DAYS-Toleranz oben gedacht)."""
    prior = today - timedelta(days=1)
    while prior.weekday() >= 5:  # Sa=5, So=6
        prior -= timedelta(days=1)
    return prior

# ============================================================
# Design tokens (E-Mail-sicher: Inline-Styles, keine CSS-Variablen)
# ============================================================
INK = "#1c2430"
MUTED = "#66707e"
FAINT = "#9aa3b2"
BORDER = "#dde1e8"
CARD_BG = "#ffffff"
PAGE_BG = "#eef1f5"
ACCENT = "#1f3a5f"
ACCENT_SOFT = "#e8edf4"

STATUS = {
    "OK":    {"fg": "#0f7a4d", "bg": "#e5f5ee", "label": "OK"},
    "WARN":  {"fg": "#b6650a", "bg": "#fdf1e0", "label": "WARNUNG"},
    "ERROR": {"fg": "#c0342c", "bg": "#fceaea", "label": "FEHLER"},
    "INFO":  {"fg": "#51607a", "bg": "#eef0f5", "label": "INFO"},
}
SEVERITY_ORDER = {"ERROR": 0, "WARN": 1, "INFO": 2, "OK": 3}

FONT_UI = "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"
FONT_MONO = "'SFMono-Regular',Consolas,'Liberation Mono',Menlo,monospace"


def _pill(status: str) -> str:
    s = STATUS[status]
    return (
        f"<span style=\"display:inline-block;font-family:{FONT_UI};font-size:11px;"
        f"font-weight:600;letter-spacing:.04em;text-transform:uppercase;"
        f"color:{s['fg']};background:{s['bg']};border-radius:4px;"
        f"padding:3px 8px;line-height:1.4\">{s['label']}</span>"
    )


# ============================================================
# Checks
# ============================================================

def _check_pipeline_freshness() -> tuple[str, list[dict]]:
    """MAX(Datum) je Kernquelle - alles muss nah beieinander liegen UND nah an
    heute. Fing am 2026-08-17 genau den Bug, dass account_twr/total_twr nach
    phase12/13 auf altem Datum haengen blieben, waehrend mandate/account/
    total weiterliefen.

    Der reine Quellen-untereinander-Vergleich (lag_days) reicht nicht: friert
    der GESAMTE Stack gemeinsam ein (z.B. weil load_prices.py wegen einer
    fehlenden Yahoo-Close-Notierung nie ueber ein Datum hinauskommt, siehe
    design_log 2026-08-19), bleibt der relative Abstand zwischen den Quellen
    bei 0 - die Ampel waere gruen, obwohl alles tagealt ist. Deshalb
    zusaetzlich max_overall gegen CURRENT_DATE pruefen (today_lag_days)."""
    df = db_utils.query_df(
        """
        SELECT 'Kurse/Renditen (asset_daily_return)' AS source, MAX(return_date) AS max_date
            FROM portfolio_intelligence.asset_daily_return
        UNION ALL
        SELECT 'Mandats-/Depot-Bewertung (entity_daily_valuation)', MAX(valuation_date)
            FROM portfolio_intelligence.entity_daily_valuation
        UNION ALL
        SELECT 'Gesamtdepot-TWR (twr_daily_total)', MAX(valuation_date)
            FROM portfolio_intelligence.twr_daily_total
        UNION ALL
        SELECT 'Tracking Error (Mandat/Depot/Gesamt)', MAX(calc_date)
            FROM portfolio_intelligence.tracking_error_rolling WHERE entity_type IN ('mandate','account','total')
        UNION ALL
        SELECT 'Tracking Error (TWR-basiert)', MAX(calc_date)
            FROM portfolio_intelligence.tracking_error_rolling WHERE entity_type IN ('account_twr','total_twr')
        UNION ALL
        SELECT 'MCTR-Snapshot', MAX(calc_date) FROM portfolio_intelligence.mctr_snapshot
        """
    )
    max_overall = df["max_date"].max()
    today_lag_days = (date.today() - max_overall).days if max_overall is not None else None
    expected = _last_expected_business_day(date.today())
    business_lag_days = (expected - max_overall).days if max_overall is not None else None
    df["lag_days"] = df["max_date"].apply(lambda d: (max_overall - d).days if d is not None else None)
    relative_bad = df["lag_days"].apply(lambda x: x is None or x > FRESHNESS_LAG_TOLERANCE_DAYS).any()
    if relative_bad or business_lag_days is None or business_lag_days > 1:
        status = "ERROR"
    elif business_lag_days == 1:
        status = "WARN"
    else:
        status = "OK"
    rows = [
        {"Quelle": r["source"], "Letzter Stand": r["max_date"], "Rueckstand": f"{int(r['lag_days'])} Tage" if r["lag_days"] else "aktuell"}
        for r in df.to_dict("records")
    ]
    rows.append({
        "Quelle": "Gesamter Stack vs. heute",
        "Letzter Stand": max_overall,
        "Rueckstand": f"{today_lag_days} Tage" if today_lag_days else "aktuell",
    })
    return status, rows


def is_pipeline_current() -> bool:
    """True nur wenn der Stack absolut aktuell ist (0 Handelstage Rueckstand,
    siehe _check_pipeline_freshness). Von run_pipeline.py genutzt (Import aus
    private-portfolio-tracker, Nutzerwunsch 2026-08-19), um den Portfolio-
    Performance-Report (etf_tracker_send_report.py) nur bei aktuellen Daten
    zu verschicken - bei mehreren Laeufen pro Tag (06/12/16 Uhr) soll ein
    eingefrorener Stack (z.B. der Yahoo-NaN-Close-Fall vom 2026-08-19) nicht
    denselben veralteten Report mehrfach rausschicken."""
    status, _ = _check_pipeline_freshness()
    return status == "OK"


def _check_new_price_anomalies() -> tuple[str, list[dict]]:
    """v_dashboard_price_anomaly-Treffer der letzten 7 Tage - alte, bereits
    bekannte Ausreisser (z.B. COVID-Crash) sind normal und werden nicht
    jedes Mal neu gemeldet, nur frische."""
    df = db_utils.query_df(
        """
        SELECT legacy_isin, name, return_date, ROUND(daily_return_pct::numeric, 2) AS daily_return_pct,
               ROUND(z_score::numeric, 1) AS z_score
        FROM portfolio_intelligence.v_dashboard_price_anomaly
        WHERE return_date >= CURRENT_DATE - INTERVAL '7 days'
        ORDER BY return_date DESC
        """
    )
    status = "WARN" if not df.empty else "OK"
    rows = [
        {"ISIN": r["legacy_isin"], "Name": r["name"], "Datum": r["return_date"],
         "Tagesrendite": f"{r['daily_return_pct']:+.2f} %", "Z-Score": r["z_score"]}
        for r in df.to_dict("records")
    ]
    return status, rows


def _resolve_entity_name_sql(alias: str) -> str:
    """SQL-Ausdruck, der entity_type/entity_id auf einen lesbaren Namen
    aufloest (Mandat=ETF-Name, Depot=Kontoname, Gesamt=fester Text)."""
    return f"""
        CASE
            WHEN {alias}.entity_type = 'mandate' THEN a.name
            WHEN {alias}.entity_type IN ('account','account_twr') THEN av.account_name
            ELSE 'Gesamtportfolio'
        END
    """


def _check_normrt_consistency() -> tuple[str, list[dict]]:
    name_expr = _resolve_entity_name_sql("ter")
    df = db_utils.query_df(
        f"""
        SELECT {name_expr} AS name, bp.code AS benchmark, ter.window_days,
               ROUND(ter.norm_return_budget::numeric, 3) AS norm_return_budget,
               ROUND(ter.norm_return_real::numeric, 3) AS norm_return_real,
               ROUND(ter.utilization_pct::numeric, 0) AS utilization_pct,
               ABS(ter.norm_return_budget - ter.norm_return_real * ter.utilization_pct / 100.0) AS diff
        FROM portfolio_intelligence.tracking_error_rolling ter
        LEFT JOIN portfolio_intelligence.etf_mandate em ON ter.entity_type = 'mandate' AND em.mandate_id = ter.entity_id
        LEFT JOIN portfolio_intelligence.asset a ON a.asset_id = em.asset_id
        LEFT JOIN portfolio_intelligence.account_view av ON ter.entity_type IN ('account','account_twr') AND av.account_view_id = ter.entity_id
        JOIN portfolio_intelligence.benchmark_profile bp ON bp.benchmark_id = ter.benchmark_id
        WHERE ter.norm_return_budget IS NOT NULL AND ter.norm_return_real IS NOT NULL
          AND ter.utilization_pct IS NOT NULL
          AND ter.calc_date = (SELECT MAX(calc_date) FROM portfolio_intelligence.tracking_error_rolling)
        ORDER BY diff DESC
        LIMIT 5
        """
    )
    if df.empty:
        return "OK", []
    status = "ERROR" if float(df["diff"].max()) > 0.05 else "OK"
    rows = [
        {"Position": r["name"], "Benchmark": r["benchmark"], "Fenster": f"{r['window_days']}T",
         "NormRt Budget": r["norm_return_budget"], "NormRt Real": r["norm_return_real"],
         "Auslastung": f"{r['utilization_pct']:.0f} %", "Abweichung": f"{r['diff']:.5f}"}
        for r in df.to_dict("records")
    ]
    return status, rows


def _check_mctr_euler_consistency() -> tuple[str, list[dict]]:
    df = db_utils.query_df(
        """
        SELECT m.calc_date, bp.code AS benchmark, m.window_days,
               ROUND(SUM(m.ctr_pct_pa)::numeric, 4) AS sum_ctr,
               ROUND(MAX(m.portfolio_te_pct_pa)::numeric, 4) AS te_p,
               ABS(SUM(m.ctr_pct_pa) - MAX(m.portfolio_te_pct_pa)) AS diff
        FROM portfolio_intelligence.mctr_snapshot m
        JOIN portfolio_intelligence.benchmark_profile bp ON bp.benchmark_id = m.benchmark_id
        GROUP BY m.calc_date, bp.code, m.window_days
        ORDER BY diff DESC
        """
    )
    if df.empty:
        return "WARN", []
    status = "ERROR" if float(df["diff"].max()) > 0.05 else "OK"
    rows = [
        {"Datum": r["calc_date"], "Benchmark": r["benchmark"], "Fenster": f"{r['window_days']}T",
         "Sum(CTR)": r["sum_ctr"], "TE Portfolio": r["te_p"], "Abweichung": f"{r['diff']:.5f}"}
        for r in df.to_dict("records")
    ]
    return status, rows


def _check_open_design_log() -> tuple[str, list[dict]]:
    df = db_utils.query_df(
        """
        SELECT logged_date, area, title
        FROM portfolio_intelligence.design_log
        WHERE status = 'open'
        ORDER BY logged_date DESC
        """
    )
    rows = [{"Datum": r["logged_date"], "Bereich": r["area"], "Punkt": r["title"]} for r in df.to_dict("records")]
    return "INFO", rows


# ============================================================
# HTML-Rendering (Tabellen-Layout, Inline-Styles - e-mail-sicher)
# ============================================================

_NUMERIC_HINT = ("Fenster", "Budget", "Real", "Auslastung", "Abweichung", "CTR", "TE ", "Z-Score", "Tagesrendite", "Rueckstand")


def _is_numeric_col(col: str) -> bool:
    return any(h in col for h in _NUMERIC_HINT)


def _table_html(rows: list[dict]) -> str:
    if not rows:
        return (
            f"<p style=\"margin:0;font-family:{FONT_UI};font-size:13px;color:{MUTED}\">"
            f"Keine Auffaelligkeiten.</p>"
        )
    cols = list(rows[0].keys())
    head_cells = "".join(
        f"<th style=\"text-align:{'right' if _is_numeric_col(c) else 'left'};padding:6px 10px;"
        f"font-family:{FONT_UI};font-size:11px;font-weight:600;letter-spacing:.03em;"
        f"text-transform:uppercase;color:{FAINT};border-bottom:1px solid {BORDER}\">{c}</th>"
        for c in cols
    )
    body_rows = []
    for r in rows[:20]:
        cells = "".join(
            f"<td style=\"text-align:{'right' if _is_numeric_col(c) else 'left'};padding:6px 10px;"
            f"font-family:{FONT_MONO if _is_numeric_col(c) else FONT_UI};font-size:12.5px;"
            f"color:{INK};border-bottom:1px solid {BORDER}\">{r[c]}</td>"
            for c in cols
        )
        body_rows.append(f"<tr>{cells}</tr>")
    more = (
        f"<p style=\"margin:8px 0 0;font-family:{FONT_UI};font-size:12px;color:{FAINT}\">"
        f"... und {len(rows) - 20} weitere Zeilen.</p>" if len(rows) > 20 else ""
    )
    return (
        f"<table role=\"presentation\" width=\"100%\" cellpadding=\"0\" cellspacing=\"0\" "
        f"style=\"border-collapse:collapse;width:100%\"><tr>{head_cells}</tr>{''.join(body_rows)}</table>{more}"
    )


def _card(title: str, status: str, rows: list[dict]) -> str:
    s = STATUS[status]
    return f"""
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;margin:0 0 16px">
      <tr>
        <td style="background:{CARD_BG};border:1px solid {BORDER};border-left:3px solid {s['fg']};
                   border-radius:6px;padding:16px 18px">
          <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;margin-bottom:10px">
            <tr>
              <td style="font-family:{FONT_UI};font-size:14px;font-weight:600;color:{INK}">{title}</td>
              <td align="right">{_pill(status)}</td>
            </tr>
          </table>
          {_table_html(rows)}
        </td>
      </tr>
    </table>
    """


def build_report_html() -> tuple[str, str]:
    """Returns (html, overall_status)."""
    checks = [
        ("Pipeline-Frische", _check_pipeline_freshness()),
        ("Neue Kurs-Anomalien (letzte 7 Tage)", _check_new_price_anomalies()),
        ("NormRt-Konsistenz (Budget = Real x Auslastung)", _check_normrt_consistency()),
        ("MCTR-Konsistenz (Summe Risikobeitraege = Portfolio-TE)", _check_mctr_euler_consistency()),
        ("Offene Punkte (design_log)", _check_open_design_log()),
    ]

    overall = min((s for _, (s, _) in checks), key=lambda s: SEVERITY_ORDER[s])
    counts: dict[str, int] = {}
    for _, (s, _) in checks:
        counts[s] = counts.get(s, 0) + 1
    summary_bits = " · ".join(f"{n} {STATUS[s]['label'].title()}" for s, n in sorted(counts.items(), key=lambda kv: SEVERITY_ORDER[kv[0]]))

    cards = "".join(_card(title, status, rows) for title, (status, rows) in checks)
    weekday_de = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"][date.today().weekday()]
    today_label = f"{weekday_de}, {date.today().strftime('%d.%m.%Y')}"

    html = f"""
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;background:{PAGE_BG};padding:24px 0">
      <tr>
        <td align="center">
          <table role="presentation" width="600" cellpadding="0" cellspacing="0" style="border-collapse:collapse;width:600px;max-width:100%">
            <tr>
              <td style="background:{ACCENT};border-radius:8px 8px 0 0;padding:22px 24px">
                <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse">
                  <tr>
                    <td style="font-family:{FONT_UI};color:#ffffff">
                      <div style="font-size:11px;letter-spacing:.08em;text-transform:uppercase;color:{ACCENT_SOFT};opacity:.85">Diagnose-Report</div>
                      <div style="font-size:20px;font-weight:600;margin-top:2px">{today_label}</div>
                    </td>
                    <td align="right">{_pill(overall)}</td>
                  </tr>
                </table>
              </td>
            </tr>
            <tr>
              <td style="background:{CARD_BG};padding:12px 24px;border-left:1px solid {BORDER};border-right:1px solid {BORDER}">
                <p style="margin:0;font-family:{FONT_UI};font-size:13px;color:{MUTED}">
                  {summary_bits} &middot; automatischer DB-Gesundheitscheck des portfolio_intelligence-Stacks,
                  kein Performance-Report (siehe separate Mail dafuer).
                </p>
              </td>
            </tr>
            <tr>
              <td style="padding:20px 24px 4px;border-left:1px solid {BORDER};border-right:1px solid {BORDER};border-bottom:1px solid {BORDER};border-radius:0 0 8px 8px">
                {cards}
              </td>
            </tr>
          </table>
        </td>
      </tr>
    </table>
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
