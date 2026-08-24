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
- `primary_key(name, py_type, auto_increment=..., not_null=True)` adds a
  primary key. Integer keys auto-increment by default;
- `add_field` and its alias `add_column` add typed columns;
- `remove_column`, `remove_field`, and `remove_filter` remove a column already
  added to the builder;
- `unique(*columns)` adds a table-level unique constraint;
- `check(expression)` adds a table-level check constraint;
- `done()` or `str(builder)` renders SQL.

### `primary_key(name, py_type, *, auto_increment=..., not_null=True)`

`name` is the column name and `py_type` is one of the supported Python types.
With the default `auto_increment=...`, integer primary keys get SQLite
`AUTOINCREMENT`; pass `False` to disable it. `auto_increment=True` is only
valid for an integer primary key. `not_null` is ignored when auto-increment is
enabled because SQLite implies `NOT NULL`:

```python
Builder.create_table("users").primary_key(
    "id", int, auto_increment=False, not_null=True
).done()
```

### `add_field(name, py_type, *, not_null=False, unique=False, default=None, check=None, references=None)`

Add a typed column. `default` is rendered as a SQLite literal, `check` is a
raw SQLite expression, and `references` is `(table, column)` or `(table, None)`:

```python
Builder.create_table("users").add_field(
    "email",
    str,
    not_null=True,
    unique=True,
    default="unknown@example.com",
    check="length(email) > 3",
    references=("accounts", "email"),
).done()
```

`add_column(name, ...)` is an exact alias for `add_field`.

### `remove_column(name)`, `remove_field(name)`, and `remove_filter(name)`

Remove a column already queued in a `CREATE TABLE` builder. The three methods
are aliases. A missing name raises `ValueError`:

```python
table = Builder.create_table("users").add_field("temporary", str)
table.remove_column("temporary")
```

### `unique(*cols)` and `check(expr)`

Add table-level constraints. `unique` needs at least one column. `check` takes
raw SQL and is not sanitized:

```python
Builder.create_table("users").unique("email").check("age >= 0").done()
```

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

- `add_column(name, py_type, not_null=False, unique=False, default=None, check=None, references=None)`;
- `add_field(...)` is an alias for `add_column`;
- `drop_column(name)`;
- `remove_column(name)`, `remove_field(name)`, and `remove_filter(name)` are aliases for `drop_column`;
- `rename_to(new_table_name)`;
- `rename_column(old_name, new_name)`;
- `done()` renders all queued statements separated by newlines.

`add_column` uses the same parameters and constraints as the create-table
builder. Each call queues an action; the SQL is not executed until you pass
the result to `db.execute()` or use it in a migration. `rename_to` changes the
table name used by subsequent queued actions.

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
