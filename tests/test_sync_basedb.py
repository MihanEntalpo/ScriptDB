import time
import pytest
import sys
import pathlib
from typing import Dict, Any
import threading

# Add the src directory to sys.path so we can import the package
sys.path.append(str(pathlib.Path(__file__).resolve().parents[1] / "src"))
from scriptdb import SyncBaseDB, run_every_seconds, run_every_queries


class MyTestDB(SyncBaseDB):
    def migrations(self):
        return [
            {
                "name": "create_table",
                "sql": "CREATE TABLE t(id INTEGER PRIMARY KEY, x INTEGER)",
            },
            {
                "name": "add_y_and_index",
                "sqls": [
                    "ALTER TABLE t ADD COLUMN y INTEGER",
                    "CREATE INDEX idx_t_y ON t(y)",
                ],
            },
        ]


@pytest.fixture
def db(tmp_path):
    db_file = tmp_path / "test.db"
    with MyTestDB.open(db_file) as db:
        yield db


def test_open_applies_migrations(db):
    row = db.query_one("SELECT name FROM sqlite_master WHERE type='table' AND name='t'")
    assert row is not None
    mig = db.query_one("SELECT name FROM applied_migrations WHERE name='create_table'")
    assert mig is not None


def test_sqls_migration_applied(db):
    cols = db.query_column("SELECT name FROM pragma_table_info('t')")
    assert "y" in cols
    idx = db.query_one(
        "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_t_y'",
    )
    assert idx is not None


def test_wal_mode_enabled(db):
    mode = db.query_scalar("PRAGMA journal_mode")
    assert mode == "wal"


def test_wal_mode_can_be_disabled(tmp_path):
    db_file = tmp_path / "nowal.db"
    ctx = MyTestDB.open(db_file, use_wal=False)
    db = ctx._open()
    try:
        mode = db.query_scalar("PRAGMA journal_mode")
        assert mode != "wal"
    finally:
        db.close()


def test_execute_and_query(db):
    db.execute("INSERT INTO t(x) VALUES(?)", (1,))
    row = db.query_one("SELECT x FROM t")
    assert row["x"] == 1


def test_execute_many_and_query_many(db):
    db.execute_many("INSERT INTO t(x) VALUES(?)", [(1,), (2,), (3,)])
    rows = db.query_many("SELECT x FROM t ORDER BY x")
    assert [r["x"] for r in rows] == [1, 2, 3]


def test_insert_one(db):
    pk = db.insert_one("t", {"x": 5})
    row = db.query_one("SELECT id, x FROM t WHERE id=?", (pk,))
    assert row["x"] == 5


def test_insert_one_with_pk(db):
    pk = db.insert_one("t", {"id": 7, "x": 9})
    assert pk == 7
    row = db.query_one("SELECT id, x FROM t WHERE id=?", (7,))
    assert row["x"] == 9


def test_insert_many(db):
    db.insert_many("t", [{"x": 1}, {"x": 2}])
    rows = db.query_many("SELECT x FROM t ORDER BY x")
    assert [r["x"] for r in rows] == [1, 2]


def test_delete_one(db):
    pk = db.insert_one("t", {"x": 1})
    deleted = db.delete_one("t", pk)
    assert deleted == 1
    row = db.query_one("SELECT 1 FROM t WHERE id=?", (pk,))
    assert row is None


def test_delete_many(db):
    db.insert_many("t", [{"x": 1}, {"x": 2}, {"x": 3}])
    deleted = db.delete_many("t", "x >= ?", (2,))
    assert deleted == 2
    rows = db.query_many("SELECT x FROM t ORDER BY x")
    assert [r["x"] for r in rows] == [1]


def test_upsert_one(db):
    pk = db.upsert_one("t", {"id": 1, "x": 1})
    assert pk == 1
    pk = db.upsert_one("t", {"id": 1, "x": 2})
    assert pk == 1
    row = db.query_one("SELECT x FROM t WHERE id=?", (1,))
    assert row["x"] == 2


def test_upsert_one_without_pk(db):
    pk = db.upsert_one("t", {"x": 1})
    assert pk == 1
    row = db.query_one("SELECT id, x FROM t WHERE id=?", (pk,))
    assert row["x"] == 1


def test_upsert_many(db):
    db.upsert_many("t", [{"id": 1, "x": 1}, {"id": 2, "x": 2}])
    db.upsert_many("t", [{"id": 1, "x": 10}, {"id": 3, "x": 3}])
    rows = db.query_many("SELECT id, x FROM t ORDER BY id")
    assert [(r["id"], r["x"]) for r in rows] == [(1, 10), (2, 2), (3, 3)]


def test_upsert_waits_for_lock(db):
    db._upsert_lock.acquire()
    t1 = threading.Thread(target=lambda: db.upsert_one("t", {"id": 1, "x": 1}))
    t1.start()
    time.sleep(0.01)
    count = db.query_scalar("SELECT COUNT(*) FROM t")
    assert count == 0
    db._upsert_lock.release()
    t1.join()
    row = db.query_one("SELECT x FROM t WHERE id=1")
    assert row["x"] == 1


def test_upsert_many_waits_for_lock(db):
    db._upsert_lock.acquire()
    t1 = threading.Thread(target=lambda: db.upsert_many("t", [{"id": 1, "x": 1}, {"id": 2, "x": 2}]))
    t1.start()
    time.sleep(0.01)
    count = db.query_scalar("SELECT COUNT(*) FROM t")
    assert count == 0
    db._upsert_lock.release()
    t1.join()
    rows = db.query_many("SELECT id, x FROM t ORDER BY id")
    assert [(r["id"], r["x"]) for r in rows] == [(1, 1), (2, 2)]


def test_update_one(db):
    pk = db.insert_one("t", {"x": 1})
    updated = db.update_one("t", pk, {"x": 5})
    assert updated == 1
    row = db.query_one("SELECT x FROM t WHERE id=?", (pk,))
    assert row["x"] == 5


def test_query_many_gen(db):
    db.execute_many("INSERT INTO t(x) VALUES(?)", [(1,), (2,), (3,)])
    results = []
    for row in db.query_many_gen("SELECT x FROM t ORDER BY x"):
        results.append(row["x"])
    assert results == [1, 2, 3]


def test_query_one_none(db):
    row = db.query_one("SELECT x FROM t WHERE x=?", (999,))
    assert row is None


def test_query_scalar(db):
    db.execute_many("INSERT INTO t(x) VALUES(?)", [(1,), (2,)])
    count = db.query_scalar("SELECT COUNT(*) FROM t")
    assert count == 2
    missing = db.query_scalar("SELECT x FROM t WHERE id=?", (999,))
    assert missing is None


def test_query_column(db):
    db.execute_many("INSERT INTO t(x) VALUES(?)", [(1,), (2,), (3,)])
    values = db.query_column("SELECT x FROM t ORDER BY x")
    assert values == [1, 2, 3]


def test_query_dict(db):
    db.execute_many("INSERT INTO t(x) VALUES(?)", [(1,), (2,)])

    # Default to table's primary key and store whole rows
    by_pk = db.query_dict("SELECT id, x FROM t")
    assert set(by_pk.keys()) == {1, 2}
    assert by_pk[1]["x"] == 1

    # Explicit column names for key and value
    mapping = db.query_dict("SELECT id, x FROM t", key="id", value="x")
    assert mapping == {1: 1, 2: 2}

    # Callables for custom key and value
    doubled = db.query_dict(
        "SELECT id, x FROM t",
        key=lambda r: r["x"],
        value=lambda r: r["x"] * 2,
    )
    assert doubled == {1: 2, 2: 4}

    # Quoted table name still resolves primary key
    quoted = db.query_dict('SELECT id, x FROM "t"')
    assert set(quoted.keys()) == {1, 2}


def test_query_dict_requires_key_when_table_unknown(db):
    with pytest.raises(ValueError) as exc:
        db.query_dict("SELECT 1")
    assert "Cannot determine table name from sql" in str(exc.value)


def test_context_manager_closes(tmp_path):
    db_file = tmp_path / "ctx.db"
    with MyTestDB.open(str(db_file)) as db:
        db.execute("INSERT INTO t(x) VALUES(?)", (1,))
    assert db.initialized is False


def test_close_sets_initialized_false(tmp_path):
    db = MyTestDB.open(str(tmp_path / "db.sqlite"))._open()
    db.close()
    assert db.initialized is False
    with pytest.raises(RuntimeError):
        db.execute("SELECT 1")


def test_require_init_decorator():
    db = MyTestDB("test.db")
    with pytest.raises(RuntimeError):
        db.execute("SELECT 1")


def test_auto_create_false_missing_file(tmp_path):
    db_file = tmp_path / "nope.sqlite"
    with pytest.raises(RuntimeError):
        MyTestDB.open(str(db_file), auto_create=False)._open()


def test_auto_create_false_existing_file(tmp_path):
    db_file = tmp_path / "exists.sqlite"
    db_file.touch()
    ctx = MyTestDB.open(str(db_file), auto_create=False)
    db = ctx._open()
    try:
        assert db.initialized is True
    finally:
        db.close()


class DuplicateNameDB(SyncBaseDB):
    def migrations(self):
        return [
            {"name": "m1", "sql": "CREATE TABLE t(x INTEGER)"},
            {"name": "m1", "sql": "CREATE TABLE t2(x INTEGER)"},
        ]


def test_duplicate_migration_names(tmp_path):
    with pytest.raises(ValueError):
        DuplicateNameDB.open(str(tmp_path / "dup.sqlite"))._open()


class MissingNameDB(SyncBaseDB):
    def migrations(self):
        return [{"sql": "CREATE TABLE t(x INTEGER)"}]


def test_missing_migration_name(tmp_path):
    with pytest.raises(ValueError):
        MissingNameDB.open(str(tmp_path / "miss.sqlite"))._open()


class NonCallableFuncDB(SyncBaseDB):
    def migrations(self):
        return [{"name": "bad", "function": "not_callable"}]


def test_non_callable_function(tmp_path):
    with pytest.raises(TypeError):
        NonCallableFuncDB.open(str(tmp_path / "bad.sqlite"))._open()


class AsyncMigrationDB(SyncBaseDB):
    def migrations(self):
        async def async_func(db, migrations, name):
            pass

        return [{"name": "bad", "function": async_func}]


def test_async_function_invalid(tmp_path):
    with pytest.raises(TypeError):
        AsyncMigrationDB.open(str(tmp_path / "bad_sync.sqlite"))._open()


class FuncDB(SyncBaseDB):
    recorded: Dict[str, Any] = {}

    def migrations(self):
        def func(db, migrations, name):
            FuncDB.recorded = {
                "db": db,
                "migrations": migrations,
                "name": name,
            }
            db.conn.execute("CREATE TABLE t(x INTEGER)")

        return [{"name": "good", "function": func}]


def test_function_called_with_args(tmp_path):
    ctx = FuncDB.open(str(tmp_path / "good.sqlite"))
    db = ctx._open()
    try:
        assert FuncDB.recorded["db"] is db
        assert FuncDB.recorded["migrations"][0]["name"] == "good"
        assert FuncDB.recorded["name"] == "good"
    finally:
        db.close()


class MissingSqlFuncDB(SyncBaseDB):
    def migrations(self):
        return [{"name": "bad"}]


def test_missing_sql_and_function(tmp_path):
    with pytest.raises(ValueError):
        MissingSqlFuncDB.open(str(tmp_path / "bad2.sqlite"))._open()


class BadSqlsDB(SyncBaseDB):
    def migrations(self):
        return [
            {"name": "create", "sql": "CREATE TABLE t(id INTEGER PRIMARY KEY)"},
            {"name": "bad", "sqls": "ALTER TABLE t ADD COLUMN y INTEGER"},
        ]


def test_sqls_must_be_sequence(tmp_path):
    with pytest.raises(TypeError):
        BadSqlsDB.open(str(tmp_path / "bad_sqls.sqlite"))._open()


class FailingMigrationDB(SyncBaseDB):
    def migrations(self):
        return [
            {"name": "create", "sql": "CREATE TABLE t(id INTEGER PRIMARY KEY)"},
            {"name": "bad", "sql": "INSERT INTO t(nonexistent) VALUES(1)"},
        ]


def test_migration_error_wrapped(tmp_path):
    with pytest.raises(RuntimeError) as excinfo:
        FailingMigrationDB.open(str(tmp_path / "fail.sqlite"))._open()
    assert "Error while applying migration bad" in str(excinfo.value)


def test_unknown_applied_migration(tmp_path):
    db_file = tmp_path / "ghost.sqlite"
    ctx = MyTestDB.open(str(db_file))
    db = ctx._open()
    db.close()

    import sqlite3

    conn = sqlite3.connect(db_file)
    conn.execute("INSERT INTO applied_migrations(name) VALUES('ghost')")
    conn.commit()
    conn.close()

    with pytest.raises(ValueError) as exc:
        MyTestDB.open(str(db_file))._open()
    assert "ghost" in str(exc.value)
    assert "inconsistent" in str(exc.value)


class PeriodicDB(SyncBaseDB):
    def __init__(self, path: str):
        super().__init__(path)
        self.calls = 0

    def migrations(self):
        return []

    @run_every_seconds(0.05)
    def tick(self):
        self.calls += 1


def test_run_every_seconds(tmp_path):
    ctx = PeriodicDB.open(str(tmp_path / "periodic.sqlite"))
    db = ctx._open()
    try:
        time.sleep(0.12)
        assert db.calls >= 2
    finally:
        db.close()


class QueryHookDB(SyncBaseDB):
    def __init__(self, path: str):
        super().__init__(path)
        self.calls = 0

    def migrations(self):
        return []

    @run_every_queries(2)
    def hook(self):
        self.calls += 1


def test_run_every_queries(tmp_path):
    ctx = QueryHookDB.open(str(tmp_path / "hook.sqlite"))
    db = ctx._open()
    try:
        db.query_one("SELECT 1")
        db.query_one("SELECT 1")
        time.sleep(0)
        assert db.calls == 1
    finally:
        db.close()
