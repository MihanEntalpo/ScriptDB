import asyncio
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
