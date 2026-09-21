# Migrations

Migrations are Python dictionaries returned by `migrations()`. Each migration
must have a unique string `name` and exactly one of `sql`, `sqls`, or
`function`.

## One SQL statement

```python
class AppDB(SyncBaseDB):
    def migrations(self):
        return [{
            "name": "create_users",
            "sql": """
                CREATE TABLE users (
                    id INTEGER PRIMARY KEY,
                    email TEXT NOT NULL UNIQUE
                )
            """,
        }]
```

The migration name is the durable identity of the migration. Renaming an
already-applied migration makes the old name look missing and causes
validation to fail. Add a new migration for a schema change instead.

## Multiple statements

Use `sqls` for a list of statements:

```python
{
    "name": "add_profile_fields",
    "sqls": [
        "ALTER TABLE users ADD COLUMN display_name TEXT",
        "CREATE INDEX idx_users_display_name ON users(display_name)",
    ],
}
```

Alternatively, put semicolon-separated statements in `sql`. ScriptDB executes
the script as a migration unit:

```python
{
    "name": "backfill_flags",
    "sql": """
        ALTER TABLE users ADD COLUMN active INTEGER DEFAULT 1;
        UPDATE users SET active = 1 WHERE active IS NULL;
    """,
}
```

Do not include your own `BEGIN`/`COMMIT` inside a migration. ScriptDB manages
the migration transaction and rejects scripts that leave a transaction open.
Migration changes and their tracking row are committed together; a failure
rolls back the entire migration.

## Callable migrations

Use `function` when a migration needs Python logic. A sync callable receives a
`sqlite3.Connection`; an async callable receives the aiosqlite connection and
may use `await`:

```python
def seed_users(conn):
    conn.executemany(
        "INSERT INTO users(email) VALUES (?)",
        [("alice@example.com",), ("bob@example.com",)],
    )


class AppDB(SyncBaseDB):
    def migrations(self):
        return [
            {"name": "create_users", "sql": "CREATE TABLE users (email TEXT)"},
            {"name": "seed_users", "function": seed_users},
        ]
```

The callable must have the expected connection argument. Callable migrations
are not supported by `async_from_sync` or `sync_from_async`.

## Builder migrations

Builder objects can be used directly; ScriptDB converts them to SQL:

```python
from scriptdb import Builder, SyncBaseDB


class AppDB(SyncBaseDB):
    def migrations(self):
        return [
            {
                "name": "create_users",
                "sql": (
                    Builder.create_table("users")
                    .primary_key("id", int)
                    .add_field("email", str, not_null=True, unique=True)
                ),
            },
        ]
```

## Validation and failure behavior

ScriptDB validates migrations before applying them. Common errors are:

- missing or non-string `name`;
- duplicate migration names;
- more than one of `sql`, `sqls`, and `function`;
- an applied migration that no longer exists in the class;
- invalid callable signatures;
- a migration that fails during execution.

Failed initialization marks the database as not initialized and re-raises the
original error. Fix the migration and run the application again; already
committed migrations remain recorded and are not run a second time.

## Migration design rules

- Prefer one schema change per migration.
- Never edit an applied migration in place.
- Use a new migration for data backfills after adding a column.
- Keep migration names descriptive and stable, such as
  `2026_08_add_user_status`.
- Test migrations against a copy of a real database before deployment.
