"""
Phase 2 – L&G Holdings-Parser
Liest Fund-holdings_LG-*.csv aus ETF Holdings/ und schreibt:
  - portfolio.constituent  (Einzeltitel-Stammdaten, UPSERT)
  - portfolio.etf_holding  (Gewichte je ETF + Snapshot-Datum, UPSERT)

Dateiformat (utf-8):
  Zeile 0: 'sep=,'           (Excel-Hinweis, ignorieren)
  Zeile 1: 'Basket Name<name>'
  Zeile 2: 'ETF Trading ID<isin>'  →  ETF-ISIN via Regex
  Zeile 3: 'Basket Trade Date<YYYY-MM-DD>'  →  Datum via Regex
  Zeile 4: Header  Security Description,ISIN,Trading Currency,Constituent Weight (Base)
  Ab Zeile 5: Daten; Gewichtung = Dezimalbruch (0.002 = 0,2 %)

Gewichtung wird mit *100 in Prozent umgerechnet (weight_pct 0–100).

Aufruf:
    python parse_lg.py
    python parse_lg.py --dry-run
"""

import argparse
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


def parse_file(path: Path) -> tuple[str, date, list[dict], list[dict]]:
    """Gibt (etf_isin, as_of_date, constituent_rows, holding_rows) zurück."""
    lines = path.read_text(encoding="utf-8").splitlines()

    # ETF-ISIN aus Zeile 2: Wert steht direkt am Ende ("ETF Trading IDIE0005AJA0P1")
    # ISIN ist immer die letzten 12 Zeichen, daher kein Regex (würde "ID" + 10 Zeichen matchen)
    etf_isin = lines[2].strip()[-12:]
    if not re.match(r"[A-Z]{2}[A-Z0-9]{10}$", etf_isin):
        raise ValueError(f"Keine gültige ETF-ISIN in Zeile 2: {lines[2]!r}")

    # Datum aus Zeile 3 (ISO-Format YYYY-MM-DD)
    date_m = re.search(r"(\d{4}-\d{2}-\d{2})", lines[3])
    if not date_m:
        raise ValueError(f"Kein Datum in Zeile 3: {lines[3]!r}")
    as_of = date.fromisoformat(date_m.group(1))

    constituents: list[dict] = []
    holdings: list[dict] = []
    skipped = 0

    # Daten ab Zeile 5 (Zeile 4 = Header)
    for raw in lines[5:]:
        raw = raw.strip()
        if not raw:
            continue
        parts = raw.split(",", 3)
        if len(parts) < 4:
            skipped += 1
            continue

        name, isin, currency, weight_str = parts
        name     = name.strip().strip('"')
        isin     = isin.strip().strip('"')
        currency = currency.strip().strip('"')
        weight_s = weight_str.strip().strip('"')

        if not re.match(r"[A-Z]{2}[A-Z0-9]{10}", isin):
            skipped += 1
            continue
        try:
            weight_pct = round(float(weight_s) * 100, 6)
        except ValueError:
            skipped += 1
            continue
        if weight_pct <= 0:
            continue

        constituents.append({
            "isin":     isin,
            "name":     name[:200],
            "sektor":   None,
            "country":  None,
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
    parser = argparse.ArgumentParser(description="L&G Holdings-Parser")
    parser.add_argument("--dry-run", action="store_true",
                        help="Nur parsen, nichts in DB schreiben")
    args = parser.parse_args()

    files = sorted(HOLDINGS_DIR.glob("Fund-holdings_LG-*.csv"))
    if not files:
        log.error("Keine Fund-holdings_LG-*.csv in %s gefunden.", HOLDINGS_DIR)
        return

    log.info("Gefunden: %d L&G-Dateien", len(files))

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
