"""
Transaktions-Loader: Trade Republic.

Liest den Trade-Republic-Transaktions-Export (","-getrennt, Standard-
Dezimalpunkt) und schreibt die volle Kontobewegungshistorie nach
portfolio.transaction. Der Export enthaelt neben Wertpapiergeschaeften auch
reine Bankaktivitaet (Kartenzahlungen, Zinsen) - alles wird geladen, damit
die Buchhaltung vollstaendig ist; welche Zeilen fuer die TWR-Berechnung
relevant sind, entscheidet phase12_transaction_twr.py anhand txn_type.

Vorzeichen-Regel: 'shares' ist in der Quelle bereits SIGNIERT (BUY positiv,
SELL negativ) - NICHT nochmal umdrehen (Gegenteil von Scalable!).

Aufruf:
    python parse_traderepublic_transactions.py [--file PFAD] [--dry-run]

Idempotent: mehrfacher Lauf ueberschreibt bestehende Zeilen (ON CONFLICT).
"""

import argparse
import logging
from collections import Counter
from pathlib import Path

import pandas as pd

import db_utils
import parse_utils

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

DEFAULT_DIR = Path(__file__).parent.parent / "Trades" / "Trade Republic"
ACCOUNT_NAME = "Christian Trade Republic"
SOURCE_SYSTEM = "traderepublic"

# (category, type) -> normalisierter txn_type. asset_class wird nicht als
# Unterscheidungsmerkmal gebraucht (BUY/SELL gilt fuer FUND/STOCK/DERIVATIVE
# gleichermassen).
TYPE_MAP = {
    ("TRADING", "BUY"):                        "BUY",
    ("TRADING", "SELL"):                       "SELL",
    ("CASH", "CARD_TRANSACTION"):               "CARD_TRANSACTION",
    ("CASH", "CARD_TRANSACTION_INTERNATIONAL"): "CARD_TRANSACTION",
    ("CASH", "CARD_ORDERING_FEE"):              "FEE",
    ("CASH", "CUSTOMER_INBOUND"):                "DEPOSIT",
    ("CASH", "CUSTOMER_INPAYMENT"):              "DEPOSIT",
    ("CASH", "VIBAN_TRANSFER_INBOUND"):          "CASH_TRANSFER_IN",
    ("CASH", "INTEREST_PAYMENT"):                "INTEREST",
    ("CASH", "BENEFITS_SAVEBACK"):               "CASHBACK",
    ("CASH", "CUSTOMER_OUTBOUND_REQUEST"):       "WITHDRAWAL",
    ("CASH", "TRANSFER_INSTANT_OUTBOUND"):       "CASH_TRANSFER_OUT",
    ("CASH", "TRANSFER_OUTBOUND"):               "CASH_TRANSFER_OUT",
    ("CASH", "DIVIDEND"):                        "DIVIDEND",
    ("CASH", "DISTRIBUTION"):                    "DIVIDEND",
    ("CASH", "TAX_OPTIMIZATION"):                "TAX",
    ("DELIVERY", "FREE_RECEIPT"):                "FREE_RECEIPT",
}


def find_default_files() -> list[Path]:
    """Alle CSV-Dateien im Ordner, aeltere zuerst. Sicher fuer mehrere/
    ueberlappende Exporte: parse_utils.write_transactions() dedupliziert
    ueber broker_ref, der neueste Export gewinnt bei ueberlappenden Zeilen."""
    candidates = sorted(DEFAULT_DIR.glob("*.csv"))
    if not candidates:
        raise SystemExit(f"Keine CSV-Datei in {DEFAULT_DIR} gefunden.")
    return candidates


def load_raw(file: Path) -> pd.DataFrame:
    df = pd.read_csv(file, dtype=str, encoding="utf-8")
    log.info("%d Zeilen roh gelesen aus %s", len(df), file.name)
    return df


def get_account_id() -> int:
    row = db_utils.query_df(
        "SELECT account_id FROM portfolio.account WHERE name = :name", {"name": ACCOUNT_NAME}
    )
    if row.empty:
        raise SystemExit(f"Depot '{ACCOUNT_NAME}' nicht in portfolio.account gefunden.")
    return int(row["account_id"].iloc[0])


def _num(raw) -> float | None:
    if raw is None or (isinstance(raw, float) and pd.isna(raw)):
        return None
    s = str(raw).strip()
    if s == "" or s.lower() == "nan":
        return None
    try:
        return float(s)
    except ValueError:
        log.warning("Zahl nicht parsbar: %r", raw)
        return None


def normalize(df: pd.DataFrame, source_file: str) -> list[dict]:
    account_id = get_account_id()

    unmapped_counter: Counter = Counter()
    records: list[dict] = []

    for _, row in df.iterrows():
        category = row["category"]
        kind = row["type"]
        txn_type = TYPE_MAP.get((category, kind))
        if txn_type is None:
            txn_type = "OTHER"
            unmapped_counter[(category, kind, row.get("asset_class") or "")] += 1

        isin = row["symbol"] if pd.notna(row["symbol"]) and str(row["symbol"]).strip() else None
        name = row["name"] if pd.notna(row["name"]) and str(row["name"]).strip() else None
        description = row["description"] if pd.notna(row["description"]) else None

        txn_date = pd.to_datetime(row["date"]).date()
        txn_datetime = pd.to_datetime(row["datetime"], utc=True).to_pydatetime()

        records.append(
            {
                "account_id": account_id,
                "broker_ref": row["transaction_id"],
                "source_system": SOURCE_SYSTEM,
                "txn_datetime": txn_datetime,
                "txn_date": txn_date,
                "txn_type": txn_type,
                "raw_category": f"{category}|{kind}|{row.get('asset_class') or ''}",
                "isin": isin,
                "security_name": name or description,
                "quantity": _num(row["shares"]),  # bereits signiert, KEIN Vorzeichenwechsel
                "price": _num(row["price"]),
                "gross_amount": _num(row["amount"]),
                "fee": _num(row["fee"]) or 0,
                "tax": _num(row["tax"]) or 0,
                "currency": row["currency"] if pd.notna(row["currency"]) and str(row["currency"]).strip() else "EUR",
                "original_amount": _num(row["original_amount"]),
                "original_currency": row["original_currency"] if pd.notna(row["original_currency"]) and str(row["original_currency"]).strip() else None,
                "fx_rate_applied": _num(row["fx_rate"]),
                "status": "Executed",
            }
        )

    if unmapped_counter:
        log.warning("Nicht gemappte (category,type,asset_class)-Kombinationen -> als OTHER geladen:")
        for combo, count in unmapped_counter.items():
            log.warning("  %s: %d Zeile(n)", combo, count)

    return records


def main() -> None:
    parser = argparse.ArgumentParser(description="Lade Trade-Republic-Transaktionen nach portfolio.transaction")
    parser.add_argument("--file", type=Path, default=None, help="Pfad zu einer einzelnen CSV-Datei (Standard: alle in Trades/Trade Republic/)")
    parser.add_argument("--dry-run", action="store_true", help="Nur einlesen und zusammenfassen, nicht schreiben")
    args = parser.parse_args()

    files = [args.file] if args.file else find_default_files()

    records: list[dict] = []
    for file in files:
        log.info("Lese %s ...", file)
        df = load_raw(file)
        records.extend(normalize(df, source_file=file.name))

    log.info("%d Transaktionszeile(n) normalisiert (aus %d Datei(en))", len(records), len(files))

    dist = Counter(r["txn_type"] for r in records)
    log.info("txn_type-Verteilung: %s", dict(dist))

    if args.dry_run:
        log.info("[dry-run] Es wurde nichts geschrieben.")
        return

    parse_utils.write_transactions(records)
    log.info("Fertig. %d Zeilen -> portfolio.transaction geschrieben.", len(records))


if __name__ == "__main__":
    main()
