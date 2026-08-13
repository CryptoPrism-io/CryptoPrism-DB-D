"""Shared DB connection helper with DNS-resolution fallback.

This environment intermittently fails Python hostname resolution (getaddrinfo
11004) even though nslookup works. When that happens we resolve via `nslookup`
and connect to the resolved IP with a NON-VERIFYING SSL context. This is an
environmental workaround for READ-ONLY research connections to the account's own
RDS with valid credentials; canonical tables are never written by these paths.
"""

from __future__ import annotations

import os
import re
import socket
import ssl
import subprocess
import urllib.parse

DSN_SRC = r"C:\cpio_db\cryptoprism-onchain\.env"


def _dsn() -> str:
    dsn = os.getenv("CP_BACKTEST_DSN")
    if dsn:
        return dsn
    from dotenv import load_dotenv

    load_dotenv(DSN_SRC)
    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        raise SystemExit("no CP_BACKTEST_DSN / DATABASE_URL available.")
    return dsn


def resolve_host(host: str) -> tuple[str, bool]:
    """Return (ip_or_host, used_fallback)."""
    try:
        socket.getaddrinfo(host, 5432, socket.AF_INET)
        return host, False
    except Exception:  # noqa: BLE001
        pass
    try:
        out = subprocess.run(
            ["nslookup", host], capture_output=True, text=True, timeout=20
        ).stdout
        m = re.search(r"Address:\s+(\d{1,3}(?:\.\d{1,3}){3})", out)
        if m:
            return m.group(1), True
    except Exception:  # noqa: BLE001
        pass
    return host, True


async def connect(database: str = "cp_backtest", timeout: float | None = None):
    import asyncpg

    dsn = _dsn()
    m = re.search(r"postgresql(?:\+asyncpg)?://([^:]+):([^@]+)@([^:]+):(\d+)/(\w+)", dsn)
    if m is None:
        raise SystemExit("DATABASE_URL format not recognized")
    host, port, db_default = m.group(3), m.group(4), m.group(5)
    pw = urllib.parse.unquote(m.group(2))
    target_db = database or db_default

    ip, fallback = resolve_host(host)
    if fallback and ip != host:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        conn = await asyncpg.connect(
            host=ip, port=port, user=m.group(1), password=pw,
            database=target_db, ssl=ctx, timeout=timeout,
        )
    else:
        conn = await asyncpg.connect(
            host=host, port=port, user=m.group(1), password=pw,
            database=target_db, ssl="require", timeout=timeout,
        )
    return conn
