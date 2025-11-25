import sqlite3

import pytest

from scriptdb.asyncdb import AsyncBaseDB
from scriptdb.conversion import async_from_sync, sync_from_async
from scriptdb.dbbuilder import Builder
from scriptdb.syncdb import SyncBaseDB


def _schema(db_path: str):
    with sqlite3.connect(db_path) as conn:
        cur = conn.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master "
            "WHERE name!='applied_migrations' ORDER BY type, name"
        )
        return cur.fetchall()


@pytest.mark.asyncio
async def test_sync_from_async_preserves_migrations(tmp_path):
    db_async = tmp_path / "async.db"
    db_sync = tmp_path / "sync.db"

    class AsyncExample(AsyncBaseDB):
        def migrations(self):
            return [
                {
                    "name": "create_table",
                    "sql": Builder.create_table("items").primary_key("id", int).add_field("name", str),
                },
                {"name": "add_index", "sqls": ["CREATE INDEX idx_items_name ON items(name)"]},
            ]

    SyncExample = sync_from_async(AsyncExample)

    async with AsyncExample.open(str(db_async)) as adb:
        await adb.execute("INSERT INTO items(name) VALUES (?)", ("async",))

    with SyncExample.open(str(db_sync)) as sdb:
        sdb.insert_one("items", {"name": "sync"})

    assert _schema(str(db_async)) == _schema(str(db_sync))


@pytest.mark.asyncio
async def test_async_from_sync_preserves_migrations(tmp_path):
    db_sync = tmp_path / "sync_source.db"
    db_async = tmp_path / "async_from_sync.db"

    class SyncExample(SyncBaseDB):
        def migrations(self):
            return [
                {
                    "name": "create_table",
                    "sql": "CREATE TABLE widgets(id INTEGER PRIMARY KEY, value INTEGER)",
                },
                {
                    "name": "add_second_column",
                    "sql": "ALTER TABLE widgets ADD COLUMN label TEXT",
                },
            ]

    AsyncExample = async_from_sync(SyncExample)

    with SyncExample.open(str(db_sync)) as sdb:
        sdb.insert_one("widgets", {"value": 1, "label": "sync"})

    async with AsyncExample.open(str(db_async)) as adb:
        await adb.insert_one("widgets", {"value": 2, "label": "async"})

    assert _schema(str(db_sync)) == _schema(str(db_async))


@pytest.mark.asyncio
async def test_conversion_rejects_function_migrations(tmp_path):
    class AsyncWithFunction(AsyncBaseDB):
        def migrations(self):
            return [
                {"name": "callable_step", "function": self._apply_custom},
            ]

        async def _apply_custom(self, migrations, name):  # pragma: no cover - not executed
            return migrations, name

    class SyncWithFunction(SyncBaseDB):
        def migrations(self):
            return [
                {"name": "callable_step", "function": self._apply_custom},
            ]

        def _apply_custom(self, migrations, name):  # pragma: no cover - not executed
            return migrations, name

    SyncFromAsync = sync_from_async(AsyncWithFunction)
    with pytest.raises(TypeError, match="callables: migration callable_step"):
        with SyncFromAsync.open(str(tmp_path / "sync_func.db")):
            pass

    AsyncFromSync = async_from_sync(SyncWithFunction)
    with pytest.raises(TypeError, match="callables: migration callable_step"):
        async with AsyncFromSync.open(str(tmp_path / "async_func.db")):
            pass
