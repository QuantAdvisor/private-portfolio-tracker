"""
Phase 2 – Amundi Holdings-Parser
Liest alle Fondszusammensetzung_Amundi*.xlsx aus ETF Holdings/ und schreibt:
  - portfolio.constituent  (Einzeltitel-Stammdaten, UPSERT)
  - portfolio.etf_holding  (Gewichte je ETF + Snapshot-Datum, UPSERT)

Dateiformat: OOXML (.xlsx) mit kaputtem Stylesheet → openpyxl crasht.
Lösung: Rohdaten via zipfile + xml.etree.ElementTree lesen.

Layout (0-indiziert):
  Zeile 8:  B="ISIN", C=<ETF-ISIN>
  Zeile 11: enthält Datum "... zum DD/MM/YYYY"
  Zeile 14: Header  B=ISIN  C=Name  D=Anlageklasse  E=Währung  F=Gewichtung  G=Sektor  H=Land
  Ab Zeile 15: Daten; Gewichtung = Dezimalbruch (0,046 = 4,6 %)

Gewichtung wird mit *100 in Prozent umgerechnet (weight_pct 0–100).

Aufruf:
    python parse_amundi.py
    python parse_amundi.py --dry-run
"""

import argparse
import logging
import re
import xml.etree.ElementTree as ET
import zipfile
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
NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"


# ── Rohdaten aus OOXML lesen ──────────────────────────────────────────────────

def _read_shared_strings(z: zipfile.ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in z.namelist():
        return []
    with z.open("xl/sharedStrings.xml") as f:
        root = ET.parse(f).getroot()
    result = []
    for si in root.findall(f"{{{NS}}}si"):
        texts = [t.text or "" for t in si.findall(f".//{{{NS}}}t")]
        result.append("".join(texts))
    return result


def _read_sheet(z: zipfile.ZipFile, shared: list[str]) -> list[dict[str, object]]:
    """Gibt Zeilen als Dict {Spaltenbuchstabe: Wert} zurück (nur nicht-leere Zellen)."""
    with z.open("xl/worksheets/sheet1.xml") as f:
        root = ET.parse(f).getroot()
    rows = root.findall(f".//{{{NS}}}row")
    result = []
    for row in rows:
        cells = row.findall(f"{{{NS}}}c")
        row_data: dict[str, object] = {}
        for c in cells:
            ref = c.get("r", "")
            col = re.sub(r"[0-9]", "", ref)
            t = c.get("t", "")
            v = c.findtext(f"{{{NS}}}v")
            if t == "s" and v is not None and shared:
                val: object = shared[int(v)]
            elif v is not None:
                try:
                    val = float(v)
                except ValueError:
                    val = v
            else:
                val = ""
            row_data[col] = val
        if row_data:
            result.append(row_data)
    return result


# ── Datei parsen ─────────────────────────────────────────────────────────────

def _parse_date(rows: list[dict]) -> date:
    """Extrahiert Datum aus Zeile 11 (0-ind): 'Verwaltetes Vermögen ... zum DD/MM/YYYY'."""
    if len(rows) > 11:
        text = str(rows[11].get("B", ""))
        m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", text)
        if m:
            return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
    raise ValueError("Datum nicht gefunden (Zeile 11 fehlt oder falsches Format)")


def parse_file(path: Path) -> tuple[str, date, list[dict], list[dict]]:
    """Gibt (etf_isin, as_of_date, constituent_rows, holding_rows) zurück."""
    with zipfile.ZipFile(path) as z:
        shared = _read_shared_strings(z)
        rows = _read_sheet(z, shared)

    # ETF-ISIN aus Zeile 8 (0-ind), Spalte C
    etf_isin = str(rows[8].get("C", "")).strip()
    if not re.match(r"[A-Z]{2}[A-Z0-9]{10}", etf_isin):
        raise ValueError(f"Keine gültige ETF-ISIN in Zeile 8: {etf_isin!r}")

    as_of = _parse_date(rows)

    # Datenzeilen ab Index 15 (Row 14 = Header, Row 15+ = Daten)
    constituents: list[dict] = []
    holdings: list[dict] = []
    skipped = 0

    for row in rows[15:]:
        isin     = str(row.get("B", "")).strip()
        name     = str(row.get("C", "")).strip()
        asset    = str(row.get("D", "")).strip()
        currency = str(row.get("E", "")).strip()
        weight_v = row.get("F", "")
        sektor   = str(row.get("G", "")).strip() or None
        land     = str(row.get("H", "")).strip() or None

        # Nur Aktien (EQUITY), Zeilen ohne ISIN oder Gewicht überspringen
        if asset.upper() not in ("EQUITY", "AKTIEN"):
            skipped += 1
            continue
        if not re.match(r"[A-Z]{2}[A-Z0-9]{10}", isin):
            skipped += 1
            continue
        try:
            weight_pct = round(float(weight_v) * 100, 6)
        except (ValueError, TypeError):
            skipped += 1
            continue
        if weight_pct <= 0:
            skipped += 1
            continue

        constituents.append({
            "isin":     isin,
            "name":     name[:200],
            "sektor":   sektor[:100] if sektor else None,
            "country":  parse_utils.normalize_country_de(land),
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
        log.debug("%s: %d Zeilen übersprungen (kein EQUITY / kein ISIN / kein Gewicht)",
                  path.name, skipped)

    return etf_isin, as_of, constituents, holdings


# ── Hauptprogramm ─────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Amundi Holdings-Parser")
    parser.add_argument("--dry-run", action="store_true",
                        help="Nur parsen, nichts in DB schreiben")
    args = parser.parse_args()

    files = sorted(HOLDINGS_DIR.glob("Fondszusammensetzung_Amundi*.xlsx"))
    if not files:
        log.error("Keine Fondszusammensetzung_Amundi*.xlsx in %s gefunden.", HOLDINGS_DIR)
        return

    log.info("Gefunden: %d Amundi-Dateien", len(files))

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
