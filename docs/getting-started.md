# Getting started

## Installation

Install the synchronous package:

```bash
python -m pip install scriptdb
```

For the asynchronous API:

```bash
python -m pip install 'scriptdb[async]'
```

For a bundled modern SQLite build:

```bash
python -m pip install 'scriptdb[pysqlite]'
```

The package supports Python 3.8 and newer.

## Define a database class

Subclass `SyncBaseDB` or `AsyncBaseDB` and implement `migrations()`:

```python
from scriptdb import SyncBaseDB


class StoreDB(SyncBaseDB):
    def migrations(self):
        return [
            {
                "name": "create_items",
                "sql": "CREATE TABLE items (id INTEGER PRIMARY KEY, value TEXT)",
            },
        ]
```

Migrations are applied once and tracked in ScriptDB's migration table. Keep
migration names stable after they have been applied.

## Opening and closing

The recommended form is a context manager:

```python
with StoreDB.open("store.db") as db:
    db.insert_one("items", {"value": "hello"})
```

The equivalent manual form is useful when the database lifetime spans several
parts of a program:

```python
db = StoreDB.open("store.db")
try:
    db.init()
    db.insert_one("items", {"value": "hello"})
finally:
    db.close()
```

Calling a method that requires a connection before `init()` or after `close()`
raises a descriptive `RuntimeError`.

## `open()` options

Both database classes accept these common options:

| Option | Default | Meaning |
| --- | --- | --- |
| `auto_create` | `True` | Create the database file if it does not exist. |
| `use_wal` | `True` | Enable SQLite WAL journaling during initialization. |
| `row_factory` | `sqlite3.Row` | Return mapping-like SQLite rows or plain `dict` values. |
| `legacy_sqlite_support` | `False` | Emulate upserts on old SQLite; slower than native upsert. |

`AsyncBaseDB` and `AsyncCacheDB` also accept `daemonize_thread`. Set it to
`True` if an aiosqlite worker thread prevents process exit, but remember that
a daemon thread can be terminated while work is still in flight.

They also accept `handle_signals`, which defaults to `False`. Enable it only
when ScriptDB should own the process `SIGINT` and `SIGTERM` handlers; otherwise
close the database from the application's existing shutdown path.

`auto_create=False` is useful when opening an existing database is required:

```python
with StoreDB.open("must-exist.db", auto_create=False) as db:
    ...
```

It raises `RuntimeError` if the file does not exist.

## Row factories

The default `sqlite3.Row` supports both numeric and named access:

```python
row = db.query_one("SELECT 7 AS answer")
assert row["answer"] == 7
```

Use `row_factory=dict` when JSON-friendly plain dictionaries are more useful:

```python
with StoreDB.open("store.db", row_factory=dict) as db:
    row = db.query_one("SELECT 7 AS answer")
    assert row == {"answer": 7}
```

The setting affects `query_one`, `query_many`, `query_many_gen`, and the row
values used by `query_dict`, as well as post-processing callbacks.

## Sync/async conversion

When the schema uses SQL strings or Builder objects, conversion helpers can
reuse migrations without copying them:

```python
from scriptdb.conversion import async_from_sync, sync_from_async

AsyncStoreDB = async_from_sync(StoreDB)
SyncStoreDB = sync_from_async(AsyncStoreDB)
```

Callable (`function`) migrations cannot be converted because their sync/async
behavior is application-specific. The source class must be the corresponding
`SyncBaseDB` or `AsyncBaseDB` subclass.
