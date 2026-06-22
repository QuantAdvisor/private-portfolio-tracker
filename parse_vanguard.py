"""
Phase 2 – Vanguard Holdings-Parser
Liest 'Aufschlüsselung der Positionen - Vanguard*.xlsx' aus ETF Holdings/ und schreibt:
  - portfolio.unresolved_holding  (kein ISIN in der Quelle, nur Ticker)

Die Vanguard-XLSX enthält keinen ISIN-Spalt für die Konstituenten (nur Ticker).
Daher Staging in unresolved_holding; Auflösung via ticker_isin_map separat.

Dateiformat (XLSX):
  Zeile 4: 'Per DD. MonatName YYYY'  →  as_of_date
  Zeile 6: Header  Ticker, Wertpapiere, % der Assets, Sektor, Region, Marktwert, Anteile
  Ab Zeile 7: Daten; Gewichtung als '5,1111\xa0%' (deutsches Dezimal, geschütztes Leerzeichen)

ETF-ISIN = IE00BKX55T58 (Vanguard FTSE Developed World, einzige Vanguard-Datei).

Hinweis: Vanguard aktualisiert die Holdings-Datei monatlich; das Datum kann
einen Monat hinter dem aktuellen Stand liegen.

Aufruf:
    python parse_vanguard.py
    python parse_vanguard.py --dry-run
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
ETF_ISIN = "IE00BKX55T58"

DE_MONTHS: dict[str, int] = {
    "Jan": 1, "Januar": 1,
    "Feb": 2, "Februar": 2,
    "Mär": 3, "März": 3,
    "Apr": 4, "April": 4,
    "Mai": 5,
    "Jun": 6, "Juni": 6,
    "Jul": 7, "Juli": 7,
    "Aug": 8, "August": 8,
    "Sep": 9, "Sept": 9, "September": 9,
    "Okt": 10, "Oktober": 10,
    "Nov": 11, "November": 11,
    "Dez": 12, "Dezember": 12,
}


def _parse_vanguard_date(raw: str) -> date:
    """Parst 'Per 31. Mai 2026' → date(2026, 5, 31)."""
    m = re.search(r"(\d{1,2})\.\s*([A-Za-zäöüÄÖÜß]+)\s+(\d{4})", raw)
    if not m:
        raise ValueError(f"Unbekanntes Vanguard-Datumsformat: {raw!r}")
    day, mon_str, year = int(m.group(1)), m.group(2), int(m.group(3))
    month = DE_MONTHS.get(mon_str) or DE_MONTHS.get(mon_str[:3].capitalize())
    if not month:
        raise ValueError(f"Unbekannter Monatsname: {mon_str!r}")
    return date(year, month, day)


def _parse_weight(raw: object) -> float | None:
    """Parst '5,1111\xa0%' → 5.1111."""
    if raw is None:
        return None
    s = str(raw).replace("\xa0", "").replace("%", "").replace(",", ".").strip()
    try:
        return round(float(s), 6)
    except ValueError:
        return None


def parse_file(path: Path) -> tuple[str, date, list[dict]]:
    """Gibt (etf_isin, as_of_date, unresolved_rows) zurück."""
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))
    wb.close()

    # Datum aus Zeile 4 (0-ind)
    date_raw = str(rows[4][0]).strip() if rows[4] and rows[4][0] else ""
    as_of = _parse_vanguard_date(date_raw)

    log.warning(
        "Vanguard Holdings-Datum: %s – möglicherweise einen Monat alt (Vanguard "
        "veröffentlicht erst am Monatsende).", as_of
    )

    unresolved: list[dict] = []
    skipped = 0

    # Daten ab Zeile 7 (0-ind); Zeile 6 = Header
    for row in rows[7:]:
        if not row or row[0] is None:
            continue
        # Spalten: 0=Ticker, 1=Wertpapiere, 2=% der Assets, 3=Sektor, 4=Region
        if len(row) < 3:
            skipped += 1
            continue

        ticker   = str(row[0]).strip() if row[0] else ""
        name     = str(row[1]).strip() if row[1] else ""
        weight_v = row[2]
        sektor   = str(row[3]).strip() if len(row) > 3 and row[3] else ""

        if not ticker or not name:
            skipped += 1
            continue

        weight_pct = _parse_weight(weight_v)
        if weight_pct is None or weight_pct <= 0:
            continue

        unresolved.append({
            "etf_isin":   ETF_ISIN,
            "as_of_date": as_of,
            "raw_ticker": ticker[:20],
            "raw_name":   name[:200],
            "weight_pct": weight_pct,
            "sektor":     sektor[:100] if sektor else None,
            "country":    None,  # Vanguard hat keine Länder-Spalte (nur Region)
            "currency":   None,
        })

    if skipped:
        log.debug("%s: %d Zeilen übersprungen", path.name, skipped)

    return ETF_ISIN, as_of, unresolved


def main() -> None:
    parser = argparse.ArgumentParser(description="Vanguard Holdings-Parser")
    parser.add_argument("--dry-run", action="store_true",
                        help="Nur parsen, nichts in DB schreiben")
    args = parser.parse_args()

    files = sorted(HOLDINGS_DIR.glob("Aufschlüsselung der Positionen - Vanguard*.xlsx"))
    if not files:
        log.error("Keine 'Aufschlüsselung der Positionen - Vanguard*.xlsx' in %s gefunden.",
                  HOLDINGS_DIR)
        return

    log.info("Gefunden: %d Vanguard-Dateien", len(files))

    all_unresolved: list[dict] = []

    for path in files:
        try:
            etf_isin, as_of, unresolved = parse_file(path)
            log.info("%-60s  ISIN=%-14s  Datum=%s  %d Positionen",
                     path.name[:60], etf_isin, as_of, len(unresolved))
            all_unresolved.extend(unresolved)
        except Exception as exc:
            log.error("%s – Fehler: %s", path.name, exc)

    parse_utils.log_weight_sums(all_unresolved)

    if args.dry_run:
        log.info("--dry-run: %d unresolved Holdings – nichts geschrieben.", len(all_unresolved))
        return

    log.info("Schreibe %d Holdings → portfolio.unresolved_holding …", len(all_unresolved))
    try:
        parse_utils.write_unresolved(all_unresolved)
        log.info("Fertig.")
    except Exception as exc:
        log.exception("DB-Fehler: %s", exc)


if __name__ == "__main__":
    main()
