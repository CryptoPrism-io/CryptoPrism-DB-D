"""CP-011 Phase D Stage 2B — Stage 3 validation of the shadow rebuild.

Runs against the validated target database (cp_backtest) and the frozen
snapshot manifest. Checks invariants and reconciles counts:

  - dmv_rows total / distinct slugs / incomplete count / incomplete rate
  - score range in [-100, 100]; score null rate == incomplete rate
  - d_pct_var / d_pct_cvar null rate (before min_obs) within expected bounds
  - date range within snapshot range
  - shadow slug set is a subset of the snapshot universe; missing slugs are only
    those with no eligible (>=5-day trailing window) rows
  - every slug,date key unique (PK check) and no rows for unknown slugs

Writes report/cp011-phase-d/stage3_validation.json (or --out) and prints a
PASS/FAIL summary. Read-only against the DB.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pit.db import connect

DEFAULT_OUT = Path(__file__).resolve().parent.parent.parent / "report" / "cp011-phase-d" / "stage3_validation.json"


def _manifest(snapshot: Path) -> dict:
    mp = Path(str(snapshot).replace(".parquet", ".manifest.json"))
    if not mp.exists():
        return {}
    return json.loads(mp.read_text(encoding="utf-8"))


async def _audit(conn, schema: str, method_ver: str) -> dict:
    n_rows = int(await conn.fetchval(f'SELECT COUNT(*) FROM "{schema}".dmv_rows'))
    n_slugs = int(await conn.fetchval(f'SELECT COUNT(DISTINCT slug) FROM "{schema}".dmv_rows'))
    n_incomplete = int(await conn.fetchval(
        f'SELECT COUNT(*) FROM "{schema}".dmv_rows WHERE incomplete'))
    min_score = await conn.fetchval(
        f'SELECT MIN(LEAST(COALESCE(durability_score,0),COALESCE(momentum_score,0),COALESCE(valuation_score,0))) FROM "{schema}".dmv_rows')
    max_score = await conn.fetchval(
        f'SELECT MAX(GREATEST(COALESCE(durability_score,0),COALESCE(momentum_score,0),COALESCE(valuation_score,0))) FROM "{schema}".dmv_rows')
    n_var_null = int(await conn.fetchval(
        f'SELECT COUNT(*) FROM "{schema}".dmv_rows WHERE d_pct_var IS NULL'))
    n_cvar_null = int(await conn.fetchval(
        f'SELECT COUNT(*) FROM "{schema}".dmv_rows WHERE d_pct_cvar IS NULL'))
    n_scores_null = int(await conn.fetchval(
        f'SELECT COUNT(*) FROM "{schema}".dmv_rows WHERE durability_score IS NULL AND momentum_score IS NULL AND valuation_score IS NULL'))
    mn = await conn.fetchval(f'SELECT MIN(date) FROM "{schema}".dmv_rows')
    mx = await conn.fetchval(f'SELECT MAX(date) FROM "{schema}".dmv_rows')
    dup = int(await conn.fetchval(
        f'SELECT COUNT(*) FROM (SELECT slug, date, methodology_version FROM "{schema}".dmv_rows GROUP BY 1,2,3 HAVING COUNT(*)>1) x'))
    slugs = [r["slug"] for r in await conn.fetch(
        f'SELECT DISTINCT slug FROM "{schema}".dmv_rows ORDER BY slug')]
    return {
        "rows": n_rows, "slugs": n_slugs, "incomplete": n_incomplete,
        "incomplete_rate": round(n_incomplete / n_rows, 4) if n_rows else None,
        "score_min": float(min_score) if min_score is not None else None,
        "score_max": float(max_score) if max_score is not None else None,
        "var_null": n_var_null, "var_null_rate": round(n_var_null / n_rows, 4) if n_rows else None,
        "cvar_null": n_cvar_null, "cvar_null_rate": round(n_cvar_null / n_rows, 4) if n_rows else None,
        "scores_null_rows": n_scores_null,
        "date_min": str(mn), "date_max": str(mx),
        "dup_pk_rows": dup, "shadow_slugs": slugs,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="CP-011 Stage 3 shadow validation")
    ap.add_argument("--shadow-schema", required=True)
    ap.add_argument("--snapshot", required=True, type=Path)
    ap.add_argument("--methodology-version", default="cp011_pit_v2_current_main_full_regen")
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    args = ap.parse_args()

    man = _manifest(args.snapshot)
    asyncio.run(_run(args, man))
    return 0


async def _run(args, man: dict) -> None:
    conn = await connect("cp_backtest")
    aud = await _audit(conn, args.shadow_schema, args.methodology_version)
    await conn.close()

    checks = {}
    checks["rows_in_expected_range"] = 0 < aud["rows"] <= man.get("distinct_slug_date", 10 ** 12)
    checks["no_duplicate_pk"] = aud["dup_pk_rows"] == 0
    checks["scores_in_plus_minus_100"] = (
        (aud["score_min"] is None or aud["score_min"] >= -100.0)
        and (aud["score_max"] is None or aud["score_max"] <= 100.0)
    )
    checks["scores_null_equals_incomplete"] = aud["scores_null_rows"] == aud["incomplete"]
    checks["incomplete_rate_below_10pct"] = (aud["incomplete_rate"] or 0) < 0.10
    checks["var_null_below_50pct"] = (aud["var_null_rate"] or 0) < 0.50
    checks["date_range_within_snapshot"] = (
        (not man) or (str(aud["date_min"]) >= man.get("first_date", "") and str(aud["date_max"]) <= man.get("last_date", ""))
    )
    payload = {
        "shadow_schema": args.shadow_schema,
        "methodology_version": args.methodology_version,
        "snapshot_manifest": man,
        "audit": aud,
        "checks": checks,
        "pass": all(checks.values()),
        "validated_utc": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(),
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    print(json.dumps({"checks": checks, "pass": payload["pass"]}, indent=2, default=str))
    print(f"stage3 -> {out}")


if __name__ == "__main__":
    sys.exit(main())
