# ScriptDB

ScriptDB is a small Python wrapper around SQLite for scripts, integrations,
ETL jobs, and lightweight applications. It provides:

- synchronous and asynchronous database classes;
- explicit SQL migrations;
- CRUD, query, transaction, and upsert helpers;
- a persistent key-value cache with TTL support;
- a small SQL DDL builder;
- optional compatibility support for old SQLite builds.

ScriptDB does not hide SQL behind models or an ORM. You define the schema and
queries yourself, while ScriptDB handles connections, migrations, commits,
and the repetitive parts of common operations.

## Choose an interface

| Need | Use |
| --- | --- |
| Blocking scripts and ordinary synchronous code | `SyncBaseDB` |
| `asyncio` applications | `AsyncBaseDB` |
| Persistent key-value data | `SyncCacheDB` or `AsyncCacheDB` |
| Programmatic DDL generation | `Builder` |

The sync and async database APIs intentionally have matching names. The async
version adds `await` and uses an async generator for `query_many_gen`.

## Minimal example

```python
from scriptdb import SyncBaseDB


class AppDB(SyncBaseDB):
    def migrations(self):
        return [{
            "name": "create_tasks",
            "sql": """
                CREATE TABLE tasks (
                    id INTEGER PRIMARY KEY,
                    title TEXT NOT NULL,
                    done INTEGER NOT NULL DEFAULT 0
                )
            """,
        }]


with AppDB.open("app.db") as db:
    task_id = db.insert_one("tasks", {"title": "Read the docs"})
    task = db.query_one("SELECT * FROM tasks WHERE id = ?", (task_id,))
    print(task["title"])
```

`open()` returns a context manager. The database is initialized on entering
the context and closed automatically on exit. For manual lifecycle control,
use `db = AppDB.open("app.db")`, then `db.init()` and `db.close()`.

## Requirements

Native SQLite upserts require SQLite **3.24.0 or newer**. Modern Python builds
usually satisfy this. On older systems, install the optional binary backend:

```bash
python -m pip install 'scriptdb[pysqlite]'
```

Do not normally install `pysqlite3` and `pysqlite3-binary` together: both
provide the same import namespace and can overwrite each other's files. See
[SQLite troubleshooting](sqlite.md) for diagnostics and recovery commands.

## Documentation map

- [Getting started](getting-started.md) — installation, class structure, and
  lifecycle.
- [Migrations](migrations.md) — SQL, multiple statements, Builder migrations,
  callable migrations, and validation rules.
- [CRUD and queries](queries.md) — every database operation and query helper.
- [Transactions and hooks](transactions-hooks.md) — transactions, periodic
  jobs, and query-count hooks.
- [Async API](async.md) — async lifecycle, generators, signals, and daemonized
  worker threads.
- [Cache API](cache.md) — key-value storage, expiration, decorators, and RAM
  indexing.
- [SQL Builder](builder.md) — all DDL builder entry points and methods.
- [API reference](api.md) — compact method-by-method reference.
- [SQLite troubleshooting](sqlite.md) — backend selection and diagnostics.
