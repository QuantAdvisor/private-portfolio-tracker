# CLAUDE.md — ETF Look-Through Holdings Tracker

> Leitdokument für die Arbeit mit Claude Code an diesem Projekt.
> Stand: Projektstart. Sprache im Code: Englisch (Identifier), Deutsch (Reports & Kommentare wo sinnvoll).

---

## 1. Worum es geht (Scope)

Ein **konsolidierter Bestands- und Durchschau-Tracker** über mehrere Depots.
Ziel: sehen, was ich über alle ETFs und Depots hinweg **wirklich** halte — nicht „ich
habe X Anteile IWDA", sondern die aggregierten Einzeltitel-Engagements *innerhalb* der ETFs
(Look-Through / Durchschau).

Konkret beantwortet das Projekt:
- Wie viel **Nvidia / ASML / Novo Nordisk** halte ich, addiert über *alle* ETFs und Depots, in EUR und in %?
- Wie stark **überlappen** sich meine ETFs (Redundanz zwischen World-, Momentum-, Regio-ETFs)?
- Wie sieht die **konsolidierte Allokation** auf Look-Through-Basis aus — Sektor, Land, Währung, Einzeltitel-Konzentration?

### Phase 1 = Bestandstracker, KEIN Transaktionstracker
- **Zwei Frequenzen, strikt getrennt:**
  - **Kurse täglich** (`price`, `fx_rate`) — nächtlicher Cronjob, wie `equity_data` im Altprojekt.
  - **Holdings monatlich** (`etf_holding`, `position_snapshot`) — ETF-Zusammensetzung und
    eigene Stückzahlen, datierter Snapshot.
  - Begründung: Kurse altern täglich, Bestandteile praktisch nicht (Emittenten rebalancieren
    selten, veröffentlichen verzögert). Täglich Holdings ziehen wäre verschwendet.
- **Keine** Cashflows, keine Käufe/Verkäufe, keine Rendite-Zurechnung in Phase 1.
- ⚠️ **Wertänderung zwischen zwei Snapshots ist KEINE Rendite** (eine Sparplanrate sieht aus
  wie Performance). Jede Wertkurve ist als *„Vermögensentwicklung inkl. Einzahlungen"* zu
  labeln, niemals als „Rendite". Echte Renditezahlen (MWR/TWR) kommen erst in einer späteren
  Phase mit Transaktionshistorie.

### Explizit NICHT Teil dieses Projekts
- Der alte Aktien-Stock-Picker (`screener.py`, `portfolio.py`, `backtest*.py`, Signal-Logik).
  Der ist abgeschlossen, hat im Out-of-Sample-Backtest verloren (+4 % p.a. vs. ~10 % MSCI Europe)
  und dient nur als **Vorlage** für Infrastruktur-Muster (`db_utils.py`, Snapshot-Idempotenz,
  HTML-Report). Nicht wiederverwenden, nur abschauen.
- Fundamentaldaten-Loader (separates späteres Projekt).
- Optimierung / Allokationsvorschläge — dieses Tool **trackt**, es **empfiehlt nichts**.

---

## 2. Schritt 0 — IMMER zuerst: echte Dateien inspizieren

Die tatsächlichen Dateiformate kenne ich nicht im Voraus. ETF-Holdings-Exporte unterscheiden
sich **je Emittent** (iShares, Xtrackers, Amundi, SPDR …) in Spaltennamen, Trennzeichen,
Encoding, Header-Zeilen und Identifier (mal ISIN, mal Ticker, mal SEDOL, mal nur Name).

**Bevor irgendein Loader gebaut wird:**
1. Alle Dateien im Projektordner auflisten.
2. Für jede Datei: Encoding, Trennzeichen, Header-Offset, Spaltennamen und die ersten 5 Zeilen ausgeben.
3. Identifizieren: Welche Datei ist mein **Depot-Bestand** (eigene Positionen), welche sind
   **ETF-Holdings** (Bestandteile), von welchem **Emittenten** stammt jede Holdings-Datei?
4. **Erst dann** ein konkretes Spalten-Mapping vorschlagen und mit mir bestätigen.
   Nicht raten, nicht annehmen — fragen, wenn unklar.

Ein Parser pro Emittent. Das ist bewusst so; ein „universeller" Parser ist hier eine Illusion.

---

## 3. Architektur & Tech-Stack

Gleiche Grundlage wie das Vorprojekt — bewährt, nicht neu erfinden:
- **Python 3.13** in venv (`~/venv313` o. ä.), Postgres auf dem Ubuntu-Server.
- **`db_utils.py`** aus dem Vorprojekt übernehmen (SQLAlchemy + optional SSH-Tunnel,
  `.env` für Credentials). Nicht umschreiben.
- Eigenes **Schema** in der DB, getrennt vom alten `quant_advisor` — Vorschlag: `portfolio`.
- Reporting später im Stil von `send_portfolio_report.py` (HTML-Mail, deutscher Fließtext, Cronjob).

Bibliotheken: `pandas`, `sqlalchemy`, `psycopg2-binary`, `python-dotenv`, `openpyxl`
(für `.xlsx`-Holdings), später `matplotlib` für Report-Grafiken.

### Goldene Regel: DB ist die einzige Wahrheit
Abrufen/Einlesen → normalisieren → **in DB schreiben** → von dort rechnen.
Niemals direkt aus Dateien/APIs heraus aggregieren. Das garantiert Reproduzierbarkeit
und den selbst aufgebauten, datierten Verlauf.

---

## 4. Datenmodell (Vorschlag — beim ersten Lauf gegen echte Dateien schärfen)

```
account            (account_id PK, name, broker, account_type)
                   -- Depots: Scalable, ING-Gemeinschaftsdepot, Riester …

instrument         (isin PK, name, instrument_type[ETF|STOCK], currency, ...)
                   -- Stammdaten für ETFs UND Einzeltitel (Konstituenten)

position_snapshot  (account_id, isin, as_of_date, quantity)
                   PK (account_id, isin, as_of_date)
                   -- EIGENE Bestände je Depot — MONATLICH, APPEND-only

price              (isin, price_date, close, currency)
                   PK (isin, price_date)
                   -- Kurse je ETF — TÄGLICH (nächtlicher Cronjob)

fx_rate            (rate_date, currency, eur_per_unit)
                   PK (rate_date, currency)
                   -- für Währungsnormalisierung auf EUR — TÄGLICH

etf_holding        (etf_isin, as_of_date, constituent_id, weight)
                   PK (etf_isin, as_of_date, constituent_id)
                   -- Durchschau: Bestandteile je ETF — MONATLICH, APPEND-only

constituent        (constituent_id PK, isin, name_normalized, ticker, sector, country, currency)
                   -- normalisierte Identität je Einzeltitel (siehe Fallstrick 4.1)
```

> Position = **(Depot × ISIN)** — derselbe ETF kann in mehreren Depots liegen; die Depot-Sicht
> darf nicht verloren gehen. Niemals nur nach ISIN aggregieren und das Depot vergessen.

---

## 5. Die Fallstricke — hier steckt die eigentliche Arbeit

### 5.1 Identitäts-Mapping der Konstituenten (die stille Fehlerquelle Nr. 1)
Verschiedene ETFs identifizieren denselben Titel verschieden: „NVIDIA", „NVIDIA CORP",
mal per ISIN, mal Ticker, mal SEDOL. Ohne saubere Normalisierung wird **derselbe Titel
doppelt gezählt** und die Aggregation ist wertlos.
→ Identität wann immer möglich über **ISIN** auflösen. Wo nur Name vorliegt: konservativ
mappen, ungelöste Fälle **protokollieren und melden**, nicht still droppen.

### 5.2 Währung / EUR-Normalisierung
ETFs und Konstituenten notieren in verschiedenen Währungen (EUR, USD, GBP, CHF, SEK …).
Für die Aggregation muss alles auf **eine gemeinsame Währung (EUR)** gebracht werden,
sonst addiert man Äpfel und Birnen. FX-Kurse mitführen und zum jeweiligen Stichtag anwenden.

### 5.3 Snapshot = APPEND, niemals überschreiben
Monatlich einen **neuen datierten Snapshot anhängen** (`position_snapshot`, `etf_holding`),
die bestehenden nie überschreiben. Das gibt gratis: Allokations-Drift über Zeit und — später —
die Möglichkeit, aus Stückzahl-Differenzen zweier Snapshots Netto-Transaktionen zu
rekonstruieren (Brückenkopf zum MWR/TWR-Schritt, ohne Umbau).

### 5.4 Idempotente Loads
Jeder Loader muss gefahrlos wiederholbar sein (abgebrochener Cronjob darf keine Duplikate
hinterlassen): `INSERT … ON CONFLICT (…) DO UPDATE` oder vorher gezielt löschen — wie
`save_snapshot()` im Vorprojekt.

### 5.5 Datenbeschaffung ist 90 % des Aufwands, nicht die Logik
Die Aggregations-Mathematik ist trivial (gewichtetes Summieren). Die Hürde ist: Holdings-Dateien
beschaffen, parsen, normalisieren. Realistisch **halb-manuell, monatlich**: ich lade die
Emittenten-Dateien herunter, das Skript parst und aggregiert. Kein fragiles Auto-Scraping.

### 5.6 ETF-Kurse sind tückisch
yfinance ist bei europäischen UCITS-ETFs oft unzuverlässig (falscher Börsenplatz/Währung).
Kursquelle bewusst wählen und prüfen. Riester-/Versicherungsmäntel haben teils **gar keine**
sauber abrufbaren Kurse — diese Positionen ggf. gesondert behandeln oder als bekannt-fehlend markieren.

---

## 6. Kern-Berechnung (Look-Through)

### As-of-Join: tägliche Kurse × jüngste Holdings
Kurse sind täglich, Holdings monatlich. Für die Bewertung an einem beliebigen Tag t werden
die **jeweils jüngsten verfügbaren Holdings** verwendet (`as_of_date <= t`), also zwischen
den Monatssnapshots **forward-gefüllt**. Beispiel: Bewertung am 15. März = Kurs vom 15. März
× Holdings-Gewichte vom letzten Snapshot (z. B. 28. Februar).

Dies ist exakt das Muster aus dem Altprojekt (Snapshot-Gewichte gelten „ab erstem Handelstag
nach Snapshot bis zum nächsten"). In Postgres als `LATERAL`-Join bzw.
„latest `as_of_date` ≤ `price_date`".

⚠️ Konsequenz bewusst lesen: Die tägliche Look-Through-Zahl ist **„Kurse von heute × Gewichte
vom Monatssnapshot"**, nicht die heutige Real-Zusammensetzung. Zwischen zwei Snapshots driftet
die wahre ETF-Zusammensetzung leicht (Index-Anpassungen, Corporate Actions), sichtbar erst beim
nächsten Snapshot. Für ein Klumpen-/Tracking-Tool vernachlässigbar — große Positionen ändern
sich nicht über Nacht.

### Formel
Für jeden Konstituenten c, zum Tag t:

```
effektives_Engagement(c, t) =
    Σ über alle ETFs e:  eigener_Wert(e, t) × gewicht(c in e, holdings_asof(e, t))

  wobei eigener_Wert(e, t)   = Σ über Depots:  quantity(e, depot, snap_asof(t)) × etf_kurs_EUR(e, t)
        holdings_asof(e, t)  = jüngste etf_holding.as_of_date ≤ t
        snap_asof(t)         = jüngste position_snapshot.as_of_date ≤ t
```

→ Kurse (`etf_kurs_EUR`) bewegen sich täglich; Gewichte und eigene Stückzahlen bleiben
zwischen den Monatssnapshots konstant.

### Auswertungen
- **Gesamtwert & naive Allokation** je ETF/Depot — **täglich**, auf tagesaktuellen Kursen.
- **Aggregierte Einzeltitel-Liste** (EUR und % des Gesamtportfolios), absteigend → Single-Name-Konzentration.
  Bewegt sich täglich mit den Kursen, Gewichte konstant bis zum nächsten Holdings-Snapshot.
- **ETF-Überlappung** — paarweise Schnittmenge der Holdings (gewichtet / Jaccard).
- **Look-Through-Allokation** — Sektor, Land, Währung auf Durchschau-Basis vs. naive ETF-Sicht.

---

## 7. Roadmap (inkrementell, jede Phase eigenständig lauffähig)

- **Phase 0 — Fundament:** Schema anlegen, `db_utils` übernehmen, eigene Positionen +
  ETF-Kurse laden → Gesamtwert (EUR) und naive Allokation je ETF/Depot. Liefert sofort Nutzen.
  - Hier schon: **täglicher Kurs-Loader** als eigener Job, getrennt vom Holdings-Load.
- **Phase 1 — erster Emittent:** Holdings-Datei *eines* Emittenten parsen → `etf_holding` →
  Look-Through für diesen einen ETF (mit As-of-Join gegen die Tageskurse). Prototyp für den Parser-Ansatz.
- **Phase 2 — alle ETFs + Identität:** restliche Parser, Konstituenten-Normalisierung,
  aggregiertes Single-Name-Engagement + ETF-Überlappung.
- **Phase 3 — Report:** HTML-Report (deutscher Fließtext) im Stil des Vorprojekts, im Cronjob.

### Zwei getrennte Cronjobs
- **Täglich** (abends, nach Börsenschluss): Kurse + FX laden, Bewertung aktualisieren, ggf. Report.
- **Monatlich**: Holdings-Dateien (halb-manuell heruntergeladen) parsen → neuer datierter Snapshot.
- **Später:** Transaktionshistorie → MWR/TWR (echte Rendite); Ausschüttungs-Tracking; Steuer-Layer.

Immer eine Phase abschließen und lauffähig machen, bevor die nächste beginnt.

---

## 8. Arbeitsweise mit Claude Code (Konventionen)

- **Befehle einzeln ausgeben**, nicht als ein großer Block zum Reinpasten — ich führe sie
  einzeln aus und prüfe das Ergebnis.
- Bei Unklarheit **nachfragen statt raten** — besonders bei Spaltennamen, Identifiern, Währungen.
- **Keine Floskeln**, knappe präzise Begründungen.
- Reports/Outputs in **Deutsch**; Code-Identifier Englisch.
- **Defensiv programmieren:** fehlende Felder als NULL, jeder externe Zugriff in try/except,
  ungelöste Mappings protokollieren statt still verwerfen.
- Secrets nur in `.env`, nie im Code oder in Commits.
- Klein anfangen, jede Stufe testbar halten.

---

## 9. Kontext zu mir (für sinnvolle Defaults)

Portfolio-Manager (Fixed-Income-FoF), ~6 Jahre Python, arbeitet mit Python/SQL. Versteht
Quant-Methodik — technische Tiefe ist erwünscht, keine Vereinfachung nötig. Ziel des realen
Portfolios: aus Erträgen leben, Substanz erhalten, langfristig an die Tochter vererben
(langer Buy-and-Hold-Horizont). Genau deshalb ist die Durchschau auf Klumpen-/Redundanzrisiko
relevant — und ein monatlicher Tracker das richtige Werkzeug, kein aktiver Handelsansatz.
