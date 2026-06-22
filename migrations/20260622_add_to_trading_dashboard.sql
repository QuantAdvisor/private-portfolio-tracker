-- ============================================================
-- Migration 20260622 – Neue ETFs in quant_advisor.dim_etf_scalable
--
-- Fügt die 9 Altersvorsorge-Ziel-ETFs ein, damit sie
-- im Trading-Dashboard und in update_portfolio_server.py bekannt sind.
--
-- VORHER: Schema-Struktur prüfen (unterscheidet sich ggf. von den Spaltennamen hier):
--   SELECT column_name, data_type FROM information_schema.columns
--   WHERE table_schema = 'quant_advisor' AND table_name = 'dim_etf_scalable';
--
-- Auf dem Server ausführen:
--   psql -U christian_schott -d quant_advisor -f migrations/20260622_add_to_trading_dashboard.sql
-- ============================================================

-- Schritt 1: Schema-Prüfung (Ausgabe auswerten vor dem INSERT)
SELECT column_name, data_type, is_nullable
FROM information_schema.columns
WHERE table_schema = 'quant_advisor' AND table_name = 'dim_etf_scalable'
ORDER BY ordinal_position;

-- Schritt 2: Aktuelle Einträge anzeigen (1–2 Beispiel-ISINs zur Schema-Verifikation)
SELECT * FROM quant_advisor.dim_etf_scalable LIMIT 5;

-- ── Schritt 3: INSERT ────────────────────────────────────────────────────────
-- Spalten: markt (Marktbeschreibung), index (Index-Name), isin
-- ⚠ Format von markt/index gegen bestehende Zeilen prüfen:
--     SELECT markt, "index", isin FROM quant_advisor.dim_etf_scalable LIMIT 3;
-- Werte ggf. ans vorhandene Format anpassen (z.B. Groß-/Kleinschreibung, Trennzeichen).

INSERT INTO quant_advisor.dim_etf_scalable (markt, "index", isin) VALUES
    ('Emerging Markets',  'MSCI Emerging Markets IMI',       'IE00BKM4GZ66'),
    ('Japan',             'MSCI Japan IMI',                  'IE00B4L5YX21'),
    ('Asien/Pazifik',     'MSCI Pacific ex-Japan',           'IE00B52MJY50'),
    ('Europa',            'MSCI Europe Quality Factor',      'IE00BQN1K562'),
    ('Europa',            'MSCI Europe Value Factor',        'IE00BQN1K901'),
    ('USA',               'MSCI USA Momentum Factor',        'IE00BD1F4N50'),
    ('USA',               'MSCI USA Quality Factor',         'IE00BD1F4L37'),
    ('USA',               'MSCI USA Value Factor',           'IE00BD1F4M44'),
    ('Europa',            'MSCI Europe Small Cap',           'LU0322253906')
ON CONFLICT (isin) DO NOTHING;

-- Verifizierung
SELECT COUNT(*) AS etfs_gesamt FROM quant_advisor.dim_etf_scalable;
