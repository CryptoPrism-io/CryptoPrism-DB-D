#!/usr/bin/env bash
# CP-012 ??? full forward-return backtests on AWS (read-only against cp_backtest +
# the frozen snapshot from S3). Writes only the report JSON to S3 + CloudWatch.
#
# Env (from task definition):
#   CP_BACKTEST_DSN   postgresql DSN to database cp_backtest (from task secrets)
#   CP011_S3_BUCKET   artifacts bucket
#   CP011_S3_PREFIX   default cp011/stage2b
#
# Safe to re-run (report overwritten idempotently; no DB writes).

set -euo pipefail

WORK="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP="$WORK/app"
cd "$APP"

: "${CP011_S3_PREFIX:=cp011/stage2b}"
export CP_BACKTEST_DSN

mkdir -p "$WORK/artifacts"
SNAP="$WORK/artifacts/frozen_ohlcv_cp011_v2.parquet"
OUT="$WORK/artifacts/cp012_backtests.json"
LOG="$WORK/cp012_backtests.log"

log() { echo "[$(date -u +%H:%M:%S)] $*" | tee -a "$LOG"; }

log "download frozen snapshot from S3"
python "$WORK/s3_download.py" "$CP011_S3_BUCKET" "$CP011_S3_PREFIX/frozen_ohlcv_cp011_v2.parquet" "$SNAP"

log "run full CP-012 backtests"
python gcp_postgres_sandbox/pit/run_cp012.py --mode full --snapshot "$SNAP" --out "$OUT"

log "upload report + log to S3"
python "$WORK/s3_upload.py" "$OUT"
python "$WORK/s3_upload.py" "$LOG"

log "DONE ??? report in s3://$CP011_S3_BUCKET/$CP011_S3_PREFIX/cp012_backtests.json"
