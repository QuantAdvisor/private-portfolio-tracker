"""Windows-seitiger Import: die vier Broker-Parser laufen HIER, nicht auf dem
Server (2026-08-17 aus run_pipeline.py herausgetrennt, siehe design_log).

Architektur-Split: Trades/<Broker>/ ist ein lokal auf OneDrive gehaltener
Ordner mit privaten Finanzdaten (siehe [[feedback-no-private-financial-data-
to-github]]) - existiert nur auf diesem Windows-Rechner, nicht auf dem
Linux-Server. Die Parser MUESSEN daher hier laufen (DB-Zugriff ueber den
bestehenden SSH-Tunnel, wie schon bisher). Alles danach (Kurse laden,
Tracking Error, NormRt, MCTR, Reports) laeuft in run_pipeline.py auf dem
Server (Direktverbindung zur DB, kein Tunnel).

**Bewusst entkoppelt, kein direkter Trigger:** dieses Skript schreibt neue
Transaktionen nach portfolio.transaction und ist danach fertig - es stoesst
NICHT per SSH o.ae. den Server-Lauf an. Der Server-Cronjob (run_pipeline.py)
laeuft unabhaengig auf eigenem Zeitplan und verarbeitet beim naechsten Lauf,
was zu dem Zeitpunkt in der DB steht. Nutzerentscheidung 2026-08-17: einfacher
als eine Cross-Maschinen-Trigger-Mechanik, und der genaue Zeitpunkt des
Reports ist nicht zeitkritisch.

Workflow: Datei(en) in den jeweiligen Trades/<Broker>/-Ordner legen, dieses
eine Skript ausfuehren (perspektivisch: Dashboard-Button). Jeder Parser
verarbeitet IMMER alle Dateien im Ordner (idempotent, ON CONFLICT DO UPDATE) -
ein Lauf ohne neue Dateien ist ungefaehrlich.

Aufruf (von private-portfolio-tracker/ aus):
    python import_transactions.py             # Parser + Verifikation
    python import_transactions.py --dry-run    # nur Parser im --dry-run-Modus,
                                                # keine DB-Schreibvorgaenge

Oskar bewusst NICHT hier drin: der Oskar-Parser laeuft weiterhin unabhaengig
(python parse_oskar_transactions.py), da Oskar noch nicht in Phase 12/13
eingebunden ist (offener Preis-Backfill fuer 4 ISINs, siehe
PROJECT_PLAN.md Abschnitt 14-E).
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent

PARSERS = [
    "parse_scalable_transactions.py",
    "parse_traderepublic_transactions.py",
    "parse_ing_transactions.py",
    "parse_riester_transactions.py",
]


def run(script: Path, extra_args: list[str] | None = None, check: bool = True) -> int:
    args = [sys.executable, str(script)] + (extra_args or [])
    print(f"\n{'=' * 80}\n$ {' '.join(args)}\n{'=' * 80}", flush=True)
    result = subprocess.run(args, cwd=SCRIPT_DIR)
    if check and result.returncode != 0:
        raise RuntimeError(f"{script.name} endete mit Exit-Code {result.returncode}")
    return result.returncode


def main() -> None:
    sys.stdout.reconfigure(line_buffering=True)

    parser = argparse.ArgumentParser(description="Windows-seitiger Broker-Transaktions-Import")
    parser.add_argument("--dry-run", action="store_true", help="Parser im Dry-Run, keine DB-Schreibvorgaenge")
    args = parser.parse_args()

    extra = ["--dry-run"] if args.dry_run else None

    for p in PARSERS:
        run(SCRIPT_DIR / p, extra_args=extra)

    if args.dry_run:
        print("\n[--dry-run] Verifikation uebersprungen.")
        return

    run(SCRIPT_DIR / "verify_transactions_vs_snapshot.py", check=False)

    print("\n" + "=" * 80)
    print("Import fertig. Server-Backend (run_pipeline.py) laeuft unabhaengig auf eigenem Zeitplan.")
    print("Oskar separat aktualisieren: python parse_oskar_transactions.py")
    print("=" * 80)


if __name__ == "__main__":
    main()
