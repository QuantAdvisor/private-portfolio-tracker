"""
Phase 3 – HTML-Portfolio-Report (täglich per Cronjob)

Erzeugt einen HTML-Report mit:
  1. Gesamtwert und Depot-Aufteilung
  2. Top-30 Einzeltitel-Engagements (Look-Through)
  3. ETF-Überlappungsmatrix (Top-Paare)
  4. Sektor- und Länder-Allokation (Look-Through)
  5. Abdeckungs-Hinweis (aufgelöste vs. unaufgelöste Holdings)

⚠ Wertentwicklung = Vermögen inkl. Einzahlungen – KEINE Rendite.
  Echte Rendite (MWR/TWR) erst nach Transaktionshistorie möglich.

Aufruf:
    python generate_report.py
    python generate_report.py --out reports/mein_report.html
    python generate_report.py --date 2026-06-20   # Historisch
"""

import argparse
import base64
import calendar
import io
import logging
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
import pandas as pd
import yfinance as yf

import db_utils

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger(__name__)
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

REPORTS_DIR = Path(__file__).parent / "reports"

# ── Farben ───────────────────────────────────────────────────────────────────
BLUE     = "#2563EB"
SLATE    = "#334155"
LIGHT    = "#F1F5F9"
GREEN    = "#16A34A"
RED      = "#DC2626"
ORANGE   = "#EA580C"
PALETTE  = ["#2563EB","#16A34A","#EA580C","#7C3AED","#0891B2",
            "#CA8A04","#DB2777","#65A30D","#0D9488","#9333EA"]

BENCHMARK_PRIMARY = {
    "ticker": "MSCI_WORLD",   # Key in portfolio.benchmark
    "name":   "iShares Core MSCI World UCITS ETF Acc (EUNL)",
}
# Gemischte Benchmark: zur Laufzeit aus den Einzel-Tickern verkettet (kein eigener
# DB-Eintrag noetig, bleibt automatisch aktuell sobald die Komponenten neue Kurse haben).
BENCHMARK_SECONDARY = {
    "name": "60/40 MSCI World / Global Agg Bond (EUR H)",
    "weights": {"MSCI_WORLD": 0.6, "GLOBAL_AGG_BOND_H": 0.4},
}


# ── Hilfsfunktionen ───────────────────────────────────────────────────────────

def _fmt_eur(val: float) -> str:
    return f"{val:,.2f} €".replace(",", "X").replace(".", ",").replace("X", ".")

def _fmt_pct(val: float) -> str:
    return f"{val:.1f} %"

def _b64_png(fig) -> str:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=120, bbox_inches="tight",
                facecolor="white", edgecolor="none")
    plt.close(fig)
    return base64.b64encode(buf.getvalue()).decode()


# ── Datenbankabfragen ─────────────────────────────────────────────────────────

def get_depot_breakdown(ref_date: date) -> pd.DataFrame:
    """Wert je Depot und ETF, as-of ref_date."""
    return db_utils.query_df("""
        WITH snap AS (
            SELECT DISTINCT ON (account_id, isin)
                account_id, isin, quantity, as_of_date
            FROM portfolio.position_snapshot
            WHERE as_of_date <= :ref
            ORDER BY account_id, isin, as_of_date DESC
        ),
        price AS (
            SELECT DISTINCT ON (isin)
                isin, close, price_date
            FROM portfolio.price
            WHERE price_date <= :ref
            ORDER BY isin, price_date DESC
        )
        SELECT
            a.name  AS depot,
            e.ticker,
            e.name  AS etf_name,
            s.quantity,
            p.close AS kurs,
            p.price_date,
            ROUND((s.quantity * p.close)::numeric, 2) AS wert_eur
        FROM snap s
        JOIN portfolio.account a  ON a.account_id = s.account_id
        JOIN portfolio.etf e       ON e.isin = s.isin
        LEFT JOIN price p          ON p.isin = s.isin
        ORDER BY depot, wert_eur DESC NULLS LAST
    """, params={"ref": ref_date})


def get_top_holdings(ref_date: date, n: int = 30) -> pd.DataFrame:
    """Top-N Einzeltitel nach effektivem EUR-Engagement (Look-Through)."""
    return db_utils.query_df("""
        WITH snap AS (
            SELECT DISTINCT ON (account_id, isin)
                isin, quantity
            FROM portfolio.position_snapshot
            WHERE as_of_date <= :ref
            ORDER BY account_id, isin, as_of_date DESC
        ),
        etf_val AS (
            SELECT s.isin AS etf_isin,
                   SUM(s.quantity * p.close) AS etf_eur
            FROM snap s
            JOIN (
                SELECT DISTINCT ON (isin) isin, close
                FROM portfolio.price WHERE price_date <= :ref
                ORDER BY isin, price_date DESC
            ) p ON p.isin = s.isin
            GROUP BY s.isin
        ),
        holdings AS (
            SELECT DISTINCT ON (etf_isin, constituent_isin)
                etf_isin, constituent_isin, weight_pct
            FROM portfolio.etf_holding
            WHERE as_of_date <= :ref
            ORDER BY etf_isin, constituent_isin, as_of_date DESC
        ),
        total AS (SELECT SUM(etf_eur) AS gesamt FROM etf_val)
        SELECT
            c.name,
            c.isin  AS constituent_isin,
            c.country,
            c.sektor,
            ROUND(SUM(ev.etf_eur * h.weight_pct / 100)::numeric, 2) AS engagement_eur,
            ROUND((SUM(ev.etf_eur * h.weight_pct / 100) / t.gesamt * 100)::numeric, 3) AS anteil_pct
        FROM holdings h
        JOIN etf_val ev  ON ev.etf_isin = h.etf_isin
        JOIN portfolio.constituent c ON c.isin = h.constituent_isin
        CROSS JOIN total t
        GROUP BY c.isin, c.name, c.country, c.sektor, t.gesamt
        ORDER BY engagement_eur DESC
        LIMIT :n
    """, params={"ref": ref_date, "n": n})


def get_sector_allocation(ref_date: date) -> pd.DataFrame:
    """Sektor-Allokation auf Look-Through-Basis."""
    return db_utils.query_df("""
        WITH snap AS (
            SELECT DISTINCT ON (account_id, isin) isin, quantity
            FROM portfolio.position_snapshot
            WHERE as_of_date <= :ref ORDER BY account_id, isin, as_of_date DESC
        ),
        etf_val AS (
            SELECT s.isin AS etf_isin, SUM(s.quantity * p.close) AS etf_eur
            FROM snap s
            JOIN (SELECT DISTINCT ON (isin) isin, close FROM portfolio.price
                  WHERE price_date <= :ref ORDER BY isin, price_date DESC) p ON p.isin = s.isin
            GROUP BY s.isin
        ),
        holdings AS (
            SELECT DISTINCT ON (etf_isin, constituent_isin) etf_isin, constituent_isin, weight_pct
            FROM portfolio.etf_holding WHERE as_of_date <= :ref
            ORDER BY etf_isin, constituent_isin, as_of_date DESC
        ),
        total AS (SELECT SUM(etf_eur) AS gesamt FROM etf_val)
        SELECT
            COALESCE(NULLIF(TRIM(c.sektor), ''), 'Nicht klassifiziert') AS sektor,
            ROUND(SUM(ev.etf_eur * h.weight_pct / 100)::numeric, 2) AS eur,
            ROUND((SUM(ev.etf_eur * h.weight_pct / 100) / t.gesamt * 100)::numeric, 2) AS pct
        FROM holdings h
        JOIN etf_val ev ON ev.etf_isin = h.etf_isin
        JOIN portfolio.constituent c ON c.isin = h.constituent_isin
        CROSS JOIN total t
        GROUP BY sektor, t.gesamt
        ORDER BY eur DESC
        LIMIT 12
    """, params={"ref": ref_date})


def get_country_allocation(ref_date: date) -> pd.DataFrame:
    """Länder-Allokation (Top-15) auf Look-Through-Basis."""
    return db_utils.query_df("""
        WITH snap AS (
            SELECT DISTINCT ON (account_id, isin) isin, quantity
            FROM portfolio.position_snapshot
            WHERE as_of_date <= :ref ORDER BY account_id, isin, as_of_date DESC
        ),
        etf_val AS (
            SELECT s.isin AS etf_isin, SUM(s.quantity * p.close) AS etf_eur
            FROM snap s
            JOIN (SELECT DISTINCT ON (isin) isin, close FROM portfolio.price
                  WHERE price_date <= :ref ORDER BY isin, price_date DESC) p ON p.isin = s.isin
            GROUP BY s.isin
        ),
        holdings AS (
            SELECT DISTINCT ON (etf_isin, constituent_isin) etf_isin, constituent_isin, weight_pct
            FROM portfolio.etf_holding WHERE as_of_date <= :ref
            ORDER BY etf_isin, constituent_isin, as_of_date DESC
        ),
        total AS (SELECT SUM(etf_eur) AS gesamt FROM etf_val)
        SELECT
            COALESCE(NULLIF(TRIM(c.country), ''), '??') AS land,
            ROUND(SUM(ev.etf_eur * h.weight_pct / 100)::numeric, 2) AS eur,
            ROUND((SUM(ev.etf_eur * h.weight_pct / 100) / t.gesamt * 100)::numeric, 2) AS pct
        FROM holdings h
        JOIN etf_val ev ON ev.etf_isin = h.etf_isin
        JOIN portfolio.constituent c ON c.isin = h.constituent_isin
        CROSS JOIN total t
        WHERE c.country IS NOT NULL AND c.country != ''
        GROUP BY land, t.gesamt
        ORDER BY eur DESC
        LIMIT 15
    """, params={"ref": ref_date})


def get_etf_overlap(ref_date: date) -> pd.DataFrame:
    """Paarweise ETF-Überlappung: min(wA, wB) für gemeinsame Konstituenten, inkl. Depot."""
    return db_utils.query_df("""
        WITH holdings AS (
            SELECT DISTINCT ON (etf_isin, constituent_isin)
                etf_isin, constituent_isin, weight_pct
            FROM portfolio.etf_holding WHERE as_of_date <= :ref
            ORDER BY etf_isin, constituent_isin, as_of_date DESC
        ),
        etf_depots AS (
            -- Letzter Snapshot pro Depot+ETF → Depot-Namen pro ETF aggregieren
            SELECT
                ps.isin,
                string_agg(DISTINCT
                    CASE a.name
                        WHEN 'Christian Riester'        THEN 'Riester'
                        WHEN 'Scalable Christian'        THEN 'Scalable'
                        WHEN 'Ing Gemeinschaftsdepot'   THEN 'ING'
                        WHEN 'Christian Trade Republic'  THEN 'Trade Republic'
                        ELSE a.name
                    END,
                ', ' ORDER BY
                    CASE a.name
                        WHEN 'Christian Riester'        THEN 'Riester'
                        WHEN 'Scalable Christian'        THEN 'Scalable'
                        WHEN 'Ing Gemeinschaftsdepot'   THEN 'ING'
                        WHEN 'Christian Trade Republic'  THEN 'Trade Republic'
                        ELSE a.name
                    END
                ) AS depots
            FROM (
                SELECT DISTINCT ON (account_id, isin) account_id, isin
                FROM portfolio.position_snapshot
                WHERE as_of_date <= :ref
                ORDER BY account_id, isin, as_of_date DESC
            ) ps
            JOIN portfolio.account a ON a.account_id = ps.account_id
            GROUP BY ps.isin
        )
        SELECT
            ea.ticker  AS etf_a,
            da.depots  AS depot_a,
            eb.ticker  AS etf_b,
            db.depots  AS depot_b,
            ROUND(SUM(LEAST(ha.weight_pct, hb.weight_pct))::numeric, 1) AS overlap_pct,
            COUNT(*)   AS gemeinsame_titel
        FROM holdings ha
        JOIN holdings hb
            ON ha.constituent_isin = hb.constituent_isin AND ha.etf_isin < hb.etf_isin
        JOIN portfolio.etf ea ON ea.isin = ha.etf_isin
        JOIN portfolio.etf eb ON eb.isin = hb.etf_isin
        LEFT JOIN etf_depots da ON da.isin = ha.etf_isin
        LEFT JOIN etf_depots db ON db.isin = hb.etf_isin
        GROUP BY ea.ticker, da.depots, eb.ticker, db.depots
        HAVING SUM(LEAST(ha.weight_pct, hb.weight_pct)) > 0.5
        ORDER BY overlap_pct DESC
        LIMIT 20
    """, params={"ref": ref_date})


def get_coverage(ref_date: date) -> dict:
    """Abdeckungsquote: welcher Portfoliowert ist Look-Through aufgelöst."""
    df = db_utils.query_df("""
        WITH snap AS (
            SELECT DISTINCT ON (account_id, isin) isin, quantity
            FROM portfolio.position_snapshot
            WHERE as_of_date <= :ref ORDER BY account_id, isin, as_of_date DESC
        ),
        etf_val AS (
            SELECT s.isin AS etf_isin, SUM(s.quantity * p.close) AS etf_eur
            FROM snap s
            JOIN (SELECT DISTINCT ON (isin) isin, close FROM portfolio.price
                  WHERE price_date <= :ref ORDER BY isin, price_date DESC) p ON p.isin = s.isin
            GROUP BY s.isin
        ),
        holdings_covered AS (
            SELECT DISTINCT ON (etf_isin, constituent_isin) etf_isin, weight_pct
            FROM portfolio.etf_holding WHERE as_of_date <= :ref
            ORDER BY etf_isin, constituent_isin, as_of_date DESC
        )
        SELECT
            SUM(ev.etf_eur) AS gesamt_eur,
            SUM(ev.etf_eur * covered.w / 100) AS aufgeloest_eur
        FROM etf_val ev
        LEFT JOIN (
            SELECT etf_isin, SUM(weight_pct) AS w FROM holdings_covered GROUP BY etf_isin
        ) covered ON covered.etf_isin = ev.etf_isin
    """, params={"ref": ref_date})
    row = df.iloc[0]
    gesamt = float(row["gesamt_eur"] or 0)
    aufg   = float(row["aufgeloest_eur"] or 0)
    return {
        "gesamt_eur":     gesamt,
        "aufgeloest_eur": aufg,
        "coverage_pct":   100 * aufg / gesamt if gesamt else 0,
    }


# ── Wertentwicklung: Daten ───────────────────────────────────────────────────

def get_portfolio_timeseries(
    ref_date: date, start_date: date, filter_start: bool = True
) -> pd.DataFrame:
    """Täglicher Portfoliowert über konstante Stückzahlen (letzter Snapshot).

    Pivot + Forward-Fill pro ETF → Feiertage werden mit dem letzten Kurs fortgeschrieben.

    filter_start=True (Standard): nur ETFs mit Kurs am ersten Tag der Periode.
    Verhindert Wertsprünge wenn neue ETFs erst im Laufe des Zeitraums Kurshistorie bekommen
    (z.B. SCWX nach Markteinführung). Macht den Chart vergleichbar mit dem Benchmark.
    """
    price_df = db_utils.query_df("""
        WITH snap AS (
            SELECT DISTINCT ON (account_id, isin) isin, quantity
            FROM portfolio.position_snapshot
            WHERE as_of_date <= :ref
            ORDER BY account_id, isin, as_of_date DESC
        ),
        totals AS (SELECT isin, SUM(quantity) AS qty FROM snap GROUP BY isin)
        SELECT p.price_date, p.isin, p.close::float AS close, p.currency, t.qty::float AS qty
        FROM portfolio.price p
        JOIN totals t ON t.isin = p.isin
        WHERE p.price_date BETWEEN :start AND :ref
        ORDER BY p.price_date, p.isin
    """, params={"ref": ref_date, "start": start_date})

    if price_df.empty:
        return pd.DataFrame(columns=["date", "portfolio_eur"])

    fx_df = db_utils.query_df("""
        SELECT rate_date, currency, eur_per_unit::float
        FROM portfolio.fx_rate
        WHERE rate_date BETWEEN :start AND :ref
        ORDER BY rate_date, currency
    """, params={"ref": ref_date, "start": start_date})

    non_eur = price_df[price_df["currency"] != "EUR"]["currency"].unique()
    if len(non_eur) > 0 and not fx_df.empty:
        fx_rel = fx_df[fx_df["currency"].isin(non_eur)]
        if not fx_rel.empty:
            fx_piv = fx_rel.pivot(index="rate_date", columns="currency", values="eur_per_unit")
            all_dates = sorted(price_df["price_date"].unique())
            fx_piv = fx_piv.reindex(all_dates).ffill().bfill()
            fx_long = (fx_piv.reset_index()
                       .melt(id_vars=["rate_date"], var_name="currency", value_name="eur_per_unit")
                       .rename(columns={"rate_date": "price_date"}))
            price_df = price_df.merge(fx_long, on=["price_date", "currency"], how="left")
        else:
            price_df["eur_per_unit"] = None
    else:
        price_df["eur_per_unit"] = None

    eur_mask = price_df["currency"] == "EUR"
    price_df.loc[eur_mask, "eur_per_unit"] = 1.0
    price_df["eur_per_unit"] = price_df["eur_per_unit"].fillna(1.0)
    price_df["close_eur"] = price_df["close"] * price_df["eur_per_unit"]

    # Pivot: Zeile=Datum, Spalte=ISIN, Wert=EUR-Preis
    # ffill pro Spalte (ISIN) → fehlende Tage (Feiertage, LSE-Schließung) werden
    # mit dem letzten bekannten Kurs fortgeschrieben — identisches Verhalten zu
    # "jüngster Kurs ≤ Datum" in calc_portfolio_value.py
    price_wide = (price_df
                  .pivot_table(index="price_date", columns="isin",
                               values="close_eur", aggfunc="last")
                  .sort_index()
                  .ffill())

    if filter_start and not price_wide.empty:
        # ETFs ohne Kurs am ersten Tag der Periode herausfiltern.
        # Diese ETFs erzeugen einen sichtbaren Sprung im Chart, wenn sie später
        # erstmals Kurshistorie bekommen (z.B. nach Markteinführung oder erst-Download).
        present_at_start = price_wide.iloc[0].notna()
        filtered_out = (~present_at_start).sum()
        if filtered_out:
            log.info(
                "filter_start: %d ETF(s) ohne Kurs am %s ausgeschlossen (Sprung-Prävention)",
                filtered_out, price_wide.index[0],
            )
        price_wide = price_wide.loc[:, present_at_start]

    totals = (price_df[["isin", "qty"]]
              .drop_duplicates("isin")
              .set_index("isin")["qty"])

    common = price_wide.columns.intersection(totals.index)
    port_values = (price_wide[common] * totals[common]).sum(axis=1)

    ts = port_values.reset_index()
    ts.columns = ["date", "portfolio_eur"]
    ts["date"] = pd.to_datetime(ts["date"])
    return ts.sort_values("date").reset_index(drop=True)


def get_portfolio_market_value_actual(ref_date: date) -> pd.DataFrame:
    """Täglicher Portfoliowert mit den historisch tatsächlichen Stückzahlen.

    Anders als get_portfolio_timeseries() (konstante aktuelle Stückzahl rückwirkend
    angewendet) wird hier pro Kurstag je (Depot, ETF) der jüngste Snapshot ≤ diesem
    Tag verwendet — As-of-Join wie in CLAUDE.md Abschnitt 6 beschrieben. Käufe/Verkäufe
    zwischen zwei Snapshots erscheinen als Stufe im Chart, nicht als Kursbewegung getarnt.

    Beginnt am ältesten vorhandenen position_snapshot.as_of_date — davor ist die
    tatsächliche Zusammensetzung unbekannt. Der Startpunkt wandert mit jedem neuen
    monatlichen Snapshot nicht weiter zurück (Historie wächst nur nach vorn).
    """
    bounds = db_utils.query_df(
        "SELECT MIN(as_of_date) AS start_date FROM portfolio.position_snapshot WHERE as_of_date <= :ref",
        params={"ref": ref_date},
    )
    start_date = bounds["start_date"].iloc[0]
    if pd.isna(start_date):
        return pd.DataFrame(columns=["date", "portfolio_eur"])

    snap_df = db_utils.query_df("""
        SELECT account_id, isin, as_of_date, quantity::float AS quantity
        FROM portfolio.position_snapshot
        WHERE as_of_date <= :ref
        ORDER BY account_id, isin, as_of_date
    """, params={"ref": ref_date})

    price_df = db_utils.query_df("""
        SELECT price_date, isin, close::float AS close, currency
        FROM portfolio.price
        WHERE price_date BETWEEN :start AND :ref
        ORDER BY price_date, isin
    """, params={"start": start_date, "ref": ref_date})

    if snap_df.empty or price_df.empty:
        return pd.DataFrame(columns=["date", "portfolio_eur"])

    fx_df = db_utils.query_df("""
        SELECT rate_date, currency, eur_per_unit::float
        FROM portfolio.fx_rate
        WHERE rate_date BETWEEN :start AND :ref
        ORDER BY rate_date, currency
    """, params={"start": start_date, "ref": ref_date})

    non_eur = price_df[price_df["currency"] != "EUR"]["currency"].unique()
    if len(non_eur) > 0 and not fx_df.empty:
        fx_rel = fx_df[fx_df["currency"].isin(non_eur)]
        if not fx_rel.empty:
            fx_piv = fx_rel.pivot(index="rate_date", columns="currency", values="eur_per_unit")
            all_dates = sorted(price_df["price_date"].unique())
            fx_piv = fx_piv.reindex(all_dates).ffill().bfill()
            fx_long = (fx_piv.reset_index()
                       .melt(id_vars=["rate_date"], var_name="currency", value_name="eur_per_unit")
                       .rename(columns={"rate_date": "price_date"}))
            price_df = price_df.merge(fx_long, on=["price_date", "currency"], how="left")
        else:
            price_df["eur_per_unit"] = None
    else:
        price_df["eur_per_unit"] = None

    eur_mask = price_df["currency"] == "EUR"
    price_df.loc[eur_mask, "eur_per_unit"] = 1.0
    price_df["eur_per_unit"] = price_df["eur_per_unit"].fillna(1.0)
    price_df["close_eur"] = price_df["close"] * price_df["eur_per_unit"]

    price_wide = (price_df
                  .pivot_table(index="price_date", columns="isin",
                               values="close_eur", aggfunc="last")
                  .sort_index()
                  .ffill())
    all_dates = price_wide.index

    # Stückzahlen je (Depot, ETF) auf die Kurstage forward-fillen. Union statt reindex,
    # damit Snapshot-Termine, die nicht zufällig auf einen Handelstag fallen, nicht
    # beim reindex verloren gehen.
    qty_wide = snap_df.pivot_table(
        index="as_of_date", columns=["account_id", "isin"], values="quantity", aggfunc="last"
    )
    union_index = qty_wide.index.union(all_dates)
    qty_wide = qty_wide.reindex(union_index).sort_index().ffill().reindex(all_dates)

    # über Depots summieren → eine Spalte je ISIN
    qty_by_isin = qty_wide.T.groupby(level="isin").sum(min_count=1).T

    common = price_wide.columns.intersection(qty_by_isin.columns)
    port_values = (price_wide[common] * qty_by_isin[common]).sum(axis=1, min_count=1)

    ts = port_values.reset_index()
    ts.columns = ["date", "portfolio_eur"]
    ts["date"] = pd.to_datetime(ts["date"])
    return ts.dropna().sort_values("date").reset_index(drop=True)


def get_benchmark_timeseries(ticker: str, start_date: date, ref_date: date) -> pd.DataFrame:
    """Benchmark-Kurse aus portfolio.benchmark_price; Fallback: yfinance.

    Primärquelle ist die DB (nach einmaligem Laden via load_prices.py --since 2023-01-01).
    yfinance-Fallback greift beim ersten Lauf bevor die Benchmark-Tabellen befüllt sind.
    """
    try:
        df = db_utils.query_df("""
            SELECT price_date AS date, close::float AS benchmark
            FROM portfolio.benchmark_price
            WHERE ticker = :ticker
              AND price_date BETWEEN :start AND :ref
            ORDER BY price_date
        """, params={"ticker": ticker, "start": start_date, "ref": ref_date})

        if not df.empty:
            df["date"] = pd.to_datetime(df["date"])
            log.info("Benchmark '%s' – %d Tage aus DB geladen", ticker, len(df))
            return df.sort_values("date").reset_index(drop=True)
    except Exception as exc:
        log.warning("Benchmark-DB-Abfrage fehlgeschlagen: %s", exc)

    # Fallback: yfinance (erster Lauf / Benchmark-Tabelle noch leer)
    try:
        bm_info = db_utils.query_df(
            "SELECT yf_symbol FROM portfolio.benchmark WHERE ticker = :ticker",
            params={"ticker": ticker},
        )
        yf_sym = bm_info["yf_symbol"].iloc[0] if not bm_info.empty else "EXS1.DE"
    except Exception:
        yf_sym = "EXS1.DE"

    log.info("Benchmark nicht in DB – lade via yfinance (%s)", yf_sym)
    try:
        hist = yf.download(
            yf_sym,
            start=start_date.isoformat(),
            end=(ref_date + timedelta(days=1)).isoformat(),
            progress=False,
            auto_adjust=True,
        )
    except Exception as exc:
        log.warning("Benchmark-Download fehlgeschlagen: %s", exc)
        return pd.DataFrame(columns=["date", "benchmark"])

    if hist.empty:
        log.warning("Keine Benchmark-Daten für %s – %s", start_date, ref_date)
        return pd.DataFrame(columns=["date", "benchmark"])

    if isinstance(hist.columns, pd.MultiIndex):
        hist.columns = hist.columns.get_level_values(0)

    close_col = "Close" if "Close" in hist.columns else hist.columns[0]
    df = hist[[close_col]].reset_index()
    df.columns = ["date", "benchmark"]
    df["date"] = pd.to_datetime(df["date"])
    return df.sort_values("date").reset_index(drop=True)


def get_blended_benchmark_timeseries(
    weights: dict[str, float], start_date: date, ref_date: date
) -> pd.DataFrame:
    """Verkettete Misch-Benchmark aus mehreren Einzel-Tickern (z. B. 60/40 World/Bond).

    Wird zur Laufzeit aus den taeglichen Renditen der Komponenten berechnet (gewichtete
    Summe), zu einer Indexreihe verkettet (Basis = 100 am ersten gemeinsamen Datum) —
    kein eigener DB-Eintrag noetig, zieht automatisch nach sobald Komponenten neue Kurse haben.
    Nur Tage, an denen ALLE Komponenten einen Kurs haben, gehen ein (inner join).
    """
    series = {}
    for ticker in weights:
        df = get_benchmark_timeseries(ticker, start_date, ref_date)
        if df.empty:
            log.warning("Misch-Benchmark: Komponente '%s' ohne Kursdaten – übersprungen", ticker)
            return pd.DataFrame(columns=["date", "benchmark"])
        series[ticker] = df.set_index("date")["benchmark"]

    combined = pd.DataFrame(series).dropna(how="any").sort_index()
    if len(combined) < 2:
        return pd.DataFrame(columns=["date", "benchmark"])

    returns = combined.pct_change().fillna(0)
    weight_vec = pd.Series(weights)
    blended_return = returns[weight_vec.index].mul(weight_vec, axis=1).sum(axis=1)
    index_level = 100 * (1 + blended_return).cumprod()
    index_level.iloc[0] = 100.0

    out = index_level.reset_index()
    out.columns = ["date", "benchmark"]
    return out


# ── TWR & Fachkonzept-Kennzahlen (2026-08-17 ergaenzt) ───────────────────────
#
# Ab hier: ECHTE Rendite (transaktionsbasierte TWR aus portfolio_intelligence,
# siehe private-portfolio-tracker/claude.md Abschnitt "spaeter: Transaktions-
# historie -> MWR/TWR"). Im Unterschied zu den obigen Look-Through-Funktionen
# (weiterhin snapshot-basiert, punktuelle Bestandsanalyse - bleibt bewusst
# unveraendert) rechnet dieser Teil mit taeglich cashflow-bereinigter TWR aus
# Phase 12/13 sowie Tracking Error/NormRt/MCTR aus Phase 10/16/17.

def get_twr_index(start_date: date, ref_date: date) -> pd.DataFrame:
    """Gesamtdepot-TWR als Indexreihe (100 = erster Tag der Historie), aus
    twr_daily_total.cumulative_twr_pct. Spaltennamen bewusst identisch zu
    get_portfolio_timeseries() (date/portfolio_eur), damit chart_performance()
    und calc_period_returns() unveraendert wiederverwendet werden koennen -
    beide arbeiten intern nur mit Verhaeltnissen, nicht mit absoluten EUR."""
    df = db_utils.query_df("""
        SELECT valuation_date AS date, (100 * (1 + cumulative_twr_pct / 100.0)) AS portfolio_eur
        FROM portfolio_intelligence.twr_daily_total
        WHERE valuation_date BETWEEN :start AND :ref
        ORDER BY valuation_date
    """, params={"start": start_date, "ref": ref_date})
    if df.empty:
        return df
    df["date"] = pd.to_datetime(df["date"])
    return df


def get_benchmark_index_from_intelligence(code: str, start_date: date, ref_date: date) -> pd.DataFrame:
    """Benchmark-Indexreihe aus portfolio_intelligence.benchmark_performance
    (Phase 4 - bereits fertig materialisiert, inkl. 60/40-Mischbenchmark,
    kein Laufzeit-Verketten wie bei get_blended_benchmark_timeseries() noetig)."""
    df = db_utils.query_df("""
        SELECT bp.performance_date AS date, bp.close_price AS benchmark
        FROM portfolio_intelligence.benchmark_performance bp
        JOIN portfolio_intelligence.benchmark_profile prof ON prof.benchmark_id = bp.benchmark_id
        WHERE prof.code = :code AND bp.performance_date BETWEEN :start AND :ref
        ORDER BY bp.performance_date
    """, params={"code": code, "start": start_date, "ref": ref_date})
    if df.empty:
        return df
    df["date"] = pd.to_datetime(df["date"])
    return df


def get_twr_account_summary() -> pd.DataFrame:
    """Kumulierte TWR je Depot (juengster Stand) - fuer die KPI-Zeile."""
    return db_utils.query_df("""
        SELECT av.account_name, td.cumulative_twr_pct
        FROM portfolio_intelligence.twr_daily td
        JOIN portfolio_intelligence.account_view av ON av.account_view_id = td.account_view_id
        WHERE td.valuation_date = (SELECT MAX(valuation_date) FROM portfolio_intelligence.twr_daily)
        ORDER BY td.cumulative_twr_pct DESC
    """)


def get_te_table() -> pd.DataFrame:
    """Tracking Error je Depot/Gesamt vs. beide Policy-Benchmarks, 3 Fenster -
    direkt aus der Dashboard-View, keine eigene Logik noetig."""
    return db_utils.query_df("""
        SELECT series_name, benchmark_code, window_days, tracking_error_pct, is_full_window
        FROM portfolio_intelligence.v_dashboard_portfolio_te
        ORDER BY CASE WHEN series_name = 'Gesamtportfolio' THEN 0 ELSE 1 END, series_name, benchmark_code, window_days
    """)


def get_mandate_ampel_table(window_days: int = 60) -> pd.DataFrame:
    """TE-Limit-Auslastung je Mandat ggue. MSCI World, 60-Tage-Fenster -
    zeigt nur Mandate mit gesetztem Limit (Phase 11), sortiert nach Ampel-
    Schwere (rot zuerst)."""
    return db_utils.query_df("""
        SELECT a.name, ter.tracking_error_annualized AS te_pct, ter.te_limit_pct,
               ter.utilization_pct, ter.traffic_light
        FROM portfolio_intelligence.tracking_error_rolling ter
        JOIN portfolio_intelligence.etf_mandate em ON em.mandate_id = ter.entity_id
        JOIN portfolio_intelligence.asset a ON a.asset_id = em.asset_id
        JOIN portfolio_intelligence.benchmark_profile bp ON bp.benchmark_id = ter.benchmark_id
        WHERE ter.entity_type = 'mandate' AND ter.window_days = :wd AND bp.code = 'MSCI_WORLD'
          AND ter.te_limit_pct IS NOT NULL
          AND ter.calc_date = (SELECT MAX(calc_date) FROM portfolio_intelligence.tracking_error_rolling)
        ORDER BY CASE ter.traffic_light WHEN 'red' THEN 0 WHEN 'yellow' THEN 1 ELSE 2 END,
                 ter.utilization_pct DESC
    """, params={"wd": window_days})


def get_total_market_value(start_date: date | None, ref_date: date) -> pd.DataFrame:
    """Marktwertverlauf Gesamtdepot, transaktionsbasiert (entity_daily_valuation,
    entity_type='total' - siehe Phase-9-Migration 2026-08-17, volle Historie
    seit 2018 statt vorher 41 Tage aus dem monatlichen Snapshot). Ersetzt
    get_portfolio_market_value_actual() als Datenquelle fuer den Marktwert-
    Chart. start_date=None laedt die gesamte verfuegbare Historie."""
    params = {"ref": ref_date}
    where_start = ""
    if start_date is not None:
        where_start = "AND valuation_date >= :start"
        params["start"] = start_date
    df = db_utils.query_df(f"""
        SELECT valuation_date AS date, market_value_eur AS portfolio_eur
        FROM portfolio_intelligence.entity_daily_valuation
        WHERE entity_type = 'total' AND valuation_date <= :ref {where_start}
        ORDER BY valuation_date
    """, params=params)
    if df.empty:
        return df
    df["date"] = pd.to_datetime(df["date"])
    return df


def get_mctr_top(benchmark_code: str = "MSCI_WORLD", window_days: int = 60, n: int = 8) -> pd.DataFrame:
    """Groesste Risikobeitraege zum Gesamtportfolio-TE (Marginal Contribution
    to Tracking Error, Phase 17), absteigend nach |Anteil am Portfolio-TE|."""
    return db_utils.query_df("""
        SELECT a.name, m.weight_pct, m.mctr_pct_pa, m.ctr_pct_pa, m.pct_of_total_te
        FROM portfolio_intelligence.mctr_snapshot m
        JOIN portfolio_intelligence.benchmark_profile bp ON bp.benchmark_id = m.benchmark_id
        JOIN portfolio_intelligence.asset a ON a.asset_id = m.mandate_asset_id
        WHERE bp.code = :code AND m.window_days = :wd
        ORDER BY ABS(m.pct_of_total_te) DESC
        LIMIT :n
    """, params={"code": benchmark_code, "wd": window_days, "n": n})


def _ts_at_or_before(ts: pd.Series, target: date) -> float | None:
    """Wert am letzten verfügbaren Datum ≤ target (ts.index = DatetimeIndex)."""
    mask = ts.index <= pd.Timestamp(target)
    if not mask.any():
        return None
    return float(ts[mask].iloc[-1])


def _period_perf(ts: pd.Series, start: date, end: date) -> float | None:
    """%-Änderung von start bis end. Gibt None zurück wenn Daten fehlen."""
    v_start = _ts_at_or_before(ts, start)
    v_end   = _ts_at_or_before(ts, end)
    if v_start is None or v_end is None or v_start == 0:
        return None
    return (v_end / v_start - 1) * 100


def calc_period_returns(
    df_port: pd.DataFrame, df_bench: pd.DataFrame, ref_date: date
) -> list[dict]:
    """Perioden-Performance: 1T, 1W, 1M, Q1–Q4, Y-1, Y-2, Y-3."""
    if df_port.empty:
        return []

    port  = df_port.set_index("date")["portfolio_eur"]
    bench = df_bench.set_index("date")["benchmark"] if not df_bench.empty else pd.Series(dtype=float)

    year = ref_date.year

    def q_start(q: int) -> date:
        return date(year, (q - 1) * 3 + 1, 1)

    def q_end(q: int) -> date:
        m = q * 3
        return date(year, m, calendar.monthrange(year, m)[1])

    periods: list[dict] = []

    # Kurzfristige Perioden
    for label, delta_days in [("1T", 1), ("1W", 7), ("1M", 30)]:
        start = ref_date - timedelta(days=delta_days)
        p = _period_perf(port, start, ref_date)
        b = _period_perf(bench, start, ref_date) if not bench.empty else None
        periods.append({
            "label": label,
            "port": p,
            "bench": b,
            "diff": (p - b) if p is not None and b is not None else None,
            "future": False,
        })

    # Quartale des laufenden Jahres
    for q in range(1, 5):
        qs, qe = q_start(q), q_end(q)
        if qs > ref_date:
            periods.append({"label": f"Q{q}", "port": None, "bench": None, "diff": None, "future": True})
            continue
        effective_end = min(qe, ref_date)
        suffix = " (lfd.)" if qe > ref_date else ""
        p = _period_perf(port, qs, effective_end)
        b = _period_perf(bench, qs, effective_end) if not bench.empty else None
        periods.append({
            "label": f"Q{q}{suffix}",
            "port": p,
            "bench": b,
            "diff": (p - b) if p is not None and b is not None else None,
            "future": False,
        })

    # Vorjahre
    for y_back in range(1, 4):
        y = year - y_back
        ys, ye = date(y, 1, 1), date(y, 12, 31)
        p = _period_perf(port, ys, ye)
        b = _period_perf(bench, ys, ye) if not bench.empty else None
        periods.append({
            "label": f"Y-{y_back} ({y})",
            "port": p,
            "bench": b,
            "diff": (p - b) if p is not None and b is not None else None,
            "future": False,
        })

    return periods


# ── Charts ────────────────────────────────────────────────────────────────────

def chart_depot_pie(df_depot: pd.DataFrame) -> str:
    depot_totals = (df_depot.groupby("depot")["wert_eur"]
                    .sum().fillna(0).sort_values(ascending=False))
    fig, ax = plt.subplots(figsize=(5, 4))
    wedges, texts, autotexts = ax.pie(
        depot_totals.values.astype(float),
        labels=None,
        autopct="%1.1f%%",
        colors=PALETTE[:len(depot_totals)],
        startangle=140,
        pctdistance=0.75,
        wedgeprops={"linewidth": 1, "edgecolor": "white"},
    )
    for at in autotexts:
        at.set_fontsize(9)
        at.set_color("white")
        at.set_fontweight("bold")
    ax.legend(depot_totals.index, loc="lower center", bbox_to_anchor=(0.5, -0.15),
              ncol=2, fontsize=8, frameon=False)
    ax.set_title("Depot-Aufteilung", fontsize=11, pad=8, color=SLATE)
    fig.patch.set_facecolor("white")
    return _b64_png(fig)


def chart_top_holdings(df_top: pd.DataFrame, n: int = 20) -> str:
    df = df_top.head(n).copy()
    df["label"] = df["name"].str[:35]
    fig, ax = plt.subplots(figsize=(9, 5.5))
    bars = ax.barh(df["label"][::-1], df["engagement_eur"][::-1],
                   color=BLUE, alpha=0.85, height=0.7)
    ax.xaxis.set_major_formatter(mticker.FuncFormatter(
        lambda x, _: f"{x/1000:.0f}k €" if x >= 1000 else f"{x:.0f} €"))
    ax.set_xlabel("Effektives Engagement (EUR)", fontsize=9, color=SLATE)
    ax.set_title(f"Top-{n} Einzeltitel (Look-Through)", fontsize=11, color=SLATE)
    ax.tick_params(axis="y", labelsize=8)
    ax.tick_params(axis="x", labelsize=8)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.grid(axis="x", alpha=0.3)
    fig.patch.set_facecolor("white")
    fig.tight_layout()
    return _b64_png(fig)


def chart_sector(df_sector: pd.DataFrame) -> str:
    df = df_sector[df_sector["pct"] >= 0.5].copy()
    fig, ax = plt.subplots(figsize=(7, 4))
    bars = ax.bar(range(len(df)), df["pct"], color=PALETTE[:len(df)], alpha=0.85, width=0.6)
    ax.set_xticks(range(len(df)))
    ax.set_xticklabels([s[:22] for s in df["sektor"]], rotation=35, ha="right", fontsize=8)
    ax.set_ylabel("Anteil (%)", fontsize=9, color=SLATE)
    ax.set_title("Sektor-Allokation (Look-Through)", fontsize=11, color=SLATE)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.grid(axis="y", alpha=0.3)
    for bar, val in zip(bars, df["pct"]):
        ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + 0.2,
                f"{val:.1f}%", ha="center", fontsize=7.5, color=SLATE)
    fig.patch.set_facecolor("white")
    fig.tight_layout()
    return _b64_png(fig)


def chart_market_value(df_port: pd.DataFrame) -> str:
    """Marktwertverlauf Gesamtdepot in absoluten EUR, mit tatsächlichen historischen
    Stückzahlen (siehe get_portfolio_market_value_actual). Käufe/Verkäufe zwischen zwei
    Snapshots erscheinen als Stufe, nicht als Kursbewegung getarnt.
    """
    if df_port.empty:
        return ""

    ts = df_port.set_index("date")["portfolio_eur"].astype(float)

    fig, ax = plt.subplots(figsize=(10, 3.8))
    ax.plot(ts.index, ts.values, color=BLUE, linewidth=1.6)
    ax.fill_between(ts.index, ts.values, ts.values.min() * 0.98, color=BLUE, alpha=0.08)

    ax.yaxis.set_major_formatter(mticker.FuncFormatter(
        lambda x, _: f"{x/1000:,.0f} k €".replace(",", ".")))
    ax.set_ylabel("Marktwert (EUR)", fontsize=9, color=SLATE)
    ax.set_title("Marktwertverlauf Gesamtdepot (tatsächliche Stückzahlen)", fontsize=11, color=SLATE)
    ax.tick_params(axis="both", labelsize=8)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.grid(axis="y", alpha=0.25)
    ax.xaxis.set_major_locator(mdates.AutoDateLocator())
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%d.%m.%y"))
    fig.patch.set_facecolor("white")
    fig.tight_layout()
    return _b64_png(fig)


def chart_performance(
    df_port: pd.DataFrame, benchmarks: list[tuple[pd.DataFrame, str, str, str]], ref_date: date
) -> str:
    """YTD-Liniendiagramm, alle Serien normiert auf 100 zum Jahresanfang.

    benchmarks: Liste von (df_bench, name, farbe, linestyle).
    """
    year_start = pd.Timestamp(date(ref_date.year, 1, 1))

    port = df_port[df_port["date"] >= year_start].set_index("date")["portfolio_eur"]

    if port.empty:
        return ""

    fig, ax = plt.subplots(figsize=(10, 3.8))

    port_norm = port / port.iloc[0] * 100
    ax.plot(port_norm.index, port_norm.values, color=BLUE,
            linewidth=1.8, label="Portfolio (hypothetisch)")

    for df_bench, name, color, linestyle in benchmarks:
        bench = (df_bench[df_bench["date"] >= year_start].set_index("date")["benchmark"]
                  if not df_bench.empty else pd.Series(dtype=float))
        if bench.empty:
            continue
        common = port.index.intersection(bench.index)
        if len(common) >= 5:
            bench_c = bench.reindex(common)
            bench_norm = bench_c / bench_c.iloc[0] * 100
            ax.plot(bench_norm.index, bench_norm.values, color=color,
                    linewidth=1.5, linestyle=linestyle, label=name)

    ax.axhline(100, color="#CBD5E1", linewidth=0.8, linestyle=":")
    ax.set_ylabel("Indexiert (Jahresanfang = 100)", fontsize=9, color=SLATE)
    ax.legend(fontsize=9, frameon=False, loc="upper left")
    ax.tick_params(axis="both", labelsize=8)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.grid(axis="y", alpha=0.25)
    ax.xaxis.set_major_locator(mdates.MonthLocator())
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%b"))
    fig.patch.set_facecolor("white")
    fig.tight_layout()
    return _b64_png(fig)


# ── HTML-Bausteine ────────────────────────────────────────────────────────────

def _tbl_row(*cells, header=False, cls="") -> str:
    tag = "th" if header else "td"
    inner = "".join(f"<{tag}>{c}</{tag}>" for c in cells)
    row_cls = f' class="{cls}"' if cls else ""
    return f"<tr{row_cls}>{inner}</tr>\n"


def _str_or_dash(val, maxlen: int = 9999) -> str:
    """Gibt val[:maxlen] zurück wenn val ein nicht-leerer String ist, sonst '–'."""
    if isinstance(val, str) and val.strip():
        return val[:maxlen]
    return "–"


def html_depot_table(df: pd.DataFrame) -> str:
    rows = _tbl_row("Depot", "Ticker", "ETF", "Stück", "Kurs", "Wert", header=True)
    prev_depot = None
    for _, r in df.iterrows():
        depot_cell = r["depot"] if r["depot"] != prev_depot else ""
        prev_depot = r["depot"]
        wert = r["wert_eur"]
        rows += _tbl_row(
            depot_cell,
            _str_or_dash(r["ticker"]),
            _str_or_dash(r["etf_name"], 55),
            f"{r['quantity']:,.4f}".replace(",", ".") if pd.notna(r["quantity"]) else "–",
            _fmt_eur(r["kurs"]) if pd.notna(r["kurs"]) else "–",
            _fmt_eur(wert) if pd.notna(wert) else "–",
        )
    # Summen je Depot
    for depot, grp in df.groupby("depot", sort=False):
        summe = grp["wert_eur"].sum()
        rows += _tbl_row("", "", f"<strong>{depot} gesamt</strong>",
                         "", "", f"<strong>{_fmt_eur(summe)}</strong>", cls="subtotal")
    return f'<table class="data-tbl">\n{rows}</table>'


def html_top_holdings(df: pd.DataFrame) -> str:
    rows = _tbl_row("#", "Name", "Land", "Sektor", "EUR", "Anteil", header=True)
    for i, (_, r) in enumerate(df.iterrows(), 1):
        rows += _tbl_row(
            i,
            _str_or_dash(r["name"], 50),
            _str_or_dash(r.get("country")),
            _str_or_dash(r.get("sektor"), 35),
            _fmt_eur(r["engagement_eur"]),
            _fmt_pct(r["anteil_pct"]),
        )
    return f'<table class="data-tbl">\n{rows}</table>'


def html_overlap(df: pd.DataFrame) -> str:
    rows = _tbl_row("ETF A", "Depot(s)", "ETF B", "Depot(s)", "Überlappung", "Gemeinsame Titel",
                    header=True)
    for _, r in df.iterrows():
        rows += _tbl_row(
            r["etf_a"],
            _str_or_dash(r.get("depot_a")),
            r["etf_b"],
            _str_or_dash(r.get("depot_b")),
            _fmt_pct(r["overlap_pct"]),
            int(r["gemeinsame_titel"]),
        )
    return f'<table class="data-tbl">\n{rows}</table>'


def html_country(df: pd.DataFrame) -> str:
    rows = _tbl_row("Land", "EUR", "Anteil", header=True)
    for _, r in df.iterrows():
        rows += _tbl_row(r["land"], _fmt_eur(r["eur"]), _fmt_pct(r["pct"]))
    return f'<table class="data-tbl">\n{rows}</table>'


def html_performance_table(
    periods: list[dict], name1: str,
    periods2: list[dict] | None = None, name2: str | None = None,
) -> str:
    def _cell(val: float | None, future: bool = False) -> str:
        if future or val is None:
            return '<td class="perf-dash">–</td>'
        sign  = "+" if val > 0 else ""
        cls   = "perf-pos" if val > 0 else ("perf-neg" if val < 0 else "perf-zero")
        return f'<td class="{cls}">{sign}{val:.1f} %</td>'

    header = ("<tr>"
              "<th>Periode</th>"
              "<th>Portfolio</th>"
              f"<th>{name1}</th>"
              "<th>Differenz</th>")
    if periods2 is not None:
        header += f"<th>{name2}</th><th>Differenz</th>"
    header += "</tr>\n"

    rows = header
    for i, p in enumerate(periods):
        future = p.get("future", False)
        rows += f"<tr><td>{p['label']}</td>{_cell(p['port'], future)}{_cell(p['bench'], future)}{_cell(p['diff'], future)}"
        if periods2 is not None:
            p2 = periods2[i]
            rows += f"{_cell(p2['bench'], future)}{_cell(p2['diff'], future)}"
        rows += "</tr>\n"
    return f'<table class="data-tbl perf-tbl">\n{rows}</table>'


def html_te_table(df: pd.DataFrame) -> str:
    """Pivotiert auf Zeitfenster-Spalten (20T/60T/252T) je Depot x Benchmark."""
    if df.empty:
        return "<p style='color:#94A3B8;font-size:13px'>Keine TE-Daten verfügbar.</p>"
    piv = df.pivot_table(index=["series_name", "benchmark_code"], columns="window_days",
                          values="tracking_error_pct", aggfunc="first")
    rows = _tbl_row("Depot", "Benchmark", "20 Tage", "60 Tage", "252 Tage", header=True)
    for (name, bench), r in piv.iterrows():
        rows += _tbl_row(
            f"<strong>{name}</strong>" if name == "Gesamtportfolio" else name,
            "MSCI World" if bench == "MSCI_WORLD" else "60/40-Blend",
            f"{r.get(20, float('nan')):.2f} %" if pd.notna(r.get(20)) else "–",
            f"{r.get(60, float('nan')):.2f} %" if pd.notna(r.get(60)) else "–",
            f"{r.get(252, float('nan')):.2f} %" if pd.notna(r.get(252)) else "–",
        )
    return f'<table class="data-tbl">\n{rows}</table>'


_AMPEL_COLOR = {"red": RED, "yellow": ORANGE, "green": GREEN}
_AMPEL_LABEL = {"red": "Rot", "yellow": "Gelb", "green": "Grün"}


def html_ampel_table(df: pd.DataFrame) -> str:
    if df.empty:
        return "<p style='color:#94A3B8;font-size:13px'>Keine Mandate mit gesetztem TE-Limit.</p>"
    rows = _tbl_row("ETF", "TE (60T)", "Limit", "Auslastung", "Status", header=True)
    for _, r in df.iterrows():
        color = _AMPEL_COLOR.get(r["traffic_light"], "#94A3B8")
        label = _AMPEL_LABEL.get(r["traffic_light"], r["traffic_light"] or "–")
        dot = f'<span style="display:inline-block;width:9px;height:9px;border-radius:50%;background:{color};margin-right:6px;vertical-align:middle"></span>'
        rows += _tbl_row(
            _str_or_dash(r["name"], 45),
            f"{r['te_pct']:.2f} %" if pd.notna(r["te_pct"]) else "–",
            f"{r['te_limit_pct']:.1f} %" if pd.notna(r["te_limit_pct"]) else "–",
            f"{r['utilization_pct']:.0f} %" if pd.notna(r["utilization_pct"]) else "–",
            f"{dot}{label}",
        )
    return f'<table class="data-tbl">\n{rows}</table>'


def html_mctr_table(df: pd.DataFrame) -> str:
    if df.empty:
        return "<p style='color:#94A3B8;font-size:13px'>Keine MCTR-Daten verfügbar.</p>"
    rows = _tbl_row("ETF", "Gewicht", "MCTR (p.a.)", "Risikobeitrag", "Anteil am Portfolio-TE", header=True)
    for _, r in df.iterrows():
        rows += _tbl_row(
            _str_or_dash(r["name"], 45),
            f"{r['weight_pct']:.1f} %",
            f"{r['mctr_pct_pa']:.2f} %",
            f"{r['ctr_pct_pa']:.2f} %",
            f"{r['pct_of_total_te']:.1f} %",
        )
    return f'<table class="data-tbl">\n{rows}</table>'


# ── Vollständiger HTML-Report ─────────────────────────────────────────────────

CSS = """
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: 'Segoe UI', system-ui, sans-serif; background: #F8FAFC; color: #1E293B;
       font-size: 14px; line-height: 1.6; }
.page { max-width: 1100px; margin: 0 auto; padding: 24px 16px; }
h1 { font-size: 22px; color: #1E293B; margin-bottom: 4px; }
h2 { font-size: 16px; color: #334155; margin: 32px 0 10px; border-bottom: 2px solid #E2E8F0;
     padding-bottom: 6px; }
h3 { font-size: 13px; color: #64748B; font-weight: 600; margin: 20px 0 6px; }
.subtitle { color: #64748B; font-size: 13px; margin-bottom: 24px; }
.kpi-row { display: flex; gap: 16px; flex-wrap: wrap; margin: 16px 0; }
.kpi { background: white; border-radius: 10px; padding: 16px 22px;
       box-shadow: 0 1px 3px rgba(0,0,0,.08); flex: 1; min-width: 180px; }
.kpi .val { font-size: 22px; font-weight: 700; color: #2563EB; }
.kpi .lbl { font-size: 11px; color: #94A3B8; text-transform: uppercase; letter-spacing: .5px; }
.kpi .sub { font-size: 12px; color: #64748B; margin-top: 2px; }
.section { background: white; border-radius: 10px; padding: 20px 24px;
           box-shadow: 0 1px 3px rgba(0,0,0,.08); margin-bottom: 20px; }
.chart-row { display: flex; gap: 20px; flex-wrap: wrap; }
.chart-row img { border-radius: 8px; max-width: 100%; }
table.data-tbl { width: 100%; border-collapse: collapse; font-size: 13px; }
table.data-tbl th { background: #F1F5F9; color: #475569; font-weight: 600;
                    padding: 7px 10px; text-align: left; border-bottom: 2px solid #E2E8F0; }
table.data-tbl td { padding: 6px 10px; border-bottom: 1px solid #F1F5F9; }
table.data-tbl tr:hover td { background: #F8FAFC; }
table.data-tbl tr.subtotal td { background: #EFF6FF; font-size: 12px; }
.warning { background: #FEF3C7; border-left: 4px solid #F59E0B; padding: 10px 14px;
           border-radius: 0 8px 8px 0; font-size: 13px; color: #92400E; margin-top: 12px; }
.coverage { display: flex; align-items: center; gap: 12px; margin: 12px 0; }
.cov-bar { flex: 1; height: 8px; background: #E2E8F0; border-radius: 4px; overflow: hidden; }
.cov-fill { height: 100%; background: #16A34A; border-radius: 4px; }
footer { text-align: center; color: #94A3B8; font-size: 11px; margin-top: 32px;
         padding-top: 16px; border-top: 1px solid #E2E8F0; }
.perf-tbl td, .perf-tbl th { text-align: right; }
.perf-tbl td:first-child, .perf-tbl th:first-child { text-align: left; font-weight: 600; }
.perf-pos  { color: #16A34A; font-weight: 600; }
.perf-neg  { color: #DC2626; font-weight: 600; }
.perf-zero { color: #64748B; }
.perf-dash { color: #CBD5E1; }
"""


def generate_html(ref_date: date) -> str:
    """2026-08-17 neu zugeschnitten (Nutzerwunsch): aus dem alten Report bleiben
    nur Look-Through (Einzeltitel) und der Marktwertverlauf (jetzt 2 Charts,
    3 Jahre + seit Beginn, aus der vollen Transaktions-Historie statt der alten
    41-Tage-Snapshot-Quelle). Depot-Pie/-Tabelle, Sektor-/Länder-Allokation und
    ETF-Überlappung sind bewusst nicht mehr Teil der Ausgabe (Funktionen bleiben
    im Modul erhalten, falls später wieder gebraucht). Neu: echte TWR-Performance
    vs. Benchmarks und ein Fachkonzept-Kennzahlen-Abschnitt (TE, TE-Limit-Ampel,
    MCTR) - siehe "TWR & Fachkonzept-Kennzahlen" weiter oben im Modul."""
    log.info("Lade Daten für %s …", ref_date)

    df_depot = get_depot_breakdown(ref_date)
    df_top   = get_top_holdings(ref_date, n=30)
    cov      = get_coverage(ref_date)

    log.info("Gesamtwert: %.2f EUR, Look-Through-Abdeckung: %.1f %%",
             cov["gesamt_eur"], cov["coverage_pct"])

    ts_start  = date(ref_date.year - 3, 1, 1)
    ytd_start = date(ref_date.year, 1, 1)

    log.info("Lade Marktwertverlauf (3 Jahre + seit Beginn) …")
    df_value_3y  = get_total_market_value(ts_start, ref_date)
    df_value_all = get_total_market_value(None, ref_date)

    log.info("Lade TWR (transaktionsbasiert) …")
    df_twr_idx     = get_twr_index(ts_start, ref_date)
    df_twr_idx_ytd = get_twr_index(ytd_start, ref_date)
    df_bench_twr1  = get_benchmark_index_from_intelligence("MSCI_WORLD", ts_start, ref_date)
    df_bench_twr2  = get_benchmark_index_from_intelligence("60_40_EQUITY_CREDIT", ts_start, ref_date)
    twr_periods    = calc_period_returns(df_twr_idx, df_bench_twr1, ref_date)
    twr_periods2   = calc_period_returns(df_twr_idx, df_bench_twr2, ref_date)
    df_twr_accounts = get_twr_account_summary()
    twr_by_depot   = dict(zip(df_twr_accounts["account_name"], df_twr_accounts["cumulative_twr_pct"]))

    log.info("Lade Fachkonzept-Kennzahlen (TE, Ampel, MCTR) …")
    df_te    = get_te_table()
    df_ampel = get_mandate_ampel_table()
    df_mctr  = get_mctr_top()

    # Depot-KPIs: EUR-Wert (Snapshot) + echte kumulierte TWR nebeneinander
    depot_summen = df_depot.groupby("depot")["wert_eur"].sum().sort_values(ascending=False)
    gesamt_eur   = depot_summen.sum()
    gesamt_twr   = float(df_twr_idx["portfolio_eur"].iloc[-1] / 100 - 1) * 100 if not df_twr_idx.empty else None

    log.info("Erstelle Charts …")
    img_top      = chart_top_holdings(df_top, n=20)
    img_value_3y  = chart_market_value(df_value_3y)
    img_value_all = chart_market_value(df_value_all)
    img_twr = chart_performance(df_twr_idx_ytd, [
        (df_bench_twr1, BENCHMARK_PRIMARY["name"],   ORANGE, "--"),
        (df_bench_twr2, BENCHMARK_SECONDARY["name"], "#7C3AED", ":"),
    ], ref_date)

    kpis = ""
    for depot, val in depot_summen.items():
        pct = 100 * val / gesamt_eur if gesamt_eur else 0
        twr = twr_by_depot.get(depot)
        twr_sub = f" &nbsp;|&nbsp; TWR {'+' if twr and twr > 0 else ''}{twr:.1f} %" if twr is not None else ""
        kpis += f"""<div class="kpi">
            <div class="val">{_fmt_eur(val)}</div>
            <div class="lbl">{depot}</div>
            <div class="sub">{_fmt_pct(pct)} des Portfolios{twr_sub}</div>
        </div>"""
    kpis += f"""<div class="kpi">
        <div class="val">{_fmt_eur(gesamt_eur)}</div>
        <div class="lbl">Gesamt</div>
        <div class="sub">Stand: {ref_date.strftime('%d.%m.%Y')}{f" &nbsp;|&nbsp; TWR {'+' if gesamt_twr and gesamt_twr > 0 else ''}{gesamt_twr:.1f} %" if gesamt_twr is not None else ""}</div>
    </div>"""

    cov_pct   = cov["coverage_pct"]
    cov_color = "#16A34A" if cov_pct >= 80 else "#EA580C" if cov_pct >= 50 else "#DC2626"

    top1 = df_top.iloc[0] if not df_top.empty else None
    top1_text = (f"Größtes Einzeltitel-Engagement: <strong>{top1['name']}</strong> "
                 f"mit {_fmt_eur(top1['engagement_eur'])} ({_fmt_pct(top1['anteil_pct'])})."
                 if top1 is not None else "")

    ts = datetime.now().strftime("%d.%m.%Y %H:%M Uhr")
    value_all_start = df_value_all["date"].min().strftime("%d.%m.%Y") if not df_value_all.empty else "–"

    html = f"""<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Portfolio-Report {ref_date.strftime('%d.%m.%Y')}</title>
<style>{CSS}</style>
</head>
<body>
<div class="page">

<h1>Portfolio-Report</h1>
<p class="subtitle">Stichtag: {ref_date.strftime('%d. %B %Y')} &nbsp;|&nbsp; Erstellt: {ts}</p>

<!-- ── Gesamtvermögen ── -->
<h2>Gesamtvermögen</h2>
<div class="kpi-row">{kpis}</div>

<!-- ── Performance (echte TWR) ── -->
<h2>Performance (TWR)</h2>
<p style="color:#64748B;font-size:13px;margin-bottom:12px;">
Transaktionsbasierte, cashflow-bereinigte Time-Weighted Return (Phase 12/13) –
echte Rendite, nicht die reine Vermögensentwicklung.
</p>
{"<div class='section'><h3>Indexierte Performance vs. Benchmarks (YTD)</h3><img src='data:image/png;base64," + img_twr + "' alt='TWR-Performance-Chart' style='width:100%;max-width:960px'></div>" if img_twr else "<p style='color:#94A3B8;font-size:13px'>Kein TWR-Chart verfügbar.</p>"}
<div class="section">
{html_performance_table(twr_periods, BENCHMARK_PRIMARY["name"], twr_periods2, BENCHMARK_SECONDARY["name"]) if twr_periods else "<p style='color:#94A3B8;font-size:13px'>Keine TWR-Perioden-Daten verfügbar.</p>"}
</div>

<!-- ── Marktwertverlauf ── -->
<h2>Marktwertverlauf Gesamtdepot</h2>
<div class="section">
<h3>Letzte 3 Jahre</h3>
{"<img src='data:image/png;base64," + img_value_3y + "' alt='Marktwertverlauf 3 Jahre' style='width:100%;max-width:960px'>" if img_value_3y else "<p style='color:#94A3B8;font-size:13px'>Keine Daten.</p>"}
</div>
<div class="section">
<h3>Seit Beginn ({value_all_start})</h3>
{"<img src='data:image/png;base64," + img_value_all + "' alt='Marktwertverlauf seit Beginn' style='width:100%;max-width:960px'>" if img_value_all else "<p style='color:#94A3B8;font-size:13px'>Keine Daten.</p>"}
</div>

<!-- ── Fachkonzept-Kennzahlen ── -->
<h2>Risikokennzahlen (Fachkonzept)</h2>
<p style="color:#64748B;font-size:13px;margin-bottom:12px;">
Tracking Error je Depot/Gesamt vs. beide Policy-Benchmarks (MSCI World, 60/40-Blend),
TE-Limit-Auslastung je Mandat und die größten Risikobeiträge zum Gesamtportfolio-TE
(Marginal Contribution to Tracking Error, Ledoit-Wolf-Kovarianzschätzung).
</p>
<div class="section">
<h3>Tracking Error</h3>
{html_te_table(df_te)}
</div>
<div class="section">
<h3>TE-Limit-Auslastung je Mandat (60 Tage, ggü. MSCI World)</h3>
{html_ampel_table(df_ampel)}
</div>
<div class="section">
<h3>Größte Risikobeiträge (MCTR, 60 Tage, ggü. MSCI World)</h3>
{html_mctr_table(df_mctr)}
</div>

<!-- ── Look-Through ── -->
<h2>Look-Through: Einzeltitel-Engagements</h2>
<p style="color:#64748B;font-size:13px;margin-bottom:12px;">
Effektives Engagement addiert über alle ETFs und Depots hinweg (Kurs × Stückzahl × Gewicht).
{top1_text}
</p>
<div class="coverage">
    <span style="font-size:13px;color:#64748B;white-space:nowrap">Look-Through-Abdeckung:</span>
    <div class="cov-bar"><div class="cov-fill" style="width:{min(cov_pct,100):.0f}%;background:{cov_color}"></div></div>
    <span style="font-size:13px;font-weight:600;color:{cov_color}">{_fmt_pct(cov_pct)}</span>
    <span style="font-size:12px;color:#94A3B8">({_fmt_eur(cov['aufgeloest_eur'])} von {_fmt_eur(cov['gesamt_eur'])})</span>
</div>

<div class="section">
<img src="data:image/png;base64,{img_top}" alt="Top-Titel" style="width:100%;max-width:860px">
<h3>Top-30 Einzeltitel</h3>
{html_top_holdings(df_top)}
</div>

<footer>
Datenquellen: portfolio_intelligence (TWR/TE/NormRt/MCTR, transaktionsbasiert), yfinance (Kurse),
iShares/Xtrackers/Amundi/L&G/SPDR/VanEck/Vanguard (Holdings), OpenFIGI (ISIN-Auflösung).
&nbsp;|&nbsp; Kein Anlageratschlag.
</footer>

</div>
</body>
</html>"""

    return html


# ── Hauptprogramm ─────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Portfolio HTML-Report")
    parser.add_argument("--date", type=date.fromisoformat,
                        default=date.today(), help="Stichtag (YYYY-MM-DD)")
    parser.add_argument("--out",  type=Path, default=None,
                        help="Ausgabedatei (Standard: reports/portfolio_YYYYMMDD.html)")
    args = parser.parse_args()

    ref_date = args.date
    out_path = args.out or (REPORTS_DIR / f"portfolio_{ref_date.strftime('%Y%m%d')}.html")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    html = generate_html(ref_date)
    out_path.write_text(html, encoding="utf-8")
    log.info("Report gespeichert: %s", out_path.resolve())


if __name__ == "__main__":
    main()
