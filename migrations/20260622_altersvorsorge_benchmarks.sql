-- ============================================================
-- Migration 20260622 – Altersvorsorge-ETFs + Benchmark-Tabellen
--
-- Auf dem Server ausführen:
--   psql -U christian_schott -d quant_advisor -f migrations/20260622_altersvorsorge_benchmarks.sql
--
-- Idempotent: ON CONFLICT DO NOTHING / IF NOT EXISTS
-- ============================================================

-- ── 9 neue Altersvorsorge-Ziel-ETFs ──────────────────────────────────────────
-- Ticker = NULL: load_prices.py überspringt diese automatisch (WHERE ticker IS NOT NULL).
-- Nach der Investition Ticker ergänzen und mit yfinance verifizieren.

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

CREATE TABLE IF NOT EXISTS portfolio.benchmark (
    ticker     VARCHAR(20)  PRIMARY KEY,
    name       VARCHAR(200) NOT NULL,
    yf_symbol  VARCHAR(25)  NOT NULL,
    currency   VARCHAR(3)   NOT NULL DEFAULT 'EUR'
);

CREATE TABLE IF NOT EXISTS portfolio.benchmark_price (
    ticker     VARCHAR(20)  NOT NULL REFERENCES portfolio.benchmark(ticker),
    price_date DATE         NOT NULL,
    close      NUMERIC(18, 6) NOT NULL,
    PRIMARY KEY (ticker, price_date)
);

CREATE INDEX IF NOT EXISTS ix_benchmark_price_date
    ON portfolio.benchmark_price (ticker, price_date DESC);

INSERT INTO portfolio.benchmark (ticker, name, yf_symbol, currency) VALUES
    ('MSCI_WORLD',    'iShares Core MSCI World (EXS1.DE)',       'EXS1.DE',  'EUR'),
    ('NASDAQ_100',    'iShares Core NASDAQ 100 (CSNDX.DE)',      'CSNDX.DE', 'EUR'),
    ('EURO_STOXX_50', 'iShares Core Euro STOXX 50 (EXW1.DE)',    'EXW1.DE',  'EUR'),
    ('STOXX_EU_600',  'iShares STOXX Europe 600 (EXSA.DE)',      'EXSA.DE',  'EUR'),
    ('MSCI_ACWI',     'iShares MSCI ACWI UCITS ETF (IUSQ.DE)',   'IUSQ.DE',  'EUR')
ON CONFLICT (ticker) DO NOTHING;

-- ── Verifizierung ─────────────────────────────────────────────────────────────
SELECT COUNT(*) AS etf_gesamt FROM portfolio.etf;
-- Erwartet: 33 (24 bestehend + 9 neue)

SELECT ticker, name, yf_symbol FROM portfolio.benchmark ORDER BY ticker;
-- Erwartet: 5 Benchmarks
