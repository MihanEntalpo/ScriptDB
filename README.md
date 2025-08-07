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
            {"name": "create_table", "sql": "CREATE TABLE items(id INTEGER PRIMARY KEY, name TEXT)"}
        ]

async def main():
    db = await MyDB.open("app.db")
    await db.execute("INSERT INTO items(name) VALUES(?)", ("apple",))
    row = await db.query_one("SELECT name FROM items")
    print(row["name"])  # -> apple
    await db.close()
```

## Usage examples

The `BaseDB` API supports migrations and offers helpers for common operations
and background tasks:

```python
from scriptdb import BaseDB, run_every_seconds, run_every_queries

class MyDB(BaseDB):
    def migrations(self):
        return [
            {"name": "init", "sql": """
                CREATE TABLE t(
                    id INTEGER PRIMARY KEY,
                    x INTEGER NOT NULL,
                    created_at INTEGER NOT NULL
                )
            """},
            {"name": "add_index", "sql": "CREATE INDEX idx_t_created_at ON t(created_at)"},
            {"name": "create_meta", "sql": "CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT)"},
        ]

    # Periodically remove rows older than a minute
    @run_every_seconds(60)
    async def cleanup(self):
        await self.execute("DELETE FROM t WHERE x < 0")

    # Write a checkpoint every 100 executed queries
    @run_every_queries(100)
    async def checkpoint(self):
        await self.execute("PRAGMA wal_checkpoint")

async def main():
    db = await MyDB.open("app.db")

    # Insert many rows at once
    await db.execute_many("INSERT INTO t(x) VALUES(?)", [(1,), (2,), (3,)])

    # Fetch all rows
    rows = await db.query_many("SELECT x FROM t")
    print([r["x"] for r in rows])

    # Stream rows one by one
    async for row in db.query_many_gen("SELECT x FROM t"):
        print(row["x"])

    await db.close()
```

### Helper methods

`BaseDB` includes convenience helpers for common insert, update and delete
operations:

```python
# Insert one row and get its primary key
pk = await db.insert_one("t", {"x": 1})

# Insert many rows
await db.insert_many("t", [{"x": 2}, {"x": 3}])

# Upsert a single row
await db.upsert_one("t", {"id": pk, "x": 10})

# Upsert many rows
await db.upsert_many("t", [{"id": 1, "x": 11}, {"id": 4, "x": 4}])

# Delete rows
await db.delete_one("t", pk)
await db.delete_many("t", "x < ?", (0,))
```

### Query helpers

The library also offers helpers for common read patterns:

```python
# Get a single value
count = await db.query_scalar("SELECT COUNT(*) FROM t")

# Get a list from the first column of each row
ids = await db.query_column("SELECT id FROM t ORDER BY id")

# Build dictionaries from rows
# Use primary key automatically
users = await db.query_dict("SELECT * FROM users")

# Explicit column names for key and value
names = await db.query_dict(
    "SELECT id, name FROM users", key="id", value="name"
)

# Callables for custom key and value
full_names = await db.query_dict(
    "SELECT * FROM users",
    key=lambda r: r["id"],
    value=lambda r: f"{r['first_name']} {r['last_name']}",
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
    cache = await CacheDB.open("cache.db")
    await cache.set("answer", b"42", expire_sec=60)
    if await cache.is_set("answer"):
        print("cached!")
    print(await cache.get("answer"))  # b"42"
    await cache.close()
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
