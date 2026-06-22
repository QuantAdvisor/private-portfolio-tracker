"""
Phase 2 – ISIN-Resolver via OpenFIGI (Reverse-Lookup)

Strategie:
  Wir haben ISINs in portfolio.constituent (aus ETFs mit ISIN-Spalte).
  OpenFIGI wird genutzt um für jede dieser ISINs alle zugehörigen Ticker
  abzurufen. Daraus entsteht eine Ticker→ISIN-Map, mit der die Ticker
  in portfolio.unresolved_holding aufgelöst werden können.

Vorteil: Kein yfinance, kein API-Key, kein Rate-Limiting-Problem.
  1.714 Konstituenten → ~18 Batch-Requests à 100 ISINs.

Ablauf:
  1. Lade alle ISINs aus portfolio.constituent
  2. Frage OpenFIGI: ID_ISIN → alle Ticker für diese ISIN
  3. Baue inverse Map: ticker → isin
  4. Löse unresolved_holding auf (wie resolve_isins.py Step 2)
  5. Schreibe ticker_isin_map + migrate zu etf_holding

Aufruf:
    python resolve_isins_openfigi.py
    python resolve_isins_openfigi.py --dry-run
    python resolve_isins_openfigi.py --batch-size 100
"""

import argparse
import json
import logging
import time
import urllib.request
from datetime import date

import db_utils
import parse_utils

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

OPENFIGI_URL = "https://api.openfigi.com/v3/mapping"

UPSERT_TIM = """
INSERT INTO portfolio.ticker_isin_map (ticker, isin, source, verified_at)
VALUES (:ticker, :isin, :source, :verified_at)
ON CONFLICT (ticker) DO UPDATE SET
    isin        = EXCLUDED.isin,
    source      = EXCLUDED.source,
    verified_at = EXCLUDED.verified_at
"""

MIGRATE_SQL = """
INSERT INTO portfolio.etf_holding
    (etf_isin, as_of_date, constituent_isin, weight_pct, source_file)
SELECT sub.etf_isin, sub.as_of_date, sub.isin, SUM(sub.weight_pct), MIN(sub.source_file)
FROM (
    SELECT u.etf_isin, u.as_of_date, m.isin, u.weight_pct,
           'resolved:openfigi:' || u.raw_ticker AS source_file
    FROM portfolio.unresolved_holding u
    JOIN portfolio.ticker_isin_map m ON m.ticker = u.raw_ticker
) sub
GROUP BY sub.etf_isin, sub.as_of_date, sub.isin
ON CONFLICT (etf_isin, as_of_date, constituent_isin) DO UPDATE SET
    weight_pct  = EXCLUDED.weight_pct,
    source_file = EXCLUDED.source_file,
    loaded_at   = now()
"""


def _openfigi_batch(isins: list[str]) -> dict[str, list[str]]:
    """
    Sendet bis zu 100 ISINs an OpenFIGI und gibt {isin: [ticker, ...]} zurück.
    Jede ISIN kann mehrere Ticker haben (verschiedene Börsenplätze).
    """
    jobs = [{"idType": "ID_ISIN", "idValue": isin} for isin in isins]
    data = json.dumps(jobs).encode()
    req = urllib.request.Request(
        OPENFIGI_URL,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            results = json.loads(r.read())
    except Exception as exc:
        log.warning("OpenFIGI-Batch-Fehler: %s", exc)
        return {}

    isin_to_tickers: dict[str, list[str]] = {}
    for isin, result in zip(isins, results):
        if "data" not in result:
            continue
        tickers = []
        for entry in result["data"]:
            ticker = entry.get("ticker", "").strip()
            if ticker and ticker.isascii():
                tickers.append(ticker)
        if tickers:
            isin_to_tickers[isin] = tickers

    return isin_to_tickers


def build_ticker_isin_map(batch_size: int = 100) -> dict[str, str]:
    """
    Fragt alle Konstituenten-ISINs bei OpenFIGI ab und baut {ticker: isin}.
    Bei mehreren ISINs für denselben Ticker: ersten Treffer behalten.
    """
    df = db_utils.query_df("SELECT DISTINCT isin FROM portfolio.constituent ORDER BY isin")
    all_isins = df["isin"].tolist()
    log.info("%d ISINs aus constituent → OpenFIGI-Lookup in %d Batches",
             len(all_isins), (len(all_isins) + batch_size - 1) // batch_size)

    ticker_to_isin: dict[str, str] = {}
    total_batches = (len(all_isins) + batch_size - 1) // batch_size

    for i in range(0, len(all_isins), batch_size):
        batch = all_isins[i : i + batch_size]
        batch_num = i // batch_size + 1

        isin_tickers = _openfigi_batch(batch)
        new_mappings = 0
        for isin, tickers in isin_tickers.items():
            for ticker in tickers:
                if ticker not in ticker_to_isin:
                    ticker_to_isin[ticker] = isin
                    new_mappings += 1

        log.info("Batch %d/%d: %d ISINs → %d neue Ticker-Mappings (gesamt: %d)",
                 batch_num, total_batches, len(batch), new_mappings, len(ticker_to_isin))

        # Pause zwischen Batches: ohne API-Key max 25 Requests/Min → 2.5s
        if i + batch_size < len(all_isins):
            time.sleep(2.4)

    log.info("OpenFIGI-Lookup abgeschlossen: %d eindeutige Ticker aus %d ISINs",
             len(ticker_to_isin), len(all_isins))
    return ticker_to_isin


def resolve_unresolved(ticker_to_isin: dict[str, str]) -> dict[str, str]:
    """
    Gleicht unresolved_holding.raw_ticker gegen die OpenFIGI-Map ab.
    Gibt {raw_ticker: isin} für neu aufgelöste Ticker zurück.
    """
    already_mapped = {
        row["ticker"]
        for row in db_utils.query_df(
            "SELECT ticker FROM portfolio.ticker_isin_map"
        ).to_dict("records")
    }

    df = db_utils.query_df("""
        SELECT DISTINCT raw_ticker
        FROM portfolio.unresolved_holding
        WHERE raw_ticker NOT IN (SELECT ticker FROM portfolio.ticker_isin_map)
        ORDER BY raw_ticker
    """)
    unresolved_tickers = set(df["raw_ticker"].tolist())

    resolved: dict[str, str] = {}
    for ticker in unresolved_tickers:
        if ticker in ticker_to_isin:
            resolved[ticker] = ticker_to_isin[ticker]

    log.info("Aufgelöst: %d / %d unresolved Ticker (%.1f %%)",
             len(resolved), len(unresolved_tickers),
             100 * len(resolved) / max(len(unresolved_tickers), 1))
    return resolved


def main() -> None:
    parser = argparse.ArgumentParser(description="ISIN-Resolver via OpenFIGI (Reverse-Lookup)")
    parser.add_argument("--dry-run",    action="store_true")
    parser.add_argument("--batch-size", type=int, default=10,
                        help="Jobs pro OpenFIGI-Request (ohne API-Key max 10)")
    args = parser.parse_args()

    # 1. OpenFIGI-Lookup: ISIN → Ticker-Liste → invertieren
    ticker_to_isin = build_ticker_isin_map(batch_size=args.batch_size)

    # 2. Unresolved abgleichen
    new_mappings = resolve_unresolved(ticker_to_isin)

    if not new_mappings:
        log.info("Keine neuen Mappings gefunden.")
        return

    log.info("Neue Mappings: %d Ticker", len(new_mappings))

    if args.dry_run:
        for t, isin in list(new_mappings.items())[:20]:
            log.info("  %s → %s", t, isin)
        log.info("--dry-run: nichts geschrieben.")
        return

    # 3. ticker_isin_map befüllen
    records = [
        {"ticker": t, "isin": isin, "source": "openfigi_reverse", "verified_at": date.today()}
        for t, isin in new_mappings.items()
    ]
    db_utils.execute_many(UPSERT_TIM, records)
    log.info("  → %d Einträge in ticker_isin_map geschrieben", len(records))

    # 4. Migration: unresolved_holding → etf_holding
    before = db_utils.query_df("SELECT COUNT(*) AS n FROM portfolio.etf_holding").iloc[0, 0]
    db_utils.execute(MIGRATE_SQL)
    after  = db_utils.query_df("SELECT COUNT(*) AS n FROM portfolio.etf_holding").iloc[0, 0]
    migrated = int(after) - int(before)
    log.info("  → %d neue Zeilen von unresolved_holding → etf_holding migriert", migrated)

    log.info("Fertig.")


if __name__ == "__main__":
    main()
