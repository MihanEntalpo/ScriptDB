# Transactions and background hooks

## Automatic commits

Outside an explicit transaction, write helpers commit after their statement.
For a group of related writes, use a transaction so a failure rolls all of
them back.

## Transaction context manager

### Sync

```python
with db.transaction():
    db.execute("INSERT INTO accounts(name) VALUES (?)", ("Alice",))
    db.execute("INSERT INTO audit(message) VALUES (?)", ("account created",))
```

### Async

```python
async with db.transaction():
    await db.execute("INSERT INTO accounts(name) VALUES (?)", ("Alice",))
    await db.execute("INSERT INTO audit(message) VALUES (?)", ("account created",))
```

The context manager calls `BEGIN`, commits on normal exit, and rolls back when
an exception escapes. Nested transactions are not supported.

## Explicit transaction methods

Use `begin()`, `commit()`, and `rollback()` when boundaries are controlled by
separate parts of the program:

```python
db.begin()
try:
    db.execute("UPDATE inventory SET count = count - 1 WHERE sku = ?", ("A-1",))
    db.commit()
except Exception:
    db.rollback()
    raise
```

Calling `commit()` or `rollback()` without an active transaction, or calling
`begin()` twice, raises `RuntimeError`.

## Periodic hooks: `run_every_seconds`

Decorate a sync method or async coroutine to run repeatedly after database
initialization:

```python
from scriptdb import SyncBaseDB, run_every_seconds


class AppDB(SyncBaseDB):
    def migrations(self):
        return []

    @run_every_seconds(60)
    def cleanup(self):
        self.execute("DELETE FROM events WHERE created_at < datetime('now', '-7 days')")
```

The sync hook runs in a daemon thread. The async hook runs as an asyncio task.
`close()` stops the background work. Keep periodic methods short and handle
expected application errors inside the method.

## Query-count hooks: `run_every_queries`

Run maintenance after a number of executed queries:

```python
from scriptdb import AsyncBaseDB, run_every_queries


class AppDB(AsyncBaseDB):
    def migrations(self):
        return []

    @run_every_queries(100)
    async def checkpoint(self):
        await self.execute("PRAGMA wal_checkpoint")
```

The async hook is scheduled as a task rather than blocking the query that
triggered it. The decorator argument must be a positive query count.

## Lifecycle guard: `require_init`

`require_init` is useful for custom methods that must only run while a database
is open:

```python
from scriptdb.abstractdb import require_init


class AppDB(SyncBaseDB):
    def migrations(self):
        return []

    @require_init
    def vacuum(self):
        self.execute("VACUUM")
```

Most built-in methods already use this guard. Calling them outside the open
lifecycle raises `RuntimeError`.
