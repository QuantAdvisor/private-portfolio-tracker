"""
Task 5 – Aktienliste von ~400 auf ~700 erweitern.

Liest die vorhandenen Aktien aus quant_advisor.dim_stock_ticker,
identifiziert geeignete Kandidaten aus portfolio.constituent
(1.714 Titel mit ISIN, Sektor, Land, Ticker bekannt) und schreibt neue Einträge.

Ausführen (lokal, SSH-Tunnel via db_utils):
    python expand_stock_universe.py --preview      # zeigt Plan, schreibt nichts
    python expand_stock_universe.py --insert --n 300   # fügt bis zu 300 neue Titel ein

Strategie:
- Priorität: nicht-europäische Länder (US, JP, TW, KR, CA, AU, …) –
  denn die vorhandene Liste ist bereits EU-lastig.
- Filter: muss ISIN und Ticker haben (für yfinance-Download im anderen Projekt).
- Ranking: Häufigkeit in ETF-Holdings (je mehr ETFs einen Titel halten, desto
  etablierter / liquider ist er) als Qualitäts-Proxy.
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

EU_COUNTRIES = {
    "AT", "BE", "CH", "CZ", "DE", "DK", "ES", "FI", "FR", "GB",
    "GR", "HU", "IE", "IT", "LU", "NL", "NO", "PL", "PT", "SE", "SK",
}


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


def get_candidates(existing_isins: set[str], target: int, prefer_non_eu: bool = True) -> pd.DataFrame:
    """Top-N Kandidaten aus portfolio.constituent die noch nicht in dim_stock_ticker stehen."""
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
          AND c.ticker IS NOT NULL
          AND c.name IS NOT NULL
        GROUP BY c.isin, c.ticker, c.name, c.sektor, c.country, c.currency, c.wkn
        ORDER BY COUNT(DISTINCT eh.etf_isin) DESC, c.name
    """)

    # Bereits vorhandene ISINs ausfiltern
    df = df[~df["isin"].isin(existing_isins)].copy()

    if prefer_non_eu:
        # Erst Nicht-EU, dann EU (um die EU-Lastigkeit zu reduzieren)
        df["eu_flag"] = df["country"].isin(EU_COUNTRIES).astype(int)
        df = df.sort_values(["eu_flag", "etf_count"], ascending=[True, False])
    else:
        df = df.sort_values("etf_count", ascending=False)

    df = df.head(target).reset_index(drop=True)

    log.info(
        "Kandidaten: %d gesamt | %d Nicht-EU | %d EU",
        len(df),
        (~df["country"].isin(EU_COUNTRIES)).sum(),
        df["country"].isin(EU_COUNTRIES).sum(),
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

    log.info("=== Kandidaten aus portfolio.constituent ===")
    candidates = get_candidates(existing, target=args.n, prefer_non_eu=True)

    # Vorschau
    pd.set_option("display.max_rows", 50)
    log.info(
        "Top-20 Kandidaten (sortiert nach EU-Status, dann ETF-Häufigkeit):\n%s",
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
