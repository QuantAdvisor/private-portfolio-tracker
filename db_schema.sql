-- ============================================================
-- ETF Look-Through Tracker – Datenbankschema
-- Schema: portfolio  (getrennt von quant_advisor)
--
-- Ausführen auf dem Server:
--   psql -U christian_schott -d quant_advisor -f db_schema.sql
-- ============================================================

CREATE SCHEMA IF NOT EXISTS portfolio;

-- ── Depots ───────────────────────────────────────────────────────────────────
-- Statische Liste der eigenen Depots.
CREATE TABLE IF NOT EXISTS portfolio.account (
    account_id   SERIAL       PRIMARY KEY,
    name         VARCHAR(100) NOT NULL UNIQUE,   -- z. B. 'Scalable Christian'
    broker       VARCHAR(50),                    -- 'Scalable', 'ING', 'TradeRepublic'
    account_type VARCHAR(30)                     -- 'Brokerdepot', 'Riester', 'Gemeinschaftsdepot'
);

-- ── ETF-Stammdaten ────────────────────────────────────────────────────────────
-- Gehaltene ETFs (24 Stück) + geplante Altersvorsorge-Ziel-ETFs (9 Stück, noch nicht investiert).
CREATE TABLE IF NOT EXISTS portfolio.etf (
    isin         VARCHAR(12)  PRIMARY KEY,
    name         VARCHAR(200) NOT NULL,
    ticker       VARCHAR(20),
    currency     VARCHAR(3),                     -- Handelswährung des ETF
    emittent     VARCHAR(50),                    -- 'iShares', 'Xtrackers', 'Amundi', …
    index_name   VARCHAR(200)                    -- z. B. 'MSCI World Min Vol'
);

-- ── Konstituenten-Stammdaten ──────────────────────────────────────────────────
-- Einzeltitel innerhalb der ETFs. Selbes Format wie quant_advisor.dim_stock_ticker,
-- aber NULL-Constraints gelockert (viele Quellen liefern keine WKN / kein Ticker).
--
-- Befüllung:
--   - Quellen mit ISIN: direkt → portfolio.constituent
--   - Quellen ohne ISIN (iShares CSV, Vanguard): erst → portfolio.unresolved_holding,
--     nach Ticker→ISIN-Auflösung (ticker_isin_map) → portfolio.constituent
CREATE TABLE IF NOT EXISTS portfolio.constituent (
    isin         VARCHAR(12)  PRIMARY KEY,
    wkn          VARCHAR(6),                     -- meist NULL (kein Export-Standard)
    ticker       VARCHAR(20),                    -- Exchange-Ticker, z. B. 'AAPL'
    name         VARCHAR(200) NOT NULL,
    sektor       VARCHAR(100),                   -- z. B. 'IT', 'Finanzwesen'
    industry     VARCHAR(100),
    country      VARCHAR(5),                     -- ISO-2, z. B. 'DE', 'US'
    currency     VARCHAR(3)                      -- ISO-3, z. B. 'EUR', 'USD'
);

-- ── Ticker→ISIN-Mapping ───────────────────────────────────────────────────────
-- Für iShares CSV (kein ISIN, nur Ticker) und Vanguard (kein ISIN, nur Ticker).
-- Manuell befüllen oder via yfinance / OpenFIGI anreichern.
CREATE TABLE IF NOT EXISTS portfolio.ticker_isin_map (
    ticker       VARCHAR(20)  PRIMARY KEY,
    isin         VARCHAR(12)  NOT NULL REFERENCES portfolio.constituent(isin),
    source       VARCHAR(20)  NOT NULL DEFAULT 'manual', -- 'manual', 'yahoo', 'openfigi'
    verified_at  DATE         NOT NULL DEFAULT CURRENT_DATE
);

-- ── Ungelöste Holdings (Staging) ──────────────────────────────────────────────
-- Einträge aus iShares CSV / Vanguard, bei denen der Ticker noch nicht in
-- ticker_isin_map steht. Werden nach Auflösung in etf_holding überführt.
-- Idempotent: bei erneutem Load werden bestehende Zeilen überschrieben.
CREATE TABLE IF NOT EXISTS portfolio.unresolved_holding (
    etf_isin     VARCHAR(12)  NOT NULL REFERENCES portfolio.etf(isin),
    as_of_date   DATE         NOT NULL,
    raw_ticker   VARCHAR(20)  NOT NULL,
    raw_name     VARCHAR(200),
    weight_pct   NUMERIC(10, 6) NOT NULL,        -- in Prozent (0–100)
    sektor       VARCHAR(100),
    country      VARCHAR(5),
    currency     VARCHAR(3),
    loaded_at    TIMESTAMPTZ  NOT NULL DEFAULT now(),
    PRIMARY KEY (etf_isin, as_of_date, raw_ticker)
);

-- ── ETF-Holdings-Snapshots ────────────────────────────────────────────────────
-- Bestandteile je ETF pro Snapshot-Datum. APPEND-only — nie überschreiben.
-- weight_pct in Prozent (z. B. 10.36 für 10,36 %).
-- Summe je (etf_isin, as_of_date) sollte ~100 % ergeben (Rundungsdifferenzen möglich).
CREATE TABLE IF NOT EXISTS portfolio.etf_holding (
    etf_isin         VARCHAR(12)  NOT NULL REFERENCES portfolio.etf(isin),
    as_of_date       DATE         NOT NULL,
    constituent_isin VARCHAR(12)  NOT NULL REFERENCES portfolio.constituent(isin),
    weight_pct       NUMERIC(10, 6) NOT NULL,    -- in Prozent (0–100)
    source_file      VARCHAR(200),               -- Dateiname des Quelldokuments (Audit)
    loaded_at        TIMESTAMPTZ  NOT NULL DEFAULT now(),
    PRIMARY KEY (etf_isin, as_of_date, constituent_isin)
);

-- ── Eigene Positionen ─────────────────────────────────────────────────────────
-- Stückzahlen je Depot und ETF, datierter Monatssnapshot. APPEND-only.
-- avg_cost NULL für Riester (Kaufkurs unbekannt).
CREATE TABLE IF NOT EXISTS portfolio.position_snapshot (
    account_id   INTEGER      NOT NULL REFERENCES portfolio.account(account_id),
    isin         VARCHAR(12)  NOT NULL REFERENCES portfolio.etf(isin),
    as_of_date   DATE         NOT NULL,
    quantity     NUMERIC(18, 6) NOT NULL,
    avg_cost     NUMERIC(18, 4),                 -- Kaufkurs in ETF-Handelswährung; NULL = unbekannt
    loaded_at    TIMESTAMPTZ  NOT NULL DEFAULT now(),
    PRIMARY KEY (account_id, isin, as_of_date)
);

-- ── ETF-Kurse (täglich) ───────────────────────────────────────────────────────
-- Schlusskurs in Handelswährung des ETF (nicht unbedingt EUR).
-- EUR-Umrechnung erfolgt zur Abfragezeit via fx_rate.
-- Täglicher Cronjob; idempotent via ON CONFLICT DO UPDATE.
CREATE TABLE IF NOT EXISTS portfolio.price (
    isin         VARCHAR(12)  NOT NULL REFERENCES portfolio.etf(isin),
    price_date   DATE         NOT NULL,
    close        NUMERIC(18, 6) NOT NULL,
    currency     VARCHAR(3)   NOT NULL,
    source       VARCHAR(20)  NOT NULL DEFAULT 'yfinance',
    PRIMARY KEY (isin, price_date)
);

-- ── FX-Kurse (täglich) ───────────────────────────────────────────────────────
-- EUR pro Fremdwährungseinheit (eur_per_unit = 1 / EUR-Kurs).
-- EUR selbst wird nicht gespeichert (eur_per_unit = 1.0 implizit).
-- Täglicher Cronjob; idempotent via ON CONFLICT DO UPDATE.
CREATE TABLE IF NOT EXISTS portfolio.fx_rate (
    rate_date    DATE         NOT NULL,
    currency     VARCHAR(3)   NOT NULL,          -- z. B. 'USD', 'GBP', 'CHF'
    eur_per_unit NUMERIC(18, 8) NOT NULL,        -- 1 USD = x EUR
    PRIMARY KEY (rate_date, currency)
);

-- ── Indizes ───────────────────────────────────────────────────────────────────

-- Schneller As-of-Join für Holdings: "jüngster Snapshot ≤ t"
CREATE INDEX IF NOT EXISTS ix_etf_holding_etf_date
    ON portfolio.etf_holding (etf_isin, as_of_date DESC);

-- Schneller As-of-Join für Positionen
CREATE INDEX IF NOT EXISTS ix_position_snapshot_date
    ON portfolio.position_snapshot (account_id, isin, as_of_date DESC);

-- Kurszugriff nach Datum
CREATE INDEX IF NOT EXISTS ix_price_date
    ON portfolio.price (price_date);

CREATE INDEX IF NOT EXISTS ix_fx_rate_date
    ON portfolio.fx_rate (rate_date);

-- Constituenten-Suche nach Name (für Normalisierung)
CREATE INDEX IF NOT EXISTS ix_constituent_name
    ON portfolio.constituent (name);

-- ── Seed-Daten: Depots ────────────────────────────────────────────────────────
-- Einmalig befüllen; ON CONFLICT DO NOTHING für Idempotenz.

INSERT INTO portfolio.account (name, broker, account_type) VALUES
    ('Scalable Christian',        'Scalable',       'Brokerdepot'),
    ('Ing Gemeinschaftsdepot',    'ING',            'Gemeinschaftsdepot'),
    ('Christian Riester',         'DWS/Riester',    'Riester'),
    ('Christian Trade Republic',  'Trade Republic', 'Brokerdepot'),
    ('Christian Oskar VL',        'Oskar',          'VL-Sparplan')
ON CONFLICT (name) DO NOTHING;

-- ── Seed-Daten: ETF-Stammdaten ────────────────────────────────────────────────
INSERT INTO portfolio.etf (isin, name, ticker, currency, emittent, index_name) VALUES
    -- Scalable Christian – Income
    ('IE00B14X4T88', 'iShares Asia Pacific Dividend UCITS ETF (Dist)',              'IQQX', 'EUR', 'iShares',     'Dow Jones Asia/Pacific Select Dividend 30'),
    ('IE00B652H904', 'iShares Emerging Markets Dividend UCITS ETF (Dist)',           'EUNY', 'EUR', 'iShares',     'Dow Jones Emerging Markets Select Dividend'),
    ('IE00B0M63060', 'iShares UK Dividend UCITS ETF (Dist)',                         'IQQD', 'GBP', 'iShares',     'FTSE UK Dividend+'),
    ('IE0005AJA0P1', 'L&G Global Quality Dividends UCITS ETF (Dist)',                'LGQD', 'USD', 'L&G',         'Solactive Quality Dividend Index'),
    ('IE00B6YX5D40', 'SPDR S&P US Dividend Aristocrats UCITS ETF (Dist)',            'SPYD', 'USD', 'SPDR',        'S&P High Yield Dividend Aristocrats'),
    ('NL0011683594', 'VanEck Morningstar Developed Markets Dividend Leaders ETF',    'TDIV', 'EUR', 'VanEck',      'Morningstar Developed Markets Large Cap Dividend Leaders'),
    ('LU0292095535', 'Xtrackers Euro Stoxx Quality Dividend UCITS ETF (Dist)',       'EXSG', 'EUR', 'Xtrackers',   'Euro STOXX Select Dividend 30'),
    -- Scalable Christian – Growth
    ('IE00BF4RFH31', 'iShares MSCI World Small Cap UCITS ETF (Acc)',                 'IUSN', 'USD', 'iShares',     'MSCI World Small Cap'),
    ('LU2903252349', 'Xtrackers MSCI AC World Swap UCITS ETF 1C (Scalable)',        'XACW', 'USD', 'Xtrackers',   'MSCI AC World'),
    -- ING Gemeinschaftsdepot
    ('IE00B53QG562', 'iShares Core MSCI Europe UCITS ETF (Acc)',                     'CEMR', 'EUR', 'iShares',     'MSCI Europe'),
    ('IE00B4L5YC18', 'iShares Core MSCI Emerging Markets IMI UCITS ETF (Acc)',       'IS3N', 'USD', 'iShares',     'MSCI Emerging Markets IMI'),
    ('IE00BKX55T58', 'Vanguard FTSE Developed World UCITS ETF (Dist)',               'VWCE', 'USD', 'Vanguard',    'FTSE Developed World'),
    ('IE000OEF25S1', 'Invesco MSCI World Equal Weight UCITS ETF (Acc)',              'MWEQ', 'USD', 'Invesco',     'MSCI World Equal Weighted'),
    -- Christian Riester
    ('IE00BL25JN58', 'Xtrackers MSCI World Minimum Volatility UCITS ETF 1C',        'XDEB', 'EUR', 'Xtrackers',   'MSCI World Minimum Volatility'),
    ('IE00B8KGV557', 'iShares Edge MSCI EM Minimum Volatility UCITS ETF (Acc)',      'EUNZ', 'USD', 'iShares',     'MSCI Emerging Markets Minimum Volatility'),
    ('IE00B8FHGS14', 'iShares Edge MSCI World Minimum Volatility UCITS ETF (Acc)',   'MVEA', 'EUR', 'iShares',     'MSCI World Minimum Volatility'),
    ('IE00B86MWN23', 'iShares MSCI Europe Minimum Volatility UCITS ETF (Acc)',       'EUN0', 'EUR', 'iShares',     'MSCI Europe Minimum Volatility'),
    ('LU1681041627', 'Amundi MSCI Europe Minimum Volatility Factor UCITS ETF C',    'MV7A', 'EUR', 'Amundi',      'MSCI Europe Minimum Volatility'),
    ('IE00BKVL7331', 'iShares Edge MSCI USA Minimum Volatility ESG UCITS ETF (Acc)','IQQ0', 'USD', 'iShares',     'MSCI USA Minimum Volatility ESG'),
    -- Christian Trade Republic
    ('DE000A2QP349', 'iShares MDAX UCITS ETF (DE)',                                  'EXID', 'EUR', 'iShares',     'MDAX'),
    ('LU2611732475', 'Amundi SDAX UCITS ETF Dist',                                  'SDAX', 'EUR', 'Amundi',      'SDAX'),
    ('IE00BP3QZ825', 'iShares MSCI World Momentum Factor UCITS ETF (Acc)',            'IS3R', 'USD', 'iShares',     'MSCI World Momentum'),
    ('IE00BP3QZ601', 'iShares Edge MSCI World Quality Factor UCITS ETF (Acc)',        'IS3Q', 'USD', 'iShares',     'MSCI World Quality Factor'),
    ('IE00BQN1K786', 'iShares MSCI Europe Momentum Factor UCITS ETF (Acc)',           'SXR7', 'EUR', 'iShares',     'MSCI Europe Momentum Factor'),
    ('LU1681042435', 'Amundi MSCI Europe Growth UCITS ETF Acc',                      'MXEG', 'EUR', 'Amundi',      'MSCI Europe Growth')
ON CONFLICT (isin) DO NOTHING;

-- ── Seed-Daten: Altersvorsorge-Ziel-ETFs (noch nicht investiert) ──────────────
-- Geplante Umschichtung im Riester-Vertrag. Ticker = NULL bis zur Investition
-- (load_prices.py überspringt ETFs ohne Ticker automatisch via WHERE ticker IS NOT NULL).
-- Tickers nach Investition nachtragen und prüfen (yfinance kann abweichen).
INSERT INTO portfolio.etf (isin, name, ticker, currency, emittent, index_name) VALUES
    ('IE00BKM4GZ66', 'iShares Core MSCI Emerging Markets IMI UCITS ETF (Acc)',       NULL,   'USD', 'iShares',   'MSCI Emerging Markets IMI'),
    ('IE00B4L5YX21', 'iShares Core MSCI Japan IMI UCITS ETF (Acc)',                  NULL,   'USD', 'iShares',   'MSCI Japan IMI'),
    ('IE00B52MJY50', 'iShares Core MSCI Pacific ex-Japan UCITS ETF (Acc)',            NULL,   'USD', 'iShares',   'MSCI Pacific ex-Japan'),
    ('IE00BQN1K562', 'iShares Edge MSCI Europe Quality Factor UCITS ETF (Acc)',       NULL,   'EUR', 'iShares',   'MSCI Europe Quality Factor'),
    ('IE00BQN1K901', 'iShares Edge MSCI Europe Value Factor UCITS ETF (Acc)',         NULL,   'EUR', 'iShares',   'MSCI Europe Value Factor'),
    ('IE00BD1F4N50', 'iShares Edge MSCI USA Momentum Factor UCITS ETF (Acc)',         NULL,   'USD', 'iShares',   'MSCI USA Momentum Factor'),
    ('IE00BD1F4L37', 'iShares Edge MSCI USA Quality Factor UCITS ETF (Acc)',          NULL,   'USD', 'iShares',   'MSCI USA Quality Factor'),
    ('IE00BD1F4M44', 'iShares Edge MSCI USA Value Factor UCITS ETF (Acc)',            NULL,   'USD', 'iShares',   'MSCI USA Value Factor'),
    ('LU0322253906', 'Xtrackers MSCI Europe Small Cap UCITS ETF 1C',                 'XXSC', 'EUR', 'Xtrackers', 'MSCI Europe Small Cap')
ON CONFLICT (isin) DO NOTHING;

-- ── Benchmark-Stammdaten ──────────────────────────────────────────────────────
-- Vergleichsindizes für den Performance-Report. Separate Tabelle (kein FK auf portfolio.etf),
-- da Benchmarks nicht im eigenen Portfolio gehalten werden (keine position_snapshot).
CREATE TABLE IF NOT EXISTS portfolio.benchmark (
    ticker     VARCHAR(20)  PRIMARY KEY,            -- interner Schlüssel, z. B. 'MSCI_WORLD'
    name       VARCHAR(200) NOT NULL,               -- Anzeigename im Report
    yf_symbol  VARCHAR(25)  NOT NULL,               -- yfinance-Symbol, z. B. 'EXS1.DE'
    currency   VARCHAR(3)   NOT NULL DEFAULT 'EUR'  -- Preiswährung des yfinance-Symbols
);

CREATE TABLE IF NOT EXISTS portfolio.benchmark_price (
    ticker     VARCHAR(20)  NOT NULL REFERENCES portfolio.benchmark(ticker),
    price_date DATE         NOT NULL,
    close      NUMERIC(18, 6) NOT NULL,
    PRIMARY KEY (ticker, price_date)
);

CREATE INDEX IF NOT EXISTS ix_benchmark_price_date
    ON portfolio.benchmark_price (ticker, price_date DESC);

-- ── Transaktionen (Scalable, Trade Republic) ─────────────────────────────────
-- Volle Kontobewegungshistorie (nicht nur Buy/Sell) je Depot, fuer spaetere
-- MWR/TWR-Berechnung (siehe claude.md Abschnitt 7: "Spaeter: Transaktions-
-- historie -> MWR/TWR"). APPEND/UPSERT, nie DELETE.
--
-- isin ist bewusst NICHT als FK auf portfolio.etf definiert: portfolio.etf
-- ist ausschliesslich die aktuell getrackten ETFs. Trade Republic enthaelt
-- aber Einzelaktien-/Derivate-Historie (z.B. einzelne Aktien, gehebelte
-- Zertifikate), die nie in portfolio.etf landen sollen. Eine FK wuerde
-- entweder diese Buchhaltungszeilen ablehnen oder portfolio.etf verwaessern.
CREATE TABLE IF NOT EXISTS portfolio.transaction (
    account_id         INTEGER      NOT NULL REFERENCES portfolio.account(account_id),
    broker_ref         VARCHAR(120) NOT NULL,   -- Scalable 'reference' | TR 'transaction_id'
    source_system      VARCHAR(20)  NOT NULL,   -- 'scalable' | 'traderepublic'
    txn_datetime        TIMESTAMPTZ  NOT NULL,
    txn_date             DATE         NOT NULL,   -- rohe Quell-Datumsspalte (TZ-robuster Join-Schluessel)
    txn_type              VARCHAR(24)  NOT NULL,   -- normalisierte Taxonomie, siehe CHECK
    raw_category          VARCHAR(80)  NOT NULL,   -- Original-Kombination, z.B. 'Security|Savings plan'
    isin                   VARCHAR(12),             -- NULL bei Cash-only; keine FK (s.o.)
    security_name        VARCHAR(200),
    quantity               NUMERIC(18, 6),          -- signiert: + Zugang, - Abgang; NULL bei Cash-only
    price                   NUMERIC(18, 6),
    gross_amount          NUMERIC(18, 4),          -- signierter Cash-Effekt, Quellvorzeichen uebernommen
    fee                     NUMERIC(18, 4)  NOT NULL DEFAULT 0,
    tax                     NUMERIC(18, 4)  NOT NULL DEFAULT 0,
    currency               VARCHAR(3)   NOT NULL,
    original_amount        NUMERIC(18, 4),          -- TR-only: Betrag vor FX-Umrechnung
    original_currency      VARCHAR(3),
    fx_rate_applied         NUMERIC(18, 6),
    status                  VARCHAR(20)  NOT NULL DEFAULT 'Executed',
    loaded_at               TIMESTAMPTZ  NOT NULL DEFAULT now(),
    PRIMARY KEY (account_id, broker_ref),
    CONSTRAINT chk_transaction_txn_type CHECK (txn_type IN (
        'BUY', 'SELL', 'DRIP', 'FREE_RECEIPT',
        'TRANSFER_SECURITY_IN', 'TRANSFER_SECURITY_OUT',
        'DIVIDEND', 'FEE', 'INTEREST', 'TAX',
        'DEPOSIT', 'WITHDRAWAL', 'CASH_TRANSFER_IN', 'CASH_TRANSFER_OUT',
        'CARD_TRANSACTION', 'CASHBACK', 'OTHER'
    ))
);

CREATE INDEX IF NOT EXISTS ix_transaction_account_isin_date
    ON portfolio.transaction (account_id, isin, txn_date);
CREATE INDEX IF NOT EXISTS ix_transaction_date
    ON portfolio.transaction (txn_date);
CREATE INDEX IF NOT EXISTS ix_transaction_type
    ON portfolio.transaction (txn_type);

INSERT INTO portfolio.benchmark (ticker, name, yf_symbol, currency) VALUES
    ('MSCI_WORLD',       'iShares Core MSCI World UCITS ETF Acc (EUNL.DE)',                       'EUNL.DE',  'EUR'),
    ('NASDAQ_100',       'iShares Core NASDAQ 100 (CSNDX.DE)',                                    'CSNDX.DE', 'EUR'),
    ('EURO_STOXX_50',    'iShares Core Euro STOXX 50 (EXW1.DE)',                                  'EXW1.DE',  'EUR'),
    ('STOXX_EU_600',     'iShares STOXX Europe 600 (EXSA.DE)',                                    'EXSA.DE',  'EUR'),
    ('MSCI_ACWI',        'iShares MSCI ACWI UCITS ETF (IUSQ.DE)',                                 'IUSQ.DE',  'EUR'),
    ('GLOBAL_AGG_BOND_H','iShares Core Global Aggregate Bond UCITS ETF EUR Hedged Acc (EUNA.DE)', 'EUNA.DE',  'EUR'),
    ('CORP_BOND_IG',     'iShares Global Corp Bond UCITS ETF EUR Hedged (Dist, IBCQ.DE)',         'IBCQ.DE',  'EUR')
ON CONFLICT (ticker) DO NOTHING;
-- Hinweis: NASDAQ_100 yf_symbol wurde nachtraeglich auf 'CNDX.AS' korrigiert (CSNDX.DE delisted,
-- siehe Server-Deployment-Log), CSNDX.DE hier nur als urspruengliche Seed-Referenz belassen.
-- MSCI_WORLD 2026-08-08 von EXS1.DE (Dist) auf EUNL.DE (Acc, WKN A0RPWH) umgestellt (User-Wunsch).
-- GLOBAL_AGG_BOND_H 2026-08-08 neu: Komponente der 60/40-Misch-Benchmark in generate_report.py
-- (wird dort zur Laufzeit mit MSCI_WORLD verkettet, kein eigener "MIX"-Tabelleneintrag).
-- CORP_BOND_IG 2026-08-17 neu: vorgesehene TE-Benchmark fuer kuenftige Renten-ETFs
-- (Fachkonzept-Assetklasse "Credit IG"), Nutzerwunsch: global, EUR-hedged, kein
-- reiner EUR-Corporate-Index. Zunaechst IBCX.DE (nur EUR) gewaehlt, auf
-- Nutzerkorrektur auf IBCQ.DE (iShares Global Corp Bond EUR Hedged, Dist,
-- Bloomberg Global Aggregate Corporate Index EUR Hedged) umgestellt. Ist ein
-- Dist-Fonds (26 Dividenden-Events lt. yfinance) - profitiert vom
-- auto_adjust=False-Fix im Benchmark-Loader. Bisher nur die Benchmark-
-- Kursreihe angelegt, noch keine Renten-ETFs im Bestand, die dagegen
-- gemessen werden.
