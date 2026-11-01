#!/usr/bin/env python3
"""Deliberately import a local AutoVault portable backup into Neon and private Blob."""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY))

from autovault.hosted import connection, ensure_schema
from autovault.migration import import_source_into_database, portable_backup_bytes, source_data, staged_backup


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "backup",
        type=Path,
        help="Portable .tar.gz backup or project ZIP containing portable-data/autovault-data-backup.tar.gz",
    )
    args = parser.parse_args()
    if sys.version_info < (3, 10):
        parser.error("Use Python 3.10 or later; Python 3.12 is recommended.")
    if not os.environ.get("DATABASE_URL"):
        parser.error("Set DATABASE_URL to the Neon Postgres connection string.")
    if not os.environ.get("BLOB_READ_WRITE_TOKEN"):
        parser.error("Set BLOB_READ_WRITE_TOKEN for the connected private Vercel Blob store.")
    if not args.backup.is_file():
        parser.error(f"Backup file does not exist: {args.backup}")

    try:
        from vercel.blob import sync as blob
    except ImportError:
        parser.error("Install project dependencies with: python3 -m pip install -r requirements.txt")

    content = portable_backup_bytes(args.backup)
    with staged_backup(content) as root:
        source = source_data(root)
        ensure_schema()

        with connection() as conn:
            if conn.execute("SELECT 1 FROM workflows LIMIT 1").fetchone():
                parser.error("Hosted library is not empty; refusing to upload or overwrite data.")

        def upload(pathname: str, data: bytes) -> None:
            blob.put_blob(
                pathname,
                data,
                access="private",
                content_type="application/octet-stream",
                allow_overwrite=True,
            )

        for item in source["workflows"]:
            workflow_id = item["workflow"]["id"]
            for object_hash, object_bytes in item["objects"].items():
                upload(f"workflows/{workflow_id}/objects/{object_hash}", object_bytes)

        with connection() as conn:
            count = import_source_into_database(source, conn, None)
    print(f"Imported {count} workflow(s), with notes, run history, and saved snapshots.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
