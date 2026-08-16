-- ============================================================
-- Migration 20260816 - Transaktionshistorie (Scalable, Trade Republic)
--
-- Neue additive Tabelle fuer die volle Kontobewegungshistorie beider Broker,
-- Grundlage fuer die erste echte TWR-Berechnung (portfolio-intelligence-
-- platform/phase12_transaction_twr.py). Keine bestehende Tabelle wird
-- veraendert.
--
-- Auf dem Server ausfuehren (oder lokal via db_utils/SSH-Tunnel):
--   psql -U christian_schott -d quant_advisor -f migrations/20260816_add_transaction_table.sql
--
-- Idempotent: CREATE TABLE IF NOT EXISTS
-- ============================================================

CREATE TABLE IF NOT EXISTS portfolio.transaction (
    account_id         INTEGER      NOT NULL REFERENCES portfolio.account(account_id),
    broker_ref         VARCHAR(120) NOT NULL,
    source_system      VARCHAR(20)  NOT NULL,
    txn_datetime        TIMESTAMPTZ  NOT NULL,
    txn_date             DATE         NOT NULL,
    txn_type              VARCHAR(24)  NOT NULL,
    raw_category          VARCHAR(80)  NOT NULL,
    isin                   VARCHAR(12),
    security_name        VARCHAR(200),
    quantity               NUMERIC(18, 6),
    price                   NUMERIC(18, 6),
    gross_amount          NUMERIC(18, 4),
    fee                     NUMERIC(18, 4)  NOT NULL DEFAULT 0,
    tax                     NUMERIC(18, 4)  NOT NULL DEFAULT 0,
    currency               VARCHAR(3)   NOT NULL,
    original_amount        NUMERIC(18, 4),
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

-- Verifizierung
SELECT table_name FROM information_schema.tables WHERE table_schema = 'portfolio' AND table_name = 'transaction';
