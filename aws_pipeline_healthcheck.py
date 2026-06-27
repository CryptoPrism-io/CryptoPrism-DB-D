#!/usr/bin/env python3
"""
AWS pipeline health check for CryptoPrism-DB-D.

Purpose: detect, from the SAME context the pipeline runs in (a GitHub Actions
runner), whether an AWS-side change has broken the data pipeline -- e.g. an RDS
security-group lockdown, a rotated DB password, or a changed RDS endpoint.

Why this exists: the old env_test exited 0 even when the DB was unreachable, so a
broken pipeline still showed a green check. This script exits NON-ZERO on any real
failure, so CI goes red and we catch breakage here instead of in production.

Run it as the FIRST job in the daily chain (or on demand after any AWS change).

Reads the same env vars the workflows already provide:
    DB_HOST, DB_PORT, DB_USER, DB_PASSWORD, DB_NAME
Optional:
    DB_SSLMODE                 (default: require)
    HEALTHCHECK_MAX_AGE_DAYS   (default: 2)  max allowed staleness for daily tables
    HEALTHCHECK_SKIP_FRESHNESS (set to "1" to only test connectivity/auth)

Exit codes: 0 = healthy, 1 = a check failed.
"""
import os
import sys
import socket
from datetime import datetime, date, timezone


def fail(msg: str) -> "None":
    # GitHub Actions error annotation + plain line, then hard fail.
    print(f"::error::[healthcheck] {msg}")
    print(f"\nHEALTHCHECK FAILED: {msg}")
    sys.exit(1)


def ok(msg: str) -> None:
    print(f"[healthcheck] OK   - {msg}")


def to_utc(v):
    """Normalise a date / datetime / date-string to an aware UTC datetime, else None.

    Some columns (e.g. crypto_listings_latest_1000.last_updated) are stored as text,
    so handle strings by parsing the leading 'YYYY-MM-DD[ HH:MM:SS]' portion.
    """
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    if isinstance(v, date):
        return datetime(v.year, v.month, v.day, tzinfo=timezone.utc)
    if isinstance(v, str):
        s = v.strip().replace("T", " ")
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                return datetime.strptime(s[: len(fmt) + 2], fmt).replace(tzinfo=timezone.utc)
            except ValueError:
                continue
        try:
            return datetime.strptime(s[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


def step(msg: str) -> None:
    print(f"[healthcheck] .... - {msg}")


def main() -> None:
    # ---- 1. Config present --------------------------------------------------
    required = ["DB_HOST", "DB_PORT", "DB_USER", "DB_PASSWORD", "DB_NAME"]
    missing = [k for k in required if not os.getenv(k)]
    if missing:
        fail(f"missing required env vars: {', '.join(missing)}")

    host = os.environ["DB_HOST"]
    port = int(os.environ["DB_PORT"])
    user = os.environ["DB_USER"]
    dbname = os.environ["DB_NAME"]
    sslmode = os.getenv("DB_SSLMODE", "require")
    max_age = int(os.getenv("HEALTHCHECK_MAX_AGE_DAYS", "2"))

    print(f"[healthcheck] target {user}@{host}:{port}/{dbname} sslmode={sslmode}")

    # ---- 2. TCP reachability (catches SG / network changes, with a clear hint)
    step(f"TCP connect to {host}:{port}")
    try:
        with socket.create_connection((host, port), timeout=15):
            ok("port reachable")
    except OSError as e:
        fail(
            f"cannot reach {host}:{port} ({e}). "
            "This is almost always an RDS security-group / network change "
            "blocking this runner's IP. If on a GitHub-hosted runner, the runner's "
            "egress IP must be allowed into the RDS SG (or run on the self-hosted runner)."
        )

    # ---- 3. Auth + live query (catches rotated password / perms) -------------
    try:
        import psycopg2
    except ImportError:
        fail("psycopg2 not installed (pip install psycopg2-binary)")

    try:
        conn = psycopg2.connect(
            host=host, port=port, user=user, password=os.environ["DB_PASSWORD"],
            dbname=dbname, sslmode=sslmode, connect_timeout=20,
        )
    except Exception as e:  # noqa: BLE001 - report any driver error verbatim
        fail(f"DB connect/auth failed: {e}")

    cur = conn.cursor()
    cur.execute("SELECT 1")
    cur.fetchone()
    ok("DB connect + auth + query")

    if os.getenv("HEALTHCHECK_SKIP_FRESHNESS") == "1":
        print("\n[healthcheck] connectivity/auth healthy (freshness skipped).")
        conn.close()
        sys.exit(0)

    # ---- 4. Data freshness (catches silent write failures) ------------------
    # Only DB-D-owned tables that MUST update daily. (onchain/news are out of scope.)
    checks = [
        ("crypto_listings_latest_1000", "last_updated"),
        ("1K_coins_ohlcv", "timestamp"),
        ("108_1K_coins_ohlcv", "timestamp"),
    ]
    now = datetime.now(timezone.utc)
    problems = []
    for table, col in checks:
        try:
            cur.execute(f'SELECT max("{col}") FROM "{table}"')
            mx = cur.fetchone()[0]
        except Exception as e:  # noqa: BLE001
            conn.rollback()
            problems.append(f"{table}: could not query ({e})")
            continue
        if mx is None:
            problems.append(f"{table}: table is empty")
            continue
        # Normalise to an aware datetime for age math (handles text date columns).
        mxt = to_utc(mx)
        if mxt is None:
            problems.append(f"{table}: could not parse max({col})={mx!r} ({type(mx).__name__})")
            continue
        age_days = (now - mxt).total_seconds() / 86400
        if age_days > max_age:
            problems.append(f"{table}: stale - newest {col}={mx} ({age_days:.1f} days old, limit {max_age})")
        else:
            ok(f"{table} fresh (newest {col}={mx}, {age_days:.1f} days old)")

    conn.close()

    if problems:
        fail("data freshness problems -> pipeline likely not writing:\n  - " + "\n  - ".join(problems))

    print("\n[healthcheck] ALL CHECKS PASSED - AWS path and pipeline are healthy.")
    sys.exit(0)


if __name__ == "__main__":
    main()
