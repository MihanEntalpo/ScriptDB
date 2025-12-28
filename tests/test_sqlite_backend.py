import importlib
import sys
import types

import pytest


def _fake_sqlite_module(name: str, version) -> types.ModuleType:
    module = types.ModuleType(name)
    module.sqlite_version_info = version
    module.sqlite_version = ".".join(str(part) for part in version)
    module.Row = type("Row", (), {})
    module.Binary = lambda blob: blob
    module.connect = lambda *args, **kwargs: None
    return module


@pytest.fixture
def reload_backend(monkeypatch, request):
    saved_modules = {
        key: sys.modules.get(key)
        for key in ("sqlite3", "pysqlite3.dbapi2", "scriptdb.sqlite_backend")
    }

    def restore():
        for key, value in saved_modules.items():
            if value is None:
                sys.modules.pop(key, None)
            else:
                sys.modules[key] = value
        if "scriptdb.sqlite_backend" in sys.modules:
            importlib.reload(sys.modules["scriptdb.sqlite_backend"])

    request.addfinalizer(restore)

    def _reload(sqlite_module=None, pysqlite_module=...):
        sys.modules.pop("scriptdb.sqlite_backend", None)
        if sqlite_module is not ...:
            if sqlite_module is None:
                sys.modules.pop("sqlite3", None)
            else:
                sys.modules["sqlite3"] = sqlite_module
        if pysqlite_module is not ...:
            if pysqlite_module is None:
                sys.modules.pop("pysqlite3.dbapi2", None)
            else:
                sys.modules["pysqlite3.dbapi2"] = pysqlite_module

        return importlib.import_module("scriptdb.sqlite_backend")

    return _reload


def test_prefers_pysqlite_when_available(monkeypatch, reload_backend):
    pysqlite_module = _fake_sqlite_module("pysqlite3.dbapi2", (3, 39, 4))
    sqlite_module = _fake_sqlite_module("sqlite3", (3, 8, 3))

    backend = reload_backend(sqlite_module, pysqlite_module)

    assert backend.SQLITE_BACKEND == "pysqlite3"
    assert backend.sqlite3 is pysqlite_module
    assert backend.SQLITE_TOO_OLD is False
    assert sys.modules["sqlite3"] is pysqlite_module


def test_warns_on_old_sqlite(monkeypatch, reload_backend):
    sqlite_module = _fake_sqlite_module("sqlite3", (3, 7, 17))

    with pytest.warns(RuntimeWarning, match="3\\.7\\.17"):
        backend = reload_backend(sqlite_module, None)

    assert backend.SQLITE_BACKEND == "sqlite3"
    assert backend.SQLITE_TOO_OLD is True
    assert backend.sqlite3 is sqlite_module
