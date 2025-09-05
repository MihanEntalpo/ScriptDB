import sqlite3

import pytest
from scriptdb import SyncBaseDB
from scriptdb.dbbuilder import Builder, _default_literal
from datetime import date, datetime

INJECTION = 'x"; DROP TABLE safe;--'

def test_create_table_builder_generates_sql():
    sql = str(
        Builder.create_table("users")
        .primary_key("id", int)
        .add_field("name", str, not_null=True)
        .add_field("is_active", bool, default=False, not_null=True)
        .unique("name")
    )
    expected_sql = (
        'CREATE TABLE IF NOT EXISTS "users" ('
        '"id" INTEGER PRIMARY KEY AUTOINCREMENT, '
        '"name" TEXT NOT NULL, '
        '"is_active" INTEGER NOT NULL DEFAULT 0, '
        'UNIQUE ("name"));'
    )
    assert sql == expected_sql


def test_alter_table_builder_generates_sql():
    sql = str(
        Builder.alter_table("users")
        .add_column("age", int, not_null=True, default=0)
        .rename_column("age", "user_age")
        .rename_to("people")
    )
    expected_sql = (
        'ALTER TABLE "users" ADD COLUMN "age" INTEGER NOT NULL DEFAULT 0;'
        '\nALTER TABLE "users" RENAME COLUMN "age" TO "user_age";'
        '\nALTER TABLE "users" RENAME TO "people";'
    )
    assert sql == expected_sql


def test_drop_column_builder_generates_sql():
    sql = str(Builder.alter_table("users").drop_column("old"))
    assert sql == 'ALTER TABLE "users" DROP COLUMN "old";'


def test_primary_key_autoincrement_requires_integer():
    builder = Builder.create_table("test")
    with pytest.raises(ValueError):
        builder.primary_key("id", str, auto_increment=True)


def test_primary_key_auto_increment_auto_behavior():
    sql_int = str(Builder.create_table("t").primary_key("id", int))
    assert "AUTOINCREMENT" in sql_int
    sql_text = str(Builder.create_table("t").primary_key("name", str))
    assert "AUTOINCREMENT" not in sql_text


def test_drop_table_builder_generates_sql():
    assert str(Builder.drop_table("users")) == 'DROP TABLE IF EXISTS "users";'
    assert (
        str(Builder.drop_table("users", if_exists=False))
        == 'DROP TABLE "users";'
    )


def test_index_sql_generation():
    create = str(Builder.create_index("users", on="name", name="idx_users_name"))
    assert (
        create
        == 'CREATE INDEX IF NOT EXISTS "idx_users_name" ON "users" ("name");'
    )
    unique = str(
        Builder.create_index(
            "users",
            on=["name", "age"],
            unique=True,
            if_not_exists=False,
            name="idx_users_name_age",
        )
    )
    assert (
        unique
        == 'CREATE UNIQUE INDEX "idx_users_name_age" ON "users" ("name", "age");'
    )
    drop = str(Builder.drop_index("users", on="name", name="idx_users_name"))
    assert drop == 'DROP INDEX IF EXISTS "idx_users_name";'
    auto = str(Builder.create_index("users", on="age"))
    assert auto == 'CREATE INDEX IF NOT EXISTS "users_age_idx" ON "users" ("age");'
    auto_drop = str(Builder.drop_index("users", on="age"))
    assert auto_drop == 'DROP INDEX IF EXISTS "users_age_idx";'


def test_create_index_requires_column():
    with pytest.raises(ValueError):
        Builder.create_index("t", on=[], name="idx_bad")


@pytest.mark.parametrize(
    "value, literal",
    [
        (None, "NULL"),
        (True, "1"),
        (False, "0"),
        (123, "123"),
        (3.14, "3.14"),
        (b"\x00\xff", "X'00ff'"),
        ("O'Reilly", "'O''Reilly'"),
        (date(2020, 1, 2), "'2020-01-02'"),
        (datetime(2020, 1, 2, 3, 4, 5), "'2020-01-02 03:04:05'"),
    ],
)
def test_default_literal(value, literal):
    assert _default_literal(value) == literal


def test_create_table_builder_with_constraints():
    sql = str(
        Builder.create_table("articles", if_not_exists=False, without_rowid=True)
        .primary_key("id", int)
        .add_field("author_id", int, not_null=True, references=("users", "id"))
        .add_field("title", str, unique=True, default="Untitled")
        .unique("author_id", "title")
        .check("length(title) > 0")
    )
    expected = (
        'CREATE TABLE "articles" ('
        '"id" INTEGER PRIMARY KEY AUTOINCREMENT, '
        '"author_id" INTEGER NOT NULL REFERENCES "users"("id"), '
        "\"title\" TEXT UNIQUE DEFAULT 'Untitled', "
        'UNIQUE ("author_id", "title"), '
        'CHECK (length(title) > 0)) WITHOUT ROWID;'
    )
    assert sql == expected


def test_date_datetime_columns():
    sql = str(
        Builder.create_table("d")
        .primary_key("id", int)
        .add_field("d", date)
        .add_field("dt", datetime)
    )
    assert '"d" TEXT' in sql
    assert '"dt" TEXT' in sql


def test_unsupported_python_type():
    with pytest.raises(ValueError):
        Builder.create_table("bad").add_field("data", dict)


class _MemDB(SyncBaseDB):
    def migrations(self):  # pragma: no cover - simple subclass
        return []


def test_builder_sql_executes_via_syncdb():
    create_sql = str(
        Builder.create_table("users")
        .primary_key("id", int)
        .add_field("name", str)
    )
    alter_sql = str(
        Builder.alter_table("users")
        .add_column("age", int, default=0)
        .rename_column("age", "user_age")
        .rename_to("people")
    )
    drop_sql = str(Builder.drop_table("people"))

    with _MemDB.open(":memory:") as db:
        db.conn.executescript(create_sql)
        db.conn.executescript(alter_sql)
        db.execute(
            "INSERT INTO people(name, user_age) VALUES(?, ?)",
            ("Alice", 30),
        )
        row = db.query_one("SELECT name, user_age FROM people WHERE id=1")
        assert row["name"] == "Alice" and row["user_age"] == 30
        db.conn.executescript(drop_sql)
        with pytest.raises(sqlite3.OperationalError):
            db.query_one("SELECT 1 FROM people")


def test_drop_column_executes():
    create_sql = str(
        Builder.create_table("t")
        .primary_key("id", int)
        .add_field("old", int)
    )
    drop_sql = str(Builder.alter_table("t").drop_column("old"))
    with _MemDB.open(":memory:") as db:
        db.conn.executescript(create_sql)
        db.conn.executescript(drop_sql)
        cols = db.query_column("SELECT name FROM pragma_table_info('t')")
        assert "old" not in cols


def test_alter_table_multiple_actions_execute():
    create_sql = str(
        Builder.create_table("t")
        .primary_key("id", int)
        .add_field("name", str)
    )
    alter_sql = str(
        Builder.alter_table("t")
        .add_column("age", int, default=0)
        .add_column("temp", str)
        .drop_column("temp")
        .rename_column("name", "username")
        .rename_to("people")
    )
    with _MemDB.open(":memory:") as db:
        db.conn.executescript(create_sql)
        db.conn.executescript(alter_sql)
        db.execute(
            "INSERT INTO people(username, age) VALUES(?, ?)",
            ("Alice", 25),
        )
        row = db.query_one("SELECT username, age FROM people WHERE id=1")
        assert row["username"] == "Alice" and row["age"] == 25
        with pytest.raises(sqlite3.OperationalError):
            db.query_one("SELECT 1 FROM t")


def test_index_builder_sql_executes():
    create_table_sql = str(
        Builder.create_table("t")
        .primary_key("id", int)
        .add_field("name", str)
    )
    create_index_sql = str(Builder.create_index("t", on="name", name="idx_t_name"))
    drop_index_sql = str(Builder.drop_index("t", on="name", name="idx_t_name"))
    with _MemDB.open(":memory:") as db:
        db.conn.executescript(create_table_sql)
        db.conn.executescript(create_index_sql)
        idxs = db.query_column(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='t'"
        )
        assert "idx_t_name" in idxs
        db.conn.executescript(drop_index_sql)
        idxs = db.query_column(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='t'"
        )
        assert "idx_t_name" not in idxs


@pytest.mark.parametrize(
    "col_name",
    [
        "simple",
        "with space",
        'we"rd',
        "select",
        "漢字",
    ],
)
def test_weird_column_names_execute(col_name):
    sql = str(
        Builder.create_table("t")
        .primary_key("id", int)
        .add_field(col_name, str)
    )
    with _MemDB.open(":memory:") as db:
        db.conn.executescript(sql)
        cols = db.query_column("SELECT name FROM pragma_table_info('t')")
        assert col_name in cols


@pytest.mark.parametrize("bad_name", ["\x00bad"])
def test_invalid_column_names_fail(bad_name):
    sql = str(
        Builder.create_table("t")
        .primary_key("id", int)
        .add_field(bad_name, int)
    )
    with _MemDB.open(":memory:") as db:
        with pytest.raises(ValueError):
            db.conn.executescript(sql)


def _prepare_safe(db):
    db.conn.executescript('CREATE TABLE safe(id INTEGER);')


def test_create_table_name_injection():
    inj_table = INJECTION
    sql = str(Builder.create_table(inj_table).primary_key("id", int))
    with _MemDB.open(":memory:") as db:
        _prepare_safe(db)
        db.conn.executescript(sql)
        names = db.query_column("SELECT name FROM sqlite_master WHERE type='table'")
        assert "safe" in names and inj_table in names


def test_create_table_column_and_unique_injection():
    inj_col = INJECTION
    sql = str(
        Builder.create_table("t")
        .primary_key("id", int)
        .add_field(inj_col, int)
        .unique(inj_col)
    )
    with _MemDB.open(":memory:") as db:
        _prepare_safe(db)
        db.conn.executescript(sql)
        cols = db.query_column("SELECT name FROM pragma_table_info('t')")
        assert inj_col in cols
        names = db.query_column("SELECT name FROM sqlite_master WHERE type='table'")
        assert "safe" in names


def test_references_injection_in_create_table():
    inj_table = INJECTION
    inj_col = INJECTION + "_id"
    ref_sql = str(Builder.create_table(inj_table).primary_key(inj_col, int))
    main_sql = str(
        Builder.create_table("main")
        .primary_key("id", int)
        .add_field("ref", int, references=(inj_table, inj_col))
    )
    with _MemDB.open(":memory:") as db:
        _prepare_safe(db)
        db.conn.executescript(ref_sql)
        db.conn.executescript(main_sql)
        names = db.query_column("SELECT name FROM sqlite_master WHERE type='table'")
        assert {"safe", inj_table, "main"} <= set(names)


def test_references_injection_in_alter_table():
    ref_table = INJECTION + "_r"
    ref_col = INJECTION + "_id"
    create_ref = str(Builder.create_table(ref_table).primary_key(ref_col, int))
    create_main = str(Builder.create_table("main").primary_key("id", int))
    alter = str(
        Builder.alter_table("main")
        .add_column("ref", int, references=(ref_table, ref_col))
    )
    with _MemDB.open(":memory:") as db:
        _prepare_safe(db)
        db.conn.executescript(create_ref)
        db.conn.executescript(create_main)
        db.conn.executescript(alter)
        names = db.query_column("SELECT name FROM sqlite_master WHERE type='table'")
        assert {"safe", ref_table, "main"} <= set(names)


def test_alter_table_with_injection_names():
    inj_table = INJECTION
    inj_col = INJECTION + "_c"
    create_sql = str(Builder.create_table(inj_table).primary_key("id", int))
    alter_sql = str(
        Builder.alter_table(inj_table)
        .add_column(inj_col, int)
        .rename_column(inj_col, "norm")
        .rename_to("renamed")
    )
    with _MemDB.open(":memory:") as db:
        _prepare_safe(db)
        db.conn.executescript(create_sql)
        db.conn.executescript(alter_sql)
        names = db.query_column("SELECT name FROM sqlite_master WHERE type='table'")
        assert "safe" in names and "renamed" in names


def test_drop_column_injection():
    inj_col = INJECTION
    create_sql = str(
        Builder.create_table("t")
        .primary_key("id", int)
        .add_field(inj_col, int)
    )
    drop_sql = str(Builder.alter_table("t").drop_column(inj_col))
    with _MemDB.open(":memory:") as db:
        _prepare_safe(db)
        db.conn.executescript(create_sql)
        db.conn.executescript(drop_sql)
        cols = db.query_column("SELECT name FROM pragma_table_info('t')")
        assert inj_col not in cols
        names = db.query_column("SELECT name FROM sqlite_master WHERE type='table'")
        assert "safe" in names


def test_rename_column_old_name_injection():
    inj_col = INJECTION
    create_sql = str(
        Builder.create_table("t")
        .primary_key("id", int)
        .add_field(inj_col, int)
    )
    rename_sql = str(Builder.alter_table("t").rename_column(inj_col, "clean"))
    with _MemDB.open(":memory:") as db:
        _prepare_safe(db)
        db.conn.executescript(create_sql)
        db.conn.executescript(rename_sql)
        cols = db.query_column("SELECT name FROM pragma_table_info('t')")
        assert "clean" in cols and inj_col not in cols
        names = db.query_column("SELECT name FROM sqlite_master WHERE type='table'")
        assert "safe" in names


def test_drop_table_name_injection():
    inj_table = INJECTION
    create_sql = str(Builder.create_table(inj_table).primary_key("id", int))
    drop_sql = str(Builder.drop_table(inj_table))
    with _MemDB.open(":memory:") as db:
        _prepare_safe(db)
        db.conn.executescript(create_sql)
        db.conn.executescript(drop_sql)
        names = db.query_column("SELECT name FROM sqlite_master WHERE type='table'")
        assert "safe" in names and inj_table not in names


def test_index_name_and_column_injection():
    inj_idx = INJECTION
    inj_col = INJECTION + "_c"
    inj_table = INJECTION + "_t"
    create_table_sql = str(
        Builder.create_table(inj_table)
        .primary_key("id", int)
        .add_field(inj_col, int)
    )
    index_sql = str(
        Builder.create_index(inj_table, on=inj_col, name=inj_idx)
    )
    with _MemDB.open(":memory:") as db:
        _prepare_safe(db)
        db.conn.executescript(create_table_sql)
        db.conn.executescript(index_sql)
        idxs = db.query_column("SELECT name FROM sqlite_master WHERE type='index'")
        assert inj_idx in idxs
        names = db.query_column("SELECT name FROM sqlite_master WHERE type='table'")
        assert "safe" in names and inj_table in names


def test_drop_index_name_injection():
    inj_idx = INJECTION
    create_table_sql = str(
        Builder.create_table("t")
        .primary_key("id", int)
        .add_field("c", int)
    )
    index_sql = str(Builder.create_index("t", on="c", name=inj_idx))
    drop_sql = str(Builder.drop_index("t", on="c", name=inj_idx))
    with _MemDB.open(":memory:") as db:
        _prepare_safe(db)
        db.conn.executescript(create_table_sql)
        db.conn.executescript(index_sql)
        db.conn.executescript(drop_sql)
        idxs = db.query_column("SELECT name FROM sqlite_master WHERE type='index'")
        assert inj_idx not in idxs
        names = db.query_column("SELECT name FROM sqlite_master WHERE type='table'")
        assert "safe" in names
