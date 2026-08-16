"""
Transaktions-Loader: Oskar VL-Sparplan (Depotfuehrung: Baader Bank AG).

Liest die monatlichen "Baader Bank_Monatlicher Kontoauszug"-PDFs aus
Trades/Oskar/. Die "Oskar_Bericht ueber die Finanzportfolioverwaltung"- und
"Baader Bank_Depotauszug"-Dokumente enthalten keine Einzeltransaktionen
(Bestands-/TWR-Reports) und werden hier nicht geladen.

Jede Buchung ist ein mehrzeiliger Block, der mit einer Datumszeile beginnt und
mit "Vorgangs-Nr.: ..." endet:
    26.09.2024 Kauf 30.09.2024 7,39 -
    IN.MK.-I.S+P
    ISIN IE00BKS7L097
    STK 0,104
    Vorgangs-Nr.: WWUM 00368331571

Vorzeichen-Konvention (verifiziert gegen den realen Kontoauszug-Bestand):
  - Ein TRAILING "-" direkt hinter dem Betrag auf der ersten Zeile markiert
    eine Belastung (Kauf, Lastschrift/Gebuehren) -> gross_amount negativ.
    KEIN trailing "-" auf der ersten Zeile = Gutschrift (Verkauf, Einzahlung,
    Rechnungsabschluss) -> gross_amount positiv.
  - Bei Verkauf steht das Minus stattdessen an der STK-Zeile ("STK 0,001-")
    und markiert dort die negative (verkaufte) Stueckzahl.

Sechs Transaktionstypen (Erlaeuterungen-Praefix -> txn_type):
    Kauf                          -> BUY
    Verkauf                       -> SELL
    Gutschrift                    -> DEPOSIT (VL-Einzahlung)
    Lastschrift                   -> FEE (Verwaltungsgebuehr Oskar.de/Scalable Capital)
    Transaktionskostenpauschale   -> FEE
    Rechnungsabschluss            -> INTEREST (Zinsabschluss Verrechnungskonto, selten)

broker_ref: die "Vorgangs-Nr." ist bereits ein natuerlicher, eindeutiger
Schluessel (wie bei ING) - kein synthetischer Ersatz noetig.

Aufruf:
    python parse_oskar_transactions.py [--dir PFAD] [--dry-run]

Idempotent: mehrfacher Lauf ueberschreibt bestehende Zeilen (ON CONFLICT).
Verarbeitet immer alle PDFs im Ordner.

WICHTIG: Fuer eine echte TWR-Berechnung braucht Oskar zusaetzlich einen
Preis-Backfill fuer 11 von 12 gehandelten ISINs (siehe PROJECT_PLAN.md,
Abschnitt 14-E) - das Laden der Transaktionen hier ist davon unabhaengig und
bereits fuer sich verifizierbar (Mengen-Abgleich gegen die Depotauszuege).
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

DEFAULT_DIR = Path(__file__).parent.parent / "Trades" / "Oskar"
ACCOUNT_NAME = "Christian Oskar VL"
SOURCE_SYSTEM = "oskar_baader"

BLOCK_START = re.compile(r"^(\d{2}\.\d{2}\.\d{4})\s+(.+?)\s+(\d{2}\.\d{2}\.\d{4})\s+([\d.,]+)(\s*-)?\s*$")
ISIN_RE = re.compile(r"ISIN\s+([A-Z0-9]{12})")
STK_RE = re.compile(r"STK\s+(-?[\d.,]+)(-)?")
VORGANG_RE = re.compile(r"Vorgangs-Nr\.:\s*(.+)$", re.MULTILINE)

TYPE_PREFIXES = [
    ("Kauf", "BUY"),
    ("Verkauf", "SELL"),
    ("Gutschrift", "DEPOSIT"),
    ("Lastschrift", "FEE"),
    ("Transaktionskostenpauschale", "FEE"),
    ("Rechnungsabschluss", "INTEREST"),
]


def _num(raw: str | None) -> float | None:
    if raw is None:
        return None
    s = raw.strip().replace("\xa0", "").replace(".", "").replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def _de_date(raw: str) -> date:
    return datetime.strptime(raw.strip(), "%d.%m.%Y").date()


def find_files(directory: Path) -> list[Path]:
    files = sorted(directory.glob("Baader Bank_Monatlicher Kontoauszug*.pdf"))
    if not files:
        raise SystemExit(f"Keine Kontoauszug-PDFs in {directory} gefunden.")
    return files


def get_account_id() -> int:
    row = db_utils.query_df(
        "SELECT account_id FROM portfolio.account WHERE name = :name", {"name": ACCOUNT_NAME}
    )
    if row.empty:
        raise SystemExit(
            f"Depot '{ACCOUNT_NAME}' nicht in portfolio.account gefunden. "
            f"Bitte zuerst anlegen (siehe PROJECT_PLAN.md Abschnitt 14-E)."
        )
    return int(row["account_id"].iloc[0])


def _classify(erlaeuterung: str) -> str:
    for prefix, txn_type in TYPE_PREFIXES:
        if erlaeuterung.startswith(prefix):
            return txn_type
    return "OTHER"


def parse_blocks(text: str, account_id: int, source_file: str) -> list[dict]:
    lines = text.split("\n")

    blocks: list[str] = []
    current: list[str] = []
    for line in lines:
        if BLOCK_START.match(line):
            if current:
                blocks.append("\n".join(current))
            current = [line]
        elif current:
            current.append(line)
    if current:
        blocks.append("\n".join(current))

    records: list[dict] = []
    for block in blocks:
        first_line = block.split("\n", 1)[0]
        m = BLOCK_START.match(first_line)
        if not m:
            continue

        buchungstag = _de_date(m.group(1))
        erlaeuterung = m.group(2).strip()
        amount_raw = m.group(4)
        is_debit = m.group(5) is not None

        txn_type = _classify(erlaeuterung)
        if txn_type == "OTHER":
            log.warning("Unbekannter Transaktionstyp, uebersprungen: %r (%s)", first_line, source_file)
            continue

        amount = _num(amount_raw)
        if amount is None:
            log.warning("Betrag nicht parsbar, uebersprungen: %r (%s)", first_line, source_file)
            continue
        gross_amount = -amount if is_debit else amount

        vorgang_m = VORGANG_RE.search(block)
        if not vorgang_m:
            log.warning("Keine Vorgangs-Nr. gefunden, uebersprungen: %r (%s)", first_line, source_file)
            continue
        broker_ref = vorgang_m.group(1).strip()

        isin = None
        quantity = None
        if txn_type in ("BUY", "SELL"):
            isin_m = ISIN_RE.search(block)
            stk_m = STK_RE.search(block)
            isin = isin_m.group(1) if isin_m else None
            if stk_m:
                qty = _num(stk_m.group(1))
                # Vorzeichen NICHT aus der STK-Zeile lesen - verifiziert
                # uneinheitlich (mal fuehrendes, mal folgendes, mal gar kein
                # Minuszeichen fuer Verkaeufe). Robuster: Vorzeichen direkt aus
                # dem bereits bestimmten txn_type ableiten (BUY=+, SELL=-).
                quantity = abs(qty) if txn_type == "BUY" else -abs(qty)
            if isin is None or quantity is None:
                log.warning("Kauf/Verkauf-Block ohne ISIN/STK, uebersprungen: %r (%s)", first_line, source_file)
                continue

        records.append(
            {
                "account_id": account_id,
                "broker_ref": broker_ref,
                "source_system": SOURCE_SYSTEM,
                "txn_datetime": datetime.combine(buchungstag, datetime.min.time()),
                "txn_date": buchungstag,
                "txn_type": txn_type,
                "raw_category": erlaeuterung,
                "isin": isin,
                "security_name": None,
                "quantity": quantity,
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
        )

    return records


def main() -> None:
    parser = argparse.ArgumentParser(description="Lade Oskar-Kontoauszuege nach portfolio.transaction")
    parser.add_argument("--dir", type=Path, default=None, help="Ordner mit den PDFs (Standard: Trades/Oskar/)")
    parser.add_argument("--dry-run", action="store_true", help="Nur einlesen und zusammenfassen, nicht schreiben")
    args = parser.parse_args()

    directory = args.dir or DEFAULT_DIR
    files = find_files(directory)
    log.info("%d Kontoauszug-Datei(en) in %s gefunden", len(files), directory)

    account_id = get_account_id()

    records: list[dict] = []
    for f in files:
        with pdfplumber.open(f) as pdf:
            text = "\n".join((p.extract_text() or "") for p in pdf.pages)
        records.extend(parse_blocks(text, account_id, f.name))

    log.info("%d Transaktionszeile(n) geparst", len(records))

    dist = Counter(r["txn_type"] for r in records)
    log.info("txn_type-Verteilung: %s", dict(dist))

    if args.dry_run:
        log.info("[dry-run] Es wurde nichts geschrieben.")
        return

    parse_utils.write_transactions(records)
    log.info("Fertig. %d Zeilen -> portfolio.transaction geschrieben.", len(records))


if __name__ == "__main__":
    main()
