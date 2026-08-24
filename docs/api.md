# API reference

This is a compact reference of the supported public API. The detailed guides
contain runnable examples and behavioral notes.

## Package exports

```python
from scriptdb import (
    AbstractBaseDB,
    AsyncBaseDB,
    AsyncCacheDB,
    Builder,
    SyncBaseDB,
    SyncCacheDB,
    run_every_queries,
    run_every_seconds,
    sqlite_backend_name,
    sqlite_version,
)
```

`sqlite_backend_name` is the selected backend name (`pysqlite3` or
`sqlite3`), and `sqlite_version` is the selected SQLite version string.

## Database classes

### `SyncBaseDB`

```text
SyncBaseDB(
    db_path,
    auto_create=True,
    *,
    row_factory=sqlite3.Row,
    use_wal=True,
    legacy_sqlite_support=False,
)
```

### `AsyncBaseDB`

```text
AsyncBaseDB(
    db_path,
    auto_create=True,
    *,
    use_wal=True,
    row_factory=sqlite3.Row,
    daemonize_thread=False,
    legacy_sqlite_support=False,
)
```

Both classes require a `migrations()` method and expose:

| Method | Purpose |
| --- | --- |
| `open(path, ...)` | Return a context manager/awaitable for initialization. |
| `init()` | Open the connection and apply pending migrations. |
| `close()` | Stop background work and close the connection. |
| `execute()` | Execute one SQL statement. |
| `execute_many()` | Execute one statement for many parameter rows. |
| `insert_one()` / `insert_many()` | Insert mappings. |
| `upsert_one()` / `upsert_many()` | Insert or update by a single-column primary key. |
| `update_one()` | Update selected columns by primary key. |
| `delete_one()` / `delete_many()` | Delete by key or predicate. |
| `query_one()` / `query_many()` | Read one row or a list of rows. |
| `query_many_gen()` | Stream rows. |
| `query_scalar()` | Read the first column of the first row. |
| `query_column()` | Read the first column from every row. |
| `query_dict()` | Convert rows to a dictionary. |
| `begin()` / `commit()` / `rollback()` | Control an explicit transaction. |
| `transaction()` | Transaction context manager. |

Every query helper accepts `params` where appropriate. Read helpers also
accept `postprocess_func`.

## Decorators

### `run_every_seconds(seconds)`

Register a method or coroutine to run periodically after initialization:

```python
@run_every_seconds(30)
def cleanup(self):
    ...
```

### `run_every_queries(queries)`

Schedule a method or coroutine after every configured number of executed
queries:

```python
@run_every_queries(100)
async def checkpoint(self):
    ...
```

### `require_init`

Guard a custom method so it can only run while the database is initialized:

```python
from scriptdb.abstractdb import require_init
```

## Cache classes

`SyncCacheDB` and `AsyncCacheDB` add these methods to the corresponding base
class:

| Method | Purpose |
| --- | --- |
| `get(key, default=None)` | Read a value or default. |
| `is_set(key)` | Test whether a non-expired key exists. |
| `set(key, value, expire_sec=None)` | Store a pickled value with optional TTL. |
| `delete(key)` | Delete one key. |
| `del_many(key_mask)` | Delete keys matching `*`. |
| `keys(key_mask)` | List non-expired keys matching `*`. |
| `clear()` | Delete all keys. |
| `cache(expire_sec=None, key_func=None)` | Decorator for memoization. |

Both cache classes also accept `cache_keys_in_ram=False`.

## Conversion helpers

```python
from scriptdb.conversion import async_from_sync, sync_from_async

AsyncClass = async_from_sync(SyncClass)
SyncClass = sync_from_async(AsyncClass)
```

Only SQL string and Builder migrations can be converted. Callable migrations
raise `TypeError`.

## SQLite backend diagnostics

For detailed backend information:

```python
from scriptdb import sqlite_backend

info = sqlite_backend.SQLITE_BACKEND_INFO
print(info.backend, info.sqlite_version, info.module_path)
print(info.pysqlite_distribution_installed)
print(info.pysqlite_binary_distribution_installed)
print(info.pysqlite_import_error)
```

`sqlite_backend.ensure_upsert_supported()` raises `RuntimeError` when native
upsert is unavailable. See [SQLite troubleshooting](sqlite.md).
