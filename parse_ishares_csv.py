"""
Phase 2 – iShares CSV Holdings-Parser
Liest alle *_holdings.csv aus ETF Holdings/ (iShares-Format) und schreibt:
  - portfolio.unresolved_holding  (kein ISIN in der Quelle, nur Ticker)

Die iShares-CSVs enthalten keinen ISIN-Spalt für die Konstituenten (nur
Exchange-Ticker wie 'AAPL', '7267', '005930'). Daher Staging in
unresolved_holding; Auflösung zu constituent + etf_holding separat via
ticker_isin_map sobald diese befüllt ist.

Dateiformat (utf-8-sig, Komma-Trenner):
  Zeile 0: 'Fondsposition per,"19.Juni2026"'  →  Datum via Regex
  Zeile 1: '\xa0'  (leer)
  Zeile 2: Header  Emittententicker,Name,Sektor,Anlageklasse,Marktwert,
                   Gewichtung (%),Nominalwert,Nominale,Kurs,Standort,Börse,Marktwährung
  Ab Zeile 3: Daten

Ticker-zu-ETF-ISIN: TICKER_TO_ISIN-Dict (Dateiname-Ticker → ETF-ISIN).
Gewichtung in CSV: deutsches Dezimal ("10,30" = 10,30 %).
Anlageklasse-Filter: nur "Aktien" (überspringt Barmittel, Futures, etc.).

Aufruf:
    python parse_ishares_csv.py
    python parse_ishares_csv.py --dry-run
"""

import argparse
import csv
import logging
import re
from datetime import date
from pathlib import Path

import parse_utils

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

HOLDINGS_DIR = Path(__file__).parent / "ETF Holdings"

# Dateiname-Ticker → ETF-ISIN
# EUNM = IE00B4L5YC18 (IS3N auf Xetra, EUNM auf der London Stock Exchange)
TICKER_TO_ISIN: dict[str, str] = {
    "CEMR": "IE00B53QG562",
    "EUN0": "IE00B86MWN23",
    "EUNM": "IE00B4L5YC18",
    "EUNY": "IE00B652H904",
    "EUNZ": "IE00B8KGV557",
    "EXID": "DE000A2QP349",
    "IQQ0": "IE00BKVL7331",
    "IQQD": "IE00B0M63060",
    "IQQX": "IE00B14X4T88",
    "IS3Q": "IE00BP3QZ601",
    "IS3R": "IE00BP3QZ825",
    "IUSN": "IE00BF4RFH31",
    "MVEA": "IE00B8FHGS14",
    "SXR7": "IE00BQN1K786",
}

# Deutsche Monatsnamen → Monatsnummer
DE_MONTHS: dict[str, int] = {
    "Jan": 1, "Januar": 1,
    "Feb": 2, "Februar": 2,
    "Mär": 3, "März": 3, "Mar": 3,
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


def _ticker_from_filename(path: Path) -> str | None:
    """Extrahiert Ticker aus 'IQQX_holdings.csv' oder 'MVEA_holdings (1).csv'."""
    m = re.match(r"([A-Z0-9]+)_holdings", path.name)
    return m.group(1) if m else None


def _parse_de_date(raw: str) -> date:
    """Parst deutsches Datum '19.Juni2026' → date(2026, 6, 19)."""
    m = re.search(r'(\d{1,2})\.([A-Za-zäöüÄÖÜß]+)(\d{4})', raw)
    if not m:
        raise ValueError(f"Unbekanntes iShares-Datumsformat: {raw!r}")
    day, mon_str, year = int(m.group(1)), m.group(2), int(m.group(3))
    month = DE_MONTHS.get(mon_str) or DE_MONTHS.get(mon_str[:3].capitalize())
    if not month:
        raise ValueError(f"Unbekannter Monatsname: {mon_str!r}")
    return date(year, month, day)


def _parse_de_float(s: str) -> float | None:
    """Parst '10,30' oder '10.30' → 10.30."""
    try:
        return float(s.replace(",", ".").strip())
    except ValueError:
        return None


def parse_file(path: Path) -> tuple[str, date, list[dict]]:
    """Gibt (etf_isin, as_of_date, unresolved_rows) zurück."""
    ticker = _ticker_from_filename(path)
    if not ticker:
        raise ValueError(f"Ticker nicht aus Dateiname ableitbar: {path.name}")
    etf_isin = TICKER_TO_ISIN.get(ticker)
    if not etf_isin:
        raise ValueError(f"Kein ISIN-Mapping für Ticker {ticker!r} (Datei: {path.name})")

    # Rohdaten: erste Zeile für Datum lesen, dann CSV ab Zeile 3
    raw_lines = path.read_text(encoding="utf-8-sig").splitlines()
    as_of = _parse_de_date(raw_lines[0])

    # CSV ab Zeile 2 (Header) einlesen
    reader = csv.DictReader(raw_lines[2:])

    unresolved: list[dict] = []
    skipped = 0

    for row in reader:
        ticker_c  = (row.get("Emittententicker") or "").strip().strip('"')
        name      = (row.get("Name") or "").strip().strip('"')
        sektor    = (row.get("Sektor") or "").strip().strip('"')
        asset     = (row.get("Anlageklasse") or "").strip().strip('"')
        weight_s  = (row.get("Gewichtung (%)") or "").strip().strip('"')
        standort  = (row.get("Standort") or "").strip().strip('"')
        currency  = (row.get("Marktwährung") or "").strip().strip('"')

        # Nur Aktien laden (Barmittel, Futures etc. überspringen)
        if asset.lower() not in ("aktien", "aktie"):
            skipped += 1
            continue
        if not ticker_c or not name:
            skipped += 1
            continue

        weight_pct = _parse_de_float(weight_s)
        if weight_pct is None or weight_pct <= 0:
            continue

        unresolved.append({
            "etf_isin":   etf_isin,
            "as_of_date": as_of,
            "raw_ticker": ticker_c[:20],
            "raw_name":   name[:200],
            "weight_pct": weight_pct,
            "sektor":     sektor[:100] if sektor else None,
            "country":    parse_utils.normalize_country_de(standort),
            "currency":   currency[:3] if currency else None,
        })

    if skipped:
        log.debug("%s: %d Zeilen übersprungen (kein Aktien/kein Ticker)", path.name, skipped)

    return etf_isin, as_of, unresolved


def main() -> None:
    parser = argparse.ArgumentParser(description="iShares CSV Holdings-Parser")
    parser.add_argument("--dry-run", action="store_true",
                        help="Nur parsen, nichts in DB schreiben")
    args = parser.parse_args()

    # Alle *_holdings.csv (aber nicht Xtrackers Constituent_*.xlsx)
    files = sorted(HOLDINGS_DIR.glob("*_holdings.csv"))
    if not files:
        log.error("Keine *_holdings.csv in %s gefunden.", HOLDINGS_DIR)
        return

    log.info("Gefunden: %d iShares-CSV-Dateien", len(files))

    all_unresolved: list[dict] = []
    seen_tickers: set[str] = set()  # Duplikat-Erkennung (z. B. MVEA + MVEA (1))

    for path in files:
        ticker = _ticker_from_filename(path)
        try:
            etf_isin, as_of, unresolved = parse_file(path)
            dup_marker = " (Duplikat – übersprungen)" if ticker in seen_tickers else ""
            log.info("%-35s  ISIN=%-14s  Datum=%s  %d Positionen%s",
                     path.name[:35], etf_isin, as_of, len(unresolved), dup_marker)
            if ticker not in seen_tickers:
                seen_tickers.add(ticker)
                all_unresolved.extend(unresolved)
        except Exception as exc:
            log.error("%s – Fehler: %s", path.name, exc)

    # Gewicht-Summen prüfen (unresolved_holding hat kein weight_pct-Key-Unterschied)
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
