# ScriptDB

ScriptDB is a tiny wrapper around SQLite with built‑in migration
support. It can be used asynchronously or synchronously. 
ScriptDB is designed for small integration scripts and ETL jobs 
where using an external database would be unnecessary. 
The project aims to provide a pleasant developer experience
while keeping the API minimal.

## Features

* **Async and sync** – choose between the async [`aiosqlite`](https://github.com/omnilib/aiosqlite)
  backend or the synchronous stdlib `sqlite3` backend.
* **Migrations** – declare migrations as SQL snippet(s) or Python callables and
  let ScriptDB apply them once.
* **Lightweight** – no server to run and no complicated setup; perfect for
  throw‑away scripts or small tools.
* **WAL by default** – connections use SQLite's write-ahead logging mode;
  disable with `use_wal=False` if rollback journals are required.

Composite primary keys are not supported; each table must have a single-column primary key.

## Requirements

ScriptDB requires SQLite version **3.21.0** or newer. This covers SQLite
releases bundled with modern Python builds, so no additional dependency is
needed on most systems.

## Installation

Project can be installed with:

To use sync implementation

```bash
pip install scriptdb
```

To use async version (installs aiosqlite)

```bash
pip install scriptdb[async]
```

## Development

Create and activate a virtual environment in the repository root:

```bash
python -m venv venv
source venv/bin/activate
pip install -e .[async,test]
```

## Sync or Async

Both the asynchronous and synchronous interfaces expose the same API.
The only difference is whether methods are coroutines (`AsyncBaseDB` and
`AsyncCacheDB`) or regular blocking functions (`SyncBaseDB` and
`SyncCacheDB`). Import `BaseDB` and `CacheDB` from `scriptdb.asyncdb` for async
usage or from `scriptdb.syncdb` for synchronous usage, or explicitly import
`AsyncBaseDB`/`SyncBaseDB` or `AsyncCacheDB`/`SyncCacheDB` to avoid ambiguity.

## Asynchronous quick start

Create a subclass of `AsyncBaseDB` and provide a list of migrations:

```python
from scriptdb import AsyncBaseDB

class MyDB(AsyncBaseDB):
    def migrations(self):
        return [
            {
                "name": "create_links",
                "sql": """
                    CREATE TABLE links(
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
            {
                "name": "add_created_idx",
                # run multiple statements sequentially
                "sqls": [
                    "ALTER TABLE links ADD COLUMN created_at TEXT",  # new column
                    "CREATE INDEX idx_links_created_at ON links(created_at)",  # index
                ],
            },
        ]

async def main():
    async with MyDB.open("app.db") as db:  # WAL journaling is enabled by default
        await db.execute(
            "INSERT INTO links(url, status, progress, is_done) VALUES(?,?,?,?)",
            ("https://example.com/data", 0, 0, 0),
        )
        row = await db.query_one("SELECT url FROM links")
        print(row["url"])  # -> https://example.com/data

    # Manual open/close without a context manager
    db = await MyDB.open("app.db")
    try:
        await db.execute(
            "INSERT INTO links(url, status, progress, is_done) VALUES(?,?,?,?)",
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

## Synchronous quick start

Create a subclass of `SyncBaseDB` for blocking use:

```python
from scriptdb import SyncBaseDB

class MyDB(SyncBaseDB):
    def migrations(self):
        return [
            {
                "name": "create_links",
                "sql": """
                    CREATE TABLE links(
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
            {
                "name": "add_created_idx",
                "sqls": [
                    "ALTER TABLE links ADD COLUMN created_at TEXT",  # new column
                    "CREATE INDEX idx_links_created_at ON links(created_at)",  # index
                ],
            },
        ]

with MyDB.open("app.db") as db:  # WAL journaling is enabled by default
    db.execute(
        "INSERT INTO links(url, status, progress, is_done) VALUES(?,?,?,?)",
        ("https://example.com/data", 0, 0, 0),
    )
    row = db.query_one("SELECT url FROM links")
    print(row["url"])  # -> https://example.com/data

# Manual open/close without a context manager
db = MyDB.open("app.db")
try:
    db.execute(
        "INSERT INTO links(url, status, progress, is_done) VALUES(?,?,?,?)",
        ("https://example.com/other", 0, 0, 0),
    )
finally:
    db.close()
```

Always close the database connection with `close()` or use the `with`
context manager as shown above. Leaving a database open may keep background
tasks alive and prevent your application from exiting cleanly.

## Usage examples

The `AsyncBaseDB` API supports migrations and offers helpers for common operations
and background tasks:

```python
from scriptdb import AsyncBaseDB, run_every_seconds, run_every_queries

class MyDB(AsyncBaseDB):
    def migrations(self):
        return [
            {
                "name": "init",
                "sql": """
                    CREATE TABLE links(
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
            {"name": "idx_status", "sql": "CREATE INDEX idx_links_status ON links(status)"},
            {"name": "create_meta", "sql": "CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT)"},
        ]

    # Periodically remove finished links
    @run_every_seconds(60)
    async def cleanup(self):
        await self.execute("DELETE FROM links WHERE is_done = 1")

    # Write a checkpoint every 100 executed queries
    @run_every_queries(100)
    async def checkpoint(self):
        await self.execute("PRAGMA wal_checkpoint")

async def main():
    async with MyDB.open("app.db") as db:  # pass use_wal=False to disable WAL

        # Insert many links at once
        await db.execute_many(
            "INSERT INTO links(url) VALUES(?)",
            [("https://a",), ("https://b",), ("https://c",)],
        )

        # Fetch all URLs
        rows = await db.query_many("SELECT url FROM links")
        print([r["url"] for r in rows])

        # Stream links one by one
        async for row in db.query_many_gen("SELECT url FROM links"):
            print(row["url"])
```

### Helper methods

`BaseDB` includes convenience helpers for common insert, update and delete
operations:

```python
# Insert one record and get its primary key
pk = await db.insert_one("links", {"url": "https://a"})

# Insert many records
await db.insert_many("links", [{"url": "https://b"}, {"url": "https://c"}])

# Upsert a single record
await db.upsert_one("links", {"resource_id": pk, "status": 200})

# Upsert many records
await db.upsert_many(
    "links",
    [
        {"resource_id": 1, "status": 200},
        {"resource_id": 2, "status": 404},
    ],
)

# Update selected columns in a record
await db.update_one("links", pk, {"progress": 50})

# Delete records
await db.delete_one("links", pk)
await db.delete_many("links", "status = ?", (404,))
```

### Query helpers

The library also offers helpers for common read patterns:

```python
# Get a single value
count = await db.query_scalar("SELECT COUNT(*) FROM links")

# Get a list from the first column of each row
ids = await db.query_column("SELECT resource_id FROM links ORDER BY resource_id")

# Build dictionaries from rows
# Use primary key automatically
records = await db.query_dict("SELECT * FROM links")

# Explicit column names for key and value
urls = await db.query_dict(
    "SELECT resource_id, url FROM links", key="resource_id", value="url"
)

# Callables for custom key and value
status_by_url = await db.query_dict(
    "SELECT * FROM links",
    key=lambda r: r["url"],
    value=lambda r: r["status"],
)
```

## Useful implementations

### CacheDB

`AsyncCacheDB` and `SyncCacheDB` provide a simple key‑value store with optional
expiration.

```python
from scriptdb import AsyncCacheDB

async def main():
    async with AsyncCacheDB.open("cache.db") as cache:
        await cache.set("answer", b"42", expire_sec=60)
        if await cache.is_set("answer"):
            print("cached!")
        print(await cache.get("answer"))  # b"42"
```

```python
from scriptdb import SyncCacheDB

with SyncCacheDB.open("cache.db") as cache:
    cache.set("answer", b"42", expire_sec=60)
    if cache.is_set("answer"):
        print("cached!")
    print(cache.get("answer"))  # b"42"
```

A value without `expire_sec` will be kept indefinitely. Use `is_set` to check for
keys without retrieving their values. To easily cache function results, use the
`cache` decorator method from a cache instance:

```python
import asyncio
from scriptdb import AsyncCacheDB

async def main():
    async with AsyncCacheDB.open("cache.db") as cache:

        @cache.cache(expire_sec=30)
        async def slow():
            await asyncio.sleep(1)
            return 1

        await slow()
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
