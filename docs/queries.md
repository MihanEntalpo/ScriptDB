# CRUD and query helpers

`SyncBaseDB` and `AsyncBaseDB` expose the same database operations. The
examples below use the sync spelling; add `await` to calls in async code.

Assume this schema:

```python
CREATE_TASKS = """
CREATE TABLE tasks (
    id INTEGER PRIMARY KEY,
    title TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'new',
    priority INTEGER NOT NULL DEFAULT 0
)
"""
```

## Raw execution

### `execute(sql, params=None)`

Execute one SQL statement with positional or named parameters. Parameters must
be passed separately; do not interpolate user input into SQL.

```python
db.execute("UPDATE tasks SET status = ? WHERE id = ?", ("done", 1))
db.execute(
    "UPDATE tasks SET priority = :priority WHERE id = :id",
    {"priority": 10, "id": 1},
)
```

The returned cursor can be inspected for `rowcount`, `lastrowid`, or fetched
rows. ScriptDB commits automatically unless a manual transaction is active.

### `execute_many(sql, seq_params)`

Execute one statement for an iterable of parameter sequences:

```python
db.execute_many(
    "INSERT INTO tasks(title, priority) VALUES (?, ?)",
    [("First", 1), ("Second", 2)],
)
```

Use a transaction when several batches must succeed or fail together.

## Insert, update, upsert, and delete

### `insert_one(table, row)`

Insert a mapping and return the supplied primary key or SQLite's generated
`lastrowid`:

```python
task_id = db.insert_one("tasks", {"title": "Ship release", "priority": 5})
```

The helper does not silently update an existing row. Use `upsert_one` when
insert-or-update behavior is intended.

### `insert_many(table, rows)`

Insert a list of mappings:

```python
db.insert_many(
    "tasks",
    [
        {"title": "Write docs", "priority": 1},
        {"title": "Run tests", "priority": 2},
    ],
)
```

An empty list is a no-op. Every mapping must have the same keys or ScriptDB
raises `ValueError`; for different shapes, use separate calls or raw SQL. The
complete batch is rolled back if any row fails.

### `upsert_one(table, row)`

Insert a row or update its non-primary-key columns when the single-column
primary key already exists:

```python
db.upsert_one("tasks", {"id": 1, "title": "Updated title", "status": "done"})
```

Native upsert requires SQLite 3.24.0 or newer. A row with only its primary key
uses `DO NOTHING` if it already exists.

### `upsert_many(table, rows)`

Apply multiple upserts:

```python
db.upsert_many(
    "tasks",
    [
        {"id": 1, "status": "done"},
        {"id": 2, "status": "queued"},
    ],
)
```

Upserts are serialized with a lock and each batch is atomic. On old SQLite, pass
`legacy_sqlite_support=True` when opening the database to use the slower
compatibility implementation.

### `update_one(table, pk, row)`

Update selected columns by primary key and return the affected row count:

```python
changed = db.update_one("tasks", 1, {"status": "done", "priority": 0})
```

This helper requires a single-column primary key and does not update the key
column itself.

### `delete_one(table, pk)`

Delete one row by primary key and return its row count:

```python
deleted = db.delete_one("tasks", 1)
```

### `delete_many(table, where, params=None)`

Delete rows matching a caller-provided predicate:

```python
deleted = db.delete_many("tasks", "status = ? AND priority < ?", ("done", 2))
```

The `where` expression is SQL, so only use trusted SQL text; values belong in
`params`.

## Reading rows

### `query_one(sql, params=None, postprocess_func=None)`

Return one row or `None`:

```python
task = db.query_one("SELECT * FROM tasks WHERE id = ?", (1,))
if task is not None:
    print(task["title"])
```

### `query_many(sql, params=None, postprocess_func=None)`

Return all matching rows as a list:

```python
tasks = db.query_many(
    "SELECT * FROM tasks WHERE status = ? ORDER BY priority DESC",
    ("new",),
)
```

### `query_many_gen(sql, params=None, postprocess_func=None)`

Stream rows instead of materializing a list. In sync code it is a generator;
in async code use `async for`:

```python
for task in db.query_many_gen("SELECT * FROM tasks ORDER BY id"):
    process(task)
```

Streaming is useful for large result sets. Consume the generator while the
database remains open.

### `query_scalar(sql, params=None, postprocess_func=None)`

Return the first column of the first row, or `None` if there is no row:

```python
count = db.query_scalar("SELECT COUNT(*) FROM tasks")
```

### `query_column(sql, params=None, postprocess_func=None)`

Return the first column from every row:

```python
ids = db.query_column("SELECT id FROM tasks ORDER BY id")
```

### `query_dict(sql, params=None, key=None, value=None, postprocess_func=None)`

Build a dictionary from query results:

```python
# With no key/value arguments, the first column is used as the key and
# the complete row is used as the value.
tasks_by_id = db.query_dict("SELECT * FROM tasks")

# Explicit column names.
titles = db.query_dict(
    "SELECT id, title FROM tasks",
    key="id",
    value="title",
)

# Custom transformations.
status_by_title = db.query_dict(
    "SELECT * FROM tasks",
    key=lambda row: row["title"],
    value=lambda row: row["status"],
)
```

## Post-processing rows

`postprocess_func` receives each row before the helper builds its return value:

```python
import json


def decode(row):
    return {**row, "metadata": json.loads(row["metadata"])}


rows = db.query_many("SELECT id, metadata FROM events", postprocess_func=decode)
```

For `query_scalar`, `query_column`, and `query_dict`, the callback must return
a row-like object with the columns expected by the selected helper.

## Primary key limitations

Helpers that need a key inspect `PRAGMA table_info`. They require a single
primary-key column. Composite primary keys are valid in raw SQL, but
`insert`/`update`/`upsert` helpers that need one key raise `ValueError`; use
`execute` and query helpers for those tables.
