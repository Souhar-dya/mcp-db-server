#!/usr/bin/env python3
"""Basic database smoke test using DATABASE_URL from environment/.env."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from db import DatabaseManager


def redact_url(url: str) -> str:
    if not url:
        return "<not-set>"
    # Keep scheme and host visible, hide credentials.
    if "@" in url and "://" in url:
        scheme, rest = url.split("://", 1)
        if "@" in rest:
            _, host_and_path = rest.split("@", 1)
            return f"{scheme}://***:***@{host_and_path}"
    return url


async def main() -> int:
    db = DatabaseManager()

    print("Using DATABASE_URL:", redact_url(os.getenv("DATABASE_URL", "")))
    print("Detected DB type:", db.database_type)

    connected = await db.test_connection()
    print("Connection test:", "PASS" if connected else "FAIL")
    if not connected:
        if db.engine:
            await db.engine.dispose()
        return 1

    tables = await db.list_tables()
    print("List tables:", f"PASS ({len(tables)} tables)")

    if db.database_type == "mongodb":
        # For MongoDB path, validate safe-query execution with supported SQL-like patterns.
        if tables:
            collection = tables[0]["table_name"]
            count_result = await db.execute_safe_query(f"SELECT COUNT(*) as count FROM {collection}")
            ok_count = bool(count_result) and "count" in count_result[0]
            print("COUNT query:", "PASS" if ok_count else f"FAIL (result={count_result})")
        else:
            print("COUNT query:", "SKIP (no collections found)")
    else:
        result = await db.execute_safe_query("SELECT 1 AS test_value")
        ok_select = bool(result) and result[0].get("test_value") == 1
        print("SELECT 1:", "PASS" if ok_select else f"FAIL (result={result})")

    if tables:
        first_table = tables[0]["table_name"]
        schema = await db.describe_table(first_table)
        print("Describe table:", f"PASS ({first_table}, {len(schema)} columns)")
    else:
        print("Describe table:", "SKIP (no tables found)")

    if db.engine:
        await db.engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
