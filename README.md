# ScriptDB

ScriptDB is a tiny asynchronous wrapper around SQLite with built‑in migration
support. It is designed for small integration scripts and ETL jobs where using
an external database would be unnecessary. The project aims to provide a
pleasant developer experience while keeping the API minimal.

## Features

* **Async first** – built on top of [`aiosqlite`](https://github.com/omnilib/aiosqlite)
  for non‑blocking database access.
* **Migrations** – declare migrations as SQL snippets or Python callables and
  let ScriptDB apply them once.
* **Lightweight** – no server to run and no complicated setup; perfect for
  throw‑away scripts or small tools.
* **WAL by default** – connections use SQLite's write-ahead logging mode;
  disable with `use_wal=False` if rollback journals are required.

Composite primary keys are not supported; each table must have a single-column primary key.

## Installation

The project can be installed from source:

```bash
pip install .
```

Once published to PyPI it will be installable with:

```bash
pip install scriptdb
```

## Quick start

Create a subclass of `BaseDB` and provide a list of migrations:

```python
from scriptdb import BaseDB

class MyDB(BaseDB):
    def migrations(self):
        return [
            {
                "name": "create_resources",
                "sql": """
                    CREATE TABLE resources(
                        resource_id INTEGER PRIMARY KEY,
                        referrer_url TEXT,
                        url TEXT,
                        status INTEGER,
                        progress INTEGER,
                        is_done INTEGER,
                        content BLOB
                    )
                """,
            }
        ]

async def main():
    async with MyDB.open("app.db") as db:  # WAL journaling is enabled by default
        await db.execute(
            "INSERT INTO resources(url, status, progress, is_done) VALUES(?,?,?,?)",
            ("https://example.com/data", 0, 0, 0),
        )
        row = await db.query_one("SELECT url FROM resources")
        print(row["url"])  # -> https://example.com/data

    # Manual open/close without a context manager
    db = await MyDB.open("app.db")
    try:
        await db.execute(
            "INSERT INTO resources(url, status, progress, is_done) VALUES(?,?,?,?)",
            ("https://example.com/other", 0, 0, 0),
        )
    finally:
        await db.close()
```

Always close the database connection with `close()` or use the `async with`
context manager as shown above. If you call `MyDB.open()` without a context
manager, remember to `await db.close()` when finished. Leaving a database open
may keep background tasks alive and prevent your application from exiting
cleanly.

## Usage examples

The `BaseDB` API supports migrations and offers helpers for common operations
and background tasks:

```python
from scriptdb import BaseDB, run_every_seconds, run_every_queries

class MyDB(BaseDB):
    def migrations(self):
        return [
            {
                "name": "init",
                "sql": """
                    CREATE TABLE resources(
                        resource_id INTEGER PRIMARY KEY,
                        referrer_url TEXT,
                        url TEXT,
                        status INTEGER,
                        progress INTEGER,
                        is_done INTEGER,
                        content BLOB
                    )
                """,
            },
            {"name": "idx_status", "sql": "CREATE INDEX idx_resources_status ON resources(status)"},
            {"name": "create_meta", "sql": "CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT)"},
        ]

    # Periodically remove finished resources
    @run_every_seconds(60)
    async def cleanup(self):
        await self.execute("DELETE FROM resources WHERE is_done = 1")

    # Write a checkpoint every 100 executed queries
    @run_every_queries(100)
    async def checkpoint(self):
        await self.execute("PRAGMA wal_checkpoint")

async def main():
    async with MyDB.open("app.db") as db:  # pass use_wal=False to disable WAL

        # Insert many resources at once
        await db.execute_many(
            "INSERT INTO resources(url) VALUES(?)",
            [("https://a",), ("https://b",), ("https://c",)],
        )

        # Fetch all URLs
        rows = await db.query_many("SELECT url FROM resources")
        print([r["url"] for r in rows])

        # Stream resources one by one
        async for row in db.query_many_gen("SELECT url FROM resources"):
            print(row["url"])
```

### Helper methods

`BaseDB` includes convenience helpers for common insert, update and delete
operations:

```python
# Insert one resource and get its primary key
pk = await db.insert_one("resources", {"url": "https://a"})

# Insert many resources
await db.insert_many("resources", [{"url": "https://b"}, {"url": "https://c"}])

# Upsert a single resource
await db.upsert_one("resources", {"resource_id": pk, "status": 200})

# Upsert many resources
await db.upsert_many(
    "resources",
    [
        {"resource_id": 1, "status": 200},
        {"resource_id": 2, "status": 404},
    ],
)

# Update selected columns in a resource
await db.update_one("resources", pk, {"progress": 50})

# Delete resources
await db.delete_one("resources", pk)
await db.delete_many("resources", "status = ?", (404,))
```

### Query helpers

The library also offers helpers for common read patterns:

```python
# Get a single value
count = await db.query_scalar("SELECT COUNT(*) FROM resources")

# Get a list from the first column of each row
ids = await db.query_column("SELECT resource_id FROM resources ORDER BY resource_id")

# Build dictionaries from rows
# Use primary key automatically
resources = await db.query_dict("SELECT * FROM resources")

# Explicit column names for key and value
urls = await db.query_dict(
    "SELECT resource_id, url FROM resources", key="resource_id", value="url"
)

# Callables for custom key and value
status_by_url = await db.query_dict(
    "SELECT * FROM resources",
    key=lambda r: r["url"],
    value=lambda r: r["status"],
)
```

## Useful implementations

### CacheDB

`CacheDB` provides a simple key‑value store with optional expiration. Because
it inherits from `BaseDB`, it is created and used the same way as other
databases in the library.

```python
from scriptdb import CacheDB

async def main():
    async with CacheDB.open("cache.db") as cache:
        await cache.set("answer", b"42", expire_sec=60)
        if await cache.is_set("answer"):
            print("cached!")
        print(await cache.get("answer"))  # b"42"
```

A value without `expire_sec` will be kept indefinitely. Use `is_set` to check for
keys without retrieving their values. To easily cache
function results, use the decorator:

```python
import asyncio
from scriptdb.cachedb import cache

@cache(expire_sec=30)
async def slow():
    await asyncio.sleep(1)
    return 1
```

Subsequent calls within 30 seconds will return the cached result without
executing the function. You can supply `key_func` to control how the cache key
is generated.

## Running tests

```bash
pytest
```

## Contributing

Issues and pull requests are welcome. Please run the tests before submitting
changes.

## License

This project is licensed under the terms of the MIT license. See
[LICENSE](LICENSE) for details.

## AI Usage disclaimer

* The package was created with help of OpenAI Codex.
* All algorithms, functionality, and logic were devised by a human.
* The human supervised and reviewed every function and method generated by Codex.
* Some parts were manually corrected, as it is often difficult to obtain sane edits from AI.
* Although some code were made by an LLM, this is not vibe-coding, you can trust this code as if I written it myself
