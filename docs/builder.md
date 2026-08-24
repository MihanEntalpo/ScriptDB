# SQL Builder

`Builder` generates SQL strings for common SQLite DDL operations. It is a
convenience layer, not an ORM: inspect the generated SQL and execute it with
`db.execute()` or use it directly in a migration.

```python
from scriptdb import Builder

sql = Builder.create_table("users").primary_key("id", int).add_field("name", str)
print(sql)        # __str__ delegates to done()
print(sql.done())
```

Supported Python types are `int`, `str`, `float`, `bytes`, `bool`, `date`, and
`datetime`. Date and datetime values are stored as SQLite `TEXT` declarations.

> **Security:** identifiers are quoted, but raw SQL options such as `check`
> expressions are not sanitized. Never pass untrusted input to table names,
> column names, or raw constraint expressions.

## `Builder.create_table`

Start a `CREATE TABLE` statement:

```python
sql = (
    Builder.create_table("users", if_not_exists=True)
    .primary_key("id", int)
    .add_field("email", str, not_null=True, unique=True)
    .add_field("active", bool, default=True)
    .unique("email")
    .check("length(email) > 3")
    .done()
)
```

Options:

- `if_not_exists=True` adds `IF NOT EXISTS`;
- `without_rowid=True` adds `WITHOUT ROWID`;
- `primary_key(name, py_type, auto_increment=..., not_null=False)` adds a
  primary key. Integer keys auto-increment by default;
- `add_field` and its alias `add_column` add typed columns;
- `remove_column`, `remove_field`, and `remove_filter` remove a column already
  added to the builder;
- `unique(*columns)` adds a table-level unique constraint;
- `check(expression)` adds a table-level check constraint;
- `done()` or `str(builder)` renders SQL.

Example with references and defaults:

```python
sql = (
    Builder.create_table("posts")
    .primary_key("id", int)
    .add_field("author_id", int, references=("users", "id"))
    .add_field("published", bool, default=False, not_null=True)
    .done()
)
```

## `Builder.create_table_from_dict`

Infer columns from a flat representative dictionary:

```python
sql = Builder.create_table_from_dict(
    "users",
    {"id": 1, "name": "Alice", "active": True},
).done()
```

An `id` value of type `int` or `str` becomes a primary key. Nested mappings,
lists, tuples, and sets are rejected because inference only supports flat
values. An empty dictionary is invalid.

## `Builder.alter_table`

Queue one or more `ALTER TABLE` actions:

```python
sql = (
    Builder.alter_table("users")
    .add_column("age", int, default=0)
    .rename_column("name", "display_name")
    .done()
)
```

Methods:

- `add_column` / `add_field`;
- `drop_column` / `remove_column` / `remove_field` / `remove_filter`;
- `rename_to(new_table_name)`;
- `rename_column(old_name, new_name)`;
- `done()` renders all queued statements separated by newlines.

An `ALTER TABLE` builder with no actions raises `ValueError`.

## Drop table and indexes

```python
drop_table = Builder.drop_table("old_users", if_exists=True).done()

create_index = Builder.create_index(
    "users",
    on=["last_name", "first_name"],
    unique=False,
    name="idx_users_name",
).done()

drop_index = Builder.drop_index(
    "users",
    on=["last_name", "first_name"],
    name="idx_users_name",
).done()
```

`create_index(table, on, unique=False, if_not_exists=True, name=None)` accepts
a column string or list. When `name` is omitted it is generated as
`{table}_{columns}_idx`.

`drop_table` and `drop_index` accept `if_exists`. `drop_index` can infer the
same generated name from `table` and `on`; pass `name` for a custom index.

## Builders in migrations

Builders can be stored directly in `sql` or inside `sqls`:

```python
class AppDB(SyncBaseDB):
    def migrations(self):
        return [{
            "name": "create_users",
            "sql": (
                Builder.create_table("users")
                .primary_key("id", int)
                .add_field("name", str, not_null=True)
            ),
        }]
```
