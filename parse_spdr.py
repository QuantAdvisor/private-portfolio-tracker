"""
Phase 2 – SPDR Holdings-Parser
Liest holdings-daily-emea-en-spyd-gy*.xlsx aus ETF Holdings/ und schreibt:
  - portfolio.constituent  (Einzeltitel-Stammdaten, UPSERT)
  - portfolio.etf_holding  (Gewichte je ETF + Snapshot-Datum, UPSERT)

Dateiformat (XLSX):
  Zeile 0: Fund Name
  Zeile 1: ISIN  →  ETF-ISIN in Spalte B
  Zeile 2: Ticker
  Zeile 3: Holdings As Of  →  Datum in Spalte B (Format: '18-Jun-2026')
  Zeile 4: leer
  Zeile 5: Header  ISIN, SEDOL, Security Name, Currency, Number of Shares,
                   Percent of Fund, Trade Country Name, Local Price,
                   Sector Classification, Industry Classification, Base Market Value
  Ab Zeile 6: Daten; Percent of Fund = schon in Prozent (z. B. 2.344397)

Aufruf:
    python parse_spdr.py
    python parse_spdr.py --dry-run
"""

import argparse
import logging
import re
from datetime import date, datetime
from pathlib import Path

import openpyxl

import parse_utils

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

HOLDINGS_DIR = Path(__file__).parent / "ETF Holdings"

# Englische Monatskürzel (Jan, Feb, … aus SPDR-Datumsformat '18-Jun-2026')
EN_MONTHS = {
    "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
    "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12,
}


def _parse_spdr_date(raw: str) -> date:
    """Parst '18-Jun-2026' → date(2026, 6, 18)."""
    m = re.match(r"(\d{1,2})-([A-Za-z]+)-(\d{4})", raw.strip())
    if not m:
        raise ValueError(f"Unbekanntes SPDR-Datumsformat: {raw!r}")
    day, mon_str, year = int(m.group(1)), m.group(2), int(m.group(3))
    month = EN_MONTHS.get(mon_str[:3].capitalize())
    if not month:
        raise ValueError(f"Unbekannter Monatsname: {mon_str!r}")
    return date(year, month, day)


def parse_file(path: Path) -> tuple[str, date, list[dict], list[dict]]:
    """Gibt (etf_isin, as_of_date, constituent_rows, holding_rows) zurück."""
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb.active

    rows = list(ws.iter_rows(values_only=True))
    wb.close()

    # Zeile 1 (0-ind): ISIN-Zeile; Spalte B (Index 1)
    etf_isin = str(rows[1][1]).strip() if rows[1][1] else ""
    if not re.match(r"[A-Z]{2}[A-Z0-9]{10}", etf_isin):
        raise ValueError(f"Keine gültige ETF-ISIN in Zeile 1: {etf_isin!r}")

    # Zeile 3 (0-ind): Holdings As Of; Spalte B
    date_raw = str(rows[3][1]).strip() if rows[3][1] else ""
    as_of = _parse_spdr_date(date_raw)

    # Datenzeilen ab Index 6 (Zeile 5 = Header)
    constituents: list[dict] = []
    holdings: list[dict] = []
    skipped = 0

    for row in rows[6:]:
        if not row or row[0] is None:
            continue
        # Spalten: 0=ISIN, 1=SEDOL, 2=Security Name, 3=Currency,
        #          4=NumShares, 5=Percent of Fund, 6=Trade Country Name,
        #          7=Local Price, 8=Sector, 9=Industry, 10=Base Market Value
        isin     = str(row[0]).strip() if row[0] else ""
        name     = str(row[2]).strip() if row[2] else ""
        currency = str(row[3]).strip() if row[3] else ""
        weight_v = row[5]
        country  = str(row[6]).strip() if row[6] else ""
        sektor   = str(row[8]).strip() if row[8] else ""

        if not re.match(r"[A-Z]{2}[A-Z0-9]{10}", isin):
            skipped += 1
            continue
        if not name:
            skipped += 1
            continue
        try:
            weight_pct = round(float(weight_v), 6)
        except (ValueError, TypeError):
            skipped += 1
            continue
        if weight_pct <= 0:
            continue

        constituents.append({
            "isin":     isin,
            "name":     name[:200],
            "sektor":   sektor[:100] if sektor else None,
            "country":  parse_utils.normalize_country_en(country),
            "currency": currency[:3] if currency else None,
        })
        holdings.append({
            "etf_isin":         etf_isin,
            "as_of_date":       as_of,
            "constituent_isin": isin,
            "weight_pct":       weight_pct,
            "source_file":      path.name,
        })

    if skipped:
        log.debug("%s: %d Zeilen übersprungen", path.name, skipped)

    return etf_isin, as_of, constituents, holdings


def main() -> None:
    parser = argparse.ArgumentParser(description="SPDR Holdings-Parser")
    parser.add_argument("--dry-run", action="store_true",
                        help="Nur parsen, nichts in DB schreiben")
    args = parser.parse_args()

    files = sorted(HOLDINGS_DIR.glob("holdings-daily-emea-en-spyd-gy*.xlsx"))
    if not files:
        log.error("Keine holdings-daily-emea-en-spyd-gy*.xlsx in %s gefunden.", HOLDINGS_DIR)
        return

    log.info("Gefunden: %d SPDR-Dateien", len(files))

    all_constituents: list[dict] = []
    all_holdings: list[dict] = []

    for path in files:
        try:
            etf_isin, as_of, constituents, holdings = parse_file(path)
            log.info("%-60s  ISIN=%-14s  Datum=%s  %d Positionen",
                     path.name[:60], etf_isin, as_of, len(holdings))
            all_constituents.extend(constituents)
            all_holdings.extend(holdings)
        except Exception as exc:
            log.error("%s – Fehler: %s", path.name, exc)

    parse_utils.log_weight_sums(all_holdings)

    if args.dry_run:
        log.info("--dry-run: %d unique Konstituenten, %d Holdings – nichts geschrieben.",
                 len({c["isin"] for c in all_constituents}), len(all_holdings))
        return

    log.info("Schreibe %d unique Konstituenten + %d Holdings …",
             len({c["isin"] for c in all_constituents}), len(all_holdings))
    try:
        parse_utils.write_constituents_and_holdings(all_constituents, all_holdings)
        log.info("Fertig.")
    except Exception as exc:
        log.exception("DB-Fehler: %s", exc)


if __name__ == "__main__":
    main()
