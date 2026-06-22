"""
Phase 2 – ISIN-Resolver für unresolved_holding
Löst die Ticker in portfolio.unresolved_holding zu ISINs auf und überführt
die aufgelösten Zeilen in portfolio.etf_holding.

Zwei Schritte:
  1. Name-Match:  raw_name = constituent.name  (sofort, kein API-Call)
  2. yfinance:    Ticker + Ländersuffix → isin  (parallel, ~3-5 Min für 7000 Tickers)

Ergebnisse landen in:
  - portfolio.ticker_isin_map   (raw_ticker → constituent.isin, für Wiederverwertung)
  - portfolio.constituent       (ggf. neuer Eintrag wenn nur via yfinance bekannt)
  - portfolio.etf_holding       (aufgelöste Zeilen)

Aufruf:
    python resolve_isins.py --dry-run
    python resolve_isins.py                  # Step 1 (Name-Match) + Step 2 (yfinance)
    python resolve_isins.py --name-only      # Nur Step 1 (schnell)
    python resolve_isins.py --yf-only        # Nur Step 2 (yfinance)
    python resolve_isins.py --workers 20     # Parallelisierung (Default 10)
"""

import argparse
import logging
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from typing import Optional

import yfinance as yf

import db_utils
import parse_utils

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Exchange-Suffix je Land (ISO-2 → yfinance-Suffix) ────────────────────────
# Für US-Aktien kein Suffix nötig. Bei Fehlschlag: kein Suffix versuchen.
COUNTRY_YF_SUFFIX: dict[str, str] = {
    "US": "",
    "JP": ".T",
    "KR": ".KS",
    "AU": ".AX",
    "HK": ".HK",
    "TW": ".TW",
    "SG": ".SI",
    "IN": ".NS",
    "CN": ".SS",    # Shanghai; Shenzhen: .SZ – nehmen erstmal Shanghai
    "MY": ".KL",
    "ID": ".JK",
    "TH": ".BK",
    "PH": ".PS",
    "NZ": ".NZ",
    "GB": ".L",
    "IE": ".IR",
    "DE": ".DE",
    "FR": ".PA",
    "NL": ".AS",
    "BE": ".BR",
    "IT": ".MI",
    "ES": ".MC",
    "SE": ".ST",
    "NO": ".OL",
    "DK": ".CO",
    "FI": ".HE",
    "CH": ".SW",
    "AT": ".VI",
    "PT": ".LS",
    "GR": ".AT",
    "PL": ".WA",
    "LU": "",       # Luxemburg meist US oder IE gelistet
    "IL": ".TA",
    "ZA": ".JO",
    "BR": ".SA",
    "MX": ".MX",
    "CA": ".TO",
    "QA": ".QA",
    "AE": ".DU",
    "SA": ".SR",
    "DK": ".CO",
    "TR": ".IS",
    "HU": ".BD",
    "CZ": ".PR",
    "RO": ".RO",
    "KE": ".NR",
    "PK": ".KA",
    "PE": ".LM",
    "CL": ".SN",
    "CO": ".BC",
}

# ── SQL ───────────────────────────────────────────────────────────────────────

UPSERT_TIM = """
INSERT INTO portfolio.ticker_isin_map (ticker, isin, source, verified_at)
VALUES (:ticker, :isin, :source, :verified_at)
ON CONFLICT (ticker) DO UPDATE SET
    isin        = EXCLUDED.isin,
    source      = EXCLUDED.source,
    verified_at = EXCLUDED.verified_at
"""

UPSERT_CONSTITUENT = parse_utils.UPSERT_CONSTITUENT

MIGRATE_SQL = """
INSERT INTO portfolio.etf_holding
    (etf_isin, as_of_date, constituent_isin, weight_pct, source_file)
SELECT sub.etf_isin, sub.as_of_date, sub.isin, SUM(sub.weight_pct), MIN(sub.source_file)
FROM (
    SELECT u.etf_isin, u.as_of_date, m.isin, u.weight_pct, 'resolved:' || u.raw_ticker AS source_file
    FROM portfolio.unresolved_holding u
    JOIN portfolio.ticker_isin_map m ON m.ticker = u.raw_ticker
) sub
GROUP BY sub.etf_isin, sub.as_of_date, sub.isin
ON CONFLICT (etf_isin, as_of_date, constituent_isin) DO UPDATE SET
    weight_pct  = EXCLUDED.weight_pct,
    source_file = EXCLUDED.source_file,
    loaded_at   = now()
"""


# ── Step 1: Name-basierter Match ─────────────────────────────────────────────

def step1_name_match() -> dict[str, str]:
    """
    Matched raw_name (unresolved) gegen name (constituent) – exact, case-insensitive.
    Gibt {raw_ticker: isin} zurück.
    """
    sql = """
        SELECT DISTINCT ON (u.raw_ticker)
            u.raw_ticker,
            c.isin,
            c.name AS constituent_name
        FROM portfolio.unresolved_holding u
        JOIN portfolio.constituent c
            ON UPPER(TRIM(u.raw_name)) = UPPER(TRIM(c.name))
        WHERE u.raw_ticker NOT IN (SELECT ticker FROM portfolio.ticker_isin_map)
        ORDER BY u.raw_ticker, c.isin
    """
    df = db_utils.query_df(sql)
    result: dict[str, str] = {}
    for _, row in df.iterrows():
        result[row["raw_ticker"]] = row["isin"]
    log.info("Step 1 (Name-Match): %d Ticker aufgelöst", len(result))
    return result


# ── Step 2: yfinance-Lookup ───────────────────────────────────────────────────

def _is_numeric_ticker(ticker: str) -> bool:
    """Rein-numerische Ticker (Korean KRX, chinesische Codes) ohne Ländersuffix nicht auflösbar."""
    return bool(re.match(r"^\d+$", ticker))


def _yf_lookup(ticker: str, country: str | None, currency: str | None) -> Optional[str]:
    """
    Sucht ISIN für einen Ticker via yfinance. Gibt ISIN oder None zurück.
    Versucht zuerst mit Ländersuffix, dann ohne (für multi-listed Securities).
    Kleine Pause nach jedem Call, um Rate-Limiting zu vermeiden.
    """
    suffix = COUNTRY_YF_SUFFIX.get(country or "", "")
    candidates = [ticker + suffix]
    if suffix:  # Fallback: kein Suffix
        candidates.append(ticker)

    for sym in candidates:
        try:
            time.sleep(0.15)   # ~6–7 Requests/s pro Worker → kein 401
            t = yf.Ticker(sym)
            isin = t.isin
            if isin and isinstance(isin, str) and re.match(r"[A-Z]{2}[A-Z0-9]{10}", isin):
                return isin
        except Exception:
            pass
    return None


def _safe_currency(val: object) -> str | None:
    """Gibt val[:3] zurück wenn es ein nicht-leerer String ist, sonst None.
    Fängt pandas NaN (float) ab."""
    if isinstance(val, str) and val:
        return val[:3]
    return None


def step2_yfinance(
    unresolved: list[dict],
    workers: int = 10,
) -> dict[str, tuple[str, dict]]:
    """
    Parallel yfinance-Lookup für alle (ticker, country, currency) Paare.
    Gibt {raw_ticker: (isin, constituent_dict)} zurück.
    Überspringt rein-numerische Ticker ohne Ländersuffix (Korean/chinesische Codes).
    """
    already_mapped = {
        row["ticker"]
        for row in db_utils.query_df("SELECT ticker FROM portfolio.ticker_isin_map").to_dict("records")
    }

    unique: dict[str, dict] = {}
    skipped_numeric = 0
    for row in unresolved:
        t = row["raw_ticker"]
        if t in already_mapped or t in unique:
            continue
        country = row["country"] if isinstance(row["country"], str) else None
        # Rein-numerische Ticker ohne Ländersuffix überspringen — nicht auflösbar
        if _is_numeric_ticker(t) and not COUNTRY_YF_SUFFIX.get(country or "", ""):
            skipped_numeric += 1
            continue
        unique[t] = {"country": country, "currency": row["currency"], "name": row["raw_name"]}

    if skipped_numeric:
        log.info("  Übersprungen (numerisch ohne Suffix): %d Ticker", skipped_numeric)
    log.info("Step 2 (yfinance): %d eindeutige Ticker zu prüfen …", len(unique))

    result: dict[str, tuple[str, dict]] = {}
    done = 0
    errors = 0
    start = time.time()

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(_yf_lookup, ticker, info["country"], info["currency"]): (ticker, info)
            for ticker, info in unique.items()
        }
        for future in as_completed(futures):
            ticker, info = futures[future]
            try:
                isin = future.result()
            except Exception:
                isin = None
                errors += 1

            if isin:
                result[ticker] = (isin, {
                    "isin":     isin,
                    "name":     info["name"][:200],
                    "sektor":   None,
                    "country":  info["country"],
                    "currency": _safe_currency(info["currency"]),
                })

            done += 1
            if done % 100 == 0:
                elapsed = time.time() - start
                log.info("  %d / %d … %d aufgelöst (%.0f s)", done, len(unique), len(result), elapsed)

    elapsed = time.time() - start
    log.info("Step 2 abgeschlossen: %d / %d aufgelöst (%.0f s, %d Fehler)",
             len(result), len(unique), elapsed, errors)
    return result


# ── DB-Schreiben ──────────────────────────────────────────────────────────────

def write_ticker_isin_map(mappings: dict[str, str], source: str) -> None:
    """Schreibt {raw_ticker: isin} in ticker_isin_map."""
    records = [
        {"ticker": t, "isin": isin, "source": source, "verified_at": date.today()}
        for t, isin in mappings.items()
    ]
    if records:
        db_utils.execute_many(UPSERT_TIM, records)
        log.info("  → %d Einträge in ticker_isin_map geschrieben (%s)", len(records), source)


def write_constituents(constituents: list[dict]) -> None:
    if not constituents:
        return
    seen: set[str] = set()
    unique = []
    for c in constituents:
        if c["isin"] not in seen:
            seen.add(c["isin"])
            unique.append(c)
    db_utils.execute_many(UPSERT_CONSTITUENT, unique)
    log.info("  → %d neue/aktualisierte Konstituenten in constituent geschrieben", len(unique))


def migrate_unresolved() -> int:
    """Überführt alle aufgelösten Zeilen aus unresolved_holding in etf_holding."""
    before = db_utils.query_df("SELECT COUNT(*) AS n FROM portfolio.etf_holding").iloc[0, 0]
    db_utils.execute(MIGRATE_SQL)
    after  = db_utils.query_df("SELECT COUNT(*) AS n FROM portfolio.etf_holding").iloc[0, 0]
    rows = int(after) - int(before)
    log.info("  → %d neue Zeilen von unresolved_holding → etf_holding migriert", rows)
    return rows


# ── Hauptprogramm ─────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="ISIN-Resolver für unresolved_holding")
    parser.add_argument("--dry-run",    action="store_true")
    parser.add_argument("--name-only",  action="store_true", help="Nur Name-Match (Step 1)")
    parser.add_argument("--yf-only",    action="store_true", help="Nur yfinance (Step 2)")
    parser.add_argument("--workers",    type=int, default=10, help="Parallele yfinance-Threads")
    args = parser.parse_args()

    # ── Unresolved laden ──────────────────────────────────────────────────────
    df = db_utils.query_df("""
        SELECT raw_ticker, raw_name, country, currency
        FROM portfolio.unresolved_holding
        ORDER BY raw_ticker
    """)
    unresolved = df.to_dict("records")
    log.info("Unresolved Holdings: %d Zeilen, %d unique Ticker",
             len(unresolved), len({r["raw_ticker"] for r in unresolved}))

    all_ticker_isin: dict[str, str] = {}
    new_constituents: list[dict] = []

    # ── Step 1: Name-Match ────────────────────────────────────────────────────
    if not args.yf_only:
        name_matches = step1_name_match()
        all_ticker_isin.update(name_matches)

    # ── Step 2: yfinance ──────────────────────────────────────────────────────
    if not args.name_only:
        # Bereits per Name aufgelöste aus dem yfinance-Set entfernen
        remaining = [r for r in unresolved if r["raw_ticker"] not in all_ticker_isin]
        yf_results = step2_yfinance(remaining, workers=args.workers)
        for ticker, (isin, constituent) in yf_results.items():
            all_ticker_isin[ticker] = isin
            new_constituents.append(constituent)

    # ── Zusammenfassung ───────────────────────────────────────────────────────
    total_unique = len({r["raw_ticker"] for r in unresolved})
    log.info("Gesamt aufgelöst: %d / %d unique Ticker (%.1f %%)",
             len(all_ticker_isin), total_unique,
             100 * len(all_ticker_isin) / max(total_unique, 1))

    if args.dry_run:
        log.info("--dry-run: nichts geschrieben.")
        return

    # ── In DB schreiben ───────────────────────────────────────────────────────
    if not all_ticker_isin:
        log.info("Keine neuen Mappings – fertig.")
        return

    # Nur Mappings schreiben, für die der Konstituent in constituent existiert
    # (Name-Match) oder neu angelegt wird (yfinance):
    if new_constituents:
        write_constituents(new_constituents)

    write_ticker_isin_map(all_ticker_isin, source="name_match+yfinance")
    migrated = migrate_unresolved()

    log.info("Fertig. %d Ticker→ISIN-Mappings, %d Holdings migriert.", len(all_ticker_isin), migrated)


if __name__ == "__main__":
    main()
