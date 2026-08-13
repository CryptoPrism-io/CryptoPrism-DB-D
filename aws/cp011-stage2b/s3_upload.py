#!/usr/bin/env python
"""Upload a single file to the CP-011 artifacts bucket (boto3; task-role creds).

Usage: python s3_upload.py <local_path>
Uploads to s3://$CP011_S3_BUCKET/$CP011_S3_PREFIX/<basename>.
"""
from __future__ import annotations

import os
import sys

import boto3


def main() -> int:
    path = sys.argv[1]
    bucket = os.environ["CP011_S3_BUCKET"]
    prefix = os.environ.get("CP011_S3_PREFIX", "cp011/stage2b").strip("/")
    key = f"{prefix}/{os.path.basename(path)}"
    boto3.client("s3").upload_file(path, bucket, key)
    print(f"s3://{bucket}/{key}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
