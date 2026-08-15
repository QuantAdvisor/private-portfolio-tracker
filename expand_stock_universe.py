"""
Task 5 – Aktienliste von ~400 auf ~700 erweitern.

Liest die vorhandenen Aktien aus quant_advisor.dim_stock_ticker,
identifiziert geeignete Kandidaten aus portfolio.constituent
(1.714 Titel mit ISIN, Sektor, Land, Ticker bekannt) und schreibt neue Einträge.

Ausführen (lokal, SSH-Tunnel via db_utils):
    python expand_stock_universe.py --preview      # zeigt Plan, schreibt nichts
    python expand_stock_universe.py --insert --n 300   # fügt bis zu 300 neue Titel ein

Strategie:
- Länder-Whitelist: MSCI World Developed Markets (US, CA, Europa, JP, AU, HK, SG, NZ, IL).
  Kein EM (CN, KR, TW, IN, BR, …).
- Größen-Filter: Nur Titel die in ≥ 2 unserer ETFs enthalten sind (Proxy für Large/Mid-Cap).
- Ranking: ETF-Häufigkeit absteigend (je mehr ETFs, desto etablierter).
- Keine Dopplungen: ISINs die bereits in dim_stock_ticker stehen werden übersprungen.
"""

import argparse
import logging
import sys

import pandas as pd

import db_utils

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger(__name__)
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# MSCI World Developed Markets (Stand 2024) — kein EM, kein FM
MSCI_WORLD_COUNTRIES = {
    # Nordamerika
    "US", "CA",
    # Europa
    "AT", "BE", "CH", "DE", "DK", "ES", "FI", "FR", "GB",
    "GR", "IE", "IL", "IT", "LU", "NL", "NO", "PT", "SE",
    # Pazifik
    "AU", "HK", "JP", "NZ", "SG",
}

MIN_ETF_COUNT = 2  # Titel muss in ≥ N unserer ETFs liegen (Größen-Proxy)


def discover_dim_schema() -> list[str]:
    """Zeigt Spalten von quant_advisor.dim_stock_ticker."""
    df = db_utils.query_df("""
        SELECT column_name, data_type, is_nullable
        FROM information_schema.columns
        WHERE table_schema = 'quant_advisor' AND table_name = 'dim_stock_ticker'
        ORDER BY ordinal_position
    """)
    log.info("Schema quant_advisor.dim_stock_ticker:\n%s", df.to_string(index=False))
    return list(df["column_name"])


def get_existing_isins() -> set[str]:
    df = db_utils.query_df("SELECT isin FROM quant_advisor.dim_stock_ticker")
    return set(df["isin"].dropna())


def diagnose_constituents() -> None:
    """Zeigt Datenlage in portfolio.constituent (einmalig zur Diagnose)."""
    df = db_utils.query_df("""
        SELECT
            COUNT(*)                                          AS total,
            COUNT(ticker)                                     AS with_ticker,
            COUNT(country)                                    AS with_country,
            COUNT(CASE WHEN country IS NOT NULL
                        AND ticker IS NOT NULL THEN 1 END)   AS with_both
        FROM portfolio.constituent
    """)
    log.info("Constituent-Daten: %s", df.to_string(index=False))

    df_ctry = db_utils.query_df("""
        SELECT country, COUNT(*) AS n
        FROM portfolio.constituent
        WHERE country IS NOT NULL
        GROUP BY country ORDER BY n DESC LIMIT 20
    """)
    log.info("Top-Länder in portfolio.constituent:\n%s", df_ctry.to_string(index=False))


def get_candidates(existing_isins: set[str], target: int) -> pd.DataFrame:
    """Top-N Kandidaten aus portfolio.constituent: MSCI World, etf_count >= MIN_ETF_COUNT.

    ticker darf NULL sein (dim_stock_ticker hat nullable ticker-Spalte).
    """
    df = db_utils.query_df("""
        SELECT
            c.isin,
            c.ticker,
            c.name,
            c.sektor,
            c.country,
            c.currency,
            c.wkn,
            COUNT(DISTINCT eh.etf_isin) AS etf_count
        FROM portfolio.constituent c
        JOIN portfolio.etf_holding eh ON eh.constituent_isin = c.isin
        WHERE c.isin IS NOT NULL
          AND c.name IS NOT NULL
        GROUP BY c.isin, c.ticker, c.name, c.sektor, c.country, c.currency, c.wkn
        ORDER BY COUNT(DISTINCT eh.etf_isin) DESC, c.name
    """)

    df = df[~df["isin"].isin(existing_isins)].copy()

    # Nur MSCI World Developed Markets
    before = len(df)
    df = df[df["country"].isin(MSCI_WORLD_COUNTRIES)]
    log.info("Länder-Filter (MSCI World): %d → %d Titel", before, len(df))

    # Nur Titel in ≥ MIN_ETF_COUNT ETFs (Größen-Proxy)
    before = len(df)
    df = df[df["etf_count"] >= MIN_ETF_COUNT]
    log.info("Größen-Filter (etf_count ≥ %d): %d → %d Titel", MIN_ETF_COUNT, before, len(df))

    df = df.sort_values("etf_count", ascending=False).head(target).reset_index(drop=True)

    log.info(
        "Finale Kandidaten: %d | Top-Länder: %s",
        len(df),
        df["country"].value_counts().head(5).to_dict(),
    )
    return df


def build_insert_records(df: pd.DataFrame, columns: list[str]) -> list[dict]:
    """Baut INSERT-Records passend zum Schema von dim_stock_ticker."""
    # Spalten-Mapping: portfolio.constituent → quant_advisor.dim_stock_ticker
    col_map = {
        "isin":     "isin",
        "ticker":   "ticker",
        "name":     "name",
        "sektor":   "sector" if "sector" in columns else "sektor",
        "country":  "country",
        "currency": "currency",
        "wkn":      "wkn",
    }

    records = []
    for _, row in df.iterrows():
        rec = {}
        for src, tgt in col_map.items():
            if tgt in columns and src in df.columns:
                val = row[src]
                rec[tgt] = None if pd.isna(val) else str(val)
        records.append(rec)
    return records


def insert_candidates(records: list[dict], columns: list[str]) -> int:
    if not records:
        return 0

    # Nur Felder einfügen die im Schema vorhanden sind
    valid_cols = [c for c in records[0].keys() if c in columns]
    col_list   = ", ".join(valid_cols)
    val_list   = ", ".join(f":{c}" for c in valid_cols)
    sql = f"""
        INSERT INTO quant_advisor.dim_stock_ticker ({col_list})
        VALUES ({val_list})
        ON CONFLICT (isin) DO NOTHING
    """
    db_utils.execute_many(sql, records)
    return len(records)


def main():
    parser = argparse.ArgumentParser(description="Aktienliste auf ~700 Titel erweitern")
    parser.add_argument("--preview", action="store_true", help="Nur Vorschau, kein INSERT")
    parser.add_argument("--insert",  action="store_true", help="INSERT ausführen")
    parser.add_argument("--n", type=int, default=300, help="Maximale Anzahl neuer Titel (Standard: 300)")
    args = parser.parse_args()

    if not args.preview and not args.insert:
        parser.print_help()
        sys.exit(1)

    log.info("=== Schema-Discovery ===")
    columns = discover_dim_schema()

    log.info("=== Vorhandene ISINs ===")
    existing = get_existing_isins()
    log.info("Aktuell in dim_stock_ticker: %d ISINs", len(existing))

    log.info("=== Diagnose portfolio.constituent ===")
    diagnose_constituents()

    log.info("=== Kandidaten aus portfolio.constituent ===")
    candidates = get_candidates(existing, target=args.n)

    # Vorschau
    pd.set_option("display.max_rows", 50)
    log.info(
        "Top-20 Kandidaten (sortiert nach ETF-Häufigkeit):\n%s",
        candidates[["isin", "ticker", "name", "country", "sektor", "etf_count"]]
        .head(20)
        .to_string(index=False),
    )
    log.info("Länder-Verteilung der Kandidaten:\n%s",
             candidates["country"].value_counts().head(20).to_string())

    if args.insert:
        log.info("=== INSERT %d neue Titel ===", len(candidates))
        records = build_insert_records(candidates, columns)
        n = insert_candidates(records, columns)
        log.info("Fertig: %d neue Titel in dim_stock_ticker eingefügt.", n)
        new_total = len(existing) + n
        log.info("Neuer Gesamtbestand: ~%d Titel", new_total)
    else:
        log.info("Preview-Modus: Kein INSERT. Mit --insert ausführen um zu schreiben.")


if __name__ == "__main__":
    main()
