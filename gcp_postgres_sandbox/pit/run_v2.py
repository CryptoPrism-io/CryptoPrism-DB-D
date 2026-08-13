"""CP-011 Phase D Stage 2B — full v2 shadow rebuild (raw-OHLCV regeneration).

Memory-safe HYBRID design (byte-identical to a monolithic single-pass build):

  - The oscillators family (ADX) is computed ONCE on the full frozen OHLCV
    snapshot, because gcp_dmv_osc.calculate_adx uses GLOBAL shifts for +DM/-DM
    (cross-slug contamination at slug boundaries). Chunking oscillators would
    change ADX values, so they must see the full frame. Output = osc_bins.parquet.
  - Every other family (momentum, tvv, ratios, PIT VaR/CVaR, PIT metrics) is
    per-slug local (ratios only additionally needs the bitcoin benchmark), so it
    is byte-identical when computed per chunk that includes bitcoin.
  - The full monolithic build and the hybrid produce byte-identical dmv_rows
    (proven by equivalence_chunked.py on the frozen sample).

Safety contract (unchanged from run_shadow.py):
  - ``--shadow-schema`` REQUIRED, must start with ``pit_dmv_``; canonical names
    rejected by pit.targets.assert_shadow_schema.
  - Source reads are read-only. Writes go only to the shadow schema.
  - Idempotent upserts keyed on (slug, date, methodology_version).
  - Resumable: chunked by slug; already-materialized chunks are skipped.

Modes:
  freeze   DB -> frozen OHLCV parquet + manifest (rows/assets/checksum/timestamp)
  osc      frozen OHLCV -> osc_bins.parquet (full-frame oscillators)
  chunk    chunk compute (momentum/tvv/ratios/var/metrics/scores) -> shadow schema
  preflight  no-write summary of what chunk mode will do
  ledger   finalize shadow_run_meta with reconciled counts

Usage:
  python run_v2.py --mode freeze --snapshot <path>
  python run_v2.py --mode osc --snapshot <path> --osc-bins <path>
  python run_v2.py --mode chunk --snapshot <path> --osc-bins <path> \
      --shadow-schema pit_dmv_cp011_v2_<TS> --start 2013-04-28 --end 2026-08-08 \
      --chunks 12 --chunk-i 0 --methodology-version cp011_pit_v2_current_main_full_regen
  (repeat --chunk-i 1..11; omit --chunk-i to run all pending chunks)
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd

from pit.var_cvar import calculate_var_cvar_pit
from pit.metrics import calculate_metrics_pit
from pit.scores import compute_scores
from pit.targets import assert_shadow_schema
from pit.regenerate import regenerate_momentum, regenerate_oscillators, \
    regenerate_tvv, regenerate_ratios
from pit.policy import METHODOLOGY_VERSION, core_bin_columns

DEFAULT_DB = "cp_backtest"
T0 = time.time()

_OUT_COLS = [
    "slug", "date", "methodology_version", "universe_method",
    "d_pct_var", "d_pct_cvar", "v_met_ath", "v_met_atl",
    "d_met_ath_days", "d_met_atl_days", "d_met_coin_age_d",
    "durability_score", "momentum_score", "valuation_score", "incomplete",
]
_CORE = core_bin_columns()


def _dsn() -> str:
    dsn = os.getenv("CP_BACKTEST_DSN")
    if dsn:
        return dsn
    from dotenv import load_dotenv

    load_dotenv(r"C:\cpio_db\cryptoprism-onchain\.env")
    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        raise SystemExit("No CP_BACKTEST_DSN / DATABASE_URL available.")
    return dsn


async def _conn(database: str = DEFAULT_DB):
    # pit.db.connect applies the DNS-resolution fallback workaround for this link
    from pit.db import connect as db_connect

    return await db_connect(database, timeout=30)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ── freeze ─────────────────────────────────────────────────────────────────
async def _fetch_all_ohlcv(batch: int = 100) -> pd.DataFrame:
    """Fetch the full OHLCV universe in slug batches.

    Uses a single persistent connection (with connect + command timeouts) and
    ``WHERE slug = ANY($1)`` batches of ``batch`` slugs, so it is fast on a
    reliable VPC link and cannot hang indefinitely. On any connection/query
    error the batch is retried (fresh connection, capped attempts).
    """
    conn = await _conn(DEFAULT_DB)
    try:
        slug_rows = await conn.fetch(
            'SELECT DISTINCT slug FROM "1K_coins_ohlcv" ORDER BY slug',
            timeout=600,
        )
    finally:
        await conn.close()
    slugs = [r["slug"] for r in slug_rows]
    frames: list[pd.DataFrame] = []
    for i in range(0, len(slugs), batch):
        chunk = slugs[i:i + batch]
        for attempt in range(5):
            try:
                conn = await _conn(DEFAULT_DB)
                rows = await conn.fetch(
                    """SELECT slug, timestamp, open, high, low, close, volume
                       FROM "1K_coins_ohlcv" WHERE slug = ANY($1::text[])
                       ORDER BY slug, timestamp""", chunk, timeout=600
                )
                await conn.close()
                break
            except Exception as e:  # noqa: BLE001
                print(f"  batch {i // batch} failed ({str(e)[:60]}) — attempt {attempt + 1}", flush=True)
                try:
                    await conn.close()
                except Exception:  # noqa: BLE001
                    pass
                await asyncio.sleep(2)
        else:
            raise RuntimeError(f"failed to fetch slug batch starting at {i}")
        frames.append(pd.DataFrame([dict(r) for r in rows]))
        print(f"  fetched {min(i + batch, len(slugs))}/{len(slugs)} slugs", flush=True)
    return pd.concat(frames, ignore_index=True)


async def _freeze(snapshot: Path) -> dict:
    df = await _fetch_all_ohlcv()
    df["timestamp"] = pd.to_datetime(df["timestamp"]).dt.tz_localize(None)
    df["m_pct_1d"] = df.groupby("slug")["close"].pct_change()
    df.to_parquet(snapshot, index=False)
    manifest = {
        "snapshot": str(snapshot),
        "captured_utc": datetime.now(timezone.utc).isoformat(),
        "rows": int(len(df)),
        "assets": int(df["slug"].nunique()),
        "distinct_slug_date": int(df.groupby(["slug", df["timestamp"].dt.date]).ngroups),
        "first_date": str(df["timestamp"].min().date()),
        "last_date": str(df["timestamp"].max().date()),
        "columns": list(df.columns),
        "sha256": _sha256(snapshot),
    }
    del df
    return manifest


# ── osc pass ───────────────────────────────────────────────────────────────
def _compute_osc_bins(ohlcv: pd.DataFrame) -> pd.DataFrame:
    osc = regenerate_oscillators(ohlcv)
    return osc[["slug", "timestamp"] + [c for c in _CORE if c.startswith("m_osc_")]]


# ── chunk compute (byte-identical to monolithic) ───────────────────────────
def _emit_chunk(ohlcv: pd.DataFrame, osc_bins: pd.DataFrame,
                method_ver: str, universe_method: str,
                include_btc: bool = True) -> pd.DataFrame:
    var_m = calculate_var_cvar_pit(ohlcv, window_days=365, min_obs=252, confidence=0.95)
    met = calculate_metrics_pit(ohlcv)

    sig = regenerate_momentum(ohlcv)
    sig = sig.merge(regenerate_tvv(ohlcv), on=["slug", "timestamp"], how="outer")
    sig = sig.merge(regenerate_ratios(ohlcv, ohlcv[ohlcv["slug"] == "bitcoin"]),
                    on=["slug", "timestamp"], how="outer")
    sig = sig.merge(osc_bins, on=["slug", "timestamp"], how="left")

    base = ohlcv[["slug", "timestamp", "m_pct_1d"]].merge(
        met[["slug", "timestamp", "v_met_ath", "v_met_atl",
             "d_met_ath_days", "d_met_atl_days", "d_met_coin_age_d"]],
        on=["slug", "timestamp"], how="left",
    ).merge(
        var_m[["slug", "timestamp", "d_pct_var", "d_pct_cvar"]],
        on=["slug", "timestamp"], how="left",
    ).merge(sig, on=["slug", "timestamp"], how="left")
    for c in _CORE:
        if c not in base.columns:
            base[c] = float("nan")

    scored = compute_scores(
        base,
        durability_cols=[c for c in _CORE if c.startswith("d_")],
        momentum_cols=[c for c in _CORE if c.startswith("m_")],
        valuation_cols=[c for c in _CORE if c.startswith("v_")],
        validate=False,
    )
    out = scored.rename(columns={
        "Durability_Score": "durability_score",
        "Momentum_Score": "momentum_score",
        "Valuation_Score": "valuation_score",
    })
    out.loc[out["incomplete"],
            ["durability_score", "momentum_score", "valuation_score"]] = None
    out["date"] = pd.to_datetime(out["timestamp"]).dt.normalize().dt.date
    out["methodology_version"] = method_ver
    out["universe_method"] = universe_method
    if not include_btc:
        out = out[out["slug"] != "bitcoin"]
    out = out.drop_duplicates(subset=["slug", "date"], keep="first").reset_index(drop=True)
    return out


# ── shadow write (idempotent, resumable) ───────────────────────────────────
async def _ensure_schema(conn, schema: str) -> None:
    await conn.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
    await conn.execute(f"""
        CREATE TABLE IF NOT EXISTS "{schema}".dmv_rows (
            slug text NOT NULL,
            date date NOT NULL,
            methodology_version text NOT NULL,
            universe_method text NOT NULL,
            d_pct_var double precision,
            d_pct_cvar double precision,
            v_met_ath double precision,
            v_met_atl double precision,
            d_met_ath_days integer,
            d_met_atl_days integer,
            d_met_coin_age_d integer,
            durability_score double precision,
            momentum_score double precision,
            valuation_score double precision,
            incomplete boolean NOT NULL,
            PRIMARY KEY (slug, date, methodology_version)
        )""")
    await conn.execute(f"""
        CREATE TABLE IF NOT EXISTS "{schema}".shadow_run_meta (
            run_id text PRIMARY KEY, shadow_schema text, methodology_version text,
            universe_method text, source_table text, source_coverage text,
            start_date date, end_date date, total_slugs int,
            source_rows int, universe_rows int, expected_slug_date int,
            chunks int, created_at timestamptz, status text,
            snapshot_sha256 text, snapshot_manifest text
        )""")
    await conn.execute(f"""
        CREATE TABLE IF NOT EXISTS "{schema}".chunk_progress (
            chunk_i int PRIMARY KEY, status text NOT NULL,
            slugs int NOT NULL, written_rows int,
            runtime_sec double precision, finished_at timestamptz
        )""")


async def _write_chunk(conn, schema: str, out: pd.DataFrame, chunk_i: int, sec: float) -> int:
    if out.empty:
        await conn.execute(
            f"""INSERT INTO "{schema}".chunk_progress
                (chunk_i, status, slugs, written_rows, runtime_sec, finished_at)
                VALUES ($1,'done',0,0,$2,$3) ON CONFLICT (chunk_i) DO UPDATE SET
                status='done', slugs=0, written_rows=0, runtime_sec=$2, finished_at=$3""",
            chunk_i, round(sec, 2), datetime.now(timezone.utc),
        )
        return 0
    recs = out[_OUT_COLS].to_dict("records")

    def _nullify(v):
        if v is None:
            return None
        try:
            if pd.isna(v):
                return None
        except (TypeError, ValueError):
            pass
        return v

    args = [tuple(_nullify(r[c]) for c in _OUT_COLS) for r in recs]
    insert_sql = (
        f"""INSERT INTO "{schema}".dmv_rows
            (slug, date, methodology_version, universe_method, d_pct_var, d_pct_cvar,
             v_met_ath, v_met_atl, d_met_ath_days, d_met_atl_days, d_met_coin_age_d,
             durability_score, momentum_score, valuation_score, incomplete)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15)
            ON CONFLICT (slug, date, methodology_version) DO UPDATE SET
              universe_method = EXCLUDED.universe_method,
              d_pct_var = EXCLUDED.d_pct_var, d_pct_cvar = EXCLUDED.d_pct_cvar,
              v_met_ath = EXCLUDED.v_met_ath, v_met_atl = EXCLUDED.v_met_atl,
              d_met_ath_days = EXCLUDED.d_met_ath_days,
              d_met_atl_days = EXCLUDED.d_met_atl_days,
              d_met_coin_age_d = EXCLUDED.d_met_coin_age_d,
              durability_score = EXCLUDED.durability_score,
              momentum_score = EXCLUDED.momentum_score,
              valuation_score = EXCLUDED.valuation_score,
              incomplete = EXCLUDED.incomplete"""
    )
    async with conn.transaction():
        await conn.executemany(insert_sql, args)
        await conn.execute(
            f"""INSERT INTO "{schema}".chunk_progress
                (chunk_i, status, slugs, written_rows, runtime_sec, finished_at)
                VALUES ($1,'done',$2,$3,$4,$5) ON CONFLICT (chunk_i) DO UPDATE SET
                status='done', slugs=$2, written_rows=$3, runtime_sec=$4, finished_at=$5""",
            chunk_i, len(out["slug"].unique()), len(recs), round(sec, 2),
            datetime.now(timezone.utc),
        )
    return len(recs)


async def _done_chunks(conn, schema: str) -> set[int]:
    try:
        rows = await conn.fetch(
            f"""SELECT chunk_i FROM "{schema}".chunk_progress WHERE status='done'"""
        )
        return {int(r["chunk_i"]) for r in rows}
    except Exception:  # noqa: BLE001
        return set()


def _partition(non_btc: list[str], chunks: int) -> list[list[str]]:
    size = -(-len(non_btc) // chunks)
    return [non_btc[i:i + size] for i in range(0, len(non_btc), size)]


# ── main ───────────────────────────────────────────────────────────────────
def main() -> int:
    ap = argparse.ArgumentParser(description="CP-011 Phase D Stage 2B shadow rebuild")
    ap.add_argument("--mode", required=True,
                    choices=["freeze", "osc", "chunk", "preflight", "ledger"])
    ap.add_argument("--snapshot", default=None)
    ap.add_argument("--osc-bins", default=None)
    ap.add_argument("--shadow-schema", default=None)
    ap.add_argument("--start", default=None)
    ap.add_argument("--end", default=None)
    ap.add_argument("--chunks", type=int, default=12)
    ap.add_argument("--chunk-i", type=int, default=None)
    ap.add_argument("--methodology-version", default=METHODOLOGY_VERSION)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if args.mode in ("freeze", "osc", "preflight", "chunk") and not args.snapshot:
        raise SystemExit(f"--snapshot is required for --mode {args.mode}")

    snapshot = Path(args.snapshot) if args.snapshot else None
    if args.mode == "freeze":
        manifest = asyncio.run(_freeze(snapshot))
        man_path = snapshot.with_suffix(".manifest.json")
        man_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        print(json.dumps(manifest, indent=2))
        print(f"saved manifest -> {man_path}")
        return 0

    if args.mode == "osc":
        if not snapshot.exists():
            raise SystemExit(f"snapshot not found: {snapshot}")
        df = pd.read_parquet(snapshot)
        print(f"osc pass: {len(df)} rows / {df['slug'].nunique()} slugs", flush=True)
        t0 = time.time()
        osc = _compute_osc_bins(df)
        del df
        out_path = Path(args.osc_bins)
        osc.to_parquet(out_path, index=False)
        print(f"osc bins: {len(osc)} rows -> {out_path} in {time.time()-t0:.0f}s", flush=True)
        return 0

    if args.mode in ("preflight", "chunk"):
        assert args.shadow_schema, "--shadow-schema required"
        schema = assert_shadow_schema(args.shadow_schema)
        assert args.start and args.end, "--start/--end required"
        if not snapshot.exists():
            raise SystemExit(f"snapshot not found: {snapshot}")
        osc_path = Path(args.osc_bins) if args.osc_bins else snapshot.with_name("osc_bins.parquet")
        if not osc_path.exists():
            raise SystemExit(f"osc bins not found: {osc_path} — run --mode osc first")

        df = pd.read_parquet(snapshot, columns=["slug"])
        all_slugs = sorted(df["slug"].unique())
        non_btc = [s for s in all_slugs if s != "bitcoin"]
        chunk_sets = _partition(non_btc, args.chunks)
        n_source = len(df)
        del df

        if args.mode == "preflight":
            print(json.dumps({
                "mode": "preflight", "shadow_schema": schema,
                "database_target": DEFAULT_DB,
                "intended_reads": [str(snapshot), str(osc_path)],
                "intended_writes": [f"{schema}.dmv_rows", f"{schema}.shadow_run_meta",
                                    f"{schema}.chunk_progress"],
                "start": args.start, "end": args.end,
                "total_slugs": len(all_slugs), "source_rows": int(n_source),
                "chunks": len(chunk_sets),
                "methodology_version": args.methodology_version,
                "idempotency_key": "(slug, date, methodology_version)",
                "shadow_only": True,
                "snapshot_sha256": _sha256(snapshot),
            }, indent=2))
            print("PREFLIGHT COMPLETE — no writes performed.")
            return 0

        # chunk mode — run the whole phase inside ONE event loop (the asyncpg
        # connection is loop-bound; multiple asyncio.run() calls create new
        # loops and fail with "Future attached to a different loop")
        run_id = f"cp011_v2_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
        manifest_path = snapshot.with_suffix(".manifest.json")
        manifest_json = json.dumps(
            json.loads(manifest_path.read_text(encoding="utf-8"))
            if manifest_path.exists() else {}, default=str)
        total_written = asyncio.run(_run_chunks(
            args, schema, chunk_sets, all_slugs, int(n_source), snapshot, osc_path,
            run_id, manifest_json,
        ))
        print(f"RUN {run_id} done -> schema {schema}")
        print(f"  written_this_invocation={total_written}")
        print(f"  total_runtime={round(time.time()-T0,1)}s")
        return 0

    if args.mode == "ledger":
        assert args.shadow_schema, "--shadow-schema required"
        schema = assert_shadow_schema(args.shadow_schema)
        asyncio.run(_ledger(schema))
        return 0

    return 1


async def _run_chunks(args, schema, chunk_sets, all_slugs, n_source,
                      snapshot, osc_path, run_id, manifest_json) -> int:
    conn = await _conn(DEFAULT_DB)
    try:
        await _ensure_schema(conn, schema)
        await _meta_insert(conn, schema, run_id, args, len(all_slugs),
                           n_source, len(chunk_sets), manifest_json)
        done = await _done_chunks(conn, schema)
        targets = [args.chunk_i] if args.chunk_i is not None else list(range(len(chunk_sets)))
        total_written = 0
        osc_full = pd.read_parquet(osc_path)
        for i in targets:
            if i >= len(chunk_sets):
                continue
            if i in done:
                print(f"[chunk {i}/{len(chunk_sets)}] already done — skipping (resume)", flush=True)
                continue
            comp_slugs = chunk_sets[i] + ["bitcoin"]
            t0 = time.time()
            ohlcv = pd.read_parquet(snapshot, filters=[("slug", "in", comp_slugs)])
            osc_chunk = osc_full[osc_full["slug"].isin(comp_slugs)]
            print(f"[chunk {i}/{len(chunk_sets)}] {len(chunk_sets[i])} slugs, "
                  f"{len(ohlcv)} rows ({time.time()-t0:.0f}s load)", flush=True)
            t1 = time.time()
            out = _emit_chunk(ohlcv, osc_chunk, args.methodology_version, "PIT_APPROX",
                              include_btc=(i == 0))
            del ohlcv, osc_chunk
            print(f"[chunk {i}] compute {len(out)} output rows in {time.time()-t1:.0f}s", flush=True)
            t2 = time.time()
            n_w = await _write_chunk(conn, schema, out, i, time.time() - t2)
            del out
            total_written += n_w
            print(f"[chunk {i}] written={n_w} chunk_runtime={time.time()-t0:.0f}s", flush=True)
        await _meta_finish(conn, schema, run_id, total_written)
        return total_written
    finally:
        await conn.close()


async def _ledger(schema: str) -> None:
    conn = await _conn(DEFAULT_DB)
    try:
        n_rows = await conn.fetchval(f'SELECT COUNT(*) FROM "{schema}".dmv_rows')
        n_slug = await conn.fetchval(
            f'SELECT COUNT(DISTINCT slug) FROM "{schema}".dmv_rows')
        print(json.dumps({"shadow_schema": schema, "dmv_rows": int(n_rows),
                          "dmv_slugs": int(n_slug)}, indent=2))
    finally:
        await conn.close()


async def _meta_insert(conn, schema, run_id, args, total_slugs, source_rows,
                       n_chunks, manifest_json):
    await conn.execute(
        f"""INSERT INTO "{schema}".shadow_run_meta
            (run_id, shadow_schema, methodology_version, universe_method, source_table,
             source_coverage, start_date, end_date, total_slugs, source_rows,
             universe_rows, expected_slug_date, chunks, created_at, status,
             snapshot_sha256, snapshot_manifest)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17)""",
        run_id, schema, args.methodology_version, "PIT_APPROX",
        "frozen 1K_coins_ohlcv snapshot", f"{args.start} .. {args.end}",
        date.fromisoformat(args.start), date.fromisoformat(args.end), total_slugs,
        source_rows, total_slugs, source_rows, n_chunks,
        datetime.now(timezone.utc), "running",
        _sha256(Path(args.snapshot)), manifest_json,
    )


async def _meta_finish(conn, schema, run_id, written):
    n_rows = await conn.fetchval(f'SELECT COUNT(*) FROM "{schema}".dmv_rows')
    await conn.execute(
        f"""UPDATE "{schema}".shadow_run_meta SET status='complete',
            universe_rows=(SELECT COUNT(DISTINCT slug) FROM "{schema}".dmv_rows),
            expected_slug_date=(SELECT COUNT(*) FROM "{schema}".dmv_rows)
            WHERE run_id=$1""", run_id)
    print(f"  ledger: total dmv_rows={n_rows} written_this_call={written}", flush=True)


if __name__ == "__main__":
    sys.exit(main())
