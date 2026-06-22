"""
Standalone DB-Zugriff für den ETF Look-Through Tracker.
Unterstützt direkte Verbindung (Server) und SSH-Tunnel (lokale Entwicklung).
Übernommen aus portfolio-tracker, unverändert.
"""

import contextlib
import os
import select
import socket
import threading
from typing import List, Dict, Any, Optional

import pandas as pd
import sqlalchemy
from sqlalchemy import text
from dotenv import load_dotenv

load_dotenv()


# ── Engine-Aufbau ─────────────────────────────────────────────────────────────

def _build_engine(host: str, port: int) -> sqlalchemy.engine.Engine:
    url = sqlalchemy.engine.URL.create(
        drivername="postgresql+psycopg2",
        username=os.environ["DB_USER"],
        password=os.environ["DB_PASSWORD"],
        host=host,
        port=port,
        database=os.environ["DATABASE"],
    )
    return sqlalchemy.create_engine(url)


def _bridge(local_conn: socket.socket, remote_chan) -> None:
    try:
        while True:
            r, _, _ = select.select([local_conn, remote_chan], [], [], 10.0)
            if local_conn in r:
                data = local_conn.recv(4096)
                if not data:
                    break
                remote_chan.sendall(data)
            if remote_chan in r:
                data = remote_chan.recv(4096)
                if not data:
                    break
                local_conn.sendall(data)
    finally:
        with contextlib.suppress(Exception):
            remote_chan.close()
        with contextlib.suppress(Exception):
            local_conn.close()


@contextlib.contextmanager
def _get_engine():
    """
    Liefert eine SQLAlchemy-Engine.
    Wenn HOST_SSH gesetzt ist, wird ein SSH-Tunnel verwendet (lokale Entwicklung).
    Auf dem Server (kein HOST_SSH) direkte Verbindung zu localhost.
    """
    ssh_host = os.environ.get("HOST_SSH")
    db_host  = os.environ.get("HOST_DB", "localhost")
    db_port  = int(os.environ.get("PORT", "5432"))

    if ssh_host:
        import paramiko

        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(
            hostname=ssh_host,
            username=os.environ["USER_SSH"],
            password=os.environ["PW_SSH"],
        )

        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", 0))
        srv.listen(5)
        local_port: int = srv.getsockname()[1]
        stop = threading.Event()

        def _accept():
            srv.settimeout(1.0)
            while not stop.is_set():
                try:
                    conn, addr = srv.accept()
                except socket.timeout:
                    continue
                except Exception:
                    break
                try:
                    chan = client.get_transport().open_channel(
                        "direct-tcpip", (db_host, db_port), addr
                    )
                except Exception:
                    conn.close()
                    continue
                threading.Thread(target=_bridge, args=(conn, chan), daemon=True).start()

        threading.Thread(target=_accept, daemon=True).start()
        engine = _build_engine("127.0.0.1", local_port)
        try:
            yield engine
        finally:
            stop.set()
            with contextlib.suppress(Exception):
                srv.close()
            with contextlib.suppress(Exception):
                client.close()
            engine.dispose()
    else:
        engine = _build_engine(db_host, db_port)
        try:
            yield engine
        finally:
            engine.dispose()


# ── Öffentliche API ───────────────────────────────────────────────────────────

def query_df(sql: str, params: Optional[Dict[str, Any]] = None) -> pd.DataFrame:
    """SELECT → DataFrame."""
    with _get_engine() as engine:
        with engine.connect() as conn:
            result = conn.execute(text(sql), params or {})
            return pd.DataFrame(result.fetchall(), columns=list(result.keys()))


def execute(sql: str, params: Optional[Dict[str, Any]] = None) -> None:
    """Einzelne DML-Anweisung (INSERT / UPDATE / DELETE)."""
    with _get_engine() as engine:
        with engine.begin() as conn:
            conn.execute(text(sql), params or {})


def execute_many(sql: str, rows: List[Dict[str, Any]]) -> None:
    """Bulk-INSERT/-UPDATE für eine Liste von Dicts."""
    if not rows:
        return
    with _get_engine() as engine:
        with engine.begin() as conn:
            conn.execute(text(sql), rows)


def execute_returning(sql: str, params: Optional[Dict[str, Any]] = None) -> pd.DataFrame:
    """DML mit RETURNING-Klausel → DataFrame (z. B. für INSERT … RETURNING id)."""
    with _get_engine() as engine:
        with engine.begin() as conn:
            result = conn.execute(text(sql), params or {})
            return pd.DataFrame(result.fetchall(), columns=list(result.keys()))
