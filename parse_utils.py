"""
Gemeinsame Hilfsfunktionen für alle ETF-Holdings-Parser.
Enthält: SQL-Statements, Länder-Mapping, DB-Schreib-Wrapper, Gewicht-Prüfung.
"""

import logging
from collections import defaultdict

import db_utils

log = logging.getLogger(__name__)


# ── Länder-Mapping (Deutsch → ISO-3166-1-Alpha-2) ────────────────────────────

COUNTRY_MAP_DE: dict[str, str] = {
    "australien":                        "AU",
    "belgien":                           "BE",
    "brasilien":                         "BR",
    "china":                             "CN",
    "dänemark":                          "DK",
    "danemark":                          "DK",
    "deutschland":                       "DE",
    "finnland":                          "FI",
    "frankreich":                        "FR",
    "griechenland":                      "GR",
    "großbritannien (uk)":               "GB",
    "grossbritannien (uk)":              "GB",
    "vereinigtes königreich":            "GB",
    "vereinigtes konigreich":            "GB",
    "hongkong":                          "HK",
    "indien":                            "IN",
    "indonesien":                        "ID",
    "irland":                            "IE",
    "israel":                            "IL",
    "italien":                           "IT",
    "japan":                             "JP",
    "kanada":                            "CA",
    "katar":                             "QA",
    "korea":                             "KR",
    "luxemburg":                         "LU",
    "malaysia":                          "MY",
    "mexiko":                            "MX",
    "neuseeland":                        "NZ",
    "niederlande":                       "NL",
    "norwegen":                          "NO",
    "österreich":                        "AT",
    "osterreich":                        "AT",
    "philippinen":                       "PH",
    "polen":                             "PL",
    "portugal":                          "PT",
    "saudi-arabien":                     "SA",
    "schweden":                          "SE",
    "schweiz":                           "CH",
    "singapur":                          "SG",
    "spanien":                           "ES",
    "südafrika":                         "ZA",
    "sudafrika":                         "ZA",
    "taiwan":                            "TW",
    "thailand":                          "TH",
    "tschechische republik":             "CZ",
    "türkei":                            "TR",
    "turkei":                            "TR",
    "ungarn":                            "HU",
    "vereinigte arabische emirate":      "AE",
    "vereinigte staaten von amerika":    "US",
    "vereinigte staaten":                "US",
    # Kurzformen aus Amundi
    "usa":                               "US",
    "uk":                                "GB",
    "s. korea":                          "KR",
    "südkorea":                          "KR",
    "sudkorea":                          "KR",
    "vae":                               "AE",
    "kenia":                             "KE",
    "ägypten":                           "EG",
    "agypten":                           "EG",
    "pakistan":                          "PK",
    "peru":                              "PE",
    "kolumbien":                         "CO",
    "chile":                             "CL",
    "griechenland":                      "GR",
    "rumänien":                          "RO",
    "rumanien":                          "RO",
    "tschechien":                        "CZ",
}

# ── Länder-Mapping (Englisch → ISO-3166-1-Alpha-2, für SPDR) ─────────────────

COUNTRY_MAP_EN: dict[str, str] = {
    "australia":                "AU",
    "austria":                  "AT",
    "belgium":                  "BE",
    "brazil":                   "BR",
    "canada":                   "CA",
    "china":                    "CN",
    "colombia":                 "CO",
    "czechia":                  "CZ",
    "czech republic":           "CZ",
    "denmark":                  "DK",
    "egypt":                    "EG",
    "finland":                  "FI",
    "france":                   "FR",
    "germany":                  "DE",
    "greece":                   "GR",
    "hong kong":                "HK",
    "hungary":                  "HU",
    "india":                    "IN",
    "indonesia":                "ID",
    "ireland":                  "IE",
    "israel":                   "IL",
    "italy":                    "IT",
    "japan":                    "JP",
    "kenya":                    "KE",
    "luxembourg":               "LU",
    "malaysia":                 "MY",
    "mexico":                   "MX",
    "netherlands":              "NL",
    "new zealand":              "NZ",
    "norway":                   "NO",
    "pakistan":                 "PK",
    "peru":                     "PE",
    "philippines":              "PH",
    "poland":                   "PL",
    "portugal":                 "PT",
    "qatar":                    "QA",
    "romania":                  "RO",
    "saudi arabia":             "SA",
    "singapore":                "SG",
    "south africa":             "ZA",
    "south korea":              "KR",
    "spain":                    "ES",
    "sweden":                   "SE",
    "switzerland":              "CH",
    "taiwan":                   "TW",
    "thailand":                 "TH",
    "turkey":                   "TR",
    "united arab emirates":     "AE",
    "united kingdom":           "GB",
    "united states":            "US",
}


def normalize_country_de(raw: str | None) -> str | None:
    if not raw:
        return None
    key = raw.lower().strip()
    key = key.replace("ü", "u").replace("ö", "o").replace("ä", "a").replace("ß", "ss")
    result = COUNTRY_MAP_DE.get(key)
    if result is None:
        log.debug("Unbekanntes Land (DE): %r – als NULL gespeichert", raw)
    return result


def normalize_country_en(raw: str | None) -> str | None:
    if not raw:
        return None
    key = raw.lower().strip()
    result = COUNTRY_MAP_EN.get(key)
    if result is None:
        log.debug("Unbekanntes Land (EN): %r – als NULL gespeichert", raw)
    return result


# ── SQL-Statements ────────────────────────────────────────────────────────────

UPSERT_CONSTITUENT = """
INSERT INTO portfolio.constituent (isin, name, sektor, country, currency)
VALUES (:isin, :name, :sektor, :country, :currency)
ON CONFLICT (isin) DO UPDATE SET
    name     = EXCLUDED.name,
    sektor   = COALESCE(EXCLUDED.sektor,   portfolio.constituent.sektor),
    country  = COALESCE(EXCLUDED.country,  portfolio.constituent.country),
    currency = COALESCE(EXCLUDED.currency, portfolio.constituent.currency)
"""

UPSERT_HOLDING = """
INSERT INTO portfolio.etf_holding
    (etf_isin, as_of_date, constituent_isin, weight_pct, source_file)
VALUES
    (:etf_isin, :as_of_date, :constituent_isin, :weight_pct, :source_file)
ON CONFLICT (etf_isin, as_of_date, constituent_isin) DO UPDATE SET
    weight_pct  = EXCLUDED.weight_pct,
    source_file = EXCLUDED.source_file,
    loaded_at   = now()
"""

UPSERT_UNRESOLVED = """
INSERT INTO portfolio.unresolved_holding
    (etf_isin, as_of_date, raw_ticker, raw_name, weight_pct, sektor, country, currency)
VALUES
    (:etf_isin, :as_of_date, :raw_ticker, :raw_name, :weight_pct, :sektor, :country, :currency)
ON CONFLICT (etf_isin, as_of_date, raw_ticker) DO UPDATE SET
    raw_name   = EXCLUDED.raw_name,
    weight_pct = EXCLUDED.weight_pct,
    sektor     = EXCLUDED.sektor,
    country    = EXCLUDED.country,
    currency   = EXCLUDED.currency,
    loaded_at  = now()
"""


# ── DB-Schreib-Wrapper ────────────────────────────────────────────────────────

def write_constituents_and_holdings(
    constituents: list[dict],
    holdings: list[dict],
) -> None:
    seen: set[str] = set()
    unique = []
    for c in constituents:
        if c["isin"] not in seen:
            seen.add(c["isin"])
            unique.append(c)
    db_utils.execute_many(UPSERT_CONSTITUENT, unique)
    db_utils.execute_many(UPSERT_HOLDING, holdings)


def write_unresolved(records: list[dict]) -> None:
    db_utils.execute_many(UPSERT_UNRESOLVED, records)


# ── Gewicht-Plausibilitätsprüfung ─────────────────────────────────────────────

def log_weight_sums(rows: list[dict], weight_key: str = "weight_pct") -> None:
    """Prüft ob Gewichtssumme je (etf_isin, as_of_date) ~100 % ergibt."""
    sums: dict[tuple, float] = defaultdict(float)
    for r in rows:
        sums[(r["etf_isin"], r["as_of_date"])] += r[weight_key]
    for (isin, dt), total in sums.items():
        if abs(total - 100) > 5:
            log.warning("Gewichtssumme %s %s = %.2f %% (erwartet ~100 %%)", isin, dt, total)
        else:
            log.info("Gewichtssumme %s %s = %.2f %%", isin, dt, total)
