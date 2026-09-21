# Lifecycle, migrations, and transactions

This page documents methods that control a database object's lifecycle and
transaction boundaries. Sync examples use `db`; async examples use
`async_db`.

## `open(db_path, *, auto_create=True, row_factory=..., use_wal=True, legacy_sqlite_support=False)`

Returns a context manager for a database subclass. `db_path` is a string or
`Path`. With `auto_create=False`, a missing file raises `RuntimeError`.
`row_factory` is either `sqlite3.Row` (default) or `dict`. `use_wal` controls
WAL mode, and `legacy_sqlite_support` enables old-SQLite upsert emulation only
when the selected SQLite is below 3.24.0. Async `open()` also accepts
`daemonize_thread` and `handle_signals`. Signal handling is disabled by
default; `handle_signals=True` registers `SIGINT` and `SIGTERM` handlers that
schedule async database closure.

```python
with AppDB.open("app.db", row_factory=dict, use_wal=False) as db:
    db.execute("SELECT 1")

async with AsyncAppDB.open("app.db", daemonize_thread=True) as async_db:
    await async_db.execute("SELECT 1")
```

The context object initializes on entry and closes on exit. It is not a raw
SQLite connection.

## `init() -> None`

Opens the connection, configures SQLite, creates the migration table, applies
pending migrations, enables foreign-key enforcement, and starts registered
hooks. The async method is awaited.

```python
db = AppDB("app.db")
db.init()
try:
    db.execute("SELECT 1")
finally:
    db.close()
```

If initialization or a migration fails, the exception is re-raised and the
database is not left in an initialized state or with an open connection.

## `close() -> None`

Stops periodic work and closes the connection. It is idempotent:

```python
db.close()
await async_db.close()
```

Use a context manager whenever possible. Async close also removes signal
handlers registered when `handle_signals=True`.

## `migrations() -> list[dict]`

Every subclass must implement this method. Each entry has a unique string
`name` and exactly one migration body:

```python
def migrations(self):
    return [
        {
            "name": "create_users",
            "sql": "CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT)",
        },
        {
            "name": "add_index",
            "sqls": ["CREATE INDEX idx_users_name ON users(name)"],
        },
    ]
```

`sql` accepts a string or Builder; `sqls` accepts a sequence of strings or
Builders; `function` accepts a connection callback. Applied names must not be
removed or renamed. See [Migrations](../migrations.md) for validation and
callable examples.

## `begin() -> None`

Start an explicit transaction:

```python
db.begin()
```

Raises `RuntimeError` if another transaction is active. Await the async
version.

## `commit() -> None`

Commit the active transaction:

```python
db.begin()
db.execute("UPDATE users SET name = ? WHERE id = ?", ("Alicia", 1))
db.commit()
```

Raises `RuntimeError` when no transaction is active. Await the async version.

## `rollback() -> None`

Roll back the active transaction:

```python
db.begin()
try:
    db.execute("DELETE FROM users")
    raise RuntimeError("cancel")
except Exception:
    db.rollback()
```

Raises `RuntimeError` when no transaction is active. Await the async version.

## `transaction()`

Returns a context manager that calls `BEGIN`, commits on normal exit, and rolls
back when an exception escapes:

```python
with db.transaction():
    db.execute("INSERT INTO audit(message) VALUES (?)", ("created",))

async with async_db.transaction():
    await async_db.execute("INSERT INTO audit(message) VALUES (?)", ("created",))
```

Nested transactions are not supported. Automatic commits from individual
helpers are suspended while this context is active.
The transaction owns the connection operation lock, so another thread or
asyncio task waits rather than accidentally joining the transaction.
