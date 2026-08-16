"""
Transaktions-Loader: Riester (Sutor Bank / Raisin Pension, vormals "Sutor
fairriester 2.0", Depotnummer 3133795601).

Liest die PDF-Quartals-/Jahres-Depotauszuege aus Trades/Raisin Riester/. Jeder
Auszug enthaelt ab Seite 3 eine vollstaendige Umsatzliste des Berichtszeitraums
(Kauf/Verkauf/Einzahlung/Riester-Zulage/Gebuehren/DTA-Uebertrag), im Gegensatz
zu ING NICHT ein PDF pro Ereignis, sondern viele Ereignisse pro PDF.

Jede Transaktionszeile ist im extrahierten Text auf zwei physische Zeilen
verteilt:
    01.04.2025 01.04.2025 Kauf Kauf iShares Edge MSCI USA Min Vol ESG 9,2867 1,0788 -71,87
    14:47 OTC IE00BKVL7331 8,3488 US$
Wird deshalb blockweise geparst: ein Block beginnt bei einer Zeile, die mit
zwei Datumsangaben startet, und laeuft bis zur naechsten solchen Zeile.

Sechs Transaktionstypen (Quelle -> txn_type):
    Kauf, Storno Kauf       -> BUY   (Anteile-Spalte ist bereits korrekt
    Verkauf, Storno Verkauf -> SELL   signiert, kein manuelles Vorzeichen-Flip)
    Einzahlung              -> DEPOSIT (automatischer Lastschrifteinzug)
    Riester-Zulage          -> DEPOSIT (staatliche Foerderung)
    Gebuehr, Gebuehren,
      generisches Storno    -> FEE
    Rechnungsabschluss      -> INTEREST (Zinsabschluss Verrechnungskonto, selten)
    DTA                     -> CASH_TRANSFER_IN (einmaliger Depotuebertrag von
                                der DWS beim Anbieterwechsel Ende 2019)

Kein Ordernummer-Feld -> broker_ref synthetisch aus Buchungsdatum + Uhrzeit +
ISIN (Wertpapier-Zeilen) bzw. Buchungsdatum + Typ + laufender Nummer (reine
Cash-Zeilen ohne ISIN/Uhrzeit).

Aufruf:
    python parse_riester_transactions.py [--dir PFAD] [--dry-run]

Idempotent: mehrfacher Lauf ueberschreibt bestehende Zeilen (ON CONFLICT).
Verarbeitet immer alle PDFs im Ordner; Doppel-Downloads (z.B. die zwei
Q2/2026-Dateien) dedupliziert parse_utils.write_transactions() ueber broker_ref.
"""

import argparse
import logging
import re
from collections import Counter, defaultdict
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

DEFAULT_DIR = Path(__file__).parent.parent / "Trades" / "Raisin Riester"
ACCOUNT_NAME = "Christian Riester"
SOURCE_SYSTEM = "sutor_raisin"

# Dokumente, die keine Umsatzliste enthalten (Steuerbescheinigungen etc.) -
# werden anhand des Dateinamens erkannt und uebersprungen. Die eigentlichen
# Depotauszuege heissen "Depotauszug per ..." oder "... Depotauszug per ...".
BLOCK_START = re.compile(r"^(\d{2}\.\d{2}\.\d{4}) (\d{2}\.\d{2}\.\d{4}) (.+)$")
TIME_RE = re.compile(r"(\d{2}:\d{2})")
ISIN_RE = re.compile(r"\b([A-Z]{2}[A-Z0-9]{9}\d)\b")
NUM_RE = re.compile(r"-?\d{1,3}(?:\.\d{3})*,\d+")

TYPE_PREFIXES = [
    ("Storno Kauf", "BUY"),
    ("Storno Verkauf", "SELL"),
    ("Kauf", "BUY"),
    ("Verkauf", "SELL"),
    ("Riester-Zulage", "DEPOSIT"),
    ("Einzahlung", "DEPOSIT"),
    ("Gebuehren", "FEE"),  # ASCII-Fallback, falls Umlaut-Dekodierung abweicht
    ("Gebühren", "FEE"),
    ("Gebuehr", "FEE"),
    ("Gebühr", "FEE"),
    ("Rechnungsabschluss", "INTEREST"),
    ("Überweisung DTA von", "DEPOSIT"),  # wiederkehrende Einzahlung per Ueberweisung (2026)
    ("Uberweisung DTA von", "DEPOSIT"),  # ASCII-Fallback
    ("DTA", "CASH_TRANSFER_IN"),  # einmaliger Depotuebertrag von der DWS (2019)
    ("Storno", "FEE"),  # generischer Fallback, z.B. "Storno Verwaltungsgebuehr..."
]

SECURITY_TYPES = {"BUY", "SELL"}


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


def extract_umsaetze_text(pdf_path: Path) -> str | None:
    with pdfplumber.open(pdf_path) as pdf:
        text = "\n".join((p.extract_text() or "") for p in pdf.pages)
    idx = text.find("Umsätze vom")
    if idx == -1:
        idx = text.find("Ums")  # sehr defensiver Fallback bei Umlaut-Extraktionsproblemen
        if idx == -1 or "tze vom" not in text[idx:idx + 20]:
            return None
    return text[idx:]


def _classify(rest: str) -> tuple[str, str]:
    """Gibt (txn_type, matched_prefix) zurueck, oder ('OTHER', '') falls kein Muster passt."""
    for prefix, txn_type in TYPE_PREFIXES:
        if rest.startswith(prefix):
            return txn_type, prefix
    return "OTHER", ""


def parse_blocks(text: str, account_id: int, source_file: str) -> list[dict]:
    lines = [l for l in text.split("\n") if l.strip()]

    # Bloecke bilden: Start bei einer Zeile mit zwei fuehrenden Datumsangaben,
    # alle Folgezeilen bis zum naechsten Block-Start gehoeren dazu.
    blocks: list[str] = []
    current: list[str] = []
    for line in lines:
        if BLOCK_START.match(line):
            if current:
                blocks.append(" ".join(current))
            current = [line]
        elif current:
            current.append(line)
    if current:
        blocks.append(" ".join(current))

    seq_counter: dict[tuple, int] = defaultdict(int)
    records: list[dict] = []
    for block in blocks:
        m = BLOCK_START.match(block)
        if not m:
            continue
        buchungsdatum = _de_date(m.group(1))
        rest = m.group(3)

        txn_type, prefix = _classify(rest)
        if txn_type == "OTHER":
            log.warning("Unbekannter Transaktionstyp, uebersprungen: %r (%s)", block[:100], source_file)
            continue

        time_m = TIME_RE.search(rest)
        head = rest[: time_m.start()] if time_m else rest
        tail = rest[time_m.start():] if time_m else ""

        nums = NUM_RE.findall(head)
        amount = _num(nums[-1]) if nums else None
        quantity = None

        isin = None
        uhrzeit = None
        if txn_type in SECURITY_TYPES:
            quantity = _num(nums[0]) if nums else None
            # Uhrzeit/ISIN stehen normalerweise nach der Zeit auf der Folgezeile
            # ("... 14:47 OTC IE00... "). Frueher Bestand (DWS-Uebergangsphase,
            # Dez 2019 - Maerz 2020, Handelsplatz "ausserboerslich") hat KEINE
            # Uhrzeit - dort direkt im gesamten Rest nach der ISIN suchen.
            isin_m = ISIN_RE.search(tail) if time_m else ISIN_RE.search(rest)
            isin = isin_m.group(1) if isin_m else None
            uhrzeit = time_m.group(1) if time_m else None
            if isin is None or quantity is None or amount is None:
                log.warning("Kauf/Verkauf-Zeile unvollstaendig, uebersprungen: %r (%s)", block[:120], source_file)
                continue
            # Transaktionstyp (prefix) MUSS Teil des Schluessels sein: ein Kauf
            # und seine eigene Storno-Kauf-Stornierung koennen dieselbe Minute
            # + ISIN teilen (verifiziert: zwei echte Faelle im Datenbestand) -
            # ohne prefix wuerde eine der beiden Zeilen die andere ueberschreiben.
            # Zusaetzlich ein Sequenzzaehler als letzte Absicherung gegen
            # weitere, bisher nicht beobachtete Kollisionen.
            if uhrzeit is not None:
                txn_datetime = datetime.combine(
                    buchungsdatum, datetime.strptime(uhrzeit, "%H:%M").time()
                )
                key = (buchungsdatum, uhrzeit, isin, prefix)
            else:
                txn_datetime = datetime.combine(buchungsdatum, datetime.min.time())
                key = (buchungsdatum, isin, prefix)
            seq_counter[key] += 1
            seq = seq_counter[key]
            suffix = f"-{seq}" if seq > 1 else ""
            broker_ref = "RIESTER-" + "-".join(str(part) for part in key) + suffix
        else:
            if amount is None:
                log.warning("Cash-Zeile ohne Betrag, uebersprungen: %r (%s)", block[:120], source_file)
                continue
            key = (buchungsdatum, txn_type)
            seq_counter[key] += 1
            broker_ref = f"RIESTER-{buchungsdatum.isoformat()}-{txn_type}-{seq_counter[key]}"
            txn_datetime = datetime.combine(buchungsdatum, datetime.min.time())

        records.append(
            {
                "account_id": account_id,
                "broker_ref": broker_ref,
                "source_system": SOURCE_SYSTEM,
                "txn_datetime": txn_datetime,
                "txn_date": buchungsdatum,
                "txn_type": txn_type,
                "raw_category": prefix,
                "isin": isin,
                "security_name": None,
                "quantity": quantity,
                "price": None,
                "gross_amount": amount,
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
    parser = argparse.ArgumentParser(description="Lade Riester-PDF-Umsaetze nach portfolio.transaction")
    parser.add_argument("--dir", type=Path, default=None, help="Ordner mit den PDFs (Standard: Trades/Raisin Riester/)")
    parser.add_argument("--dry-run", action="store_true", help="Nur einlesen und zusammenfassen, nicht schreiben")
    args = parser.parse_args()

    directory = args.dir or DEFAULT_DIR
    files = find_files(directory)
    log.info("%d PDF-Datei(en) in %s gefunden", len(files), directory)

    account_id = get_account_id()

    records: list[dict] = []
    skipped_files = 0
    for f in files:
        text = extract_umsaetze_text(f)
        if text is None:
            skipped_files += 1
            continue
        records.extend(parse_blocks(text, account_id, f.name))

    log.info(
        "%d Transaktionszeile(n) geparst, %d Datei(en) ohne Umsatzliste uebersprungen",
        len(records), skipped_files,
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
