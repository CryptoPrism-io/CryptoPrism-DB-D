#!/usr/bin/env python
"""CP-011 Stage 2B — mandatory AWS-side pre-launch probe.

Gate (item 6): before ANY write, from inside AWS (private VPC):
  1. DNS: resolve the RDS hostname -> IP(s)
  2. TCP: connect to the RDS endpoint:5432
  3. SELECT 1 through the validated CP_BACKTEST_DSN
  4. Inspect for existing CP-011 shadow schemas (pit_dmv_*) + their
     shadow_run_meta (run_id / methodology_version / snapshot_sha256 / status)
     and chunk_progress counts, so the executor can confirm whether a valid
     run exists to resume and that no rebuild is already running.

Exits non-zero if DNS, TCP or SELECT 1 fails. Read-only.
"""
from __future__ import annotations

import os
import re
import socket
import sys
import urllib.parse

DSN = os.environ.get("CP_BACKTEST_DSN")
if not DSN:
    raise SystemExit("CP_BACKTEST_DSN not set")


def _host(port: int = 5432) -> None:
    m = re.search(r"postgresql(?:\+asyncpg)?://([^:]+):([^@]+)@([^:]+):(\d+)/(\w+)", DSN)
    if m is None:
        raise SystemExit("CP_BACKTEST_DSN format not recognized")
    host, port = m.group(3), int(m.group(4))
    print(f"host={host} port={port} db={m.group(5)}")
    try:
        infos = socket.getaddrinfo(host, port, socket.AF_UNSPEC, socket.SOCK_STREAM)
        ips = sorted({info[4][0] for info in infos})
        print(f"DNS OK -> {ips}")
    except Exception as e:  # noqa: BLE001
        print(f"DNS FAIL {type(e).__name__}: {str(e)[:120]}")
        return
    ok = False
    for ip in ips:
        try:
            with socket.create_connection((ip, port), timeout=10):
                print(f"TCP OK -> {ip}:{port}")
                ok = True
                break
        except Exception as e:  # noqa: BLE001
            print(f"TCP FAIL {ip}:{port} {type(e).__name__}: {str(e)[:80]}")
    if not ok:
        print("PROBE FAIL: no reachable address", flush=True)
        sys.exit(1)


def _select1() -> None:
    import asyncio

    import asyncpg

    m = re.search(r"postgresql(?:\+asyncpg)?://([^:]+):([^@]+)@([^:]+):(\d+)/(\w+)", DSN)
    pw = urllib.parse.unquote(m.group(2))

    async def go() -> None:
        conn = await asyncpg.connect(
            host=m.group(3), port=int(m.group(4)), user=m.group(1),
            password=pw, database=m.group(5), ssl="require",
        )
        v = await conn.fetchval("SELECT 1")
        print(f"SELECT 1 OK -> {v} (user={m.group(1)} db={m.group(5)})")
        schemas = await conn.fetch(
            "SELECT schema_name FROM information_schema.schemata "
            "WHERE schema_name LIKE 'pit_dmv%' ORDER BY schema_name"
        )
        print(f"existing pit_dmv_* schemas: {len(schemas)}")
        for row in schemas:
            s = row["schema_name"]
            try:
                meta = await conn.fetchrow(
                    f'SELECT run_id, methodology_version, snapshot_sha256, status, '
                    f'created_at FROM "{s}".shadow_run_meta ORDER BY created_at DESC LIMIT 1'
                )
            except Exception:  # noqa: BLE001
                meta = None
            try:
                done = await conn.fetchval(
                    f'SELECT COUNT(*) FROM "{s}".chunk_progress WHERE status=\'done\''
                )
            except Exception:  # noqa: BLE001
                done = None
            if meta:
                print(f"  {s}: run={meta['run_id']} method={meta['methodology_version']} "
                      f"sha={str(meta['snapshot_sha256'])[:12]}... status={meta['status']} "
                      f"done_chunks={done} created={meta['created_at']}")
            else:
                print(f"  {s}: no shadow_run_meta (incomplete/stale)")
        await conn.close()

    asyncio.run(go())


if __name__ == "__main__":
    _host()
    _select1()
    print("PROBE PASS", flush=True)
