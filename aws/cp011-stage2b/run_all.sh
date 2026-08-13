#!/usr/bin/env bash
# CP-011 Stage 2B ??? AWS worker driver (validated commit 5a34c95 / runner 2b9db61,
# reference regenerate_ratios, hybrid runner run_v2.py).
#
# Runs inside AWS with private VPC connectivity to the RDS dbcp-aws endpoint:
#   probe -> freeze -> pick/reuse schema -> osc pass -> preflight ->
#   12 resumable chunks -> ledger -> Stage 3 validation.
# Durable artifacts are synced to S3. Shadow-only writes to a SINGLE
# pit_dmv_cp011_v2_* schema, reused ONLY when methodology_version + snapshot
# sha256 + checkpoint metadata match (pick_schema.py); never duplicated.
#
# Env (REQUIRED):
#   CP_BACKTEST_DSN   postgresql DSN to database cp_backtest (assembled from Secrets Manager)
#                     (assembled on the worker from Secrets Manager/SSM ??? never
#                      transferred from a laptop .env)
#   CP011_S3_BUCKET   durable-artifact bucket (no 's3://' prefix)
# Optional:
#   SHADOW_SCHEMA     pin the schema name (validated; refuses to touch an
#                     unrelated run)
#   CP011_S3_PREFIX   default cp011/stage2b
#   CP011_CHUNKS      default 12
#   METHOD_VERSION    default cp011_pit_v2_current_main_full_regen
#   START / END       default 2013-04-28 / end read from the frozen manifest
#   DB_SSL            default true
#
# Safe to re-run: chunk mode skips completed chunks (chunk_progress table).

set -euo pipefail

WORK="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP="$WORK/app"
cd "$APP"

: "${CP011_S3_PREFIX:=cp011/stage2b}"
: "${CP011_CHUNKS:=12}"
: "${METHOD_VERSION:=cp011_pit_v2_current_main_full_regen}"
: "${START:=2013-04-28}"
: "${DB_SSL:=true}"

[ -n "${CP_BACKTEST_DSN:-}" ] || { echo "FATAL: CP_BACKTEST_DSN not set"; exit 1; }
[ -n "${CP011_S3_BUCKET:-}" ] || { echo "FATAL: CP011_S3_BUCKET not set"; exit 1; }

export CP_BACKTEST_DSN DB_SSL METHOD_VERSION
LOG="$WORK/cp011_stage2b.log"
mkdir -p "$WORK/artifacts"
SNAP="$WORK/artifacts/frozen_ohlcv_cp011_v2.parquet"
OSCB="$WORK/artifacts/osc_bins_cp011_v2.parquet"
MANIFEST="${SNAP%.parquet}.manifest.json"

log() { echo "[$(date -u +%H:%M:%S)] $*" | tee -a "$LOG"; }
s3() { python "$WORK/s3_upload.py" "$1" >>"$LOG" 2>&1 || log "s3 sync failed: $1"; }

log "=== 0/7 pre-launch probe (DNS -> TCP -> SELECT 1 -> existing schemas) ==="
python "$WORK/probe_rds.py" || { log "PROBE FAILED ??? aborting before any write"; exit 1; }

log "=== 1/7 freeze ==="
python gcp_postgres_sandbox/pit/run_v2.py --mode freeze --snapshot "$SNAP"
s3 "$SNAP"; s3 "$MANIFEST"

SNAPSHOT_SHA256="$(python -c "import json;print(json.load(open(r'$MANIFEST'))['sha256'])")"
export SNAPSHOT_SHA256

log "=== 2/7 pick/reuse schema (strict match, never duplicate) ==="
SHADOW_SCHEMA="$(python "$WORK/pick_schema.py" | tail -1)"
log "shadow schema = $SHADOW_SCHEMA"
END="${END:-$(python -c "import json;print(json.load(open(r'$MANIFEST'))['last_date'])")}"
export END

log "=== 3/7 osc pass ==="
python gcp_postgres_sandbox/pit/run_v2.py --mode osc --snapshot "$SNAP" --osc-bins "$OSCB"
s3 "$OSCB"

log "=== 4/7 preflight ==="
python gcp_postgres_sandbox/pit/run_v2.py --mode preflight \
  --snapshot "$SNAP" --osc-bins "$OSCB" \
  --shadow-schema "$SHADOW_SCHEMA" --start "$START" --end "$END" \
  --chunks "$CP011_CHUNKS" --methodology-version "$METHOD_VERSION"

log "=== 5/7 chunks 0..$((CP011_CHUNKS-1)) (resumable) ==="
for i in $(seq 0 $((CP011_CHUNKS-1))); do
  log "--- chunk $i ---"
  python gcp_postgres_sandbox/pit/run_v2.py --mode chunk \
    --snapshot "$SNAP" --osc-bins "$OSCB" \
    --shadow-schema "$SHADOW_SCHEMA" --start "$START" --end "$END" \
    --chunks "$CP011_CHUNKS" --chunk-i "$i" \
    --methodology-version "$METHOD_VERSION"
done

log "=== 6/7 ledger + artifact sync ==="
python gcp_postgres_sandbox/pit/run_v2.py --mode ledger --shadow-schema "$SHADOW_SCHEMA"
s3 "$LOG"

log "=== 7/7 Stage 3 validation ==="
python gcp_postgres_sandbox/pit/stage3_validate.py --shadow-schema "$SHADOW_SCHEMA" \
  --snapshot "$SNAP" --methodology-version "$METHOD_VERSION" \
  2>>"$LOG" || log "Stage 3 validation reported issues (see log)"
VREP="$WORK/artifacts/stage3_validation.json"
[ -f "$VREP" ] && s3 "$VREP"

log "DONE ??? schema=$SHADOW_SCHEMA sha=$SNAPSHOT_SHA256"
