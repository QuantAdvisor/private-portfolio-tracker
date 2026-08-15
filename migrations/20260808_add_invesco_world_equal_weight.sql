-- ============================================================
-- Migration 20260808 – Neuer ETF im ING-Gemeinschaftsdepot
--
-- Kauf: 360 Stück IE000OEF25S1 zu 5,876 (avg_cost), finanziert durch
-- Teilverkauf von 40 Stück IE00B4L5YC18 (EM-ETF, verbleibend 39,84325 Stück).
--
-- Auf dem Server ausführen (oder lokal via db_utils/SSH-Tunnel):
--   psql -U christian_schott -d quant_advisor -f migrations/20260808_add_invesco_world_equal_weight.sql
--
-- Idempotent: ON CONFLICT DO NOTHING
-- ============================================================

INSERT INTO portfolio.etf (isin, name, ticker, currency, emittent, index_name) VALUES
    ('IE000OEF25S1', 'Invesco MSCI World Equal Weight UCITS ETF (Acc)', 'MWEQ', 'USD', 'Invesco', 'MSCI World Equal Weighted')
ON CONFLICT (isin) DO NOTHING;

-- Verifizierung
SELECT isin, name, ticker, currency, emittent FROM portfolio.etf WHERE isin = 'IE000OEF25S1';
