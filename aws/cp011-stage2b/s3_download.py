#!/usr/bin/env python
"""Download an object from the CP-011 artifacts bucket (boto3; task-role creds).

Usage: python s3_download.py <bucket> <key> <local_path>
"""
from __future__ import annotations

import sys

import boto3


def main() -> int:
    bucket = sys.argv[1]
    key = sys.argv[2]
    path = sys.argv[3]
    boto3.client("s3").download_file(bucket, key, path)
    print(f"downloaded s3://{bucket}/{key} -> {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
