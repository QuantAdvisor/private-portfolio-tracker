"""
Phase 0 – Kurs-Loader (täglich)
Lädt ETF-Schlusskurse und EUR-FX-Kurse via yfinance.

ETF-Preise: bevorzugt Xetra (.DE) → Preis in EUR, kein FX-Umweg nötig.
FX-Kurse: eur_per_unit = 1 EUR = x Fremdwährung kehrt um zu EUR/Einheit.

Aufruf:
    python load_prices.py              # gestern bis heute
    python load_prices.py --days 10    # letzten 10 Tage nachladen
    python load_prices.py --since 2026-01-01

Idempotent: ON CONFLICT DO UPDATE auf (isin, price_date) bzw. (currency, rate_date).
"""

import argparse
import logging
import sys
from datetime import date, timedelta

import pandas as pd
import yfinance as yf

import db_utils

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


# ── Ticker-Mapping ────────────────────────────────────────────────────────────
# Standard: <ticker>.DE (Xetra, Preis in EUR).
# Ausnahmen werden hier überschrieben.
# ⚠️ Bitte prüfen und ggf. korrigieren – yfinance-Ticker können von
#    Börsenkürzel abweichen (insb. Amundi, VanEck, Vanguard).

YF_TICKER_OVERRIDE: dict[str, str] = {
    # VanEck: Euronext Amsterdam
    "NL0011683594": "TDIV.AS",
    # Vanguard FTSE Developed World Dist: Euronext Amsterdam (verifiziert via yfinance.info)
    "IE00BKX55T58": "VEVE.AS",
    # Scalable MSCI AC World: Xetra-Ticker (verifiziert durch Nutzer)
    "LU2903252349": "SCWX.DE",
    # Amundi MSCI Europe Min Vol: Xetra (verifiziert durch Nutzer)
    "LU1681041627": "MIVA.DE",
    # Amundi MSCI Europe Growth: Euronext Paris (verifiziert durch Nutzer)
    "LU1681042435": "CG9.PA",
    # Amundi SDAX: Xetra (verifiziert durch Nutzer)
    "LU2611732475": "C005.DE",
    # L&G Global Quality Dividends: Xetra (verifiziert durch Nutzer)
    "IE0005AJA0P1": "LDGL.DE",
    # Xtrackers MSCI World Min Vol: Xetra
    "IE00BL25JN58": "XDEB.DE",
    # iShares EM Min Vol: Xetra (Ticker EUNZ)
    "IE00B8KGV557": "EUNZ.DE",
    # Xtrackers Euro Stoxx Quality Dividend: Borsa Italiana (Mailand) – Ticker EXSG existiert nicht auf Xetra
    "LU0292095535": "XD3E.MI",
    # yfinance hat MVEA.DE und IQQ0.DE intern vertauscht (Datenfehler yfinance):
    # MVEA.DE liefert den Preis von iShares USA MinVol ESG (~7,38 EUR)
    # IQQ0.DE liefert den Preis von iShares World MinVol (~63,46 EUR)
    # → Tickers kreuzen, damit jede ISIN den richtigen Preis bekommt.
    # Verifiziert gegen DWS-Depot-App: IE00B8FHGS14 = 63,46 EUR, IE00BKVL7331 = 7,38 EUR
    "IE00B8FHGS14": "IQQ0.DE",   # iShares World MinVol  → yfinance-Symbol gibt ~63,46 EUR
    "IE00BKVL7331": "MVEA.DE",   # iShares USA MinVol ESG → yfinance-Symbol gibt ~7,38 EUR
    # yfinance-Datenmix zwischen CEMR.DE und SXR7.DE (beide iShares Europe-ETFs auf Xetra):
    # CEMR.DE (iShares Core MSCI EMU, IE00B53QG562) → liefert Momentum-Daten (~16 EUR) [falsch]
    # SXR7.DE (iShares MSCI Europe Momentum, IE00BQN1K786) → liefert EMU-Daten (~246 EUR) [falsch]
    # Fixes: EMU via SIX (CSEMU.SW), Momentum via CEMR.DE (das liefert ~16 EUR ✓)
    # Verifiziert: EMU gegen ING-App (~245,90 EUR), Momentum gegen TR-App (~16,01 EUR)
    "IE00B53QG562": "CSEMU.SW",  # iShares Core MSCI EMU → SIX gibt ~246 EUR ✓
    "IE00BQN1K786": "CEMR.DE",   # iShares MSCI Europe Momentum → yfinance CEMR.DE gibt ~16 EUR ✓
    # yfinance IS3N.DE liefert ~49,81 EUR (falscher Preis / falsche Share-Klasse).
    # Korrekt: IEMA.AS (Euronext Amsterdam, EUR-gelistet, iShares MSCI EM Acc).
    # Verifiziert gegen ING-App: ISHSIII-MSCI EM USD(ACC) = 58,502 EUR
    "IE00B4L5YC18": "IEMA.AS",   # iShares MSCI EM Acc → yfinance-Symbol gibt ~58,61 EUR
    # Oskar VL-Sparplan – Preis-Backfill (siehe PROJECT_PLAN.md Abschnitt 14-E):
    "IE00BFXR5W90": "LGAG.L",     # L&G Asia Pacific ex Japan – LSE, quotiert in GBp (Pence)
    "IE00BKS7L097": "SPXE.L",     # Invesco S&P 500 Scored & Screened – LSE, quotiert in USD
    "IE00BKSCBX74": "WSCSRI.SW",  # UBS MSCI World Small Cap SRI – SIX, quotiert in USD
    "IE000O5FBC47": "CLMT.L",     # Amundi S&P 500 Climate Paris Aligned – LSE, quotiert in GBP (nicht Pence)
}

# ISINs, für die kein yfinance-Ticker existiert.
# Preise müssen manuell oder über eine andere Quelle nachgetragen werden.
# Betroffene ETFs: L&G (nicht auf yfinance indexiert), 3 Amundi ETFs
# (Euronext Paris / Xetra, aber ohne yfinance-Coverage).
YF_NO_PRICE: set[str] = set()  # alle 24 ETFs haben nun einen yfinance-Ticker

# Für LSE-Ticker: yfinance liefert Preise in GBp (Pence) → durch 100 teilen
GBP_PENCE_TICKERS: set[str] = {"LGQD.L", "LGAG.L"}

# LSE/SIX-Ticker mit USD-Handelswährung (verifiziert: Preisniveau passt nicht zu GBp/CHF)
USD_TICKERS: set[str] = {"SPXE.L", "WSCSRI.SW"}

# LSE-Ticker, die (untypisch) direkt in GBP statt GBp notieren (verifiziert am Preisniveau)
GBP_DIRECT_TICKERS: set[str] = {"CLMT.L"}


# ── FX-Kurse ──────────────────────────────────────────────────────────────────
# EUR/<currency>=X gibt an, wie viele Fremdwährungseinheiten 1 EUR kostet.
# eur_per_unit = 1 / rate (= wie viel EUR ist 1 Fremdwährungseinheit).
FX_PAIRS: dict[str, str] = {
    "USD": "EURUSD=X",
    "GBP": "EURGBP=X",
    "CHF": "EURCHF=X",
    "JPY": "EURJPY=X",
    "GBp": "EURGBP=X",   # GBp (Pence) → gleicher Kurs, Faktor 100 separat
}


# ── Hilfsfunktionen ───────────────────────────────────────────────────────────

def _date_range(start: date, end: date) -> tuple[str, str]:
    return start.isoformat(), (end + timedelta(days=1)).isoformat()


def _last_price_date() -> date:
    """Neuestes gespeichertes Kurs-Datum aus der DB, oder vor 5 Tagen."""
    try:
        result = db_utils.query_df("SELECT MAX(price_date) AS d FROM portfolio.price")
        val = result["d"].iloc[0]
        if val is not None:
            return val if isinstance(val, date) else val.date()
    except Exception:
        pass
    return date.today() - timedelta(days=5)


# ── Kurs-Loader ───────────────────────────────────────────────────────────────

def load_etf_prices(start: date, end: date) -> int:
    etfs = db_utils.query_df("SELECT isin, ticker FROM portfolio.etf WHERE ticker IS NOT NULL")
    if etfs.empty:
        log.warning("Keine ETFs in portfolio.etf gefunden.")
        return 0

    upsert_sql = """
        INSERT INTO portfolio.price (isin, price_date, close, currency, source)
        VALUES (:isin, :price_date, :close, :currency, :source)
        ON CONFLICT (isin, price_date) DO UPDATE SET
            close     = EXCLUDED.close,
            currency  = EXCLUDED.currency,
            source    = EXCLUDED.source
    """

    start_str, end_str = _date_range(start, end)
    total_rows = 0
    failed: list[str] = []

    no_price_isins = [i for i in YF_NO_PRICE if i in etfs["isin"].values]
    if no_price_isins:
        log.info(
            "Kein yfinance-Ticker für %d ETFs (bekannte Lücke) – manuell nachtragen:\n  %s",
            len(no_price_isins),
            "\n  ".join(no_price_isins),
        )

    for _, row in etfs.iterrows():
        isin   = row["isin"]
        if isin in YF_NO_PRICE:
            continue
        ticker = row["ticker"]
        yf_sym = YF_TICKER_OVERRIDE.get(isin, f"{ticker}.DE")

        try:
            # auto_adjust=False (bewusst, verifiziert 2026-08-16): yfinance passt bei
            # auto_adjust=True die HISTORISCHEN Schlusskurse rueckwirkend um alle seither
            # gezahlten Dividenden nach unten an (Total-Return-Stil) - bei ausschuettenden
            # ("Dist") ETFs verzerrt das jeden alten Kurs umso staerker, je laenger die
            # Dividendenhistorie seither ist. Dieses System trackt Dividenden bereits separat
            # als echte Cashflows (portfolio.transaction, txn_type=DIVIDEND) - eine zusaetzliche
            # rueckwirkende Preisanpassung fuehrt zu einer Diskrepanz zwischen dem im System
          # gespeicherten Kurs und dem tatsaechlich vom Broker abgerechneten Kurs.
            # Konkret gefunden: IE00B0M63060 (IQQD, Dist) zeigte am 2023-11-01 einen
            # adjustierten Kurs von 6,34 EUR, waehrend der echte Scalable-Abrechnungskurs
            # 7,301 EUR betrug (der unadjustierte Kurs 7,345 EUR liegt nur 0,6% daneben -
            # normale Handelsspanne). Dieser ~13%-Fehler zog einen Phantom-Tagesverlust von
            # -17% in Scalables TWR-Kette. auto_adjust=False behebt das systematisch fuer
            # alle ausschuettenden ETFs im Preisbestand, nicht nur diesen einen Fall.
            hist = yf.download(
                yf_sym,
                start=start_str,
                end=end_str,
                progress=False,
                auto_adjust=False,
            )
        except Exception as exc:
            log.warning("%-14s (%s) – Download-Fehler: %s", isin, yf_sym, exc)
            failed.append(yf_sym)
            continue

        if hist.empty:
            log.warning("%-14s (%s) – Keine Kursdaten für %s–%s", isin, yf_sym, start, end)
            failed.append(yf_sym)
            continue

        # yfinance gibt MultiIndex-Columns zurück wenn multi=True (Standardfall)
        if isinstance(hist.columns, pd.MultiIndex):
            hist.columns = hist.columns.get_level_values(0)

        close_col = "Close" if "Close" in hist.columns else hist.columns[0]
        if yf_sym in GBP_PENCE_TICKERS:
            currency = "GBp"
        elif yf_sym in USD_TICKERS:
            currency = "USD"
        elif yf_sym in GBP_DIRECT_TICKERS:
            currency = "GBP"
        else:
            currency = "EUR"

        records = []
        for idx, price_row in hist.iterrows():
            close_val = price_row[close_col]
            if pd.isna(close_val):
                continue
            # GBp → GBP
            if yf_sym in GBP_PENCE_TICKERS:
                close_val = float(close_val) / 100.0
                currency  = "GBP"
            records.append({
                "isin":       isin,
                "price_date": idx.date() if hasattr(idx, "date") else idx,
                "close":      round(float(close_val), 6),
                "currency":   currency,
                "source":     "yfinance",
            })

        if records:
            db_utils.execute_many(upsert_sql, records)
            log.info("%-14s (%s) – %d Kurse gespeichert", isin, yf_sym, len(records))
            total_rows += len(records)
        else:
            log.warning("%-14s (%s) – Alle Kurse NaN", isin, yf_sym)
            failed.append(yf_sym)

    if failed:
        log.warning(
            "\n%d Ticker ohne Kursdaten – bitte YF_TICKER_OVERRIDE prüfen:\n  %s",
            len(failed),
            "\n  ".join(failed),
        )
    return total_rows


# ── FX-Loader ─────────────────────────────────────────────────────────────────

def load_fx_rates(start: date, end: date) -> int:
    upsert_sql = """
        INSERT INTO portfolio.fx_rate (rate_date, currency, eur_per_unit)
        VALUES (:rate_date, :currency, :eur_per_unit)
        ON CONFLICT (rate_date, currency) DO UPDATE SET
            eur_per_unit = EXCLUDED.eur_per_unit
    """

    start_str, end_str = _date_range(start, end)
    total_rows = 0

    for currency, yf_sym in FX_PAIRS.items():
        if currency == "GBp":
            continue  # wird aus GBP abgeleitet

        try:
            hist = yf.download(yf_sym, start=start_str, end=end_str,
                               progress=False, auto_adjust=True)
        except Exception as exc:
            log.warning("FX %s (%s) – Download-Fehler: %s", currency, yf_sym, exc)
            continue

        if hist.empty:
            log.warning("FX %s (%s) – Keine Daten", currency, yf_sym)
            continue

        if isinstance(hist.columns, pd.MultiIndex):
            hist.columns = hist.columns.get_level_values(0)

        close_col = "Close" if "Close" in hist.columns else hist.columns[0]

        records = []
        for idx, fx_row in hist.iterrows():
            rate = fx_row[close_col]   # EUR/<currency>: 1 EUR = rate units
            if pd.isna(rate) or float(rate) == 0:
                continue
            eur_per_unit = round(1.0 / float(rate), 8)
            price_date   = idx.date() if hasattr(idx, "date") else idx
            records.append({
                "rate_date":    price_date,
                "currency":     currency,
                "eur_per_unit": eur_per_unit,
            })
            # GBp = GBP / 100
            records.append({
                "rate_date":    price_date,
                "currency":     "GBp",
                "eur_per_unit": round(eur_per_unit / 100.0, 10),
            })

        if records:
            # Duplikate durch GBp-Eintrag deduplizieren
            seen = set()
            deduped = []
            for r in records:
                key = (r["rate_date"], r["currency"])
                if key not in seen:
                    seen.add(key)
                    deduped.append(r)
            db_utils.execute_many(upsert_sql, deduped)
            log.info("FX %-4s (%s) – %d Kurse gespeichert",
                     currency, yf_sym, len([r for r in deduped if r["currency"] == currency]))
            total_rows += len(deduped)

    return total_rows


# ── Benchmark-Loader ──────────────────────────────────────────────────────────

def load_benchmark_prices(start: date, end: date) -> int:
    """Lädt Benchmark-Schlusskurse (EUR) aus yfinance → portfolio.benchmark_price."""
    try:
        benchmarks = db_utils.query_df(
            "SELECT ticker, yf_symbol, currency FROM portfolio.benchmark"
        )
    except Exception as exc:
        log.warning("portfolio.benchmark nicht gefunden (Migration nötig?): %s", exc)
        return 0

    if benchmarks.empty:
        return 0

    upsert_sql = """
        INSERT INTO portfolio.benchmark_price (ticker, price_date, close)
        VALUES (:ticker, :price_date, :close)
        ON CONFLICT (ticker, price_date) DO UPDATE SET close = EXCLUDED.close
    """

    start_str, end_str = _date_range(start, end)
    total_rows = 0

    for _, row in benchmarks.iterrows():
        bm_ticker = row["ticker"]
        yf_sym    = row["yf_symbol"]

        try:
            hist = yf.download(
                yf_sym, start=start_str, end=end_str, progress=False, auto_adjust=True
            )
        except Exception as exc:
            log.warning("Benchmark %-15s (%s) – Download-Fehler: %s", bm_ticker, yf_sym, exc)
            continue

        if hist.empty:
            log.warning("Benchmark %-15s (%s) – Keine Daten", bm_ticker, yf_sym)
            continue

        if isinstance(hist.columns, pd.MultiIndex):
            hist.columns = hist.columns.get_level_values(0)

        close_col = "Close" if "Close" in hist.columns else hist.columns[0]

        records = []
        for idx, price_row in hist.iterrows():
            close_val = price_row[close_col]
            if pd.isna(close_val):
                continue
            records.append({
                "ticker":     bm_ticker,
                "price_date": idx.date() if hasattr(idx, "date") else idx,
                "close":      round(float(close_val), 6),
            })

        if records:
            db_utils.execute_many(upsert_sql, records)
            log.info("Benchmark %-15s (%s) – %d Kurse gespeichert", bm_ticker, yf_sym, len(records))
            total_rows += len(records)
        else:
            log.warning("Benchmark %-15s (%s) – Alle Kurse NaN", bm_ticker, yf_sym)

    return total_rows


# ── Hauptprogramm ─────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Lade ETF- und FX-Kurse in portfolio.price / fx_rate")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--days",  type=int,              help="Letzte N Tage laden")
    group.add_argument("--since", type=date.fromisoformat, help="Start-Datum (YYYY-MM-DD)")
    args = parser.parse_args()

    today = date.today()

    if args.days:
        start = today - timedelta(days=args.days)
    elif args.since:
        start = args.since
    else:
        # Standard: ab letztem gespeicherten Datum (oder -5 Tage)
        start = _last_price_date()

    log.info("Lade Kurse %s → %s", start, today)

    n_prices = load_etf_prices(start, today)
    n_fx     = load_fx_rates(start, today)
    n_bench  = load_benchmark_prices(start, today)

    log.info("Fertig: %d ETF-Kurszeilen, %d FX-Zeilen, %d Benchmark-Zeilen geschrieben.",
             n_prices, n_fx, n_bench)


if __name__ == "__main__":
    main()
