import builtins
import sys
import pytest


def test_syncdb_works_without_aiosqlite(monkeypatch):
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "aiosqlite":
            raise ImportError("No module named 'aiosqlite'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    for mod in [m for m in list(sys.modules) if m.startswith("scriptdb") or m == "aiosqlite"]:
        monkeypatch.delitem(sys.modules, mod, raising=False)

    import scriptdb  # noqa: F401

    from scriptdb import SyncBaseDB, SyncCacheDB

    class DB(SyncBaseDB):
        def migrations(self):
            return []

    with DB.open(":memory:") as db:
        db.execute("SELECT 1")

    with SyncCacheDB.open(":memory:") as cache:
        cache.set("a", 1)
        assert cache.get("a") == 1

    with pytest.raises(ImportError):
        from scriptdb import AsyncBaseDB  # noqa: F401

    with pytest.raises(ImportError):
        from scriptdb import AsyncCacheDB  # noqa: F401
