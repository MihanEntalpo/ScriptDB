import asyncio
import pytest
import pytest_asyncio
from typing import Dict, Any
from scriptdb import AsyncBaseDB, run_every_seconds, run_every_queries


class MyTestDB(AsyncBaseDB):
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


@pytest_asyncio.fixture
async def db(tmp_path):
    db_file = tmp_path / "test.db"
    async with MyTestDB.open(db_file, daemonize_thread=True) as db:
        yield db


@pytest.mark.asyncio
async def test_open_applies_migrations(db):
    row = await db.query_one("SELECT name FROM sqlite_master WHERE type='table' AND name='t'")
    assert row is not None
    mig = await db.query_one("SELECT name FROM applied_migrations WHERE name='create_table'")
    assert mig is not None


@pytest.mark.asyncio
async def test_sqls_migration_applied(db):
    cols = await db.query_column("SELECT name FROM pragma_table_info('t')")
    assert "y" in cols
    idx = await db.query_one(
        "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_t_y'",
    )
    assert idx is not None


@pytest.mark.asyncio
async def test_wal_mode_enabled(db):
    mode = await db.query_scalar("PRAGMA journal_mode")
    assert mode == "wal"


@pytest.mark.asyncio
async def test_wal_mode_can_be_disabled(tmp_path):
    db_file = tmp_path / "nowal.db"
    db = await MyTestDB.open(db_file, use_wal=False, daemonize_thread=True)
    try:
        mode = await db.query_scalar("PRAGMA journal_mode")
        assert mode != "wal"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_execute_and_query(db):
    await db.execute("INSERT INTO t(x) VALUES(?)", (1,))
    row = await db.query_one("SELECT x FROM t")
    assert row["x"] == 1


@pytest.mark.asyncio
async def test_execute_many_and_query_many(db):
    await db.execute_many("INSERT INTO t(x) VALUES(?)", [(1,), (2,), (3,)])
    rows = await db.query_many("SELECT x FROM t ORDER BY x")
    assert [r["x"] for r in rows] == [1, 2, 3]


@pytest.mark.asyncio
async def test_insert_one(db):
    pk = await db.insert_one("t", {"x": 5})
    row = await db.query_one("SELECT id, x FROM t WHERE id=?", (pk,))
    assert row["x"] == 5


@pytest.mark.asyncio
async def test_insert_one_with_pk(db):
    pk = await db.insert_one("t", {"id": 7, "x": 9})
    assert pk == 7
    row = await db.query_one("SELECT id, x FROM t WHERE id=?", (7,))
    assert row["x"] == 9


@pytest.mark.asyncio
async def test_insert_many(db):
    await db.insert_many("t", [{"x": 1}, {"x": 2}])
    rows = await db.query_many("SELECT x FROM t ORDER BY x")
    assert [r["x"] for r in rows] == [1, 2]


@pytest.mark.asyncio
async def test_delete_one(db):
    pk = await db.insert_one("t", {"x": 1})
    deleted = await db.delete_one("t", pk)
    assert deleted == 1
    row = await db.query_one("SELECT 1 FROM t WHERE id=?", (pk,))
    assert row is None


@pytest.mark.asyncio
async def test_delete_many(db):
    await db.insert_many("t", [{"x": 1}, {"x": 2}, {"x": 3}])
    deleted = await db.delete_many("t", "x >= ?", (2,))
    assert deleted == 2
    rows = await db.query_many("SELECT x FROM t ORDER BY x")
    assert [r["x"] for r in rows] == [1]


@pytest.mark.asyncio
async def test_upsert_one(db):
    pk = await db.upsert_one("t", {"id": 1, "x": 1})
    assert pk == 1
    pk = await db.upsert_one("t", {"id": 1, "x": 2})
    assert pk == 1
    row = await db.query_one("SELECT x FROM t WHERE id=?", (1,))
    assert row["x"] == 2


@pytest.mark.asyncio
async def test_upsert_one_without_pk(db):
    pk = await db.upsert_one("t", {"x": 1})
    assert pk == 1
    row = await db.query_one("SELECT id, x FROM t WHERE id=?", (pk,))
    assert row["x"] == 1


@pytest.mark.asyncio
async def test_upsert_many(db):
    await db.upsert_many("t", [{"id": 1, "x": 1}, {"id": 2, "x": 2}])
    await db.upsert_many("t", [{"id": 1, "x": 10}, {"id": 3, "x": 3}])
    rows = await db.query_many("SELECT id, x FROM t ORDER BY id")
    assert [(r["id"], r["x"]) for r in rows] == [(1, 10), (2, 2), (3, 3)]


@pytest.mark.asyncio
async def test_upsert_waits_for_lock(db):
    await db._upsert_lock.acquire()
    t1 = asyncio.create_task(db.upsert_one("t", {"id": 1, "x": 1}))
    await asyncio.sleep(0.01)
    count = await db.query_scalar("SELECT COUNT(*) FROM t")
    assert count == 0
    db._upsert_lock.release()
    await t1
    row = await db.query_one("SELECT x FROM t WHERE id=1")
    assert row["x"] == 1


@pytest.mark.asyncio
async def test_upsert_many_waits_for_lock(db):
    await db._upsert_lock.acquire()
    t1 = asyncio.create_task(db.upsert_many("t", [{"id": 1, "x": 1}, {"id": 2, "x": 2}]))
    await asyncio.sleep(0.01)
    count = await db.query_scalar("SELECT COUNT(*) FROM t")
    assert count == 0
    db._upsert_lock.release()
    await t1
    rows = await db.query_many("SELECT id, x FROM t ORDER BY id")
    assert [(r["id"], r["x"]) for r in rows] == [(1, 1), (2, 2)]


@pytest.mark.asyncio
async def test_update_one(db):
    pk = await db.insert_one("t", {"x": 1})
    updated = await db.update_one("t", pk, {"x": 5})
    assert updated == 1
    row = await db.query_one("SELECT x FROM t WHERE id=?", (pk,))
    assert row["x"] == 5


@pytest.mark.asyncio
async def test_query_many_gen(db):
    await db.execute_many("INSERT INTO t(x) VALUES(?)", [(1,), (2,), (3,)])
    results = []
    async for row in db.query_many_gen("SELECT x FROM t ORDER BY x"):
        results.append(row["x"])
    assert results == [1, 2, 3]


@pytest.mark.asyncio
async def test_query_one_none(db):
    row = await db.query_one("SELECT x FROM t WHERE x=?", (999,))
    assert row is None


@pytest.mark.asyncio
async def test_query_scalar(db):
    await db.execute_many("INSERT INTO t(x) VALUES(?)", [(1,), (2,)])
    count = await db.query_scalar("SELECT COUNT(*) FROM t")
    assert count == 2
    missing = await db.query_scalar("SELECT x FROM t WHERE id=?", (999,))
    assert missing is None


@pytest.mark.asyncio
async def test_query_column(db):
    await db.execute_many("INSERT INTO t(x) VALUES(?)", [(1,), (2,), (3,)])
    values = await db.query_column("SELECT x FROM t ORDER BY x")
    assert values == [1, 2, 3]


@pytest.mark.asyncio
async def test_query_dict(db):
    await db.execute_many("INSERT INTO t(x) VALUES(?)", [(1,), (2,)])

    # Default to table's primary key and store whole rows
    by_pk = await db.query_dict("SELECT id, x FROM t")
    assert set(by_pk.keys()) == {1, 2}
    assert by_pk[1]["x"] == 1

    # Explicit column names for key and value
    mapping = await db.query_dict("SELECT id, x FROM t", key="id", value="x")
    assert mapping == {1: 1, 2: 2}

    # Callables for custom key and value
    doubled = await db.query_dict(
        "SELECT id, x FROM t",
        key=lambda r: r["x"],
        value=lambda r: r["x"] * 2,
    )
    assert doubled == {1: 2, 2: 4}

    # Quoted table name still resolves primary key
    quoted = await db.query_dict('SELECT id, x FROM "t"')
    assert set(quoted.keys()) == {1, 2}


@pytest.mark.asyncio
async def test_query_dict_requires_key_when_table_unknown(db):
    with pytest.raises(ValueError) as exc:
        await db.query_dict("SELECT 1")
    assert "Cannot determine table name from sql" in str(exc.value)


@pytest.mark.asyncio
async def test_async_with_closes(tmp_path):
    db_file = tmp_path / "ctx.db"
    async with MyTestDB.open(str(db_file), daemonize_thread=True) as db:
        await db.execute("INSERT INTO t(x) VALUES(?)", (1,))
    assert db.initialized is False


@pytest.mark.asyncio
async def test_close_sets_initialized_false(tmp_path):
    db = await MyTestDB.open(str(tmp_path / "db.sqlite"), daemonize_thread=True)
    await db.close()
    assert db.initialized is False
    with pytest.raises(RuntimeError):
        await db.execute("SELECT 1")


@pytest.mark.asyncio
async def test_require_init_decorator():
    db = MyTestDB("test.db", daemonize_thread=True)
    with pytest.raises(RuntimeError):
        await db.execute("SELECT 1")


@pytest.mark.asyncio
async def test_auto_create_false_missing_file(tmp_path):
    db_file = tmp_path / "nope.sqlite"
    with pytest.raises(RuntimeError):
        await MyTestDB.open(str(db_file), auto_create=False, daemonize_thread=True)


@pytest.mark.asyncio
async def test_auto_create_false_existing_file(tmp_path):
    db_file = tmp_path / "exists.sqlite"
    db_file.touch()
    db = await MyTestDB.open(str(db_file), auto_create=False, daemonize_thread=True)
    try:
        assert db.initialized is True
    finally:
        await db.close()


class DuplicateNameDB(AsyncBaseDB):
    def migrations(self):
        return [
            {"name": "m1", "sql": "CREATE TABLE t(x INTEGER)"},
            {"name": "m1", "sql": "CREATE TABLE t2(x INTEGER)"},
        ]


@pytest.mark.asyncio
async def test_duplicate_migration_names(tmp_path):
    with pytest.raises(ValueError):
        await DuplicateNameDB.open(str(tmp_path / "dup.sqlite"), daemonize_thread=True)


class MissingNameDB(AsyncBaseDB):
    def migrations(self):
        return [{"sql": "CREATE TABLE t(x INTEGER)"}]


@pytest.mark.asyncio
async def test_missing_migration_name(tmp_path):
    with pytest.raises(ValueError):
        await MissingNameDB.open(str(tmp_path / "miss.sqlite"), daemonize_thread=True)


class NonCallableFuncDB(AsyncBaseDB):
    def migrations(self):
        return [{"name": "bad", "function": "not_callable"}]


@pytest.mark.asyncio
async def test_non_callable_function(tmp_path):
    with pytest.raises(TypeError):
        await NonCallableFuncDB.open(str(tmp_path / "bad.sqlite"), daemonize_thread=True)


class NonAsyncFuncDB(AsyncBaseDB):
    def migrations(self):
        def sync_func(db, migrations, name):
            pass

        return [{"name": "bad", "function": sync_func}]


@pytest.mark.asyncio
async def test_non_async_function(tmp_path):
    with pytest.raises(TypeError):
        await NonAsyncFuncDB.open(str(tmp_path / "bad_sync.sqlite"), daemonize_thread=True)


class AsyncFuncDB(AsyncBaseDB):
    recorded: Dict[str, Any] = {}

    def migrations(self):
        async def func(db, migrations, name):
            AsyncFuncDB.recorded = {
                "db": db,
                "migrations": migrations,
                "name": name,
            }
            await db.conn.execute("CREATE TABLE t(x INTEGER)")

        return [{"name": "good", "function": func}]


@pytest.mark.asyncio
async def test_async_function_called_with_args(tmp_path):
    db = await AsyncFuncDB.open(str(tmp_path / "good.sqlite"), daemonize_thread=True)
    try:
        assert AsyncFuncDB.recorded["db"] is db
        assert AsyncFuncDB.recorded["migrations"][0]["name"] == "good"
        assert AsyncFuncDB.recorded["name"] == "good"
    finally:
        await db.close()


class MissingSqlFuncDB(AsyncBaseDB):
    def migrations(self):
        return [{"name": "bad"}]


@pytest.mark.asyncio
async def test_missing_sql_and_function(tmp_path):
    with pytest.raises(ValueError):
        await MissingSqlFuncDB.open(str(tmp_path / "bad2.sqlite"), daemonize_thread=True)


class BadSqlsDB(AsyncBaseDB):
    def migrations(self):
        return [
            {"name": "create", "sql": "CREATE TABLE t(id INTEGER PRIMARY KEY)"},
            {"name": "bad", "sqls": "ALTER TABLE t ADD COLUMN y INTEGER"},
        ]


@pytest.mark.asyncio
async def test_sqls_must_be_sequence(tmp_path):
    with pytest.raises(TypeError):
        await BadSqlsDB.open(str(tmp_path / "bad_sqls.sqlite"), daemonize_thread=True)


class FailingMigrationDB(AsyncBaseDB):
    def migrations(self):
        return [
            {"name": "create", "sql": "CREATE TABLE t(id INTEGER PRIMARY KEY)"},
            {"name": "bad", "sql": "INSERT INTO t(nonexistent) VALUES(1)"},
        ]


@pytest.mark.asyncio
async def test_migration_error_wrapped(tmp_path):
    with pytest.raises(RuntimeError) as excinfo:
        await FailingMigrationDB.open(str(tmp_path / "fail.sqlite"), daemonize_thread=True)
    assert "Error while applying migration bad" in str(excinfo.value)


@pytest.mark.asyncio
async def test_unknown_applied_migration(tmp_path):
    db_file = tmp_path / "ghost.sqlite"
    db = await MyTestDB.open(str(db_file), daemonize_thread=True)
    await db.close()

    import sqlite3

    conn = sqlite3.connect(db_file)
    conn.execute("INSERT INTO applied_migrations(name) VALUES('ghost')")
    conn.commit()
    conn.close()

    with pytest.raises(ValueError) as exc:
        await MyTestDB.open(str(db_file), daemonize_thread=True)
    assert "ghost" in str(exc.value)
    assert "inconsistent" in str(exc.value)


class PeriodicDB(AsyncBaseDB):
    def __init__(self, path: str):
        super().__init__(path)
        self.calls = 0

    def migrations(self):
        return []

    @run_every_seconds(0.05)
    async def tick(self):
        self.calls += 1


@pytest.mark.asyncio
async def test_run_every_seconds(tmp_path):
    db = await PeriodicDB.open(str(tmp_path / "periodic.sqlite"))
    try:
        await asyncio.sleep(0.12)
        assert db.calls >= 2
    finally:
        await db.close()


class QueryHookDB(AsyncBaseDB):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.calls = 0

    def migrations(self):
        return []

    @run_every_queries(2)
    async def hook(self):
        self.calls += 1


@pytest.mark.asyncio
async def test_run_every_queries(tmp_path):
    db = await QueryHookDB.open(str(tmp_path / "hook.sqlite"), daemonize_thread=True)
    try:
        await db.query_one("SELECT 1")
        await db.query_one("SELECT 1")
        await asyncio.sleep(0)  # allow hook to run
        assert db.calls == 1
    finally:
        await db.close()
