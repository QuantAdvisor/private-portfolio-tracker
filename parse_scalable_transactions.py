"""
Transaktions-Loader: Scalable Capital.

Liest den Broker-Transaktions-Export (";"-getrennt, deutsches Zahlenformat
mit Komma als Dezimaltrennzeichen und Punkt als Tausendertrennzeichen) und
schreibt die volle Kontobewegungshistorie nach portfolio.transaction.

Nur status == 'Executed' wird geladen (Cancelled/Rejected werden geloggt,
nicht geschrieben).

Vorzeichen-Regel (wichtigste Fehlerquelle bei diesem Broker): 'shares' ist
in der Quelle UNSIGNIERT, ausser bei 'Security transfer' (dort bereits
signiert). Also: quantity = -shares bei SELL, sonst quantity = shares
unveraendert. 'amount' ist in der Quelle bereits korrekt signiert.

Aufruf:
    python parse_scalable_transactions.py [--file PFAD] [--dry-run]

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

DEFAULT_DIR = Path(__file__).parent.parent / "Trades" / "Scalable"
ACCOUNT_NAME = "Scalable Christian"
SOURCE_SYSTEM = "scalable"

# (assetType, type) -> normalisierter txn_type. 'Security transfer' wird
# separat behandelt (Vorzeichen von 'shares' entscheidet IN/OUT).
TYPE_MAP = {
    ("Security", "Savings plan"):              "BUY",
    ("Security", "Buy"):                       "BUY",
    ("Security", "Sell"):                      "SELL",
    ("Security", "Reinvestment_Distribution"): "DRIP",
    ("Cash", "Distribution"):                  "DIVIDEND",
    ("Cash", "Deposit"):                       "DEPOSIT",
    ("Cash", "Withdrawal"):                    "WITHDRAWAL",
    ("Cash", "Fee"):                           "FEE",
    ("Cash", "Interest"):                      "INTEREST",
    ("Cash", "Cash Transfer In"):               "CASH_TRANSFER_IN",
    ("Cash", "Cash Transfer Out"):              "CASH_TRANSFER_OUT",
    ("Cash", "Taxes"):                          "TAX",
}


def _parse_de_number(raw) -> float | None:
    """Deutsches Zahlenformat: Punkt = Tausendertrennzeichen, Komma = Dezimal."""
    if raw is None:
        return None
    s = str(raw).strip().replace("\xa0", "")
    if s == "":
        return None
    s = s.replace(".", "").replace(",", ".")
    try:
        return float(s)
    except ValueError:
        log.warning("Zahl nicht parsbar: %r", raw)
        return None


def find_default_files() -> list[Path]:
    """Alle CSV-Dateien im Ordner, aeltere zuerst. Sicher fuer mehrere/
    ueberlappende Exporte: parse_utils.write_transactions() dedupliziert
    ueber broker_ref, der neueste Export gewinnt bei ueberlappenden Zeilen."""
    candidates = sorted(DEFAULT_DIR.glob("*.csv"))
    if not candidates:
        raise SystemExit(f"Keine CSV-Datei in {DEFAULT_DIR} gefunden.")
    return candidates


def load_raw(file: Path) -> pd.DataFrame:
    df = pd.read_csv(file, sep=";", quotechar='"', dtype=str, encoding="utf-8")
    log.info("%d Zeilen roh gelesen aus %s", len(df), file.name)
    return df


def get_account_id() -> int:
    row = db_utils.query_df(
        "SELECT account_id FROM portfolio.account WHERE name = :name", {"name": ACCOUNT_NAME}
    )
    if row.empty:
        raise SystemExit(f"Depot '{ACCOUNT_NAME}' nicht in portfolio.account gefunden.")
    return int(row["account_id"].iloc[0])


def normalize(df: pd.DataFrame, source_file: str) -> list[dict]:
    account_id = get_account_id()

    executed = df[df["status"] == "Executed"].copy()
    skipped = len(df) - len(executed)
    if skipped:
        log.info("%d Zeile(n) mit status != 'Executed' uebersprungen (Cancelled/Rejected)", skipped)

    unmapped_counter: Counter = Counter()
    records: list[dict] = []

    for _, row in executed.iterrows():
        asset_type = row["assetType"]
        txn_kind = row["type"]

        if (asset_type, txn_kind) == ("Security", "Security transfer"):
            shares = _parse_de_number(row["shares"])
            txn_type = "TRANSFER_SECURITY_IN" if (shares or 0) >= 0 else "TRANSFER_SECURITY_OUT"
        else:
            txn_type = TYPE_MAP.get((asset_type, txn_kind))
            if txn_type is None:
                txn_type = "OTHER"
                unmapped_counter[(asset_type, txn_kind)] += 1

        shares = _parse_de_number(row["shares"])
        quantity = None
        if shares is not None:
            quantity = -shares if txn_type == "SELL" else shares

        txn_date = pd.to_datetime(row["date"]).date()
        txn_datetime = pd.Timestamp(f"{row['date']} {row['time']}")
        try:
            txn_datetime = txn_datetime.tz_localize(
                "Europe/Berlin", ambiguous="NaT", nonexistent="shift_forward"
            )
        except Exception:
            txn_datetime = txn_datetime.tz_localize("UTC")

        records.append(
            {
                "account_id": account_id,
                "broker_ref": row["reference"],
                "source_system": SOURCE_SYSTEM,
                "txn_datetime": txn_datetime.to_pydatetime(),
                "txn_date": txn_date,
                "txn_type": txn_type,
                "raw_category": f"{asset_type}|{txn_kind}",
                "isin": row["isin"] if pd.notna(row["isin"]) and row["isin"].strip() else None,
                "security_name": row["description"] if pd.notna(row["description"]) else None,
                "quantity": quantity,
                "price": _parse_de_number(row["price"]),
                "gross_amount": _parse_de_number(row["amount"]),
                "fee": _parse_de_number(row["fee"]) or 0,
                "tax": _parse_de_number(row["tax"]) or 0,
                "currency": row["currency"],
                "original_amount": None,
                "original_currency": None,
                "fx_rate_applied": None,
                "status": row["status"],
            }
        )

    if unmapped_counter:
        log.warning("Nicht gemappte (assetType,type)-Kombinationen -> als OTHER geladen:")
        for combo, count in unmapped_counter.items():
            log.warning("  %s: %d Zeile(n)", combo, count)

    return records


def main() -> None:
    parser = argparse.ArgumentParser(description="Lade Scalable-Transaktionen nach portfolio.transaction")
    parser.add_argument("--file", type=Path, default=None, help="Pfad zu einer einzelnen CSV-Datei (Standard: alle in Trades/Scalable/)")
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
