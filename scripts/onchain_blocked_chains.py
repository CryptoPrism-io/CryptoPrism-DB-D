"""Refresh on-chain data for SOL/NEAR/MVX from BigQuery.

These 3 community datasets block Cloud Run service accounts.
This script runs in GitHub Actions with GCP auth via Workload Identity Federation.

Writes active_addresses metric to onchain_daily_metrics table in PostgreSQL.
"""

import os
import sys
from datetime import date

from google.cloud import bigquery


QUERIES = {
    "sol": """
        SELECT DATE(block_timestamp) AS d,
          COUNT(DISTINCT source) + COUNT(DISTINCT destination) AS v
        FROM `bigquery-public-data.crypto_solana_mainnet_us.Token Transfers`
        WHERE block_timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL {days} DAY)
        GROUP BY d ORDER BY d DESC
    """,
    "near": """
        SELECT block_date AS d, COUNT(DISTINCT signer_account_id) AS v
        FROM `bigquery-public-data.crypto_near_mainnet_us.transactions`
        WHERE block_date >= DATE_SUB(CURRENT_DATE(), INTERVAL {days} DAY)
          AND signer_account_id IS NOT NULL
        GROUP BY d ORDER BY d DESC
    """,
    "mvx": """
        SELECT DATE(timestamp) AS d, COUNT(DISTINCT address) AS v FROM (
          SELECT timestamp, sender AS address
          FROM `bigquery-public-data.crypto_multiversx_mainnet_eu.operations`
          WHERE timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL {days} DAY)
            AND sender IS NOT NULL
          UNION ALL
          SELECT timestamp, receiver AS address
          FROM `bigquery-public-data.crypto_multiversx_mainnet_eu.operations`
          WHERE timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL {days} DAY)
            AND receiver IS NOT NULL
        ) GROUP BY d ORDER BY d DESC
    """,
}


def main():
    days = int(os.environ.get("BACKFILL_DAYS", "3"))
    project = os.environ.get("GCP_PROJECT_ID", "social-data-pipeline-and-push")

    # DB connection from environment (same secrets as other CryptoPrism-DB scripts)
    db_host = os.environ.get("DB_HOST", "34.55.195.199")
    db_name = os.environ.get("DB_NAME", "dbcp")
    db_user = os.environ.get("DB_USER", "yogass09")
    db_pass = os.environ.get("DB_PASSWORD", "")
    db_port = os.environ.get("DB_PORT", "5432")

    if not db_pass:
        print("ERROR: DB_PASSWORD not set")
        sys.exit(1)

    import psycopg2

    # BigQuery client uses Application Default Credentials (set by WIF)
    client = bigquery.Client(project=project)

    conn = psycopg2.connect(
        host=db_host, dbname=db_name, user=db_user, password=db_pass, port=db_port
    )
    cur = conn.cursor()

    for chain, sql_template in QUERIES.items():
        sql = sql_template.format(days=days)
        print(f"{chain}: ", end="", flush=True)
        try:
            job = client.query(sql)
            rows = list(job.result())
            gb = (job.total_bytes_processed or 0) / (1024**3)

            written = 0
            for r in rows:
                d = r.d
                if isinstance(d, str):
                    d = date.fromisoformat(d[:10])
                cur.execute(
                    """INSERT INTO onchain_daily_metrics (chain, metric, metric_date, value, created_at)
                       VALUES (%s, %s, %s, %s, NOW())
                       ON CONFLICT (chain, metric, metric_date)
                       DO UPDATE SET value = %s, created_at = NOW()""",
                    (chain, "active_addresses", d, float(r.v), float(r.v)),
                )
                written += 1

            conn.commit()
            latest = f"{rows[0].v:,}" if rows else "0"
            print(f"{written} rows | latest: {latest} | {gb:.2f} GB | ${gb*6.25/1000:.4f}")

        except Exception as e:
            conn.rollback()
            print(f"ERROR: {e}")

    cur.close()
    conn.close()
    print("\nDone.")


if __name__ == "__main__":
    main()
