import sqlite3

import pytest
from scriptdb import SyncBaseDB
from scriptdb.dbbuilder import Builder, _default_literal

INJECTION = 'x"; DROP TABLE safe;--'

def test_create_table_builder_generates_sql():
    sql = (
        Builder.create_table("users")
        .primary_key("id", int)
        .add_field("name", str, not_null=True)
        .add_field("is_active", bool, default=False, not_null=True)
        .unique("name")
        .done()
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
    sql = (
        Builder.alter_table("users")
        .add_column("age", int, not_null=True, default=0)
        .rename_column("age", "user_age")
        .rename_to("people")
        .done()
    )
    expected_sql = (
        'ALTER TABLE "users" ADD COLUMN "age" INTEGER NOT NULL DEFAULT 0;'
        '\nALTER TABLE "users" RENAME COLUMN "age" TO "user_age";'
        '\nALTER TABLE "users" RENAME TO "people";'
    )
    assert sql == expected_sql


def test_drop_column_builder_generates_sql():
    sql = Builder.alter_table("users").drop_column("old").done()
    assert sql == 'ALTER TABLE "users" DROP COLUMN "old";'


def test_primary_key_autoincrement_requires_integer():
    builder = Builder.create_table("test")
    with pytest.raises(ValueError):
        builder.primary_key("id", str, auto_increment=True)


def test_primary_key_auto_increment_auto_behavior():
    sql_int = Builder.create_table("t").primary_key("id", int).done()
    assert "AUTOINCREMENT" in sql_int
    sql_text = Builder.create_table("t").primary_key("name", str).done()
    assert "AUTOINCREMENT" not in sql_text


def test_drop_table_builder_generates_sql():
    assert (
        Builder.drop_table("users").done()
        == 'DROP TABLE IF EXISTS "users";'
    )
    assert (
        Builder.drop_table("users", if_exists=False).done()
        == 'DROP TABLE "users";'
    )


def test_index_sql_generation():
    create = Builder.create_index("idx_users_name", "users", on="name")
    assert (
        create
        == 'CREATE INDEX IF NOT EXISTS "idx_users_name" ON "users" ("name");'
    )
    unique = Builder.create_index(
        "idx_users_name_age",
        "users",
        on=["name", "age"],
        unique=True,
        if_not_exists=False,
    )
    assert (
        unique
        == 'CREATE UNIQUE INDEX "idx_users_name_age" ON "users" ("name", "age");'
    )
    drop = Builder.drop_index("idx_users_name")
    assert drop == 'DROP INDEX IF EXISTS "idx_users_name";'


def test_create_index_requires_column():
    with pytest.raises(ValueError):
        Builder.create_index("idx_bad", "t", on=[])


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
    ],
)
def test_default_literal(value, literal):
    assert _default_literal(value) == literal


def test_create_table_builder_with_constraints():
    sql = (
        Builder.create_table("articles", if_not_exists=False, without_rowid=True)
        .primary_key("id", int)
        .add_field("author_id", int, not_null=True, references=("users", "id"))
        .add_field("title", str, unique=True, default="Untitled")
        .unique("author_id", "title")
        .check("length(title) > 0")
        .done()
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


def test_unsupported_python_type():
    with pytest.raises(ValueError):
        Builder.create_table("bad").add_field("data", dict)


class _MemDB(SyncBaseDB):
    def migrations(self):  # pragma: no cover - simple subclass
        return []


def test_builder_sql_executes_via_syncdb():
    create_sql = (
        Builder.create_table("users")
        .primary_key("id", int)
        .add_field("name", str)
        .done()
    )
    alter_sql = (
        Builder.alter_table("users")
        .add_column("age", int, default=0)
        .rename_column("age", "user_age")
        .rename_to("people")
        .done()
    )
    drop_sql = Builder.drop_table("people").done()

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
    create_sql = (
        Builder.create_table("t")
        .primary_key("id", int)
        .add_field("old", int)
        .done()
    )
    drop_sql = Builder.alter_table("t").drop_column("old").done()
    with _MemDB.open(":memory:") as db:
        db.conn.executescript(create_sql)
        db.conn.executescript(drop_sql)
        cols = db.query_column("SELECT name FROM pragma_table_info('t')")
        assert "old" not in cols


def test_alter_table_multiple_actions_execute():
    create_sql = (
        Builder.create_table("t")
        .primary_key("id", int)
        .add_field("name", str)
        .done()
    )
    alter_sql = (
        Builder.alter_table("t")
        .add_column("age", int, default=0)
        .add_column("temp", str)
        .drop_column("temp")
        .rename_column("name", "username")
        .rename_to("people")
        .done()
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
    create_table_sql = (
        Builder.create_table("t")
        .primary_key("id", int)
        .add_field("name", str)
        .done()
    )
    create_index_sql = Builder.create_index("idx_t_name", "t", on="name")
    drop_index_sql = Builder.drop_index("idx_t_name")
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
    sql = (
        Builder.create_table("t")
        .primary_key("id", int)
        .add_field(col_name, str)
        .done()
    )
    with _MemDB.open(":memory:") as db:
        db.conn.executescript(sql)
        cols = db.query_column("SELECT name FROM pragma_table_info('t')")
        assert col_name in cols


@pytest.mark.parametrize("bad_name", ["\x00bad"])
def test_invalid_column_names_fail(bad_name):
    sql = (
        Builder.create_table("t")
        .primary_key("id", int)
        .add_field(bad_name, int)
        .done()
    )
    with _MemDB.open(":memory:") as db:
        with pytest.raises(ValueError):
            db.conn.executescript(sql)


def _prepare_safe(db):
    db.conn.executescript('CREATE TABLE safe(id INTEGER);')


def test_create_table_name_injection():
    inj_table = INJECTION
    sql = Builder.create_table(inj_table).primary_key("id", int).done()
    with _MemDB.open(":memory:") as db:
        _prepare_safe(db)
        db.conn.executescript(sql)
        names = db.query_column("SELECT name FROM sqlite_master WHERE type='table'")
        assert "safe" in names and inj_table in names


def test_create_table_column_and_unique_injection():
    inj_col = INJECTION
    sql = (
        Builder.create_table("t")
        .primary_key("id", int)
        .add_field(inj_col, int)
        .unique(inj_col)
        .done()
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
    ref_sql = Builder.create_table(inj_table).primary_key(inj_col, int).done()
    main_sql = (
        Builder.create_table("main")
        .primary_key("id", int)
        .add_field("ref", int, references=(inj_table, inj_col))
        .done()
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
    create_ref = Builder.create_table(ref_table).primary_key(ref_col, int).done()
    create_main = Builder.create_table("main").primary_key("id", int).done()
    alter = (
        Builder.alter_table("main")
        .add_column("ref", int, references=(ref_table, ref_col))
        .done()
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
    create_sql = Builder.create_table(inj_table).primary_key("id", int).done()
    alter_sql = (
        Builder.alter_table(inj_table)
        .add_column(inj_col, int)
        .rename_column(inj_col, "norm")
        .rename_to("renamed")
        .done()
    )
    with _MemDB.open(":memory:") as db:
        _prepare_safe(db)
        db.conn.executescript(create_sql)
        db.conn.executescript(alter_sql)
        names = db.query_column("SELECT name FROM sqlite_master WHERE type='table'")
        assert "safe" in names and "renamed" in names


def test_drop_column_injection():
    inj_col = INJECTION
    create_sql = (
        Builder.create_table("t")
        .primary_key("id", int)
        .add_field(inj_col, int)
        .done()
    )
    drop_sql = Builder.alter_table("t").drop_column(inj_col).done()
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
    create_sql = (
        Builder.create_table("t")
        .primary_key("id", int)
        .add_field(inj_col, int)
        .done()
    )
    rename_sql = Builder.alter_table("t").rename_column(inj_col, "clean").done()
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
    create_sql = Builder.create_table(inj_table).primary_key("id", int).done()
    drop_sql = Builder.drop_table(inj_table).done()
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
    create_table_sql = (
        Builder.create_table(inj_table)
        .primary_key("id", int)
        .add_field(inj_col, int)
        .done()
    )
    index_sql = Builder.create_index(inj_idx, inj_table, on=inj_col)
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
    create_table_sql = (
        Builder.create_table("t")
        .primary_key("id", int)
        .add_field("c", int)
        .done()
    )
    index_sql = Builder.create_index(inj_idx, "t", on="c")
    drop_sql = Builder.drop_index(inj_idx)
    with _MemDB.open(":memory:") as db:
        _prepare_safe(db)
        db.conn.executescript(create_table_sql)
        db.conn.executescript(index_sql)
        db.conn.executescript(drop_sql)
        idxs = db.query_column("SELECT name FROM sqlite_master WHERE type='index'")
        assert inj_idx not in idxs
        names = db.query_column("SELECT name FROM sqlite_master WHERE type='table'")
        assert "safe" in names
