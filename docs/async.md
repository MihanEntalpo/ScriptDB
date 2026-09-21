# Asynchronous API

Install async support with:

```bash
python -m pip install 'scriptdb[async]'
```

`AsyncBaseDB` mirrors `SyncBaseDB`; database operations are coroutines and
`query_many_gen` is an async generator.

## Quick start

```python
from scriptdb import AsyncBaseDB


class AppDB(AsyncBaseDB):
    def migrations(self):
        return [{
            "name": "create_messages",
            "sql": "CREATE TABLE messages (id INTEGER PRIMARY KEY, body TEXT NOT NULL)",
        }]


async def main():
    async with AppDB.open("app.db") as db:
        message_id = await db.insert_one("messages", {"body": "hello"})
        message = await db.query_one(
            "SELECT * FROM messages WHERE id = ?", (message_id,)
        )
        print(message["body"])
```

You can also await the open context directly and close manually:

```python
db = await AppDB.open("app.db")
try:
    await db.execute("SELECT 1")
finally:
    await db.close()
```

Always close an async database. The underlying aiosqlite worker and scheduled
tasks can otherwise keep the process alive.

## Async equivalents

All common sync methods have async counterparts:

```python
await db.execute(sql, params)
await db.execute_many(sql, rows)
await db.insert_one(table, row)
await db.insert_many(table, rows)
await db.upsert_one(table, row)
await db.upsert_many(table, rows)
await db.update_one(table, primary_key, row)
await db.delete_one(table, primary_key)
await db.delete_many(table, where, params)
await db.query_one(sql, params)
await db.query_many(sql, params)
await db.query_scalar(sql, params)
await db.query_column(sql, params)
await db.query_dict(sql, params)
```

Stream results without loading them all into memory:

```python
async for row in db.query_many_gen("SELECT * FROM messages ORDER BY id"):
    await handle_message(row)
```

## Async transactions

```python
async with db.transaction():
    await db.execute("INSERT INTO messages(body) VALUES (?)", ("one",))
    await db.execute("INSERT INTO messages(body) VALUES (?)", ("two",))
```

The explicit methods `begin()`, `commit()`, and `rollback()` are also
available. Transactions are not nested. While one task owns a transaction,
database operations from other tasks wait for it to finish.

## Daemonized worker thread

`aiosqlite` uses a worker thread. Pass `daemonize_thread=True` if it prevents a
short-lived script from exiting:

```python
async with AppDB.open("app.db", daemonize_thread=True) as db:
    await db.execute("SELECT 1")
```

Daemon threads may be terminated before in-flight work finishes, so use this
option only when that tradeoff is acceptable.

## Signal handling

Async databases do not change application signal handlers by default. Close
the database from your application's shutdown path or use an `async with`
block.

For standalone scripts, pass `handle_signals=True` to register handlers for
`SIGINT` and `SIGTERM` that schedule `close()` on the running event loop:

```python
async with AppDB.open("app.db", handle_signals=True) as db:
    await run_script(db)
```

This option temporarily replaces existing event-loop handlers for those
signals. Multiple ScriptDB instances share one dispatcher, and the previous
handler is restored after the last instance closes. Applications that need
their handler to run during shutdown should leave this option disabled and
close ScriptDB from that handler.

## Async migrations and hooks

Migration dictionaries and the `run_every_seconds`/
`run_every_queries` decorators work like sync code, except callable migrations
and hooks should be `async def` when they perform async database operations.
