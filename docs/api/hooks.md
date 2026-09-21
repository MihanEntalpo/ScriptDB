# Hooks, conversion, and backend diagnostics

## `run_every_seconds(seconds) -> decorator`

Run a method repeatedly after initialization. `seconds` must be positive:

```python
@run_every_seconds(300)
def cleanup(self):
    self.execute("DELETE FROM events WHERE expires_at < datetime('now')")
```

Sync methods run in a daemon thread; async methods run as tasks. `close()`
stops the registered work. An exception is logged and does not permanently
stop later runs. Zero, negative, infinite, and NaN intervals are rejected.

## `run_every_queries(queries) -> decorator`

Schedule a method after the configured number of database operations:

```python
@run_every_queries(1000)
async def checkpoint(self):
    await self.execute("PRAGMA wal_checkpoint")
```

The counter resets after scheduling. A hook is not scheduled recursively by
its own database queries. Exceptions are logged; keep hooks short and handle
expected application errors inside them. Query intervals must be positive
integers.

## `require_init(method) -> method`

Guard a custom method so it can only run while the database is initialized:

```python
@require_init
def rebuild_search_index(self):
    self.execute("REINDEX")
```

Built-in connection-dependent methods already use this guard.

## `async_from_sync(sync_cls) -> type[AsyncBaseDB]`

Create an async subclass reusing migrations from a `SyncBaseDB` subclass:

```python
AsyncAppDB = async_from_sync(AppDB)
```

Only SQL strings and Builder migrations can be converted. Callable migrations
raise `TypeError` because their sync/async behavior is application-specific.

## `sync_from_async(async_cls) -> type[SyncBaseDB]`

Create a sync subclass reusing migrations from an `AsyncBaseDB` subclass:

```python
SyncAppDB = sync_from_async(AsyncAppDB)
```

The source must inherit from the matching base class, and callable migrations
are not convertible.

## `SQLITE_BACKEND_INFO`

Inspect the selected backend and pysqlite installation status:

```python
from scriptdb import sqlite_backend

info = sqlite_backend.SQLITE_BACKEND_INFO
print(info.backend, info.sqlite_version, info.module_path)
print(info.pysqlite_distribution_installed)
print(info.pysqlite_binary_distribution_installed)
print(info.pysqlite_import_error)
```

`sqlite_backend.ensure_upsert_supported()` raises `RuntimeError` when native
upsert is unavailable. See [SQLite troubleshooting](../sqlite.md).
