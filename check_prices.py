"""
Preisvergleich: yfinance (portfolio.price) vs. manuelle EUR-Kurse aus Portfolio.xlsx

Zeigt je ETF:
  - yf_kurs     : jüngster Kurs aus portfolio.price (ggf. FX-bereinigt auf EUR)
  - manuell     : manuell eingetragener EUR-Kurs aus Portfolio.xlsx
  - abw_%       : Abweichung in Prozent
  - currency    : Handelswährung laut portfolio.price
  - yf_symbol   : welches yfinance-Symbol wurde genutzt

Aufruf:
    python check_prices.py
"""

from datetime import date
from pathlib import Path

import openpyxl
import pandas as pd

import db_utils
from load_prices import YF_TICKER_OVERRIDE

PORTFOLIO_FILE = Path(__file__).parent / "ETF Portfolio" / "Portfolio.xlsx"
SHEET = "Tabelle1"


def load_manual_prices() -> pd.DataFrame:
    wb = openpyxl.load_workbook(PORTFOLIO_FILE, data_only=True)
    ws = wb[SHEET]
    rows = [row for row in ws.iter_rows(values_only=True) if any(v is not None for v in row)]
    df = pd.DataFrame(rows[1:], columns=rows[0])
    df = df.rename(columns={
        "Isisn":                "isin",
        "Name":                 "name",
        "akuteller kurs (EUR)": "price_eur_manual",
    })
    df = df[df["isin"].notna()].copy()
    df["isin"] = df["isin"].astype(str).str.strip()
    df["price_eur_manual"] = pd.to_numeric(df["price_eur_manual"], errors="coerce")
    # Eine Zeile je ISIN (Kurs ist depot-unabhängig)
    return df[["isin", "name", "price_eur_manual"]].drop_duplicates(subset=["isin"])


def load_db_prices() -> pd.DataFrame:
    sql = """
    WITH latest AS (
        SELECT DISTINCT ON (p.isin)
            p.isin, p.close, p.currency, p.price_date, p.source
        FROM portfolio.price p
        ORDER BY p.isin, p.price_date DESC
    ),
    fx AS (
        SELECT DISTINCT ON (currency)
            currency, eur_per_unit
        FROM portfolio.fx_rate
        WHERE rate_date <= :today
        ORDER BY currency, rate_date DESC
    )
    SELECT
        l.isin,
        l.close,
        l.currency,
        l.price_date,
        l.source,
        CASE l.currency
            WHEN 'EUR' THEN l.close
            WHEN 'GBp' THEN l.close * fx.eur_per_unit
            ELSE             l.close * fx.eur_per_unit
        END AS close_eur
    FROM latest l
    LEFT JOIN fx ON fx.currency = l.currency AND l.currency <> 'EUR'
    """
    return db_utils.query_df(sql, {"today": date.today()})


def main() -> None:
    manual = load_manual_prices()
    db = load_db_prices()

    etf_info = db_utils.query_df("SELECT isin, ticker FROM portfolio.etf")
    etf_info["yf_symbol"] = etf_info.apply(
        lambda r: YF_TICKER_OVERRIDE.get(r["isin"], f"{r['ticker']}.DE"), axis=1
    )

    df = (
        manual
        .merge(db, on="isin", how="left")
        .merge(etf_info[["isin", "yf_symbol"]], on="isin", how="left")
    )

    df["close_eur"]  = pd.to_numeric(df["close_eur"], errors="coerce")
    df["abw_%"] = (
        (df["close_eur"] - df["price_eur_manual"]) / df["price_eur_manual"] * 100
    ).round(2)

    df = df.sort_values("abw_%", key=lambda s: s.abs(), ascending=False, na_position="first")

    cols = ["isin", "name", "price_eur_manual", "close_eur", "abw_%", "currency", "price_date", "yf_symbol", "source"]
    out = df[cols].copy()
    out["name"] = out["name"].str[:35]
    out["price_eur_manual"] = out["price_eur_manual"].map(lambda v: f"{v:8.4f}" if pd.notna(v) else "    —   ")
    out["close_eur"] = out["close_eur"].map(lambda v: f"{v:8.4f}" if pd.notna(v) else "    —   ")
    out["abw_%"] = out["abw_%"].map(lambda v: f"{v:+6.2f} %" if pd.notna(v) else "    —   ")

    print(f"\n{'='*110}")
    print(f"  Kursvergleich: yfinance-DB  vs.  manuell  (Stand: {date.today()})")
    print(f"{'='*110}")
    print(out.to_string(index=False))
    print(f"{'='*110}")

    no_price = df[df["close_eur"].isna()]
    if not no_price.empty:
        print(f"\n  ⚠ Kein yfinance-Kurs in DB für {len(no_price)} ETF(s):")
        for _, r in no_price.iterrows():
            print(f"    {r['isin']}  {r['name'][:45]}  (Symbol: {r['yf_symbol']})")


if __name__ == "__main__":
    main()
