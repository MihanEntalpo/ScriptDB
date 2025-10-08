import asyncio
import logging
from datetime import datetime, timedelta, timezone
import pytest
import pytest_asyncio
import sys
import pathlib

# add src path
sys.path.append(str(pathlib.Path(__file__).resolve().parents[1] / "src"))
from scriptdb import AsyncCacheDB


@pytest_asyncio.fixture
async def db(tmp_path):
    db_file = tmp_path / "cache.db"
    async with AsyncCacheDB.open(str(db_file), daemonize_thread=True) as db:
        yield db


@pytest.mark.asyncio
async def test_cache_table_created(db):
    row = await db.query_one("SELECT name FROM sqlite_master WHERE name='cache'")
    assert row is not None


@pytest.mark.asyncio
async def test_set_get_delete(db):
    await db.set("a", {"x": 1})
    assert await db.get("a") == {"x": 1}
    await db.delete("a")
    assert await db.get("a") is None


@pytest.mark.asyncio
async def test_is_set(db):
    await db.set("a", 1)
    assert await db.is_set("a") is True
    await db.delete("a")
    assert await db.is_set("a") is False
    await db.set("b", 1, expire_sec=0)
    assert await db.is_set("b") is False


@pytest.mark.asyncio
async def test_keys_ignores_zero_expire(db):
    await db.set("temp", 1, expire_sec=0)
    assert await db.keys("*") == []


@pytest.mark.asyncio
async def test_del_many_keys_clear(db):
    await db.set("a_1", 1, 60)
    await db.set("a_2", 2, 60)
    await db.set("b_1", 3, 60)
    keys = await db.keys("a_*")
    assert set(keys) == {"a_1", "a_2"}
    await db.del_many("a_*")
    keys = await db.keys("*")
    assert keys == ["b_1"]
    await db.clear()
    assert await db.keys("*") == []


@pytest.mark.asyncio
async def test_cache_decorator(db):
    calls = {"add": 0}

    @db.cache(expire_sec=1)
    async def add(a, b):
        calls["add"] += 1
        return a + b

    assert await add(1, 2) == 3
    assert await add(1, 2) == 3
    assert calls["add"] == 1
    await asyncio.sleep(1.1)
    assert await add(1, 2) == 3
    assert calls["add"] == 2

    calls["sq"] = 0

    @db.cache(key_func=lambda x: f"sq:{x}")
    async def square(x):
        calls["sq"] += 1
        return x * x

    assert await square(4) == 16
    await asyncio.sleep(1.1)
    assert await square(4) == 16
    assert calls["sq"] == 1


@pytest.mark.asyncio
async def test_cache_decorator_sync(db):
    calls = {"mul": 0}

    @db.cache(expire_sec=1)
    def mul(a, b):
        calls["mul"] += 1
        return a * b

    assert await mul(2, 3) == 6
    assert await mul(2, 3) == 6
    assert calls["mul"] == 1


@pytest.mark.asyncio
async def test_cleanup_expired(db):
    await db.set("temp", "v", 0)
    await db.set("perm", "v")
    count = await db.query_scalar("SELECT COUNT(*) FROM cache")
    assert count == 2
    await asyncio.sleep(5.5)
    count = await db.query_scalar("SELECT COUNT(*) FROM cache")
    assert count == 1


@pytest.mark.asyncio
async def test_async_with_closes(tmp_path):
    db_file = tmp_path / "ctx.db"
    async with AsyncCacheDB.open(str(db_file), daemonize_thread=True) as db:
        await db.set("a", 1)
    assert db.initialized is False


@pytest.mark.asyncio
async def test_ram_index_initial_load_and_purge(tmp_path):
    db_file = tmp_path / "ram_cache.db"
    async with AsyncCacheDB.open(str(db_file), daemonize_thread=True) as db:
        await db.set("keep", "v")
        await db.set("stale", "v", expire_sec=0)

    async with AsyncCacheDB.open(
        str(db_file), daemonize_thread=True, cache_keys_in_ram=True
    ) as db:
        assert "keep" in db._ram_keys
        assert "stale" not in db._ram_keys

        await db.set("soon", "v", expire_sec=1)
        assert "soon" in db._ram_keys
        await asyncio.sleep(1.1)
        await db.set("later", "v")
        assert "soon" not in db._ram_keys


@pytest.mark.asyncio
async def test_ram_index_short_circuits_db_calls(tmp_path, monkeypatch):
    db_file = tmp_path / "ram_cache.db"
    async with AsyncCacheDB.open(
        str(db_file), daemonize_thread=True, cache_keys_in_ram=True
    ) as db:

        async def forbid_query(*args, **kwargs):
            raise AssertionError("RAM index should avoid hitting the database")

        monkeypatch.setattr(db, "query_one", forbid_query)
        monkeypatch.setattr(db, "query_scalar", forbid_query)

        assert await db.get("missing") is None
        assert await db.is_set("missing") is False

        await db.set("present", "v")
        assert await db.is_set("present") is True
        await db.delete("present")
        assert await db.is_set("present") is False
        assert await db.get("present") is None


@pytest.mark.asyncio
async def test_async_cache_open_requires_existing_file(tmp_path):
    missing = tmp_path / "nope.db"
    with pytest.raises(RuntimeError):
        AsyncCacheDB.open(str(missing), auto_create=False)
    with pytest.raises(RuntimeError):
        AsyncCacheDB(str(missing), auto_create=False)


@pytest.mark.asyncio
async def test_ram_index_marks_missing_rows_on_get(tmp_path):
    db_file = tmp_path / "ram_stale.db"
    async with AsyncCacheDB.open(
        str(db_file), daemonize_thread=True, cache_keys_in_ram=True
    ) as db:
        await db.set("ghost", "v")
        await db.execute("DELETE FROM cache WHERE key=?", ("ghost",))
        assert "ghost" in db._ram_keys

        assert await db.get("ghost") is None
        assert "ghost" not in db._ram_keys


@pytest.mark.asyncio
async def test_ram_index_purges_expired_rows_on_get(tmp_path):
    db_file = tmp_path / "ram_expire.db"
    async with AsyncCacheDB.open(
        str(db_file), daemonize_thread=True, cache_keys_in_ram=True
    ) as db:
        await db.set("ttl", "v", expire_sec=10)
        past = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
        await db.execute("UPDATE cache SET expire_utc=? WHERE key=?", (past, "ttl"))

        assert await db.get("ttl") is None
        assert "ttl" not in db._ram_keys


@pytest.mark.asyncio
async def test_ram_index_del_many_and_clear(tmp_path):
    db_file = tmp_path / "ram_bulk.db"
    async with AsyncCacheDB.open(
        str(db_file), daemonize_thread=True, cache_keys_in_ram=True
    ) as db:
        await db.set("user:1", 1)
        await db.set("user:2", 2)
        await db.set("other", 3)

        removed = await db.del_many("user:*")
        assert removed == 2
        assert sorted(db._ram_keys) == ["other"]

        await db.clear()
        assert db._ram_keys == {}
        assert db._ram_entries == []
        assert db._ram_scores == []


@pytest.mark.asyncio
async def test_ram_index_reload_logs_memory_usage(tmp_path, caplog):
    db_file = tmp_path / "ram_log.db"
    async with AsyncCacheDB.open(str(db_file), daemonize_thread=True) as db:
        await db.set("keep", "value")

    caplog.set_level(logging.DEBUG, logger="scriptdb._cache_index")
    async with AsyncCacheDB.open(
        str(db_file), daemonize_thread=True, cache_keys_in_ram=True
    ) as db:
        assert "Reloaded RAM cache index" in caplog.text
        assert "keep" in db._ram_keys


@pytest.mark.asyncio
async def test_ram_has_key_evicts_expired_entry(tmp_path):
    db_file = tmp_path / "ram_has_key.db"
    async with AsyncCacheDB.open(
        str(db_file), daemonize_thread=True, cache_keys_in_ram=True
    ) as db:
        past = datetime.now(timezone.utc) - timedelta(seconds=1)
        with db._ram_lock:
            db._ram_insert_unlocked("expired", past)

        assert await db.is_set("expired") is False
        assert "expired" not in db._ram_keys
