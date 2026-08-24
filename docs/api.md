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

## Detailed database reference

The methods below are available on `SyncBaseDB` and `AsyncBaseDB`. The async
versions have the same parameters and return values unless noted; call them
with `await`.

### `open(db_path, *, auto_create=True, row_factory=..., use_wal=True, legacy_sqlite_support=False)`

Return a context manager that initializes a database when entered. The async
version also returns an awaitable.

| Parameter | Description |
| --- | --- |
| `db_path` | `str` or `pathlib.Path` of the SQLite file. |
| `auto_create` | Create a missing file when `True`; raise `RuntimeError` when `False`. |
| `row_factory` | `sqlite3.Row` by default, or `dict` for plain dictionaries. |
| `use_wal` | Enable SQLite WAL mode during initialization. |
| `legacy_sqlite_support` | Emulate upserts when the selected SQLite is too old. |
| `daemonize_thread` | Async only; daemonize the aiosqlite worker thread. |

```python
with AppDB.open("app.db", row_factory=dict, use_wal=False) as db:
    db.execute("SELECT 1")

async with AsyncAppDB.open("app.db", daemonize_thread=True) as db:
    await db.execute("SELECT 1")
```

The returned object is not a raw connection. Prefer the context manager so
`close()` is called even if the body raises.

### `init() -> None`

Open the connection, configure the row factory and WAL, apply migrations, and
start registered hooks. The async version is awaited.

```python
db = AppDB("app.db")
db.init()
try:
    db.execute("SELECT 1")
finally:
    db.close()
```

If a migration fails, initialization is marked unsuccessful and the exception
is re-raised.

### `close() -> None`

Stop periodic tasks/threads and close the connection. The operation is
idempotent, so it is safe to call from cleanup paths more than once:

```python
db.close()
await async_db.close()
```

### `migrations() -> list[dict]`

Implement this abstract method in every subclass. Each entry needs a unique
string `name` and exactly one of `sql`, `sqls`, or `function`:

```python
def migrations(self):
    return [{
        "name": "create_users",
        "sql": "CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT)",
    }]
```

Applied names must remain present in future versions. See
[Migrations](migrations.md) for callable signatures and transaction rules.

### `execute(sql, params=None) -> Cursor`

Execute one trusted SQL statement and return its cursor. `params` is a
positional sequence, named mapping, or `None`:

```python
cur = db.execute("INSERT INTO users(name) VALUES (?)", ("Alice",))
new_id = cur.lastrowid
cur.close()
db.execute("UPDATE users SET name=:name WHERE id=:id", {"name": "Alicia", "id": new_id})
```

Writes commit automatically unless a transaction is active. Parameterize
values; do not interpolate external values into `sql`.

### `execute_many(sql, seq_params) -> Cursor`

Execute one statement for each positional sequence in `seq_params`:

```python
db.execute_many(
    "INSERT INTO users(name) VALUES (?)",
    [("Alice",), ("Bob",)],
)
```

This method expects an iterable of sequences, not mappings. Use a transaction
when the batch must be atomic with other statements.

### `insert_one(table, row) -> Any`

Insert one column/value mapping. Return the supplied primary key or SQLite's
generated `lastrowid`:

```python
user_id = db.insert_one("users", {"name": "Alice"})
db.insert_one("users", {"id": 10, "name": "Bob"})
```

The table must have a single-column primary key. This method only inserts; it
does not update an existing row.

### `insert_many(table, rows) -> None`

Insert multiple mappings using the column shape of the first row:

```python
db.insert_many("users", [{"name": "Alice"}, {"name": "Bob"}])
```

An empty list is a no-op. Use compatible keys in every mapping.

### `upsert_one(table, row) -> Any`

Insert a row or update the existing row selected by its single-column primary
key:

```python
db.upsert_one("users", {"id": 1, "name": "Alicia", "active": True})
```

If the key is omitted this behaves as an insert. A primary-key-only row uses
`DO NOTHING` when the key already exists. Native upsert requires SQLite 3.24.0
or newer.

### `upsert_many(table, rows) -> None`

Apply several upserts with compatible column shapes:

```python
db.upsert_many("users", [{"id": 1, "active": False}, {"id": 2, "name": "Bob"}])
```

An empty list is a no-op. Upserts are serialized by an internal lock. Pass
`legacy_sqlite_support=True` when opening an old SQLite database to use the
slower compatibility implementation.

### `update_one(table, pk, row) -> int`

Update the columns in `row` for the row whose primary key equals `pk`. Return
the affected row count:

```python
changed = db.update_one("users", 1, {"name": "Alicia", "active": False})
```

An empty mapping returns `0`. The primary-key column is taken from the schema.

### `delete_one(table, pk) -> int`

Delete one row by primary key and return `0` or `1`:

```python
deleted = db.delete_one("users", 1)
```

### `delete_many(table, where, params=None) -> int`

Delete rows matching the SQL predicate and return the affected row count:

```python
deleted = db.delete_many("users", "active = ? AND name LIKE ?", (False, "test:%"))
```

`where` is inserted into the SQL string and must be trusted. Put external
values in `params`.

### `query_one(sql, params=None, postprocess_func=None) -> row | None`

Fetch one row or `None`. The callback, when supplied, receives the row and its
return value is returned:

```python
user = db.query_one(
    "SELECT id, name FROM users WHERE id = ?",
    (1,),
    postprocess_func=lambda row: {"id": row["id"], "label": row["name"].upper()},
)
```

The callback is not called when the query returns no rows.

### `query_many(sql, params=None, postprocess_func=None) -> list`

Fetch every matching row into a list:

```python
users = db.query_many("SELECT id, name FROM users ORDER BY id")
labels = db.query_many("SELECT name FROM users", postprocess_func=lambda r: r["name"].upper())
```

The configured `row_factory` controls the input row type passed to the
callback.

### `query_many_gen(sql, params=None, postprocess_func=None) -> generator`

Stream rows one at a time. The sync version is a normal generator; the async
version is an async generator:

```python
for row in db.query_many_gen("SELECT * FROM users"):
    consume(row)

async for row in async_db.query_many_gen("SELECT * FROM users"):
    await consume(row)
```

Keep the database open until iteration finishes. The cursor is closed when the
generator is exhausted or closed.

### `query_scalar(sql, params=None, postprocess_func=None) -> Any`

Return the first column of the first result row, or `None` if no row exists:

```python
count = db.query_scalar("SELECT COUNT(*) FROM users")
```

The callback runs before the first column is extracted and must return a
row-like object with that column.

### `query_column(sql, params=None, postprocess_func=None) -> list`

Return the first column from every result row:

```python
ids = db.query_column("SELECT id FROM users ORDER BY id")
```

The callback is applied to every row before extraction.

### `query_dict(sql, params=None, *, key=None, value=None, postprocess_func=None) -> dict`

Convert rows into a dictionary:

| Parameter | Behavior |
| --- | --- |
| `key=None` | Infer the table from `FROM` and use its primary key. |
| `key="column"` | Use that named column. |
| `key=callable` | Call it for every row. |
| `value=None` | Store the complete row. |
| `value="column"` | Store that named column. |
| `value=callable` | Compute the value for every row. |
| `postprocess_func` | Transform rows before key/value selection. |

```python
by_id = db.query_dict("SELECT * FROM users")
names = db.query_dict("SELECT id, name FROM users", key="id", value="name")
labels = db.query_dict(
    "SELECT * FROM users",
    key=lambda row: row["id"],
    value=lambda row: row["name"].upper(),
)
```

Automatic key inference requires a recognizable `FROM table` clause and a
single-column primary key. For joins or aliases, pass `key` explicitly.

### `begin() -> None`

Start a manual transaction. Async version is awaited:

```python
db.begin()
try:
    db.execute("UPDATE users SET active = ?", (True,))
    ...
except Exception:
    db.rollback()
    raise
```

`begin()` raises if a transaction is already active.

### `commit() -> None`

Commit the active manual transaction and end it:

```python
db.begin()
db.execute("UPDATE users SET active = ?", (True,))
db.commit()
```

It raises `RuntimeError` if no transaction is active.

### `rollback() -> None`

Roll back the active manual transaction and end it:

```python
db.begin()
try:
    db.execute("UPDATE users SET active = ?", (True,))
    raise RuntimeError("cancel operation")
except Exception:
    db.rollback()
```

It raises `RuntimeError` if no transaction is active. Async versions of all
three methods are awaited.

### `transaction()`

Return a context manager that automatically begins, commits, and rolls back:

```python
with db.transaction():
    db.execute("INSERT INTO audit(message) VALUES (?)", ("done",))

async with async_db.transaction():
    await async_db.execute("INSERT INTO audit(message) VALUES (?)", ("done",))
```

Any exception escaping the block causes rollback and is re-raised. Nested
transactions are not supported.

## Detailed cache reference

`SyncCacheDB` and `AsyncCacheDB` inherit all database methods above and add the
following methods. Async calls are awaited.

### `cache_keys_in_ram=False`

This cache-only `open()` option keeps a process-local index of live keys when
set to `True`:

```python
with SyncCacheDB.open("cache.db", cache_keys_in_ram=True) as cache:
    ...
```

It speeds up repeated negative checks, but consumes memory and does not observe
writes made by other processes.

### `get(key, default=None) -> Any`

Return the unpickled value or `default` for a missing/expired key:

```python
value = cache.get("user:1", default=None)
```

### `is_set(key) -> bool`

Return whether a key exists and has not expired, without unpickling its value:

```python
if cache.is_set("report:today"):
    send(cache.get("report:today"))
```

### `set(key, value, expire_sec=None) -> None`

Pickle and store a value. `None` means no expiration; a positive number is a
TTL in seconds; zero or a negative number is immediately expired:

```python
cache.set("token", "abc", expire_sec=300)
```

Only use the cache with trusted database files because unpickling can execute
arbitrary code.

### `delete(key) -> int`

Delete one key and return the affected row count:

```python
removed = cache.delete("token")
```

### `del_many(key_mask) -> int`

Delete keys matching a mask and return the affected row count. `*` means any
sequence of characters and `_` is treated literally:

```python
cache.del_many("session:*")
```

### `keys(key_mask) -> list[str]`

Return non-expired keys matching the same mask syntax:

```python
keys = cache.keys("session:*")
```

### `clear() -> int`

Delete every cache entry and return the affected row count:

```python
removed = cache.clear()
```

### `cache(expire_sec=None, key_func=None) -> decorator`

Create a memoization decorator. The default key includes function name,
positional arguments, and keyword arguments. `key_func` receives the call
arguments and must return a string:

```python
@cache.cache(expire_sec=60, key_func=lambda user_id: f"user:{user_id}")
def load_user(user_id):
    return fetch_user(user_id)
```

`SyncCacheDB.cache` can bridge an async function with `asyncio.run`; prefer
`AsyncCacheDB.cache` inside an async application. Exceptions are not cached.

## Decorator reference

### `run_every_seconds(seconds) -> decorator`

Register a method to run repeatedly after initialization. `seconds` must be a
positive integer:

```python
@run_every_seconds(300)
def cleanup(self):
    self.execute("DELETE FROM events WHERE expires_at < datetime('now')")
```

Sync methods run in a daemon thread; async methods run as tasks. `close()`
stops the registered background work.

### `run_every_queries(queries) -> decorator`

Register a method to be scheduled after a number of executed database queries:

```python
@run_every_queries(1000)
async def checkpoint(self):
    await self.execute("PRAGMA wal_checkpoint")
```

The counter resets after scheduling. Async hooks run as tasks and should remain
short-lived.

### `require_init(method) -> method`

Guard a custom method so it raises `RuntimeError` unless the database is open:

```python
@require_init
def rebuild_search_index(self):
    self.execute("REINDEX")
```

Built-in connection-dependent methods already use this guard.

## Conversion reference

### `async_from_sync(sync_cls) -> type[AsyncBaseDB]`

Create an async subclass that reuses migrations from a `SyncBaseDB` subclass:

```python
AsyncAppDB = async_from_sync(AppDB)
```

Only SQL strings and Builder objects can be converted. A callable migration
raises `TypeError`.

### `sync_from_async(async_cls) -> type[SyncBaseDB]`

Create a sync subclass that reuses migrations from an `AsyncBaseDB` subclass:

```python
SyncAppDB = sync_from_async(AsyncAppDB)
```

The source class must inherit from the matching base class, and callable
migrations are not convertible.

## Detailed Builder reference

### `done() -> str` and `str(builder) -> str`

Render the SQL accumulated by a Builder object:

```python
sql = str(Builder.drop_table("old_events"))
```

Builder method details and security notes are in [SQL Builder](builder.md).

### `Builder.create_table(name, *, if_not_exists=True, without_rowid=False)`

Return a `CreateTableBuilder`:

```python
table = Builder.create_table("users", without_rowid=False)
```

`if_not_exists` controls `IF NOT EXISTS`; `without_rowid` controls the SQLite
`WITHOUT ROWID` suffix.

### `Builder.create_table_from_dict(name, src, *, if_not_exists=True, without_rowid=False)`

Infer a flat schema from representative values:

```python
table = Builder.create_table_from_dict(
    "users", {"id": 1, "name": "Alice", "active": True}
)
```

An `id` value of type `int` or `str` becomes a primary key. Nested containers
and an empty mapping are rejected.

### `Builder.alter_table(name)`

Return an `AlterTableBuilder` that queues `add_column`, `drop_column`,
`rename_to`, or `rename_column` actions:

```python
sql = Builder.alter_table("users").add_column("age", int, default=0).done()
```

### `Builder.drop_table(name, *, if_exists=True)`

Return a builder for `DROP TABLE`:

```python
sql = Builder.drop_table("temporary", if_exists=True).done()
```

### `Builder.create_index(table, on, *, unique=False, if_not_exists=True, name=None)`

Create an index for one column or a list of columns:

```python
sql = Builder.create_index(
    "users", ["last_name", "first_name"], unique=False, name="idx_users_name"
).done()
```

When `name` is omitted, ScriptDB generates a name from the table and columns.

### `Builder.drop_index(table, on, *, if_exists=True, name=None)`

Drop an index by its explicit name or by the same table/column combination used
for generated names:

```python
sql = Builder.drop_index("users", ["last_name", "first_name"], name="idx_users_name").done()
```

For fluent builder methods (`primary_key`, `add_field`, aliases, constraints,
and alter actions), see the examples and full parameter explanations in
[SQL Builder](builder.md).
