"""
ETF Look-Through Tracker – täglicher Kurs-Loader (Server-Cronjob).

Direktverbindung zur DB (kein SSH-Tunnel): db_utils.py erkennt fehlendes
HOST_SSH in .env automatisch.

.env auf dem Server (kein HOST_SSH, kein SSH-Tunnel):
    DB_USER=christian_schott
    DB_PASSWORD=<password>
    DATABASE=quant_advisor
    HOST_DB=localhost
    PORT=5432

Crontab (täglich Mo–Fr, 09:00):
    00 9 * * 1-5 cd /pfad/zum/projekt && /pfad/zum/venv/bin/python etf_tracker_update_prices.py
"""

import logging
import sys
from datetime import date
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent.resolve()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.FileHandler(SCRIPT_DIR / "etf_tracker_prices.log"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)

import load_prices


def main() -> None:
    today = date.today()
    start = load_prices._last_price_date()

    log.info("=== ETF Tracker Kurs-Update %s → %s ===", start, today)

    n_prices = load_prices.load_etf_prices(start, today)
    n_fx     = load_prices.load_fx_rates(start, today)
    n_bench  = load_prices.load_benchmark_prices(start, today)

    log.info("=== Fertig: %d ETF, %d FX, %d Benchmark-Zeilen ===", n_prices, n_fx, n_bench)


if __name__ == "__main__":
    main()
