import importlib
import importlib.metadata
import sys
import types
import warnings

import pytest


def _fake_sqlite_module(name: str, version) -> types.ModuleType:
    module = types.ModuleType(name)
    module.sqlite_version_info = version
    module.sqlite_version = ".".join(str(part) for part in version)
    module.Row = type("Row", (), {})
    module.Binary = lambda blob: blob
    module.connect = lambda *args, **kwargs: None
    module.__file__ = f"/site-packages/{name.replace('.', '/')}.py"
    return module


@pytest.fixture
def reload_backend(monkeypatch, request):
    saved_modules = {
        key: sys.modules.get(key)
        for key in ("sqlite3", "pysqlite3", "pysqlite3.dbapi2", "scriptdb.sqlite_backend")
    }

    def restore():
        monkeypatch.undo()
        for key, value in saved_modules.items():
            if value is None:
                sys.modules.pop(key, None)
            else:
                sys.modules[key] = value
        if "scriptdb.sqlite_backend" in sys.modules:
            importlib.reload(sys.modules["scriptdb.sqlite_backend"])
            if "scriptdb" in sys.modules:
                sys.modules["scriptdb"].sqlite_backend = sys.modules["scriptdb.sqlite_backend"]

    request.addfinalizer(restore)

    def _reload(sqlite_module=None, pysqlite_module=..., distributions=()):
        def distribution(name):
            if name in distributions:
                return object()
            raise importlib.metadata.PackageNotFoundError(name)

        monkeypatch.setattr(importlib.metadata, "distribution", distribution)
        sys.modules.pop("scriptdb.sqlite_backend", None)
        if sqlite_module is not ...:
            if sqlite_module is None:
                sys.modules.pop("sqlite3", None)
            else:
                sys.modules["sqlite3"] = sqlite_module
        if pysqlite_module is not ...:
            if pysqlite_module is None:
                sys.modules.pop("pysqlite3.dbapi2", None)
                sys.modules["pysqlite3"] = None
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


def test_old_pysqlite_reports_selected_module_and_conflicting_distributions(reload_backend):
    pysqlite_module = _fake_sqlite_module("pysqlite3.dbapi2", (3, 22, 0))

    with pytest.warns(RuntimeWarning) as recorded:
        backend = reload_backend(pysqlite_module=pysqlite_module, distributions=("pysqlite3", "pysqlite3-binary"))

    warning = str(recorded[0].message)
    assert backend.SQLITE_BACKEND_INFO.pysqlite_distribution_installed is True
    assert backend.SQLITE_BACKEND_INFO.pysqlite_binary_distribution_installed is True
    assert "Selected SQLite backend: pysqlite3" in warning
    assert "SQLite version: 3.22.0" in warning
    assert "Both `pysqlite3` and `pysqlite3-binary` are installed" in warning
    assert "pip uninstall -y pysqlite3 pysqlite3-binary" in warning
    assert "--only-binary=:all: pysqlite3-binary" in warning


def test_old_ordinary_pysqlite_recommends_binary_distribution(reload_backend):
    pysqlite_module = _fake_sqlite_module("pysqlite3.dbapi2", (3, 23, 0))

    with pytest.warns(RuntimeWarning, match="compiled against an old system SQLite") as recorded:
        reload_backend(pysqlite_module=pysqlite_module, distributions=("pysqlite3",))

    assert "pip uninstall -y pysqlite3\n" in str(recorded[0].message)
    assert "pysqlite3-binary" in str(recorded[0].message)


def test_old_binary_only_pysqlite_recommends_clean_reinstall(reload_backend):
    pysqlite_module = _fake_sqlite_module("pysqlite3.dbapi2", (3, 23, 0))

    with pytest.warns(RuntimeWarning) as recorded:
        reload_backend(pysqlite_module=pysqlite_module, distributions=("pysqlite3-binary",))

    warning = str(recorded[0].message)
    assert "Both `pysqlite3`" not in warning
    assert "stale or overwritten files" in warning
    assert "pip uninstall -y pysqlite3 pysqlite3-binary" in warning


def test_installed_pysqlite_import_error_is_reported_and_falls_back(reload_backend):
    sqlite_module = _fake_sqlite_module("sqlite3", (3, 40, 0))

    with pytest.warns(RuntimeWarning, match="Falling back to stdlib sqlite3") as recorded:
        backend = reload_backend(sqlite_module, None, distributions=("pysqlite3-binary",))

    warning = str(recorded[0].message)
    assert "`pysqlite3-binary` appears to be installed" in warning
    assert "pysqlite3.dbapi2" in warning
    assert backend.SQLITE_BACKEND == "sqlite3"
    assert backend.SQLITE_BACKEND_INFO.pysqlite_import_error is not None


def test_modern_pysqlite_does_not_warn(reload_backend):
    pysqlite_module = _fake_sqlite_module("pysqlite3.dbapi2", (3, 45, 0))

    with warnings.catch_warnings(record=True) as recorded:
        warnings.simplefilter("always")
        backend = reload_backend(pysqlite_module=pysqlite_module, distributions=("pysqlite3-binary",))

    assert not [warning for warning in recorded if warning.category is RuntimeWarning]
    assert backend.SQLITE_BACKEND_INFO.sqlite_version_info == (3, 45, 0)


def test_metadata_failure_does_not_affect_backend_selection(monkeypatch, reload_backend):
    pysqlite_module = _fake_sqlite_module("pysqlite3.dbapi2", (3, 45, 0))

    def broken_distribution(name):
        raise RuntimeError("metadata backend unavailable")

    backend = reload_backend(pysqlite_module=pysqlite_module)
    monkeypatch.setattr(importlib.metadata, "distribution", broken_distribution)

    assert backend.SQLITE_BACKEND == "pysqlite3"
    assert backend._distribution_installed("pysqlite3") is False
    assert backend._distribution_installed("pysqlite3-binary") is False


def test_import_failure_reports_both_installed_distributions(reload_backend):
    sqlite_module = _fake_sqlite_module("sqlite3", (3, 40, 0))

    with pytest.warns(RuntimeWarning) as recorded:
        reload_backend(sqlite_module, None, distributions=("pysqlite3", "pysqlite3-binary"))

    warning = str(recorded[0].message)
    assert "`pysqlite3` and `pysqlite3-binary` appears to be installed" in warning


def test_falls_back_from_dbapi2_to_plain_pysqlite(monkeypatch, reload_backend):
    pysqlite_module = _fake_sqlite_module("pysqlite3", (3, 45, 0))
    stdlib_module = _fake_sqlite_module("sqlite3", (3, 8, 3))
    backend = reload_backend(distributions=())

    def import_module(name):
        if name == "pysqlite3.dbapi2":
            raise ImportError("dbapi2 unavailable")
        if name == "pysqlite3":
            return pysqlite_module
        if name == "sqlite3":
            return stdlib_module
        raise AssertionError(f"unexpected import: {name}")

    monkeypatch.setattr(backend.importlib, "import_module", import_module)
    module, selected, too_old, info = backend._load_sqlite_module()

    assert module is pysqlite_module
    assert selected == "pysqlite3"
    assert too_old is False
    assert info.module_path == pysqlite_module.__file__


def test_ensure_upsert_supported_raises_for_old_backend(reload_backend):
    sqlite_module = _fake_sqlite_module("sqlite3", (3, 23, 0))
    backend = reload_backend(sqlite_module, None)

    with pytest.raises(RuntimeError, match="requires >= 3\\.24\\.0"):
        backend.ensure_upsert_supported()
