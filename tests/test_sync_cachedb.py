import time
import pytest
import sys
import pathlib

# add src path
sys.path.append(str(pathlib.Path(__file__).resolve().parents[1] / "src"))
from scriptdb import SyncCacheDB


@pytest.fixture
def db(tmp_path):
    db_file = tmp_path / "cache.db"
    with SyncCacheDB.open(str(db_file)) as db:
        yield db


def test_cache_table_created(db):
    row = db.query_one("SELECT name FROM sqlite_master WHERE name='cache'")
    assert row is not None


def test_set_get_delete(db):
    db.set("a", {"x": 1})
    assert db.get("a") == {"x": 1}
    db.delete("a")
    assert db.get("a") is None


def test_is_set(db):
    db.set("a", 1)
    assert db.is_set("a") is True
    db.delete("a")
    assert db.is_set("a") is False
    db.set("b", 1, expire_sec=0)
    assert db.is_set("b") is False


def test_del_many_keys_clear(db):
    db.set("a_1", 1, 60)
    db.set("a_2", 2, 60)
    db.set("b_1", 3, 60)
    keys = db.keys("a_*")
    assert set(keys) == {"a_1", "a_2"}
    db.del_many("a_*")
    keys = db.keys("*")
    assert keys == ["b_1"]
    db.clear()
    assert db.keys("*") == []


def test_cache_decorator(db):
    calls = {"add": 0}

    @db.cache(expire_sec=1)
    async def add(a, b):
        calls["add"] += 1
        return a + b

    assert add(1, 2) == 3
    assert add(1, 2) == 3
    assert calls["add"] == 1
    time.sleep(1.1)
    assert add(1, 2) == 3
    assert calls["add"] == 2

    calls["sq"] = 0

    @db.cache(key_func=lambda x: f"sq:{x}")
    async def square(x):
        calls["sq"] += 1
        return x * x

    assert square(4) == 16
    time.sleep(1.1)
    assert square(4) == 16
    assert calls["sq"] == 1


def test_cache_decorator_sync(db):
    calls = {"mul": 0}

    @db.cache(expire_sec=1)
    def mul(a, b):
        calls["mul"] += 1
        return a * b

    assert mul(2, 3) == 6
    assert mul(2, 3) == 6
    assert calls["mul"] == 1


def test_cleanup_expired(db):
    db.set("temp", "v", 0)
    db.set("perm", "v")
    count = db.query_scalar("SELECT COUNT(*) FROM cache")
    assert count == 2
    time.sleep(6)
    count = db.query_scalar("SELECT COUNT(*) FROM cache")
    assert count == 1


def test_context_manager_closes(tmp_path):
    db_file = tmp_path / "ctx.db"
    with SyncCacheDB.open(str(db_file)) as db:
        db.set("a", 1)
    assert db.initialized is False
