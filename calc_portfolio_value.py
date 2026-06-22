"""
Phase 0 – Portfoliowert-Auswertung
Zeigt den aktuellen Marktwert je ETF und je Depot in EUR.

Grundlage:
  - Jüngster position_snapshot je (account, isin)
  - Jüngster Kurs ≤ heute je ETF (LATERAL-Join)
  - FX-Umrechnung via eur_per_unit (falls Kurs nicht in EUR)

Aufruf:
    python calc_portfolio_value.py
    python calc_portfolio_value.py --date 2026-06-20   # historischer Stichtag
"""

import argparse
import sys
from datetime import date

import db_utils

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SQL = """
WITH latest_snapshot AS (
    -- Jüngster Positions-Snapshot je Depot+ETF
    SELECT DISTINCT ON (account_id, isin)
        account_id, isin, quantity, avg_cost, as_of_date AS snap_date
    FROM portfolio.position_snapshot
    WHERE as_of_date <= :ref_date
    ORDER BY account_id, isin, as_of_date DESC
),
latest_price AS (
    -- Jüngster Kurs ≤ Stichtag je ETF
    SELECT DISTINCT ON (isin)
        isin, close, currency, price_date
    FROM portfolio.price
    WHERE price_date <= :ref_date
    ORDER BY isin, price_date DESC
),
latest_fx AS (
    -- Jüngster FX-Kurs ≤ Stichtag je Währung
    SELECT DISTINCT ON (currency)
        currency, eur_per_unit, rate_date
    FROM portfolio.fx_rate
    WHERE rate_date <= :ref_date
    ORDER BY currency, rate_date DESC
)
SELECT
    a.name                                                  AS depot,
    e.isin,
    e.name                                                  AS etf_name,
    e.emittent,
    ls.quantity,
    ls.avg_cost,
    lp.close                                                AS kurs,
    lp.currency                                             AS kurs_waehrung,
    lp.price_date,
    CASE lp.currency
        WHEN 'EUR' THEN lp.close
        WHEN 'GBp' THEN lp.close * COALESCE(fx.eur_per_unit, 1)
        ELSE             lp.close * COALESCE(fx.eur_per_unit, 1)
    END                                                     AS kurs_eur,
    ROUND(
        ls.quantity * CASE lp.currency
            WHEN 'EUR' THEN lp.close
            WHEN 'GBp' THEN lp.close * COALESCE(fx.eur_per_unit, 1)
            ELSE             lp.close * COALESCE(fx.eur_per_unit, 1)
        END
    , 2)                                                    AS wert_eur
FROM latest_snapshot ls
JOIN portfolio.account a ON a.account_id = ls.account_id
JOIN portfolio.etf e     ON e.isin       = ls.isin
LEFT JOIN latest_price lp ON lp.isin     = ls.isin
LEFT JOIN latest_fx    fx ON fx.currency = lp.currency AND lp.currency <> 'EUR'
ORDER BY wert_eur DESC NULLS LAST
"""


def run(ref_date: date) -> None:
    df = db_utils.query_df(SQL, {"ref_date": ref_date})

    if df.empty:
        print("Keine Daten – position_snapshot oder price leer.")
        return

    no_price = df[df["wert_eur"].isna()]
    df = df[df["wert_eur"].notna()].copy()

    total = df["wert_eur"].sum()
    df["anteil_%"] = (df["wert_eur"] / total * 100).round(2)

    # ── ETF-Tabelle ───────────────────────────────────────────────────────────
    print(f"\n{'='*80}")
    print(f"  Portfoliowert per {ref_date}   (Kurse: jüngster verfügbarer Schlusskurs)")
    print(f"{'='*80}")

    cols = ["depot", "etf_name", "quantity", "kurs_eur", "wert_eur", "anteil_%", "price_date"]
    display = df[cols].copy()
    display["etf_name"]  = display["etf_name"].str[:40]
    display["quantity"]  = display["quantity"].map("{:>10.2f}".format)
    display["kurs_eur"]  = display["kurs_eur"].map("{:>9.2f}".format)
    display["wert_eur"]  = display["wert_eur"].map("{:>10.2f}".format)
    display["anteil_%"]  = display["anteil_%"].map("{:>6.2f} %".format)

    print(display.to_string(index=False))

    # ── Depot-Summen ──────────────────────────────────────────────────────────
    print(f"\n{'─'*50}")
    print("  Wert je Depot (EUR)")
    print(f"{'─'*50}")
    depot_sum = (
        df.groupby("depot")["wert_eur"]
        .sum()
        .sort_values(ascending=False)
        .reset_index()
    )
    depot_sum["anteil_%"] = (depot_sum["wert_eur"] / total * 100).round(1)
    for _, r in depot_sum.iterrows():
        print(f"  {r['depot']:<30} {r['wert_eur']:>10,.2f} EUR  ({r['anteil_%']:.1f} %)")

    print(f"{'─'*50}")
    print(f"  {'GESAMT':<30} {total:>10,.2f} EUR")
    print(f"{'─'*50}\n")

    # ── Fehlende Kurse ────────────────────────────────────────────────────────
    if not no_price.empty:
        print(f"  ⚠ Kein Kurs verfügbar für {len(no_price)} ETF(s) – nicht im Gesamtwert:")
        for _, r in no_price.iterrows():
            print(f"    {r['isin']}  {r['etf_name'][:45]}")
        print()


def main() -> None:
    parser = argparse.ArgumentParser(description="Portfoliowert je ETF und Depot")
    parser.add_argument(
        "--date",
        type=date.fromisoformat,
        default=date.today(),
        help="Stichtag (YYYY-MM-DD), Standard: heute",
    )
    args = parser.parse_args()
    run(args.date)


if __name__ == "__main__":
    main()
