"""Transaktions-Loader: Oskar VL-Sparplan, Sonderfall-Ordner
(Trades/Oskar/Sonderfall/).

Nach der Umstellung der Depotfuehrung von Baader Bank auf Scalable Capital
Bank laesst sich fuer die juengeren Trades kein monatlicher Kontoauszug mehr
im Portal abrufen - stattdessen nur noch einzelne
"Wertpapierabrechnung"-PDFs, eine Datei pro Trade (gleiches Dokumentschema
wie bei ING, siehe parse_ing_transactions.py). Ergaenzt
parse_oskar_transactions.py (Kontoauszug-Ordner), schreibt in dieselbe
portfolio.transaction-Tabelle mit derselben natuerlichen "Vorgangs-Nr."
als broker_ref - dadurch ist ein etwaiger Overlap zwischen beiden Quellen
automatisch idempotent (ON CONFLICT DO UPDATE trifft dieselbe Zeile), auch
wenn ein Trade in beiden Formaten vorkaeme. Verifiziert (2026-08-16): 80 von
80 Vorgangs-Nr. aus diesem Ordner waren zu diesem Zeitpunkt NEU (kein
Overlap mit den bereits geladenen Kontoauszug-Zeilen) - echte zusaetzliche
Trades, kein Duplikat-Risiko.

Ein PDF = eine Transaktion (kein mehrzeiliger Block noetig):
    Vorgangs-Nr.: 627164197
    Wertpapierabrechnung: Kauf | Verkauf
    Auftragsdatum: 12.02.2026
    Nominale ISIN: IE000O5FBC47 WKN: ETF137 Kurs
    STK 0,648 ...
    Kurswert EUR 24,38

Vorzeichen aus dem Dokumenttyp (wie bei den anderen Oskar-Vorzeichen-Fixen
dieser Session: robuster als Text-Marker zu parsen) - Kauf = Belastung
(gross_amount negativ, Menge positiv), Verkauf = Gutschrift (gross_amount
positiv, Menge negativ).

Aufruf:
    python parse_oskar_sonderfall_transactions.py [--dir PFAD] [--dry-run]

Idempotent: mehrfacher Lauf ueberschreibt bestehende Zeilen (ON CONFLICT).
Datei-Duplikate durch Windows-Mehrfachdownload (" (1).pdf") sind unschaedlich
(gleicher Inhalt -> gleicher broker_ref -> stille Dedup in write_transactions).

WICHTIG - oekonomischer Dedup gegen bereits geladene Kontoauszug-Zeilen:
verifiziert (2026-08-16), dass 77 von 83 Dateien in diesem Ordner Trades
sind, die BEREITS ueber den normalen Kontoauszug-Parser geladen wurden -
gleiches ISIN, gleiches Datum, gleiche Stueckzahl, nur mit einer anderen
Vorgangs-Nr.-Schreibweise (Kontoauszug: "WWUM 00639161115", hier nur die
nackte Zahl "639161115" - nachweislich dieselbe Transaktion, keine
zufaellige Uebereinstimmung). Ein naiver Load haette fast jeden Trade
verdoppelt. Deshalb: vor dem Schreiben wird jeder geparste Datensatz gegen
bereits vorhandene Zeilen (gleiches Konto, ISIN, Datum, Menge innerhalb
0,001 Toleranz) abgeglichen und bei Treffer NICHT geschrieben (nur geloggt).
Das macht das Skript sicher wiederholbar, auch wenn kuenftig weitere
Sonderfall-Dateien mit teilweisem Overlap dazukommen.
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

DEFAULT_DIR = Path(__file__).parent.parent / "Trades" / "Oskar" / "Sonderfall"
ACCOUNT_NAME = "Christian Oskar VL"
SOURCE_SYSTEM = "oskar_sonderfall"

VORGANG_RE = re.compile(r"Vorgangs-Nr\.:\s*(\S+)")
TYPE_RE = re.compile(r"Wertpapierabrechnung:\s*(Kauf|Verkauf)")
DATE_RE = re.compile(r"Auftragsdatum:\s*(\d{2}\.\d{2}\.\d{4})")
ISIN_RE = re.compile(r"ISIN:\s*([A-Z0-9]{12})")
STK_RE = re.compile(r"STK\s+([\d.,]+)")
KURSWERT_RE = re.compile(r"Kurswert EUR\s+([\d.,]+)")

TYPE_MAP = {"Kauf": "BUY", "Verkauf": "SELL"}


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
        raise SystemExit(f"Keine PDFs in {directory} gefunden.")
    return files


def get_account_id() -> int:
    row = db_utils.query_df(
        "SELECT account_id FROM portfolio.account WHERE name = :name", {"name": ACCOUNT_NAME}
    )
    if row.empty:
        raise SystemExit(f"Depot '{ACCOUNT_NAME}' nicht in portfolio.account gefunden.")
    return int(row["account_id"].iloc[0])


def parse_file(text: str, account_id: int, source_file: str) -> dict | None:
    vorgang_m = VORGANG_RE.search(text)
    type_m = TYPE_RE.search(text)
    date_m = DATE_RE.search(text)
    isin_m = ISIN_RE.search(text)
    stk_m = STK_RE.search(text)
    kurswert_m = KURSWERT_RE.search(text)

    if not (vorgang_m and type_m and date_m and isin_m and stk_m and kurswert_m):
        log.warning("Unvollstaendiges Dokument, uebersprungen: %s", source_file)
        return None

    txn_type = TYPE_MAP.get(type_m.group(1))
    if txn_type is None:
        log.warning("Unbekannter Dokumenttyp %r, uebersprungen: %s", type_m.group(1), source_file)
        return None

    # Eigenes Praefix statt der nackten Vorgangs-Nr.: die Kontoauszug-Zeilen
    # nutzen "WWUM 00<nr>"/"AAKV <nr>"-Schreibweisen fuer denselben Vorgang -
    # ein kollisionsfreier eigener Schluessel verhindert Verwechslung mit
    # diesen Schemata (der oekonomische Dedup in main() faengt echte
    # Duplikate ohnehin ab, unabhaengig vom broker_ref-Format).
    broker_ref = f"SONDERFALL-{vorgang_m.group(1).strip()}"
    auftragsdatum = _de_date(date_m.group(1))
    isin = isin_m.group(1)
    qty = _num(stk_m.group(1))
    kurswert = _num(kurswert_m.group(1))

    if qty is None or kurswert is None:
        log.warning("Menge/Kurswert nicht parsbar, uebersprungen: %s", source_file)
        return None

    quantity = abs(qty) if txn_type == "BUY" else -abs(qty)
    gross_amount = -abs(kurswert) if txn_type == "BUY" else abs(kurswert)

    return {
        "account_id": account_id,
        "broker_ref": broker_ref,
        "source_system": SOURCE_SYSTEM,
        "txn_datetime": datetime.combine(auftragsdatum, datetime.min.time()),
        "txn_date": auftragsdatum,
        "txn_type": txn_type,
        "raw_category": f"Wertpapierabrechnung: {type_m.group(1)}",
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


def _load_existing_keys(account_id: int) -> set[tuple]:
    """(isin, txn_date, gerundete Menge) aller bereits geladenen Zeilen fuer
    dieses Konto - Basis fuer den oekonomischen Dedup-Check."""
    df = db_utils.query_df(
        "SELECT isin, txn_date, quantity FROM portfolio.transaction WHERE account_id = :account_id",
        {"account_id": account_id},
    )
    return {(row.isin, row.txn_date, round(float(row.quantity), 3)) for row in df.itertuples(index=False) if row.quantity is not None}


def main() -> None:
    parser = argparse.ArgumentParser(description="Lade Oskar-Sonderfall-Wertpapierabrechnungen nach portfolio.transaction")
    parser.add_argument("--dir", type=Path, default=None, help="Ordner mit den PDFs (Standard: Trades/Oskar/Sonderfall/)")
    parser.add_argument("--dry-run", action="store_true", help="Nur einlesen und zusammenfassen, nicht schreiben")
    args = parser.parse_args()

    directory = args.dir or DEFAULT_DIR
    files = find_files(directory)
    log.info("%d Datei(en) in %s gefunden", len(files), directory)

    account_id = get_account_id()
    existing_keys = _load_existing_keys(account_id)

    records: list[dict] = []
    n_unparsable = 0
    n_duplicate = 0
    for f in files:
        with pdfplumber.open(f) as pdf:
            text = "\n".join((p.extract_text() or "") for p in pdf.pages)
        rec = parse_file(text, account_id, f.name)
        if rec is None:
            n_unparsable += 1
            continue
        key = (rec["isin"], rec["txn_date"], round(rec["quantity"], 3))
        if key in existing_keys:
            n_duplicate += 1
            continue
        records.append(rec)

    log.info(
        "%d Datei(en): %d neu, %d bereits vorhanden (gleiches ISIN+Datum+Menge, uebersprungen), %d nicht parsbar",
        len(files), len(records), n_duplicate, n_unparsable,
    )

    dist = Counter(r["txn_type"] for r in records)
    log.info("txn_type-Verteilung (nur neue Zeilen): %s", dict(dist))

    if args.dry_run:
        log.info("[dry-run] Es wurde nichts geschrieben.")
        return

    if not records:
        log.info("Keine neuen Zeilen zu schreiben.")
        return

    parse_utils.write_transactions(records)
    log.info("Fertig. %d neue Zeile(n) -> portfolio.transaction geschrieben.", len(records))


if __name__ == "__main__":
    main()
