"""
Rein lesende Plausibilitaetspruefung: aus portfolio.transaction abgeleitete
Stueckzahl je (account_id, isin) gegen den juengsten portfolio.position_snapshot
vergleichen. Fuer alle Konten mit Transaktions-Ledger (Scalable, ING, Trade
Republic - Riester/Oskar folgen, sobald ihre Parser stehen).

Erwartete, unkritische Abweichungsquellen (kein Bug):
  - Datumsluecke zwischen Transaktions-Enddatum und letztem Excel-Snapshot
  - komplett verkaufte/geswitchte ISINs (derived=0, korrekt fehlend im Snapshot)

Aufruf:
    python verify_transactions_vs_snapshot.py
"""

import logging

import pandas as pd

import db_utils

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger(__name__)

QUANTITY_MOVING_TYPES = (
    "BUY", "SELL", "DRIP", "FREE_RECEIPT", "TRANSFER_SECURITY_IN", "TRANSFER_SECURITY_OUT",
)

ACCOUNTS = {
    1: "Scalable Christian",
    2: "Ing Gemeinschaftsdepot",
    3: "Christian Riester",
    4: "Christian Trade Republic",
}


def derived_quantities(account_id: int) -> pd.DataFrame:
    placeholders = ", ".join(f"'{t}'" for t in QUANTITY_MOVING_TYPES)
    return db_utils.query_df(
        f"""
        SELECT isin, SUM(quantity) AS derived_qty
        FROM portfolio.transaction
        WHERE account_id = :account_id AND txn_type IN ({placeholders}) AND isin IS NOT NULL
        GROUP BY isin
        """,
        {"account_id": account_id},
    )


def snapshot_quantities(account_id: int) -> pd.DataFrame:
    return db_utils.query_df(
        """
        SELECT isin, quantity AS snapshot_qty
        FROM portfolio.position_snapshot ps
        WHERE account_id = :account_id
          AND as_of_date = (
              SELECT MAX(as_of_date) FROM portfolio.position_snapshot WHERE account_id = :account_id
          )
        """,
        {"account_id": account_id},
    )


def main() -> None:
    for account_id, name in ACCOUNTS.items():
        log.info("=== Konto %d (%s) ===", account_id, name)

        derived = derived_quantities(account_id)
        snapshot = snapshot_quantities(account_id)

        merged = derived.merge(snapshot, on="isin", how="outer").fillna(0)
        merged["delta"] = merged["derived_qty"] - merged["snapshot_qty"]
        merged["delta_pct"] = (
            merged["delta"] / merged["snapshot_qty"].replace(0, pd.NA) * 100
        )
        merged = merged.sort_values("delta", key=lambda s: s.abs(), ascending=False)

        pd.set_option("display.width", 160)
        print(merged.to_string(index=False))
        print()


if __name__ == "__main__":
    main()
