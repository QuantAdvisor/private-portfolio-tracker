"""Gemeinsame SMTP-Mail-Utility fuer die Cronjob-Reports (Kurs-Update-Report,
Diagnose-Report). Extrahiert aus etf_tracker_send_report.py 2026-08-17, als
zweiter Konsument (send_diagnostic_report.py) dieselbe Logik brauchte.

.env (Server, zusaetzlich zu den DB-Credentials):
    SMTP_HOST=smtp.gmail.com
    SMTP_PORT=587
    SMTP_USER=trades@quant-advisor.de
    SMTP_PASSWORD=<App-Passwort>
    MAIL_FROM=trades@quant-advisor.de
    MAIL_TO=christian.schott@quant-advisor.de
    # Optional, nur fuer den Portfolio-Report (etf_tracker_send_report.py) -
    # faellt auf MAIL_TO zurueck, falls nicht gesetzt. Der Diagnose-Report
    # (send_diagnostic_report.py) nutzt weiterhin nur MAIL_TO.
    MAIL_TO_PORTFOLIO_REPORT=christian.schott@quant-advisor.de,tina.schott@gmx.de
"""

from __future__ import annotations

import logging
import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

log = logging.getLogger(__name__)

SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASS = os.environ.get("SMTP_PASSWORD", "")
MAIL_FROM = os.environ.get("MAIL_FROM", SMTP_USER)
MAIL_TO = os.environ.get("MAIL_TO", "")


def send_mail(subject: str, html_body: str, to: str | None = None) -> None:
    """to: optionale Empfaenger-Liste (kommagetrennt), ueberschreibt MAIL_TO
    fuer diesen einen Versand - z.B. der Portfolio-Report an einen groesseren
    Verteiler als der technische Diagnose-Report."""
    mail_to = to if to else MAIL_TO
    if not SMTP_USER or not mail_to:
        log.warning("SMTP_USER / Empfaenger nicht konfiguriert - kein E-Mail-Versand.")
        return

    recipients = [r.strip() for r in mail_to.split(",") if r.strip()]
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = MAIL_FROM
    msg["To"] = ", ".join(recipients)
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
