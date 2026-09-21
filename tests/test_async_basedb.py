import asyncio
import sqlite3
import pytest
import pytest_asyncio
from typing import Dict, Any
from scriptdb import AsyncBaseDB, Builder, run_every_seconds, run_every_queries
import scriptdb.sqlite_backend as sqlite_backend


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


class _BadSQLsDB(AsyncBaseDB):
    def migrations(self):
        return [
            {
                "name": "bad",
                "sqls": [
                    "CREATE TABLE t(id INTEGER)",
                    "INSERT INTO missing VALUES(1)",
                ],
            }
        ]


@pytest.mark.asyncio
async def test_sqls_migration_rollback(tmp_path):
    db_file = tmp_path / "bad.db"
    with pytest.raises(RuntimeError):
        async with _BadSQLsDB.open(db_file, daemonize_thread=True):
            pass
    conn = sqlite3.connect(db_file)
    try:
        cur = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='t'"
        )
        row = cur.fetchone()
        assert row is None
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_sql_migration_and_marker_are_atomic(tmp_path):
    class BadSQLDB(AsyncBaseDB):
        def migrations(self):
            return [{"name": "bad", "sql": "CREATE TABLE retained(id); INSERT INTO missing VALUES(1)"}]

    db_file = tmp_path / "bad-single-sql.db"
    with pytest.raises(RuntimeError):
        async with BadSQLDB.open(db_file, daemonize_thread=True):
            pass
    with sqlite3.connect(db_file) as conn:
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE name='retained'").fetchone() is None
        assert conn.execute("SELECT 1 FROM applied_migrations WHERE name='bad'").fetchone() is None


@pytest.mark.asyncio
async def test_failed_async_init_closes_connection(tmp_path):
    instances = []

    class BadInitDB(AsyncBaseDB):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            instances.append(self)

        def migrations(self):
            return [{"name": "bad", "sql": "SELECT * FROM missing"}]

    with pytest.raises(RuntimeError):
        await BadInitDB.open(tmp_path / "failed-init.db", daemonize_thread=True)
    assert instances[0].conn is None


@pytest.mark.asyncio
async def test_function_migration_and_marker_are_atomic(tmp_path):
    class BadFunctionDB(AsyncBaseDB):
        def migrations(self):
            return [{"name": "bad", "function": self.bad_migration}]

        async def bad_migration(self, migrations, name):
            await self.execute("CREATE TABLE retained(id INTEGER PRIMARY KEY)")
            await self.execute("INSERT INTO retained VALUES(1)")
            raise RuntimeError("stop")

    db_file = tmp_path / "bad-function.db"
    with pytest.raises(RuntimeError, match="Error while applying migration bad"):
        await BadFunctionDB.open(db_file, daemonize_thread=True)
    with sqlite3.connect(db_file) as conn:
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE name='retained'").fetchone() is None
        assert conn.execute("SELECT 1 FROM applied_migrations WHERE name='bad'").fetchone() is None


class _MultiSQLsDB(AsyncBaseDB):
    def migrations(self):
        return [
            {
                "name": "multi_sqls_single_entry",
                "sqls": [
                    (
                        "CREATE TABLE t(id INTEGER PRIMARY KEY, val TEXT);"
                        "INSERT INTO t(id, val) VALUES (1, 'a');"
                        "INSERT INTO t(id, val) VALUES (2, 'b');"
                    )
                ],
            }
        ]


@pytest.mark.asyncio
async def test_sqls_accepts_multiple_statements(tmp_path):
    db_file = tmp_path / "multisql.db"
    async with _MultiSQLsDB.open(db_file, daemonize_thread=True) as db:
        rows = await db.query_many("SELECT id, val FROM t ORDER BY id")
    assert [(row["id"], row["val"]) for row in rows] == [(1, "a"), (2, "b")]


class _AlterBuilderMultiActionDB(AsyncBaseDB):
    def migrations(self):
        return [
            {"name": "base_table", "sql": "CREATE TABLE t(id INTEGER PRIMARY KEY)"},
            {
                "name": "add_columns_in_one_sqls_entry",
                "sqls": [
                    Builder.alter_table("t").add_column("x", int).add_column("y", str)
                ],
            },
        ]


@pytest.mark.asyncio
async def test_sql_builder_with_multiple_alter_actions(tmp_path):
    db_file = tmp_path / "builder_multi.db"
    async with _AlterBuilderMultiActionDB.open(db_file, daemonize_thread=True) as db:
        cols = await db.query_column("SELECT name FROM pragma_table_info('t')")
    assert {"id", "x", "y"} <= set(cols)


class _UserWrappedTransactionDB(AsyncBaseDB):
    def migrations(self):
        return [
            {
                "name": "wrapped_sqls",
                "sqls": [
                    """
                    BEGIN;
                    CREATE TABLE t(id INTEGER PRIMARY KEY, val TEXT);
                    INSERT INTO t(val) VALUES ('a');
                    COMMIT;
                    """,
                ],
            }
        ]


@pytest.mark.asyncio
async def test_sqls_respects_user_transactions(tmp_path):
    db_file = tmp_path / "userwrapped.db"
    async with _UserWrappedTransactionDB.open(db_file, daemonize_thread=True) as db:
        rows = await db.query_many("SELECT id, val FROM t")
    assert rows[0]["val"] == "a"


class _MissingCommitDB(AsyncBaseDB):
    def migrations(self):
        return [
            {
                "name": "missing_commit",
                "sqls": [
                    """
                    BEGIN;
                    CREATE TABLE t(id INTEGER PRIMARY KEY, val TEXT);
                    INSERT INTO t(val) VALUES ('a')
                    """,
                ],
            }
        ]


@pytest.mark.asyncio
async def test_sqls_requires_closing_transaction(tmp_path):
    db_file = tmp_path / "missing_commit.db"
    with pytest.raises(ValueError):
        async with _MissingCommitDB.open(db_file, daemonize_thread=True):
            pass


class _NoSemicolonDB(AsyncBaseDB):
    def migrations(self):
        return [
            {
                "name": "no_semicolons",
                "sqls": [
                    "CREATE TABLE t(id INTEGER PRIMARY KEY, val TEXT)",
                    "INSERT INTO t(val) VALUES ('a')",
                ],
            }
        ]


@pytest.mark.asyncio
async def test_sqls_without_semicolons_still_executes(tmp_path):
    db_file = tmp_path / "nosemi.db"
    async with _NoSemicolonDB.open(db_file, daemonize_thread=True) as db:
        rows = await db.query_many("SELECT id, val FROM t")
    assert rows[0]["val"] == "a"


class _LiteralBeginDB(AsyncBaseDB):
    def migrations(self):
        return [
            {
                "name": "literal_begin",
                "sqls": [
                    "CREATE TABLE t(id INTEGER PRIMARY KEY, note TEXT DEFAULT 'begin')",
                    "INSERT INTO t(note) VALUES ('ok')",
                ],
            }
        ]


@pytest.mark.asyncio
async def test_sqls_ignores_literal_begin(tmp_path):
    db_file = tmp_path / "literalbegin.db"
    async with _LiteralBeginDB.open(db_file, daemonize_thread=True) as db:
        note = await db.query_scalar("SELECT note FROM t")
    assert note == "ok"


class _BeginInCommentsDB(AsyncBaseDB):
    def migrations(self):
        return [
            {
                "name": "begin_in_comments",
                "sqls": [
                    """
                    /* hello, its BEGIN, but it's in comment */
                    -- and this is also BEGIN
                    /* who knows, is this */ /* BEGIN */ /* or not? */
                    CREATE TABLE t(id INTEGER PRIMARY KEY);
                    """,
                ],
            }
        ]


@pytest.mark.asyncio
async def test_sqls_ignores_begin_in_comments(tmp_path):
    db_file = tmp_path / "commentbegin.db"
    async with _BeginInCommentsDB.open(db_file, daemonize_thread=True) as db:
        exists = await db.query_scalar(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='t'"
        )
    assert exists == "t"


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
async def test_insert_many_empty(db):
    await db.insert_many("t", [])


@pytest.mark.asyncio
async def test_insert_many_rolls_back_partial_batch(db):
    await db.execute("CREATE UNIQUE INDEX idx_t_x_unique ON t(x)")
    with pytest.raises(sqlite3.IntegrityError):
        await db.insert_many("t", [{"x": 1}, {"x": 1}])
    await db.insert_one("t", {"x": 2})
    assert await db.query_column("SELECT x FROM t ORDER BY x") == [2]


@pytest.mark.asyncio
async def test_insert_many_rejects_mixed_columns(db):
    with pytest.raises(ValueError, match="at least one column"):
        await db.insert_many("t", [{}])
    with pytest.raises(ValueError, match="different columns"):
        await db.insert_many("t", [{"x": 1}, {"x": 2, "y": 3}])
    with pytest.raises(ValueError, match="different columns"):
        await db.upsert_many("t", [{"id": 1, "x": 1}, {"id": 2, "y": 3}])
    count = await db.query_scalar("SELECT COUNT(*) FROM t")
    assert count == 0


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
async def test_transaction_context_manager_commits(db):
    async with db.transaction():
        await db.execute("INSERT INTO t(x) VALUES(?)", (1,))
        await db.execute("INSERT INTO t(x) VALUES(?)", (2,))
    values = await db.query_column("SELECT x FROM t ORDER BY x")
    assert values == [1, 2]


@pytest.mark.asyncio
async def test_transaction_context_manager_rolls_back_on_error(db):
    with pytest.raises(RuntimeError):
        async with db.transaction():
            await db.execute("INSERT INTO t(x) VALUES(?)", (1,))
            raise RuntimeError("boom")
    count = await db.query_scalar("SELECT COUNT(*) FROM t")
    assert count == 0


@pytest.mark.asyncio
async def test_transaction_context_manager_rejects_nesting(db):
    async def _attempt_nested():
        async with db.transaction():
            await db.execute("INSERT INTO t(x) VALUES(?)", (1,))
            async with db.transaction():
                await db.execute("INSERT INTO t(x) VALUES(?)", (2,))

    with pytest.raises(RuntimeError):
        await _attempt_nested()

    count = await db.query_scalar("SELECT COUNT(*) FROM t")
    assert count == 0


@pytest.mark.asyncio
async def test_manual_transaction_methods(db):
    await db.begin()
    await db.execute("INSERT INTO t(x) VALUES(?)", (1,))
    await db.commit()

    await db.begin()
    await db.execute("INSERT INTO t(x) VALUES(?)", (2,))
    await db.rollback()

    values = await db.query_column("SELECT x FROM t ORDER BY x")
    assert values == [1]


@pytest.mark.asyncio
async def test_manual_transactions_reject_nested_begin(db):
    await db.begin()
    with pytest.raises(RuntimeError):
        await db.begin()
    await db.rollback()


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
async def test_upsert_one_only_pk(db):
    pk = await db.upsert_one("t", {"id": 1})
    assert pk == 1
    pk = await db.upsert_one("t", {"id": 1})
    assert pk == 1
    row = await db.query_one("SELECT id, x FROM t WHERE id=?", (1,))
    assert row["id"] == 1
    assert row["x"] is None


@pytest.mark.asyncio
async def test_upsert_many(db):
    await db.upsert_many("t", [{"id": 1, "x": 1}, {"id": 2, "x": 2}])
    await db.upsert_many("t", [{"id": 1, "x": 10}, {"id": 3, "x": 3}])
    rows = await db.query_many("SELECT id, x FROM t ORDER BY id")
    assert [(r["id"], r["x"]) for r in rows] == [(1, 10), (2, 2), (3, 3)]


@pytest.mark.asyncio
async def test_upsert_reports_old_sqlite(monkeypatch, db):
    monkeypatch.setattr(sqlite_backend, "SQLITE_TOO_OLD", True)
    monkeypatch.setattr(sqlite_backend, "UPSERT_UNSUPPORTED_MESSAGE", "too old", raising=False)
    with pytest.raises(RuntimeError, match="too old"):
        await db.upsert_one("t", {"id": 1, "x": 1})


@pytest.mark.asyncio
async def test_upsert_uses_legacy_fallback_when_enabled(tmp_path, monkeypatch):
    monkeypatch.setattr(sqlite_backend, "SQLITE_TOO_OLD", True)
    db_file = tmp_path / "legacy.db"
    async with MyTestDB.open(db_file, daemonize_thread=True, legacy_sqlite_support=True) as db:
        with pytest.warns(RuntimeWarning, match="legacy_sqlite_support=True"):
            pk = await db.upsert_one("t", {"id": 1, "x": 1})
        assert pk == 1
        await db.upsert_one("t", {"id": 1, "x": 9})
        auto_pk = await db.upsert_one("t", {"x": 7})
        assert auto_pk == 2
        await db.upsert_many("t", [{"id": 1, "x": 11}, {"id": 3, "x": 3}, {"id": 4, "x": 4}])

        rows = await db.query_many("SELECT id, x FROM t ORDER BY id")
        assert [(row["id"], row["x"]) for row in rows] == [(1, 11), (2, 7), (3, 3), (4, 4)]


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
async def test_update_one_empty_dict(db):
    pk = await db.insert_one("t", {"x": 1})
    updated = await db.update_one("t", pk, {})
    assert updated == 0
    row = await db.query_one("SELECT x FROM t WHERE id=?", (pk,))
    assert row["x"] == 1


@pytest.mark.asyncio
async def test_update_one_can_change_pk_column(db):
    await db.insert_one("t", {"id": 1, "x": 1})
    assert await db.update_one("t", 1, {"id": 2}) == 1
    assert await db.query_scalar("SELECT id FROM t") == 2


@pytest.mark.asyncio
async def test_crud_quotes_identifiers(tmp_path):
    class QuotedDB(AsyncBaseDB):
        def migrations(self):
            return [{
                "name": "m",
                "sql": 'CREATE TABLE "order items" ("primary key" INTEGER PRIMARY KEY, "select" INTEGER)',
            }]

    async with QuotedDB.open(tmp_path / "quoted.db", daemonize_thread=True) as quoted_db:
        assert await quoted_db.insert_one("order items", {"primary key": 7, "select": 1}) == 7
        assert await quoted_db.upsert_one("order items", {"primary key": 7, "select": 2}) == 7
        assert await quoted_db.update_one("order items", 7, {"select": 3}) == 1
        assert await quoted_db.delete_many("order items", '"select"=?', (3,)) == 1


@pytest.mark.asyncio
async def test_foreign_keys_are_enforced(tmp_path):
    class ForeignKeyDB(AsyncBaseDB):
        def migrations(self):
            return [{
                "name": "m",
                "sql": (
                    "CREATE TABLE parent(id INTEGER PRIMARY KEY);"
                    "CREATE TABLE child(id INTEGER PRIMARY KEY, parent_id INTEGER REFERENCES parent(id));"
                ),
            }]

    async with ForeignKeyDB.open(tmp_path / "foreign.db", daemonize_thread=True) as foreign_db:
        assert await foreign_db.query_scalar("PRAGMA foreign_keys") == 1
        with pytest.raises(sqlite3.IntegrityError):
            await foreign_db.insert_one("child", {"parent_id": 999})


@pytest.mark.asyncio
async def test_concurrent_task_waits_for_transaction_owner(tmp_path):
    db = await MyTestDB.open(tmp_path / "isolated.db", daemonize_thread=True)
    inserted = asyncio.Event()
    allow_rollback = asyncio.Event()

    async def owner():
        with pytest.raises(RuntimeError, match="rollback"):
            async with db.transaction():
                await db.insert_one("t", {"id": 1, "x": 1})
                inserted.set()
                await allow_rollback.wait()
                raise RuntimeError("rollback")

    async def outsider():
        await inserted.wait()
        task = asyncio.create_task(db.insert_one("t", {"id": 2, "x": 2}))
        await asyncio.sleep(0.01)
        assert not task.done()
        allow_rollback.set()
        await task

    try:
        await asyncio.gather(owner(), outsider())
        assert await db.query_column("SELECT id FROM t") == [2]
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_update_one_missing_table(db):
    with pytest.raises(ValueError) as exc:
        await db.update_one("missing", 1, {"x": 2})
    assert "does not exist" in str(exc.value)


@pytest.mark.asyncio
async def test_query_many_gen(db):
    await db.execute_many("INSERT INTO t(x) VALUES(?)", [(1,), (2,), (3,)])
    results = []
    async for row in db.query_many_gen("SELECT x FROM t ORDER BY x"):
        results.append(row["x"])
    assert results == [1, 2, 3]


@pytest.mark.asyncio
async def test_query_postprocess_funcs(db):
    await db.execute_many("INSERT INTO t(x) VALUES(?)", [(1,), (2,), (3,)])
    first = await db.query_one(
        "SELECT x FROM t ORDER BY x LIMIT 1",
        postprocess_func=lambda row: row["x"] + 10,
    )
    assert first == 11

    doubled = await db.query_many(
        "SELECT x FROM t ORDER BY x",
        postprocess_func=lambda row: row["x"] * 2,
    )
    assert doubled == [2, 4, 6]

    gen_rows = [
        row
        async for row in db.query_many_gen(
            "SELECT x FROM t ORDER BY x",
            postprocess_func=lambda row: {"x": row["x"]},
        )
    ]
    assert [row["x"] for row in gen_rows] == [1, 2, 3]

    tripled = await db.query_column(
        "SELECT x FROM t ORDER BY x",
        postprocess_func=lambda row: (row["x"] * 3,),
    )
    assert tripled == [3, 6, 9]


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
async def test_query_dict_key_value_callables(db):
    await db.execute_many("INSERT INTO t(x) VALUES(?)", [(1,), (2,)])
    result = await db.query_dict(
        "SELECT id, x FROM t",
        key=lambda row: row["id"],
        value=lambda row: row["x"],
    )
    assert result == {1: 1, 2: 2}


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
        return [{"name": "bad", "function": 123}]


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


class AsyncBadFunctionSignatureDB(AsyncBaseDB):
    def migrations(self):
        async def func(db, migrations):
            return None

        return [{"name": "bad", "function": func}]


@pytest.mark.asyncio
async def test_async_function_requires_three_args(tmp_path):
    with pytest.raises(TypeError, match=r"parameters \(db, migrations, name\)"):
        await AsyncBadFunctionSignatureDB.open(str(tmp_path / "sig.sqlite"), daemonize_thread=True)


class AsyncBoundMethodFuncDB(AsyncBaseDB):
    def migrations(self):
        return [
            {"name": "create", "sql": "CREATE TABLE t(id TEXT PRIMARY KEY, data TEXT)"},
            {"name": "seed", "function": self._seed_data},
        ]

    async def _seed_data(self, migrations, name):
        await self.insert_many("t", [{"id": "a", "data": "alpha"}])


@pytest.mark.asyncio
async def test_async_function_accepts_bound_method(tmp_path):
    db = await AsyncBoundMethodFuncDB.open(
        str(tmp_path / "bound.sqlite"), daemonize_thread=True
    )
    try:
        values = await db.query_column("SELECT data FROM t")
        assert values == ["alpha"]
    finally:
        await db.close()


class AsyncBadBoundFunctionSignatureDB(AsyncBaseDB):
    def migrations(self):
        return [
            {"name": "create", "sql": "CREATE TABLE t(id TEXT PRIMARY KEY)"},
            {"name": "bad", "function": self._bad_seed},
        ]

    async def _bad_seed(self, migrations):
        return None


@pytest.mark.asyncio
async def test_async_bound_function_requires_two_args(tmp_path):
    with pytest.raises(TypeError, match=r"parameters \(migrations, name\)"):
        await AsyncBadBoundFunctionSignatureDB.open(
            str(tmp_path / "bound_sig.sqlite"), daemonize_thread=True
        )


class AsyncFunctionNameDB(AsyncBaseDB):
    def migrations(self):
        return [
            {"name": "create", "sql": "CREATE TABLE t(id TEXT PRIMARY KEY, data TEXT)"},
            {"name": "seed", "function": "_seed_from_name"},
        ]

    async def _seed_from_name(self, migrations, name):
        await self.insert_many("t", [{"id": "b", "data": "beta"}])


@pytest.mark.asyncio
async def test_async_function_resolves_name(tmp_path):
    db = await AsyncFunctionNameDB.open(
        str(tmp_path / "named.sqlite"), daemonize_thread=True
    )
    try:
        values = await db.query_column("SELECT data FROM t")
        assert values == ["beta"]
    finally:
        await db.close()


class AsyncMissingFunctionNameDB(AsyncBaseDB):
    def migrations(self):
        return [{"name": "missing", "function": "does_not_exist"}]


@pytest.mark.asyncio
async def test_async_function_name_missing_attribute(tmp_path):
    with pytest.raises(ValueError):
        await AsyncMissingFunctionNameDB.open(
            str(tmp_path / "missing_fn.sqlite"), daemonize_thread=True
        )


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


class RecursiveQueryHookDB(AsyncBaseDB):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.calls = 0

    def migrations(self):
        return []

    @run_every_queries(1)
    async def hook(self):
        self.calls += 1
        await self.query_scalar("SELECT 1")


@pytest.mark.asyncio
async def test_query_hook_does_not_recurse(tmp_path):
    db = await RecursiveQueryHookDB.open(tmp_path / "recursive-hook.db", daemonize_thread=True)
    try:
        await db.query_scalar("SELECT 1")
        await asyncio.sleep(0)
        assert db.calls == 1
    finally:
        await db.close()


class FailingQueryHookDB(AsyncBaseDB):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.calls = 0

    def migrations(self):
        return []

    @run_every_queries(1)
    async def hook(self):
        self.calls += 1
        raise RuntimeError("hook failure")


@pytest.mark.asyncio
async def test_query_hook_failure_is_non_fatal_and_future_hooks_run(tmp_path):
    async with FailingQueryHookDB.open(
        tmp_path / "failing-query-hook.db", daemonize_thread=True
    ) as db:
        assert await db.query_scalar("SELECT 1") == 1
        await asyncio.sleep(0)
        assert await db.query_scalar("SELECT 2") == 2
        await asyncio.sleep(0)
        assert db.calls == 2


@pytest.mark.asyncio
async def test_periodic_hook_failure_is_non_fatal(tmp_path):
    class FailingPeriodicDB(AsyncBaseDB):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.calls = 0
            self.ran_twice = asyncio.Event()

        def migrations(self):
            return []

        @run_every_seconds(0.01)
        async def tick(self):
            self.calls += 1
            if self.calls >= 2:
                self.ran_twice.set()
            raise RuntimeError("periodic failure")

    async with FailingPeriodicDB.open(
        tmp_path / "failing-periodic.db", daemonize_thread=True
    ) as db:
        await asyncio.wait_for(db.ran_twice.wait(), timeout=1)
        assert db.calls >= 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "factory, expected_type",
    [(sqlite3.Row, sqlite3.Row), (dict, dict)],
)
async def test_row_factory_controls_async_results(tmp_path, factory, expected_type):
    db_file = tmp_path / f"async_row_factory_{factory.__name__}.db"
    async with MyTestDB.open(db_file, row_factory=factory, daemonize_thread=True) as db:
        await db.insert_many("t", [{"x": 1}, {"x": 2}])

        row = await db.query_one("SELECT * FROM t ORDER BY id LIMIT 1")
        assert isinstance(row, expected_type)

        rows = await db.query_many("SELECT * FROM t ORDER BY id")
        assert all(isinstance(r, expected_type) for r in rows)

        gen_rows = [row async for row in db.query_many_gen("SELECT * FROM t ORDER BY id")]
        assert all(isinstance(r, expected_type) for r in gen_rows)

        mapping = await db.query_dict("SELECT * FROM t ORDER BY id")
        assert all(isinstance(value, expected_type) for value in mapping.values())

        scalar = await db.query_scalar("SELECT x FROM t ORDER BY id LIMIT 1")
        assert scalar == 1

        column = await db.query_column("SELECT x FROM t ORDER BY id")
        assert column == [1, 2]
