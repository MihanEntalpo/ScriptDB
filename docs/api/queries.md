# Query helpers

All query helpers accept positional sequences or named mappings in `params`.
The row type is `sqlite3.Row` by default and `dict` when the database is opened
with `row_factory=dict`.

## `query_one(sql, params=None, postprocess_func=None) -> row | None`

Fetch one row or `None`. The callback receives the row and its return value is
returned:

```python
user = db.query_one(
    "SELECT id, name FROM users WHERE id = ?",
    (1,),
    postprocess_func=lambda row: {"id": row["id"], "label": row["name"].upper()},
)
```

The callback is not called for an empty result.

## `query_many(sql, params=None, postprocess_func=None) -> list`

Fetch all matching rows into a list:

```python
users = db.query_many("SELECT id, name FROM users ORDER BY id")
labels = db.query_many(
    "SELECT name FROM users",
    postprocess_func=lambda row: row["name"].upper(),
)
```

Use `query_many_gen()` for large results.

## `query_many_gen(sql, params=None, postprocess_func=None) -> generator`

Stream rows one at a time. The async method is an async generator:

```python
for row in db.query_many_gen("SELECT * FROM users"):
    consume(row)

async for row in async_db.query_many_gen("SELECT * FROM users"):
    await consume(row)
```

Keep the database open until iteration finishes. Cursors close when iteration
ends or the generator is closed.

## `query_scalar(sql, params=None, postprocess_func=None) -> Any`

Return the first column of the first result row, or `None`:

```python
count = db.query_scalar("SELECT COUNT(*) FROM users")
```

The callback runs before first-column extraction and must return a row-like
object containing the expected column.

## `query_column(sql, params=None, postprocess_func=None) -> list`

Return the first column from every result row:

```python
ids = db.query_column("SELECT id FROM users ORDER BY id")
```

The callback is applied before extraction.

## `query_dict(sql, params=None, *, key=None, value=None, postprocess_func=None) -> dict`

Build a dictionary from rows:

| Parameter | Behavior |
| --- | --- |
| `key=None` | Infer the table from `FROM` and use its primary key. |
| `key="column"` | Use a named column. |
| `key=callable` | Compute a key for each row. |
| `value=None` | Store each complete row. |
| `value="column"` | Use a named column as the value. |
| `value=callable` | Compute a value for each row. |
| `postprocess_func` | Transform rows before selecting key/value. |

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
single-column primary key. For joins or aliases, provide `key` explicitly.
