"""CP-011 Stage 2B — select the shadow schema for this run (STRICT reuse).

Instruction #6/#7:
  - Reuse an existing schema ONLY if its run ID, methodology version, snapshot
    checksum, and checkpoint metadata match the intended run.
  - NEVER reuse an arbitrary/stale schema, and NEVER create a duplicate schema
    merely because execution restarted.

Logic:
  1. SHADOW_SCHEMA (optional pin): if the pinned schema already has a
     shadow_run_meta row that does NOT match METHOD_VERSION + SNAPSHOT_SHA256,
     abort (refuse to touch an unrelated run). A pinned schema with no meta row
     is treated as a fresh, unstarted schema.
  2. Else scan schemas LIKE 'pit_dmv_cp011_v2%'. Reuse only those whose
     shadow_run_meta matches METHOD_VERSION AND SNAPSHOT_SHA256 exactly. Among
     matches, resume the most recently created run (chunk_progress resumes).
  3. If nothing matches, emit a fresh name pit_dmv_cp011_v2_<YYYYmmddTHHMMZ>.

Prints the chosen schema name only. Read-only against the DB.
"""
from __future__ import annotations

import os
import re
import sys
import urllib.parse
from datetime import datetime, timezone

METHOD_VERSION = os.environ.get("METHOD_VERSION", "cp011_pit_v2_current_main_full_regen")
SNAPSHOT_SHA256 = os.environ.get("SNAPSHOT_SHA256", "").strip().lower()
DSN = os.environ.get("CP_BACKTEST_DSN")
if not DSN:
    raise SystemExit("CP_BACKTEST_DSN not set")
if not SNAPSHOT_SHA256:
    raise SystemExit("SNAPSHOT_SHA256 not set (read from the frozen manifest after freeze)")


def _matches(meta) -> bool:
    return (
        meta is not None
        and str(meta["methodology_version"]) == METHOD_VERSION
        and str(meta["snapshot_sha256"] or "").lower() == SNAPSHOT_SHA256
    )


def main() -> int:
    import asyncio

    import asyncpg

    m = re.search(r"postgresql(?:\+asyncpg)?://([^:]+):([^@]+)@([^:]+):(\d+)/(\w+)", DSN)
    if m is None:
        raise SystemExit("CP_BACKTEST_DSN format not recognized")
    pw = urllib.parse.unquote(m.group(2))
    use_ssl = os.environ.get("DB_SSL", "true").lower() == "true"

    async def _go() -> None:
        conn = await asyncpg.connect(
            host=m.group(3), port=int(m.group(4)), user=m.group(1),
            password=pw, database=m.group(5),
            ssl="require" if use_ssl else None,
        )
        explicit = os.environ.get("SHADOW_SCHEMA", "").strip()
        if explicit:
            meta = await _meta(conn, explicit)
            if meta is not None and not _matches(meta):
                await conn.close()
                raise SystemExit(
                    f"REFUSED: pinned schema {explicit!r} belongs to a different run "
                    f"(method={meta['methodology_version']} sha={str(meta['snapshot_sha256'])[:12]}); "
                    "do not reuse an arbitrary stale schema."
                )
            await conn.close()
            print(explicit)
            return

        rows = await conn.fetch(
            "SELECT schema_name FROM information_schema.schemata "
            "WHERE schema_name LIKE 'pit_dmv_cp011_v2%' ORDER BY schema_name"
        )
        matches = []
        for r in rows:
            meta = await _meta(conn, r["schema_name"])
            if _matches(meta):
                matches.append((r["schema_name"], meta["created_at"]))
        await conn.close()
        if matches:
            matches.sort(key=lambda t: t[1], reverse=True)  # newest run first
            print(matches[0][0])
            return
        print("pit_dmv_cp011_v2_" + datetime.now(timezone.utc).strftime("%Y%m%d_%H%M"))

    asyncio.run(_go())
    return 0


async def _meta(conn, schema: str):
    try:
        return await conn.fetchrow(
            f'SELECT run_id, methodology_version, snapshot_sha256, created_at '
            f'FROM "{schema}".shadow_run_meta ORDER BY created_at DESC LIMIT 1'
        )
    except Exception:  # noqa: BLE001
        return None


if __name__ == "__main__":
    sys.exit(main())
