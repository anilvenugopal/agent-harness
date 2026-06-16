#!/usr/bin/env python
"""Upload sample documents to MinIO for the classify demo.

Run once after `docker compose up -d`:

    python scripts/seed_demo.py

Environment variables (all have MinIO-default fallbacks):
    S3_ENDPOINT_URL          default: http://localhost:9000
    AWS_ACCESS_KEY_ID        default: minioadmin
    AWS_SECRET_ACCESS_KEY    default: minioadmin
"""
from __future__ import annotations

import os
import pathlib
import sys

# Load .env so the script works without `source .env`.
try:
    from dotenv import load_dotenv
    load_dotenv(dotenv_path=pathlib.Path(__file__).parent.parent / ".env", override=False)
except ImportError:
    pass

SAMPLES_DIR = pathlib.Path(__file__).parent.parent / "samples"

# Maps (bucket, key) → local file path
DOCUMENTS: list[tuple[str, str, pathlib.Path]] = [
    ("documents", "complaint.txt", SAMPLES_DIR / "complaint.txt"),
]


def main() -> None:
    try:
        import boto3
    except ImportError:
        sys.exit("boto3 not installed — run: pip install boto3")

    endpoint = os.environ.get("S3_ENDPOINT_URL", "http://localhost:9000")
    access   = os.environ.get("AWS_ACCESS_KEY_ID",
                os.environ.get("MINIO_ROOT_USER", "minioadmin"))
    secret   = os.environ.get("AWS_SECRET_ACCESS_KEY",
                os.environ.get("MINIO_ROOT_PASSWORD", "minioadmin"))

    s3 = boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=access,
        aws_secret_access_key=secret,
        region_name="us-east-1",
    )

    print(f"connecting to {endpoint}")

    buckets_seen: set[str] = set()
    for bucket, key, path in DOCUMENTS:
        if bucket not in buckets_seen:
            try:
                s3.head_bucket(Bucket=bucket)
            except Exception:
                s3.create_bucket(Bucket=bucket)
                print(f"  created bucket: {bucket}")
            buckets_seen.add(bucket)

        if not path.exists():
            print(f"  missing local file: {path} — skipping", file=sys.stderr)
            continue

        import mimetypes
        mime, _ = mimetypes.guess_type(str(path))
        s3.put_object(
            Bucket=bucket,
            Key=key,
            Body=path.read_bytes(),
            ContentType=mime or "application/octet-stream",
        )
        print(f"  uploaded: {bucket}/{key}  ({path.stat().st_size} bytes)")

    print("\nSeed complete.")
    print("Run the classify demo:")
    print("  python -m harness.cli demo classify")


if __name__ == "__main__":
    main()