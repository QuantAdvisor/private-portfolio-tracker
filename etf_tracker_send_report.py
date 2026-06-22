"""
ETF Look-Through Tracker – täglicher Report per E-Mail (Server-Cronjob).

Ablauf:
  1. Report generieren (generate_report.generate_html)
  2. HTML inline als E-Mail versenden
  3. Fehler-Mail bei Exception

.env auf dem Server (zusätzlich zu den DB-Credentials):
    SMTP_HOST=smtp.gmail.com
    SMTP_PORT=587
    SMTP_USER=absender@gmail.com
    SMTP_PASSWORD=<Google-App-Passwort>
    MAIL_FROM=absender@gmail.com
    MAIL_TO=Christian_Schott@quant-advisor.de

Crontab (täglich Mo–Fr, 09:10 – nach Kurs-Update):
    10 9 * * 1-5 cd /pfad/zum/projekt && /pfad/zum/venv/bin/python etf_tracker_send_report.py
"""

import logging
import os
import smtplib
import sys
import traceback
from datetime import date, datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.FileHandler(SCRIPT_DIR / "etf_tracker_report.log"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)

from dotenv import load_dotenv
load_dotenv(SCRIPT_DIR / ".env")

import generate_report
import db_utils

SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASS = os.environ.get("SMTP_PASSWORD", "")
MAIL_FROM = os.environ.get("MAIL_FROM", SMTP_USER)
MAIL_TO   = os.environ.get("MAIL_TO", "")


def _send_mail(subject: str, html_body: str) -> None:
    if not SMTP_USER or not MAIL_TO:
        log.warning("SMTP_USER / MAIL_TO nicht konfiguriert – kein E-Mail-Versand.")
        return

    recipients = [r.strip() for r in MAIL_TO.split(",") if r.strip()]
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = MAIL_FROM
    msg["To"]      = ", ".join(recipients)
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    if SMTP_PORT == 465:
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT) as smtp:
            smtp.login(SMTP_USER, SMTP_PASS)
            smtp.sendmail(MAIL_FROM, recipients, msg.as_string())
    else:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as smtp:
            smtp.ehlo()
            smtp.starttls()
            smtp.login(SMTP_USER, SMTP_PASS)
            smtp.sendmail(MAIL_FROM, recipients, msg.as_string())

    log.info("E-Mail versandt an: %s", ", ".join(recipients))


def _get_summary_kpis(ref_date: date) -> dict:
    """Gesamtwert und Tagesveränderung für die Betreff-Zeile."""
    try:
        df = db_utils.query_df("""
            WITH snap AS (
                SELECT DISTINCT ON (account_id, isin) isin, quantity
                FROM portfolio.position_snapshot
                WHERE as_of_date <= :ref
                ORDER BY account_id, isin, as_of_date DESC
            ),
            today AS (
                SELECT DISTINCT ON (p.isin) p.isin,
                    p.close::float * COALESCE(f.eur_per_unit::float, 1.0) AS close_eur
                FROM portfolio.price p
                LEFT JOIN (
                    SELECT DISTINCT ON (currency) currency, eur_per_unit
                    FROM portfolio.fx_rate WHERE rate_date <= :ref
                    ORDER BY currency, rate_date DESC
                ) f ON f.currency = p.currency
                WHERE p.price_date <= :ref
                ORDER BY p.isin, p.price_date DESC
            ),
            yesterday AS (
                SELECT DISTINCT ON (p.isin) p.isin,
                    p.close::float * COALESCE(f.eur_per_unit::float, 1.0) AS close_eur
                FROM portfolio.price p
                LEFT JOIN (
                    SELECT DISTINCT ON (currency) currency, eur_per_unit
                    FROM portfolio.fx_rate WHERE rate_date <= :ref
                    ORDER BY currency, rate_date DESC
                ) f ON f.currency = p.currency
                WHERE p.price_date < :ref
                ORDER BY p.isin, p.price_date DESC
            )
            SELECT
                SUM(s.quantity * t.close_eur) AS wert_heute,
                SUM(s.quantity * y.close_eur) AS wert_gestern
            FROM snap s
            LEFT JOIN today     t ON t.isin = s.isin
            LEFT JOIN yesterday y ON y.isin = s.isin
        """, params={"ref": ref_date})
        row = df.iloc[0]
        wert    = float(row["wert_heute"]  or 0)
        gestern = float(row["wert_gestern"] or 0)
        delta   = wert - gestern
        delta_pct = delta / gestern * 100 if gestern else 0
        return {"wert": wert, "delta": delta, "delta_pct": delta_pct}
    except Exception as exc:
        log.warning("KPI-Abfrage fehlgeschlagen: %s", exc)
        return {"wert": 0.0, "delta": 0.0, "delta_pct": 0.0}


def main() -> None:
    run_time = datetime.now()
    ref_date = date.today()
    log.info("=== ETF Tracker Report %s ===", ref_date)

    try:
        log.info("Generiere HTML-Report …")
        html = generate_report.generate_html(ref_date)

        kpis = _get_summary_kpis(ref_date)
        wert      = kpis["wert"]
        delta_pct = kpis["delta_pct"]
        sign = "+" if delta_pct >= 0 else ""

        subject = (
            f"ETF Portfolio-Report {run_time.strftime('%d.%m.%Y')} "
            f"– {wert:,.0f} € ({sign}{delta_pct:.2f} % heute)"
        ).replace(",", ".")

    except Exception:
        err = traceback.format_exc()
        log.error("Fehler bei Report-Generierung:\n%s", err)
        _send_mail(
            subject=f"[FEHLER] ETF Portfolio-Report {run_time.strftime('%d.%m.%Y')}",
            html_body=f"<pre style='color:red;font-size:12px'>{err}</pre>",
        )
        sys.exit(1)

    _send_mail(subject, html)
    log.info("=== Fertig ===")


if __name__ == "__main__":
    main()
