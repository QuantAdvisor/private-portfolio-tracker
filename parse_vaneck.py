"""
Phase 2 – VanEck Holdings-Parser
Liest TDIV_Stand_*.xlsx aus ETF Holdings/ und schreibt:
  - portfolio.constituent  (Einzeltitel-Stammdaten, UPSERT)
  - portfolio.etf_holding  (Gewichte je ETF + Snapshot-Datum, UPSERT)

Dateiformat (XLSX):
  Zeile 0: 'Alle Fondspositionen Stand MM.YYYY'  (Datum im Dateinamen zuverlässiger)
  Zeile 1: leer
  Zeile 2: Header  Position, Bezeichnung der Position, Ticker, ISIN, Anteile, Marktwert, % des Fondsvolumens
  Ab Zeile 3: Daten; Gewichtung als String '4,84%' (deutsches Dezimal + Prozentzeichen)

ETF-ISIN = NL0011683594 (VanEck TDIV, einzige VanEck-Datei).
as_of_date aus Dateiname: TDIV_Stand_YYYYMMDD.xlsx

Aufruf:
    python parse_vaneck.py
    python parse_vaneck.py --dry-run
"""

import argparse
import logging
import re
from datetime import date
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
ETF_ISIN = "NL0011683594"


def _parse_weight(raw: object) -> float | None:
    """Parst '4,84%' oder 4.84 als Prozentzahl."""
    if raw is None:
        return None
    s = str(raw).replace("\xa0", "").replace("%", "").replace(",", ".").strip()
    try:
        return round(float(s), 6)
    except ValueError:
        return None


def _date_from_filename(path: Path) -> date:
    """Extrahiert Datum aus 'TDIV_Stand_20260619.xlsx' → date(2026, 6, 19)."""
    m = re.search(r"(\d{8})", path.name)
    if m:
        raw = m.group(1)
        return date(int(raw[:4]), int(raw[4:6]), int(raw[6:8]))
    raise ValueError(f"Kein Datum im Dateinamen: {path.name}")


def parse_file(path: Path) -> tuple[str, date, list[dict], list[dict]]:
    """Gibt (etf_isin, as_of_date, constituent_rows, holding_rows) zurück."""
    as_of = _date_from_filename(path)

    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))
    wb.close()

    constituents: list[dict] = []
    holdings: list[dict] = []
    skipped = 0

    # Daten ab Zeile 3 (0-ind); Zeile 2 = Header
    for row in rows[3:]:
        if not row or row[0] is None:
            continue
        # Spalten: 0=Position(lfd), 1=Bezeichnung, 2=Ticker, 3=ISIN,
        #          4=Anteile, 5=Marktwert, 6=% des Fondsvolumens
        if len(row) < 7:
            skipped += 1
            continue

        name     = str(row[1]).strip() if row[1] else ""
        isin     = str(row[3]).strip() if row[3] else ""
        weight_v = row[6]
        currency = None  # VanEck exportiert keine Currency-Spalte

        if not re.match(r"[A-Z]{2}[A-Z0-9]{10}", isin):
            skipped += 1
            continue
        weight_pct = _parse_weight(weight_v)
        if weight_pct is None:
            skipped += 1
            continue
        if weight_pct <= 0:
            continue

        constituents.append({
            "isin":     isin,
            "name":     name[:200],
            "sektor":   None,
            "country":  None,
            "currency": currency,
        })
        holdings.append({
            "etf_isin":         ETF_ISIN,
            "as_of_date":       as_of,
            "constituent_isin": isin,
            "weight_pct":       weight_pct,
            "source_file":      path.name,
        })

    if skipped:
        log.debug("%s: %d Zeilen übersprungen", path.name, skipped)

    return ETF_ISIN, as_of, constituents, holdings


def main() -> None:
    parser = argparse.ArgumentParser(description="VanEck Holdings-Parser")
    parser.add_argument("--dry-run", action="store_true",
                        help="Nur parsen, nichts in DB schreiben")
    args = parser.parse_args()

    files = sorted(HOLDINGS_DIR.glob("TDIV_Stand_*.xlsx"))
    if not files:
        log.error("Keine TDIV_Stand_*.xlsx in %s gefunden.", HOLDINGS_DIR)
        return

    log.info("Gefunden: %d VanEck-Dateien", len(files))

    all_constituents: list[dict] = []
    all_holdings: list[dict] = []

    for path in files:
        try:
            etf_isin, as_of, constituents, holdings = parse_file(path)
            log.info("%-40s  ISIN=%-14s  Datum=%s  %d Positionen",
                     path.name, etf_isin, as_of, len(holdings))
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
