import time
from datetime import datetime, timedelta, timezone
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


def test_keys_ignores_zero_expire(db):
    db.set("temp", 1, expire_sec=0)
    assert db.keys("*") == []


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


def test_ram_index_initial_load_and_purge(tmp_path):
    db_file = tmp_path / "ram_cache.db"
    with SyncCacheDB.open(str(db_file)) as db:
        db.set("keep", "v")
        db.set("stale", "v", expire_sec=0)

    with SyncCacheDB.open(str(db_file), cache_keys_in_ram=True) as db:
        assert "keep" in db._ram_keys
        assert "stale" not in db._ram_keys

        db.set("soon", "v", expire_sec=1)
        assert "soon" in db._ram_keys
        time.sleep(1.1)
        db.set("later", "v")
        assert "soon" not in db._ram_keys


def test_ram_index_short_circuits_db_calls(tmp_path, monkeypatch):
    db_file = tmp_path / "ram_cache.db"
    with SyncCacheDB.open(str(db_file), cache_keys_in_ram=True) as db:

        def forbid_query(*args, **kwargs):
            raise AssertionError("RAM index should avoid hitting the database")

        monkeypatch.setattr(db, "query_one", forbid_query)
        monkeypatch.setattr(db, "query_scalar", forbid_query)

        assert db.get("missing") is None
        assert db.is_set("missing") is False

        db.set("present", "v")
        assert db.is_set("present") is True
        db.delete("present")
        assert db.is_set("present") is False
        assert db.get("present") is None


def test_context_manager_closes(tmp_path):
    db_file = tmp_path / "ctx.db"
    with SyncCacheDB.open(str(db_file)) as db:
        db.set("a", 1)
    assert db.initialized is False


def test_sync_cache_open_requires_existing_file(tmp_path):
    missing = tmp_path / "nope.db"
    with pytest.raises(RuntimeError):
        SyncCacheDB.open(str(missing), auto_create=False)
    with pytest.raises(RuntimeError):
        SyncCacheDB(str(missing), auto_create=False)


def test_ram_index_marks_missing_rows_on_get(tmp_path):
    db_file = tmp_path / "ram_stale.db"
    with SyncCacheDB.open(str(db_file), cache_keys_in_ram=True) as db:
        db.set("ghost", "v")
        db.execute("DELETE FROM cache WHERE key=?", ("ghost",))
        assert "ghost" in db._ram_keys

        assert db.get("ghost") is None
        assert "ghost" not in db._ram_keys


def test_ram_index_purges_expired_rows_on_get(tmp_path):
    db_file = tmp_path / "ram_expire.db"
    with SyncCacheDB.open(str(db_file), cache_keys_in_ram=True) as db:
        db.set("ttl", "v", expire_sec=10)
        past = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
        db.execute("UPDATE cache SET expire_utc=? WHERE key=?", (past, "ttl"))

        assert db.get("ttl") is None
        assert "ttl" not in db._ram_keys


def test_ram_index_del_many_and_clear(tmp_path):
    db_file = tmp_path / "ram_bulk.db"
    with SyncCacheDB.open(str(db_file), cache_keys_in_ram=True) as db:
        db.set("user:1", 1)
        db.set("user:2", 2)
        db.set("other", 3)

        removed = db.del_many("user:*")
        assert removed == 2
        assert sorted(db._ram_keys) == ["other"]

        db.clear()
        assert db._ram_keys == {}
        assert db._ram_entries == []
        assert db._ram_scores == []


def test_ram_has_key_evicts_expired_entry(tmp_path):
    db_file = tmp_path / "ram_has_key.db"
    with SyncCacheDB.open(str(db_file), cache_keys_in_ram=True) as db:
        past = datetime.now(timezone.utc) - timedelta(seconds=1)
        with db._ram_lock:
            db._ram_insert_unlocked("expired", past)

        assert db.is_set("expired") is False
        assert "expired" not in db._ram_keys


def test_ram_index_cleanup_purges_expired(tmp_path):
    db_file = tmp_path / "ram_cleanup.db"
    with SyncCacheDB.open(str(db_file), cache_keys_in_ram=True) as db:
        db.set("linger", "v", expire_sec=1)
        time.sleep(1.1)
        db._cleanup()

        assert "linger" not in db._ram_keys
