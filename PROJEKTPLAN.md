# Projektplan: ETF Look-Through Holdings Tracker

Stand: 21.06.2026 — Basis: Inspektion aller Quelldateien + claude.md

---

## Ist-Zustand

### Portfolio (Portfolio.xlsx → Tabelle1)

**24 ETF-Positionen über 4 Depots:**

| Depot | Kategorie | ISIN | Name | Stück | Sparrate/Monat |
|---|---|---|---|---|---|
| Scalable Christian | Income | IE00B14X4T88 | iShares Asia Pacific Dividend | 64,54 | 0 |
| Scalable Christian | Income | IE00B652H904 | iShares EM Dividend | 191,21 | 0 |
| Scalable Christian | Income | IE00B0M63060 | iShares UK Dividend | 117,97 | 0 |
| Scalable Christian | Income | IE0005AJA0P1 | L&G Global Quality Dividends | 525 | 0 |
| Scalable Christian | Income | IE00B6YX5D40 | SPDR US Dividend Aristocrats | 35,50 | 0 |
| Scalable Christian | Income | NL0011683594 | VanEck Developed Markets Dividend | 74,03 | 0 |
| Scalable Christian | Income | LU0292095535 | Xtrackers Euro Stoxx Quality Dividend | 102,22 | 0 |
| Scalable Christian | Growth | IE00BF4RFH31 | iShares MSCI World Small Cap | 126,98 | 75 |
| Scalable Christian | Growth | LU2903252349 | Scalable MSCI AC World Xtrackers | 127,61 | 75 |
| ING Gemeinschaftsdepot | Basisinvestment | IE00B53QG562 | iShares Core MSCI Europe | 22,61 | 100 |
| ING Gemeinschaftsdepot | Basisinvestment | IE00B4L5YC18 | iShares Core MSCI EM | 71,76 | 150 |
| ING Gemeinschaftsdepot | Basisinvestment | IE00BKX55T58 | Vanguard FTSE Developed World | 23,37 | — |
| Christian Riester | Riestervertrag | IE00BL25JN58 | Xtrackers MSCI World Min Vol | 194,59 | ~47 |
| Christian Riester | Riestervertrag | IE00B8KGV557 | iShares Edge MSCI EM Min Vol | 81,64 | ~17 |
| Christian Riester | Riestervertrag | IE00B8FHGS14 | iShares Edge MSCI World Min Vol | 96,60 | ~35 |
| Christian Riester | Riestervertrag | IE00B86MWN23 | iShares MSCI Europe Min Vol | 102,53 | ~36 |
| Christian Riester | Riestervertrag | LU1681041627 | Amundi MSCI Europe Min Vol | 22,74 | ~18 |
| Christian Riester | Riestervertrag | IE00BKVL7331 | iShares Edge MSCI USA Min Vol ESG | 294,63 | ~12 |
| Christian Trade Republic | Growth | DE000A2QP349 | ETF MDAX | 42,78 | 25 |
| Christian Trade Republic | Growth | LU2611732475 | ETF SDAX | 1,21 | 25 |
| Christian Trade Republic | Growth | IE00BP3QZ825 | World Momentum | 0,84 | 25 |
| Christian Trade Republic | Growth | IE00BP3QZ601 | World Quality | 1,06 | 25 |
| Christian Trade Republic | Growth | IE00BQN1K786 | Europe Momentum | 4,95 | 25 |
| Christian Trade Republic | Growth | LU1681042435 | Europe Growth | 0,22 | 25 |

**Sparrate gesamt:** 715 EUR/Monat (Einzeldepots); 1.430 EUR vermutlich Haushalt gesamt.

**Datenproblem:** ISIN `IE00B53QG562` hat führendes `\xa0` (Leerzeichen) — muss beim Einlesen getrimmt werden.
**`Avg_Kaufkurs`** für Riester = `'unbekannt'` (String) → als NULL behandeln.

---

## Quelldateien-Inventar

### Holdings-Dateien: 26 Dateien, 8 Emittenten-Formate

---

### Format 1 — iShares CSV (14 Dateien)

**Encoding:** UTF-8-SIG | **Trennzeichen:** Komma | **Sprache:** Deutsch

**Header-Struktur:**
- Zeile 1: `Fondsposition per,"19.Juni2026"` (Datum)
- Zeile 2: leer
- Zeile 3: Spaltennamen
- Ab Zeile 4: Daten

**Spalten:** `Emittententicker, Name, Sektor, Anlageklasse, Marktwert, Gewichtung (%), Nominalwert, Nominale, Kurs, Standort, Börse, Marktwährung`

**⚠️ KEIN ISIN** — nur Ticker. Ticker-zu-ISIN-Mapping für Konstituenten nötig (oder yfinance/OpenFIGI).

**Zahlenformat:** Deutsch (Punkt=Tausender, Komma=Dezimal): `"14.086.746"` → parse mit `locale` oder manuell ersetzen.

| Datei | Portfolio-ISIN | ETF |
|---|---|---|
| CEMR_holdings.csv | IE00B53QG562 | iShares Core MSCI Europe |
| IQQD_holdings.csv | IE00B0M63060 | iShares UK Dividend |
| IUSN_holdings.csv | IE00BF4RFH31 | iShares MSCI World Small Cap |
| MVEA_holdings.csv | IE00B8FHGS14 | iShares Edge MSCI World Min Vol |
| EXID_holdings.csv | DE000A2QP349 | iShares MDAX (ETF MDAX) |
| EUNM_holdings.csv | IE00B4L5YC18 | iShares Core MSCI EM |
| EUNY_holdings.csv | IE00B652H904 | iShares EM High Dividend |
| EUNZ_holdings.csv | IE00B8KGV557 | iShares Edge MSCI EM Min Vol |
| EUN0_holdings.csv | IE00B86MWN23 | iShares MSCI Europe Min Vol |
| IQQ0_holdings.csv | IE00BKVL7331 | iShares Edge MSCI USA Min Vol ESG |
| IQQX_holdings.csv | IE00B14X4T88 | iShares Asia Pacific Dividend |
| IS3Q_holdings.csv | IE00BP3QZ825 | iShares MSCI World Momentum (?) |
| IS3R_holdings.csv | ? | zu klären |
| SXR7_holdings.csv | IE00BQN1K786 | iShares Europe Momentum (?) |
| MVEA_holdings (1).csv | Duplikat von MVEA_holdings.csv | ignorieren |

> ⚠️ **Mapping IS3Q/IS3R/SXR7 zu den Trade-Republic-ISINs noch zu bestätigen** (World Quality, World Momentum, Europe Momentum könnten auch QLS/andere Ticker sein). Vor dem ersten Load prüfen.

---

### Format 2 — Xtrackers/DWS XLSX (3 Dateien, openpyxl lesbar)

**Header:** Zeile 1–3 = Disclaimer/Meta, Zeile 4 = Spaltennamen

**Spalten:** `(lfd.Nr., Name, ISIN, Country, Currency, Exchange, Type of Security, Rating, Primary Listing, Industry Classification, Weighting)`

**Gewichtung:** Dezimalzahl (z. B. `0.0469...`) — direkt als Gewicht nutzbar (×100 für %).
**ISIN:** vorhanden ✓

| Datei | Portfolio-ISIN | ETF |
|---|---|---|
| Constituent_IE00BL25JN58.xlsx | IE00BL25JN58 | Xtrackers MSCI World Min Vol |
| Constituent_LU0292095535.xlsx | LU0292095535 | Xtrackers Euro Stoxx Quality Dividend |
| Constituent_LU2903252349.xlsx | LU2903252349 | Scalable MSCI AC World Xtrackers |

---

### Format 3 — Amundi XLSX (3 Dateien, ⚠️ kaputtes Stylesheet)

**Problem:** openpyxl wirft `Unable to read workbook: could not read stylesheet` → muss über `zipfile` + XML-Parsing gelesen werden (oder `pandas` mit `engine='openpyxl'` und `StyleWarning` ignorieren → testen).

**Struktur (via Raw-XML inspiziert):**
- Zeile 20: Header (B=ISIN, C=Name, D=Anlageklasse, E=Währung, F=Gewichtung, G=Sektor, H=Land)
- Ab Zeile 21: Daten
- Gewichtung: Dezimalzahl (z. B. `0.1036...`)
- ISIN: vorhanden ✓

**Parse-Strategie:** `zipfile` öffnen → `xl/sharedStrings.xml` + `xl/worksheets/sheet1.xml` parsen → shared-string-Index auflösen → DataFrame bauen. Oder `openpyxl` mit `read_only=True` testen (umgeht Style-Parsing).

| Datei | Portfolio-ISIN | ETF |
|---|---|---|
| Fondszusammensetzung_Amundi MSCI Europe Growth..._LU1681042435_17_06_2026.xlsx | LU1681042435 | Amundi Europe Growth |
| Fondszusammensetzung_Amundi MSCI Europe Minimum Volatility..._LU1681041627_17_06_2026.xlsx | LU1681041627 | Amundi Europe Min Vol |
| Fondszusammensetzung_Amundi SDAX..._LU2611732475_17_06_2026.xlsx | LU2611732475 | Amundi SDAX |

---

### Format 4 — VanEck XLSX (1 Datei, openpyxl lesbar)

**Datei:** `TDIV_Stand_20260619.xlsx` → ISIN `NL0011683594`

**Struktur:**
- Zeile 1: Titel (`Alle Fondspositionen Stand 06.19.2026`)
- Zeile 2: leer
- Zeile 3: Header (`Position, Bezeichnung der Position, Ticker, ISIN, Anteile, Marktwert, % des Fondsvolumens`)
- Ab Zeile 4: Daten

**ISIN:** vorhanden ✓
**Gewichtung:** String `"4,84%"` → strip `%`, replace `,` → `/100`
**Marktwert:** String `"$ 388.311.173.00"` → dollar + amerikanisches Format

---

### Format 5 — SPDR XLSX (1 Datei, openpyxl lesbar)

**Datei:** `holdings-daily-emea-en-spyd-gy (1).xlsx` → ISIN `IE00B6YX5D40`

**Struktur:**
- Zeilen 1–4: Metadaten (Fund Name, ISIN, Ticker, Holdings As Of)
- Zeile 5: leer
- Zeile 6: Header (`ISIN, SEDOL, Security Name, Currency, Number of Shares, Percent of Fund, Trade Country Name, Local Price, Sector Classification, Industry Classification, Market Value`)
- Ab Zeile 7: Daten

**ISIN:** vorhanden ✓
**Gewichtung:** Float `2.344397` (= %, direkt)

---

### Format 6 — L&G CSV (1 Datei)

**Datei:** `Fund-holdings_LG-Global-Quality-Dividends-UCITS-ETF-Global-Quality-Dividends-USD-Dist_19-06-2026 (1).csv` → ISIN `IE0005AJA0P1`

**Encoding:** ASCII | **Trennzeichen:** Komma

**Struktur:**
- Zeile 1: `sep=,` (Excel-Hinweis, überspringen)
- Zeilen 2–4: Metadaten (`Basket Name`, `ETF Trading ISIN`, `Basket Trade Date`)
- Zeile 5: Header (`Security Description, ISIN, Trading Currency, Constituent Weight (Base)`)
- Ab Zeile 6: Daten

**ISIN:** vorhanden ✓
**Gewichtung:** Dezimalzahl `0.002011...` → ×100 für %

---

### Format 7 — Vanguard XLSX (1 Datei, ⚠️ Dateiname-Problem)

**Datei:** `Aufschlüsselung der Positionen - Vanguard FTSE Developed World UCITS ETF (USD) Distributing - 21.6.2026.xlsx` → ISIN `IE00BKX55T58`

**Problem:** Python auf Windows kann Dateinamen mit `ü` nicht direkt öffnen (Encoding-Bug). **Workaround:** vor dem Parsen in denselben Ordner als `vanguard_vwce_YYYYMMDD.xlsx` kopieren, parsen, Kopie löschen. Oder `pathlib.Path` mit UTF-8-Filesystem prüfen.

**Struktur:**
- Zeile 1: Datum-Info (`Diese Datei wurde am 21. Juni 2026 heruntergeladen`)
- Zeilen 2–6: Metadaten
- Zeile 7: Header (`Ticker, Wertpapiere, % der Assets, Sektor, Region, Marktwert, Anteile`)
- Ab Zeile 8: Daten

**⚠️ KEIN ISIN** — nur Ticker (wie iShares CSV). Ticker-zu-ISIN-Mapping nötig.
**Gewichtung:** String `"5,1111 %"` → strip `%`, strip `\xa0`, replace `,` → float
**Datum-Problem:** Datei vom 21. Juni, aber Daten per 31. Mai → `as_of_date = 2026-05-31` (aus Zeile 5 parsen, nicht aus Dateinamen).

---

### Format 8 — iShares XLS (1 Datei, ⚠️ altes Format)

**Datei:** `iShares-Edge-MSCI-World-Quality-Factor-UCITS-ETF-USD-Acc_fund.xls` → ISIN `IE00BP3QZ601` (World Quality)

**Problem:** `.xls` (Excel 97–2003), openpyxl liest kein `.xls` → **`xlrd`** installieren (`pip install xlrd`).

**Format noch zu inspizieren** — erste Priorität im Sprint 1 bevor Parser gebaut wird.

---

## Architektur-Entscheidungen

### DB-Schema (Schema `portfolio`, neuer Namespace)

```sql
-- Stammdaten
account            (account_id SERIAL PK, name, broker, account_type)
instrument         (isin VARCHAR(12) PK, name, instrument_type, currency)

-- Eigene Positionen (monatlich, append-only)
position_snapshot  (account_id, isin, as_of_date, quantity, avg_cost)
                   PK (account_id, isin, as_of_date)

-- ETF-Bestandteile (monatlich, append-only)
etf_holding        (etf_isin, as_of_date, constituent_id, weight_pct)
                   PK (etf_isin, as_of_date, constituent_id)

-- Normalisierte Konstituenten-Identität
constituent        (constituent_id SERIAL PK, isin, name_normalized, ticker,
                   sector, country, currency)
-- Ticker-zu-ISIN-Mapping (für iShares CSV + Vanguard)
ticker_isin_map    (ticker VARCHAR(20) PK, isin VARCHAR(12), source, verified_at)

-- Kurse (täglich)
price              (isin, price_date, close_eur, currency, source)
                   PK (isin, price_date)

-- FX-Kurse (täglich)
fx_rate            (rate_date, currency_code, eur_per_unit)
                   PK (rate_date, currency_code)
```

### Infrastruktur
- `db_utils.py` aus `portfolio-tracker` 1:1 übernehmen (SSH-Tunnel + direkt, bewährt)
- Schema `portfolio` anlegen, kein Eingriff in `quant_advisor`
- `.env` mit DB-Credentials (vorhanden im Altprojekt, anpassen)
- Python 3.13 / venv; Pakete: `pandas`, `sqlalchemy`, `psycopg2-binary`, `python-dotenv`, `openpyxl`, `xlrd`, `chardet`

---

## Roadmap

### Phase 0 — Fundament (MVP: Gesamtwert täglich)

**Ziel:** Schema anlegen, eigene Positionen laden, ETF-Kurse täglich → Gesamtwert in EUR je Depot.

- [ ] `db_schema.sql` anlegen (Schema `portfolio`, alle Tabellen oben)
- [ ] `db_utils.py` kopieren + `.env` anpassen
- [ ] **Portfolio-Loader** (`load_positions.py`): `Portfolio.xlsx` lesen → `account` + `instrument` + `position_snapshot` schreiben
  - ISIN-Trim für `\xa0`-Bug
  - `avg_cost = NULL` wenn `'unbekannt'`
  - Idempotent: `ON CONFLICT DO UPDATE`
- [ ] **Kurs-Loader** (`load_prices.py`): ETF-Preise täglich via yfinance (oder manuell getestet, ggf. Fallback)
  - ⚠️ Riester-ETFs prüfen: haben diese sauber abrufbare Kurse?
  - FX-Kurse (EUR/USD, EUR/GBP, etc.) ebenfalls
- [ ] **Auswertung Phase 0** (`calc_portfolio_value.py`): Gesamtwert je ETF + je Depot in EUR

**Deliverable:** Ein Befehl gibt mir den aktuellen Portfoliowert (Kurse von gestern) aus.

---

### Phase 1 — Erster Holdings-Parser (Prototyp Look-Through)

**Ziel:** Einen vollständigen Parser für einen Emittenten, Look-Through für diesen einen ETF.

**Empfehlung Startemittent: Xtrackers/DWS** — sauberste Datei, ISIN vorhanden, openpyxl lesbar, 3 relevante ETFs.

- [ ] **Parser `parse_xtrackers.py`**: Constituent_*.xlsx lesen → `constituent` + `etf_holding` schreiben
  - Header bei Zeile 4 (`skiprows=3`)
  - `weight` als Dezimal × 100 → `weight_pct`
  - Idempotent via `(etf_isin, as_of_date, constituent_id) ON CONFLICT DO UPDATE`
- [ ] **Look-Through-Query** (SQL oder pandas): für einen ETF, für heute
  - `position_snapshot × etf_holding × price × fx_rate` mit As-of-Join
- [ ] Ausgabe: Top-20-Konstituenten dieses ETFs (EUR-Engagement, % des Gesamtportfolios)

---

### Phase 2 — Alle Emittenten + Konstituenten-Normalisierung

**Ziel:** Vollständige aggregierte Einzeltitel-Liste über alle ETFs und Depots.

**Reihenfolge nach Aufwand (einfach → komplex):**

1. VanEck (Format 4) — openpyxl, ISIN, Prozent-String
2. SPDR (Format 5) — openpyxl, ISIN, Float direkt
3. L&G (Format 6) — CSV, ISIN, Dezimal
4. iShares CSV (Format 1) — 14 Dateien, kein ISIN → Ticker-Mapping
5. Amundi (Format 3) — XML-Workaround, dann wie Xtrackers
6. Vanguard (Format 7) — Dateiname-Workaround, kein ISIN
7. iShares XLS (Format 8) — xlrd, Format erst inspizieren

**Pro Emittent:**
- [ ] Parser `parse_<emittent>.py`
- [ ] Mappings zur `constituent`-Tabelle (ISIN-basiert wo möglich, Name-basiert mit Logging wo nicht)
- [ ] `ticker_isin_map`-Tabelle befüllen für iShares CSV + Vanguard

**Auswertungen:**
- [ ] Aggregiertes Single-Name-Engagement (EUR + % Portfolio), absteigend
- [ ] ETF-Überlappungs-Matrix (paarweise Jaccard/gewichteter Schnitt)
- [ ] Look-Through-Allokation Sektor / Land / Währung

---

### Phase 3 — Report

**Ziel:** HTML-Report wie `send_portfolio_report.py` im Altprojekt.

- [ ] Cronjob täglich: Kurse + FX laden → Gesamtwert-Bericht per E-Mail
- [ ] Cronjob monatlich (halb-manuell): neue Holdings-Dateien einlesen → neuer Snapshot
- [ ] Bericht enthält:
  - Gesamtvermögen je Depot + gesamt (EUR), mit Verlaufsgrafik
  - ⚠️ Label **"Vermögensentwicklung inkl. Einzahlungen"**, NICHT "Rendite"
  - Top-20 Einzeltitel (Look-Through), Klumpenrisiko-Warnung wenn Einzel-Engagement > 5 %
  - ETF-Überlappungs-Heatmap
  - Look-Through-Allokation Sektor/Land/Währung

---

## Offene Fragen / vor Phase 0 zu klären

1. **IS3Q/IS3R/SXR7 → Trade-Republic-ISINs**: Welche CSV gehört zu World Quality (IE00BP3QZ601), World Momentum (IE00BP3QZ825) und Europe Momentum (IE00BQN1K786)? → iShares-Website prüfen oder nach Ticker googeln.
2. **Riester-Kurse**: Sind IE00BL25JN58, IE00B8KGV557 etc. über yfinance sauber abrufbar? → Testlauf.
3. **Datei-Benennung Konvention**: Zukünftige Downloads sollen `<ISIN>_holdings_YYYYMMDD.<ext>` heißen (statt emittent-spezifischer Dateinamen) — klären, ob du das Umbenennen übernimmst oder das Skript mit den Originalnamen umgeht.
4. **Avg_Kaufkurs im Riester**: Bleibt dauerhaft `NULL` oder gibt es eine Quelle (Versicherer-Portal)?
5. **Amundi-Workaround**: `openpyxl` mit `read_only=True` testen — evtl. umgeht das das Style-Problem und erspart den zipfile-Parser.

---

## Sofort-Nächster Schritt

**Phase 0, Schritt 1:** `db_schema.sql` schreiben und auf dem Server einspielen.

Sag Bescheid, dann fange ich damit an.
