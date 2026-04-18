#!/usr/bin/env python3
"""MongoDB CRUD smoke test using DATABASE_URL from environment/.env.

This validates SQL-like write operations mapped to MongoDB:
- CREATE TABLE
- INSERT INTO
- UPDATE ... WHERE
- DELETE ... WHERE
- DROP TABLE
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from db import DatabaseManager


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


async def main() -> int:
    db = DatabaseManager()
    require(db.database_type == "mongodb", f"Expected mongodb DATABASE_URL, got: {db.database_type}")

    ok = await db.test_connection()
    require(ok, "MongoDB connection failed")

    collection = f"mcp_smoke_{int(time.time())}"
    print(f"Using collection: {collection}")

    # CREATE
    create_result = await db.execute_unsafe_query(
        f"CREATE TABLE {collection} (name TEXT, age INTEGER, city TEXT)"
    )
    print("CREATE:", create_result)

    # INSERT
    await db.execute_unsafe_query(
        f"INSERT INTO {collection} (name, age, city) VALUES ('Alice', 30, 'Kolkata')"
    )
    await db.execute_unsafe_query(
        f"INSERT INTO {collection} (name, age, city) VALUES ('Bob', 28, 'Mumbai')"
    )
    count_after_insert = await db.execute_safe_query(f"SELECT COUNT(*) as count FROM {collection}")
    print("COUNT after insert:", count_after_insert)
    require(count_after_insert and count_after_insert[0].get("count") == 2, "Expected 2 documents after insert")

    # UPDATE
    update_result = await db.execute_unsafe_query(
        f"UPDATE {collection} SET city='Bengaluru' WHERE name='Bob'"
    )
    print("UPDATE:", update_result)

    # DELETE
    delete_result = await db.execute_unsafe_query(
        f"DELETE FROM {collection} WHERE name='Alice'"
    )
    print("DELETE:", delete_result)
    count_after_delete = await db.execute_safe_query(f"SELECT COUNT(*) as count FROM {collection}")
    print("COUNT after delete:", count_after_delete)
    require(count_after_delete and count_after_delete[0].get("count") == 1, "Expected 1 document after delete")

    # DROP
    drop_result = await db.execute_unsafe_query(f"DROP TABLE {collection}")
    print("DROP:", drop_result)
    tables = await db.list_tables()
    table_names = {t["table_name"] for t in tables}
    require(collection not in table_names, "Collection still exists after drop")

    if db.mongo_client:
        db.mongo_client.close()

    print("PASS: MongoDB CRUD smoke test completed")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
