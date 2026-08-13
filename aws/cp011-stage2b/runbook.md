# CP-011 Stage 2B ??? AWS execution runbook (validated commits `2b9db61` + `5a34c95`)

Status: **COMPLETE** ??? executed on AWS Fargate 2026-08-12/13. Shadow schema
`pit_dmv_cp011_v2_20260812_1315` in `cp_backtest`: **2,593,976 dmv_rows / 3,587
slugs / 1.19% incomplete / Stage 3 PASS**. Artifacts in
`s3://cryptoprism-cp011-artifacts/cp011/stage2b/`, logs in CloudWatch
`/ecs/cryptoprism-onchain/cp011-stage2b`. Final bundle `cp011-stage2b.bundle`
tip `b578444a`. See `report/cp011-phase-d/methodology_v2_freeze.json` and
`docs/CP011-sibling-reconciliation.md`. Remaining: CP-012/CP-020; shadow???canonical
promotion only with explicit approval.

## 1. Authoritative sources (verified offline)

| Item | Value |
|---|---|
| Validated work source DB | **cp_backtest** on RDS `HOST:5432` |
| Source table | `1K_coins_ohlcv` (read-only) ??? 2,602,883 rows / 3,586 slugs / 2,592,976 distinct (slug,date) |
| Target DB | **cp_backtest** (same RDS endpoint) ??? shadow schema only |
| Target schema | single `pit_dmv_cp011_v2_<TS>` (strict reuse; never duplicated) |
| Methodology version | `cp011_pit_v2_current_main_full_regen` |
| Benchmark | bitcoin (loaded into every chunk) |
| Sibling checkout `cryptoprism-onchain-cp011` | references **dbcp** ??? NOT interchangeable with cp_backtest; **not run/imported/combined**; its `008_cp011_shadow_schema.sql` **not applied** |

## 2. Hard guards (unchanged)

- No writes to canonical `FE_*`, `FE_DMV_ALL/SCORES`, CP-009, production RDS/cache, CP-018.
- Writes only to the single selected `pit_dmv_*` shadow schema (idempotent upsert
  keyed `(slug, date, methodology_version)`, resumable via `chunk_progress`).
- **Schema reuse is strict:** `pick_schema.py` reuses an existing schema only when
  its `shadow_run_meta` matches the intended `methodology_version` AND the frozen
  snapshot `sha256` (and checkpoint metadata). A pinned schema that belongs to a
  different run is **refused**. Otherwise a fresh schema is created. Never a
  duplicate because execution restarted.

## 3. Transfer the bundle (no credentials in it)

`cp011-stage2b.bundle` (git bundle, complete history to `5a34c95`). Move it to a
networked machine via USB / phone hotspot / local transfer. Clone with:
```
git clone cp011-stage2b.bundle cp011 && cd cp011 && git checkout feat/cp011-phase-d-hybrid-rebuild
```
The bundle contains **no** `.env` / secrets / keys (verified). DB credentials are
**assembled inside AWS only** from Secrets Manager / SSM:
- `CP_BACKTEST_DSN` = postgresql DSN to database **cp_backtest** (host from RDS endpoint; password from Secrets Manager)
- Source of `<PW>`: Secrets Manager `/dbcp-aws/postgres` (component creds) or
  `/cryptoprism/api/DATABASE_URL`. Never copy a laptop `.env` to AWS.

## 4. Provision the worker (Fargate first, 60-min timebox)

1. **Fargate task** in the RDS VPC/subnet (private connectivity to `dbcp-aws`).
   Image: python:3.11-slim; install `aws/cp011-stage2b/requirements.txt`;
   entrypoint `bash aws/cp011-stage2b/run_all.sh`.
2. Add the task security group to the RDS inbound rule (TCP 5432).
3. Task role: `s3:PutObject` on the artifacts bucket; `logs:*` (CloudWatch);
   `secretsmanager:GetSecretValue` / `ssm:GetParameter` for the DSN parts.
4. Set env: `CP011_S3_BUCKET`, and either `CP_BACKTEST_DSN` (assembled from
   Secrets Manager) or wire a secret-manager fetch into the task.
5. CloudWatch log group `/aws/ecs/cryptoprism-onchain/cp011-stage2b`.
6. **Timebox:** 60 minutes to a healthy task + passing probe. If Fargate launch
   fails/unscheduled in that window, fall back to a **private EC2** instance in
   the same VPC running `run_all.sh` under `nohup`.

## 5. Mandatory pre-launch gate (inside AWS, before any write)

`probe_rds.py` verifies, from the worker: DNS resolution ??? TCP :5432 ???
`SELECT 1` ??? lists existing `pit_dmv_*` schemas with their
`run_id/methodology_version/snapshot_sha256/status/chunk_progress`. This
confirms no rebuild is already running and surfaces any existing valid run to
resume. `run_all.sh` aborts if the probe fails. (This is the only `SELECT 1` +
schema-inspection that may precede the freeze.)

## 6. Execute (freeze ??? pick/reuse schema ??? osc ??? preflight ??? 12 chunks ??? ledger ??? Stage 3)

```
export CP011_S3_BUCKET='<bucket>'
nohup bash aws/cp011-stage2b/run_all.sh >/dev/null 2>&1 &
tail -f cp011_stage2b.log
```
- Order guarantees the frozen snapshot manifest (with `sha256`) exists before
  schema selection, so reuse matches the actual snapshot checksum.
- ~8.5-9 h serial (ratios dominate ???8.4 h); memory-safe ???2-3 GB peak (task/VM ???4 GB).
- **Resume:** re-run `run_all.sh` ??? same schema reused (method+sha match),
  `run_v2.py` skips completed chunks. Never duplicates.
- Expected output ???2.5-2.6 M dmv_rows; incomplete ??? a few % (early history /
  delisted with <5-day windows); scores ??? [-100, 100].

## 7. Durable artifacts (S3, `cp011/stage2b/`)

- `frozen_ohlcv_cp011_v2.parquet` + `.manifest.json` (rows/assets/sha256/capture time)
- `osc_bins_cp011_v2.parquet` ?? `cp011_stage2b.log` ?? `stage3_validation.json`

## 8. Stage 3 validation (PASS gate before CP-012/CP-020 or reconciliation)

```
python gcp_postgres_sandbox/pit/stage3_validate.py \
  --shadow-schema pit_dmv_cp011_v2_<TS> --snapshot <snapshot.parquet>
```
Checks: rows in expected range, PK uniqueness, scores ??? [-100,100],
incomplete==score-null, var/cvar null rates, date range ??? snapshot, shadow
slugs ??? universe.

## 9. Logs

- Fargate: CloudWatch `/aws/ecs/cryptoprism-onchain/cp011-stage2b`.
- EC2 fallback: `cp011_stage2b.log` (also S3-synced).

## 10. Terminal ownership

- **Terminal 1 (this one):** blocked until connectivity; the sole live Stage 2B
  executor. No retries while the network is down.
- **Terminal 2:** documentation + cross-sectional adapter design only ??? no RDS
  or AWS execution.
