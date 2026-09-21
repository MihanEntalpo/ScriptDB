# SQL, CRUD, and upserts

These methods are available on `SyncBaseDB` and `AsyncBaseDB`. Add `await` to
the async calls. Values belong in `params` or row mappings; SQL identifiers and
predicates are trusted SQL text.

## `execute(sql, params=None) -> Cursor`

Execute one statement. `params` is a positional sequence, named mapping, or
`None`; the returned cursor exposes `rowcount` and `lastrowid`:

```python
cur = db.execute("INSERT INTO users(name) VALUES (?)", ("Alice",))
user_id = cur.lastrowid
cur.close()

await async_db.execute("UPDATE users SET name=:name WHERE id=:id", {
    "name": "Alicia", "id": user_id,
})
```

Writes commit automatically unless a transaction is active. Table and column
identifiers accepted by CRUD helpers are quoted; raw SQL and `where`
expressions remain the caller's responsibility.

## `execute_many(sql, seq_params) -> Cursor`

Execute one statement for each positional sequence:

```python
db.execute_many(
    "INSERT INTO users(name) VALUES (?)",
    [("Alice",), ("Bob",)],
)
```

`seq_params` is an iterable of sequences, not a mapping. The batch itself is
atomic; use a transaction when it must also be atomic with other statements.

## `insert_one(table, row) -> Any`

Insert one column/value mapping. Return the supplied primary key or generated
`lastrowid`:

```python
user_id = db.insert_one("users", {"name": "Alice"})
await async_db.insert_one("users", {"id": 10, "name": "Bob"})
```

The table must have a single-column primary key. The method never updates an
existing row.

## `insert_many(table, rows) -> None`

Insert multiple mappings with the same column shape:

```python
db.insert_many("users", [{"name": "Alice"}, {"name": "Bob"}])
```

An empty list is a no-op. Different keys between rows raise `ValueError`, and
any database error rolls back the complete batch.

## `upsert_one(table, row) -> Any`

Insert a row or update an existing row selected by its single-column primary
key:

```python
db.upsert_one("users", {"id": 1, "name": "Alicia", "active": True})
```

If the key is omitted this behaves as an insert. Native upsert requires SQLite
3.24.0 or newer. Use `legacy_sqlite_support=True` when opening an old backend
to enable the slower compatibility path.

## `upsert_many(table, rows) -> None`

Apply several upserts with compatible column shapes:

```python
await async_db.upsert_many(
    "users",
    [{"id": 1, "active": False}, {"id": 2, "name": "Bob"}],
)
```

An empty list is a no-op. Upserts are serialized by an internal lock and the
complete batch is rolled back if any row fails.

## `update_one(table, pk, row) -> int`

Update the columns in `row` where the primary key equals `pk`. Return the
affected row count (`0` or `1`):

```python
changed = db.update_one("users", 1, {"name": "Alicia", "active": False})
```

An empty mapping returns `0`. The primary-key column is taken from the schema.

## `delete_one(table, pk) -> int`

Delete one row by primary key and return the affected row count:

```python
deleted = await async_db.delete_one("users", 1)
```

Deleting a missing key returns `0`.

## `delete_many(table, where, params=None) -> int`

Delete rows matching the SQL predicate in `where`:

```python
deleted = db.delete_many(
    "users", "active = ? AND name LIKE ?", (False, "test:%")
)
```

`where` is inserted into the statement and must be trusted. Put external
values in `params`.
