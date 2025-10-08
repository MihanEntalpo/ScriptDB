import pytest
import sys
import pathlib

sys.path.append(str(pathlib.Path(__file__).resolve().parents[1] / "src"))
from scriptdb.abstractdb import AbstractBaseDB, require_init


@pytest.mark.asyncio
async def test_require_init_async_generator_guard():
    class Dummy:
        def __init__(self) -> None:
            self.initialized = False
            self.conn = None

        @require_init
        async def stream(self):
            yield 1

    dummy = Dummy()
    agen = dummy.stream()
    with pytest.raises(RuntimeError):
        await agen.__anext__()
    await agen.aclose()


def test_abstract_base_db_requires_existing_file(tmp_path):
    class DummyDB(AbstractBaseDB):
        def migrations(self):
            return []

    missing = tmp_path / "missing.db"
    with pytest.raises(RuntimeError):
        DummyDB(str(missing), auto_create=False)


def test_abstract_base_db_migrations_not_implemented(tmp_path):
    class DummyDB(AbstractBaseDB):
        def migrations(self):
            return super().migrations()

    db = DummyDB(str(tmp_path / "dummy.db"))
    with pytest.raises(NotImplementedError):
        db.migrations()
