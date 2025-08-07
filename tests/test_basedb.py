import asyncio
import pytest
import pytest_asyncio
import sys
import pathlib

# Add the src directory to sys.path so we can import the package
sys.path.append(str(pathlib.Path(__file__).resolve().parents[1] / 'src'))
from scriptdb import BaseDB, run_every_seconds, run_every_queries


class MyTestDB(BaseDB):
    def migrations(self):
        return [
            {"name": "create_table", "sql": "CREATE TABLE t(id INTEGER PRIMARY KEY, x INTEGER)"}
        ]


@pytest_asyncio.fixture
async def db(tmp_path):
    db_file = tmp_path / "test.db"
    db = await MyTestDB.open(str(db_file))
    try:
        yield db
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_open_applies_migrations(db):
    row = await db.query_one(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='t'"
    )
    assert row is not None
    mig = await db.query_one(
        "SELECT name FROM applied_migrations WHERE name='create_table'"
    )
    assert mig is not None


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
async def test_upsert_many(db):
    await db.upsert_many("t", [{"id": 1, "x": 1}, {"id": 2, "x": 2}])
    await db.upsert_many("t", [{"id": 1, "x": 10}, {"id": 3, "x": 3}])
    rows = await db.query_many("SELECT id, x FROM t ORDER BY id")
    assert [(r["id"], r["x"]) for r in rows] == [(1, 10), (2, 2), (3, 3)]


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
async def test_close_sets_initialized_false(tmp_path):
    db = await MyTestDB.open(str(tmp_path / "db.sqlite"))
    await db.close()
    assert db.initialized is False
    with pytest.raises(RuntimeError):
        await db.execute("SELECT 1")


@pytest.mark.asyncio
async def test_require_init_decorator():
    db = MyTestDB("test.db")
    with pytest.raises(RuntimeError):
        await db.execute("SELECT 1")


class DuplicateNameDB(BaseDB):
    def migrations(self):
        return [
            {"name": "m1", "sql": "CREATE TABLE t(x INTEGER)"},
            {"name": "m1", "sql": "CREATE TABLE t2(x INTEGER)"},
        ]


@pytest.mark.asyncio
async def test_duplicate_migration_names(tmp_path):
    with pytest.raises(ValueError):
        await DuplicateNameDB.open(str(tmp_path / "dup.sqlite"))


class MissingNameDB(BaseDB):
    def migrations(self):
        return [{"sql": "CREATE TABLE t(x INTEGER)"}]


@pytest.mark.asyncio
async def test_missing_migration_name(tmp_path):
    with pytest.raises(ValueError):
        await MissingNameDB.open(str(tmp_path / "miss.sqlite"))


class NonCallableFuncDB(BaseDB):
    def migrations(self):
        return [{"name": "bad", "function": "not_callable"}]


@pytest.mark.asyncio
async def test_non_callable_function(tmp_path):
    with pytest.raises(ValueError):
        await NonCallableFuncDB.open(str(tmp_path / "bad.sqlite"))


class MissingSqlFuncDB(BaseDB):
    def migrations(self):
        return [{"name": "bad"}]


@pytest.mark.asyncio
async def test_missing_sql_and_function(tmp_path):
    with pytest.raises(ValueError):
        await MissingSqlFuncDB.open(str(tmp_path / "bad2.sqlite"))


class PeriodicDB(BaseDB):
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


class QueryHookDB(BaseDB):
    def __init__(self, path: str):
        super().__init__(path)
        self.calls = 0

    def migrations(self):
        return []

    @run_every_queries(2)
    async def hook(self):
        self.calls += 1


@pytest.mark.asyncio
async def test_run_every_queries(tmp_path):
    db = await QueryHookDB.open(str(tmp_path / "hook.sqlite"))
    try:
        await db.query_one("SELECT 1")
        await db.query_one("SELECT 1")
        await asyncio.sleep(0)  # allow hook to run
        assert db.calls == 1
    finally:
        await db.close()
