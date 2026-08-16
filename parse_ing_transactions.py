"""
Transaktions-Loader: ING (Direkt-Depot).

Liest die PDF-Wertpapierabrechnungen/-Ertragsgutschriften aus Trades/ING/ (ein
PDF pro Ereignis) und schreibt sie nach portfolio.transaction. Anders als bei
Scalable/Trade Republic gibt es keinen CSV-Export - Volltext-Extraktion per
pdfplumber, Klassifikation ueber die Kopfzeile im Dokument (nicht ueber den
Dateinamen).

Fuenf erkannte Dokumenttypen (Kopfzeile -> txn_type):
    Wertpapierabrechnung Kauf     -> BUY
    Wertpapierabrechnung Verkauf  -> SELL
    Ertragsgutschrift             -> DIVIDEND (Fonds-Ausschuettung)
    Dividendengutschrift          -> DIVIDEND (Aktien-Dividende)
    Vorabpauschale                -> TAX (kein echter Cashflow, meist EUR 0,00)

Drei weitere, erkannte aber NICHT geladene Dokumenttypen (kein Einzel-
transaktions-Ereignis): Depotauszug, Kostenaufstellung, "Wertpapiersparplan
weiterfuehren oder ruhen lassen?"-Benachrichtigung.

WICHTIG: Kopfzeilen-Pruefung in fester Reihenfolge (Kauf/Verkauf zuerst!) -
das Wort "Vorabpauschale" taucht auch als Nebentext im Steueranhang mancher
Verkaufsbelege auf; ohne Prioritaet wuerden solche Belege falsch klassifiziert
(verifiziert gegen den realen Dateibestand).

Aufruf:
    python parse_ing_transactions.py [--dir PFAD] [--dry-run]

Idempotent: mehrfacher Lauf ueberschreibt bestehende Zeilen (ON CONFLICT).
Verarbeitet IMMER alle PDFs im Ordner (nicht nur die neuesten) - jede Datei
ist ein eigenstaendiges Ereignis, Re-Downloads/Duplikate werden von
parse_utils.write_transactions() ueber broker_ref dedupliziert.
"""

import argparse
import logging
import re
from collections import Counter
from datetime import date, datetime
from pathlib import Path

import pdfplumber

import db_utils
import parse_utils

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

DEFAULT_DIR = Path(__file__).parent.parent / "Trades" / "ING"
ACCOUNT_NAME = "Ing Gemeinschaftsdepot"
SOURCE_SYSTEM = "ing"

# Kopfzeilen, die erkannt aber nicht als Transaktion geladen werden (kein
# Absturz, aber auch keine Zeile in portfolio.transaction).
IGNORED_HEADERS = (
    "Depotauszug per",
    "Jahresdepotauszug",  # Jahres-Variante; Fliesstext "...zum X abgeschlossene..."
                           # verliert bei manchen PDFs die Leerzeichen (Extraktions-
                           # Artefakt) - die eigenstaendige Ueberschrift ist robuster.
    "Kostenaufstellung",
    "Wertpapiersparplan weiterf",  # Ausfall-Benachrichtigung
)


def _num(raw: str | None) -> float | None:
    """Deutsches Zahlenformat: Punkt = Tausendertrennzeichen, Komma = Dezimal."""
    if raw is None:
        return None
    s = raw.strip().replace("\xa0", "")
    if s == "":
        return None
    s = s.replace(".", "").replace(",", ".")
    try:
        return float(s)
    except ValueError:
        log.warning("Zahl nicht parsbar: %r", raw)
        return None


def _de_date(raw: str) -> date:
    return datetime.strptime(raw.strip(), "%d.%m.%Y").date()


def extract_text(pdf_path: Path) -> str:
    with pdfplumber.open(pdf_path) as pdf:
        return "\n".join((p.extract_text() or "") for p in pdf.pages)


def find_files(directory: Path) -> list[Path]:
    files = sorted(directory.glob("*.pdf"))
    if not files:
        raise SystemExit(f"Keine PDF-Dateien in {directory} gefunden.")
    return files


def get_account_id() -> int:
    row = db_utils.query_df(
        "SELECT account_id FROM portfolio.account WHERE name = :name", {"name": ACCOUNT_NAME}
    )
    if row.empty:
        raise SystemExit(f"Depot '{ACCOUNT_NAME}' nicht in portfolio.account gefunden.")
    return int(row["account_id"].iloc[0])


# ── Feld-Extraktion je Dokumenttyp ────────────────────────────────────────────

def _parse_kauf_verkauf(text: str, is_kauf: bool, account_id: int, source_file: str) -> dict | None:
    m_order = re.search(r"Ordernummer\s+([\d.]+)", text)
    m_isin = re.search(r"ISIN \(WKN\)\s+([A-Z0-9]{12})", text)
    m_name = re.search(r"Wertpapierbezeichnung\s+(.+?)\s*\n\s*Nominale", text, re.DOTALL)
    m_qty = re.search(r"Nominale St.ck\s+([\d.,]+)", text)
    m_price = re.search(r"Kurs EUR\s+([\d.,]+)", text)
    m_exec = re.search(r"Ausf.hrungstag / -zeit\s+([\d.]+)\s+um\s+([\d:]+)\s+Uhr", text)
    m_fee = re.search(r"Provision EUR\s+([\d.,]+)", text)
    m_amount = re.search(r"Endbetrag zu Ihren (Lasten|Gunsten)\s+EUR\s+([\d.,]+)", text)

    if not (m_order and m_isin and m_qty and m_exec and m_amount):
        log.error("Kauf/Verkauf-Beleg unvollstaendig, uebersprungen: %s", source_file)
        return None

    qty = _num(m_qty.group(1))
    quantity = qty if is_kauf else -qty

    amount = _num(m_amount.group(2))
    gross_amount = -amount if m_amount.group(1) == "Lasten" else amount

    txn_date = _de_date(m_exec.group(1))
    txn_datetime = datetime.combine(txn_date, datetime.strptime(m_exec.group(2), "%H:%M:%S").time())

    security_name = re.sub(r"\s+", " ", m_name.group(1)).strip() if m_name else None

    return {
        "account_id": account_id,
        "broker_ref": m_order.group(1),
        "source_system": SOURCE_SYSTEM,
        "txn_datetime": txn_datetime,
        "txn_date": txn_date,
        "txn_type": "BUY" if is_kauf else "SELL",
        "raw_category": "Wertpapierabrechnung Kauf" if is_kauf else "Wertpapierabrechnung Verkauf",
        "isin": m_isin.group(1),
        "security_name": security_name,
        "quantity": quantity,
        "price": _num(m_price.group(1)) if m_price else None,
        "gross_amount": gross_amount,
        "fee": _num(m_fee.group(1)) if m_fee else 0,
        "tax": 0,
        "currency": "EUR",
        "original_amount": None,
        "original_currency": None,
        "fx_rate_applied": None,
        "status": "Executed",
    }


def _parse_ertrag(text: str, raw_category: str, account_id: int, source_file: str) -> dict | None:
    m_isin = re.search(r"ISIN \(WKN\)\s+([A-Z0-9]{12})", text)
    m_name = re.search(r"Wertpapierbezeichnung\s+(.+?)\s*\n\s*Nominale", text, re.DOTALL)
    m_zahltag = re.search(r"Zahltag\s+([\d.]+)", text)
    m_brutto = re.search(r"Brutto\s+([A-Z]{3})\s+([\d.,]+)", text)
    m_fx = re.search(r"Umg\. z\. Dev\.-Kurs\(([\d.,]+)\)", text)
    m_amount = re.search(r"Gesamtbetrag zu Ihren Gunsten\s+EUR\s+([\d.,]+)", text)
    m_kest = re.search(r"Kapitalertragsteuer\s+[\d.,]+\s*%\s+EUR\s+([\d.,]+)", text)
    m_soli = re.search(r"Solidarit.tszuschlag\s+[\d.,]+\s*%\s+EUR\s+([\d.,]+)", text)

    if not (m_isin and m_zahltag and m_amount):
        log.error("Ertrags-/Dividendengutschrift unvollstaendig, uebersprungen: %s", source_file)
        return None

    zahltag = _de_date(m_zahltag.group(1))
    security_name = re.sub(r"\s+", " ", m_name.group(1)).strip() if m_name else None
    tax = (_num(m_kest.group(1)) or 0 if m_kest else 0) + (_num(m_soli.group(1)) or 0 if m_soli else 0)

    return {
        "account_id": account_id,
        "broker_ref": f"DIV-{m_isin.group(1)}-{zahltag.isoformat()}",
        "source_system": SOURCE_SYSTEM,
        "txn_datetime": datetime.combine(zahltag, datetime.min.time()),
        "txn_date": zahltag,
        "txn_type": "DIVIDEND",
        "raw_category": raw_category,
        "isin": m_isin.group(1),
        "security_name": security_name,
        "quantity": None,
        "price": None,
        "gross_amount": _num(m_amount.group(1)),
        "fee": 0,
        "tax": tax,
        "currency": "EUR",
        "original_amount": _num(m_brutto.group(2)) if m_brutto else None,
        "original_currency": m_brutto.group(1) if m_brutto else None,
        "fx_rate_applied": _num(m_fx.group(1)) if m_fx else None,
        "status": "Executed",
    }


def _parse_vorabpauschale(text: str, account_id: int, source_file: str) -> dict | None:
    m_isin = re.search(r"ISIN \(WKN\)\s+([A-Z0-9]{12})", text)
    m_zahltag = re.search(r"Zahltag\s+([\d.]+)", text)
    m_amount = re.search(r"Gesamtbetrag zu Ihren (Gunsten|Lasten)\s+EUR\s+([\d.,]+)", text)

    if not (m_isin and m_zahltag and m_amount):
        log.error("Vorabpauschale-Beleg unvollstaendig, uebersprungen: %s", source_file)
        return None

    zahltag = _de_date(m_zahltag.group(1))
    amount = _num(m_amount.group(2))
    gross_amount = -amount if m_amount.group(1) == "Lasten" else amount

    return {
        "account_id": account_id,
        "broker_ref": f"TAX-VP-{m_isin.group(1)}-{zahltag.isoformat()}",
        "source_system": SOURCE_SYSTEM,
        "txn_datetime": datetime.combine(zahltag, datetime.min.time()),
        "txn_date": zahltag,
        "txn_type": "TAX",
        "raw_category": "Vorabpauschale",
        "isin": m_isin.group(1),
        "security_name": None,
        "quantity": None,
        "price": None,
        "gross_amount": gross_amount,
        "fee": 0,
        "tax": 0,
        "currency": "EUR",
        "original_amount": None,
        "original_currency": None,
        "fx_rate_applied": None,
        "status": "Executed",
    }


def classify_and_parse(text: str, account_id: int, source_file: str) -> tuple[str, dict | None]:
    """Gibt (status, record) zurueck. status ist 'ok', 'ignored' (erkannter,
    aber bewusst nicht geladener Dokumenttyp) oder 'unrecognized' (unbekannte
    Kopfzeile, wird geloggt)."""
    # Reihenfolge ist wichtig: Kauf/Verkauf zuerst pruefen (siehe Docstring).
    if "Wertpapierabrechnung Kauf" in text:
        return "ok", _parse_kauf_verkauf(text, True, account_id, source_file)
    if "Wertpapierabrechnung Verkauf" in text:
        return "ok", _parse_kauf_verkauf(text, False, account_id, source_file)
    if "Dividendengutschrift" in text:
        return "ok", _parse_ertrag(text, "Dividendengutschrift", account_id, source_file)
    if "Ertragsgutschrift" in text:
        return "ok", _parse_ertrag(text, "Ertragsgutschrift", account_id, source_file)
    if "Vorabpauschale" in text:
        return "ok", _parse_vorabpauschale(text, account_id, source_file)
    if any(h in text for h in IGNORED_HEADERS):
        return "ignored", None
    log.error("Unbekannte Kopfzeile, uebersprungen: %s\n  Textanfang: %r", source_file, text[:200])
    return "unrecognized", None


def main() -> None:
    parser = argparse.ArgumentParser(description="Lade ING-PDF-Belege nach portfolio.transaction")
    parser.add_argument("--dir", type=Path, default=None, help="Ordner mit den PDFs (Standard: Trades/ING/)")
    parser.add_argument("--dry-run", action="store_true", help="Nur einlesen und zusammenfassen, nicht schreiben")
    args = parser.parse_args()

    directory = args.dir or DEFAULT_DIR
    files = find_files(directory)
    log.info("%d PDF-Datei(en) in %s gefunden", len(files), directory)

    account_id = get_account_id()

    records: list[dict] = []
    ignored = 0
    unrecognized = 0
    for f in files:
        text = extract_text(f)
        status, record = classify_and_parse(text, account_id, f.name)
        if status == "ignored":
            ignored += 1
        elif status == "unrecognized" or record is None:
            unrecognized += 1
        else:
            records.append(record)

    log.info(
        "%d Transaktionszeile(n) geparst, %d Dokument(e) ignoriert (Depotauszug/Kostenaufstellung/Nachricht), %d nicht erkannt/unvollstaendig",
        len(records), ignored, unrecognized,
    )

    dist = Counter(r["txn_type"] for r in records)
    log.info("txn_type-Verteilung: %s", dict(dist))

    if args.dry_run:
        log.info("[dry-run] Es wurde nichts geschrieben.")
        return

    parse_utils.write_transactions(records)
    log.info("Fertig. %d Zeilen -> portfolio.transaction geschrieben.", len(records))


if __name__ == "__main__":
    main()
