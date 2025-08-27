import sys
import pathlib
import pytest

# Add the src directory to sys.path so we can import the package
sys.path.append(str(pathlib.Path(__file__).resolve().parents[1] / "src"))
from scriptdb import SyncBaseDB


class CompositePKDB(SyncBaseDB):
    def migrations(self):
        return [
            {
                "name": "create_table",
                "sql": "CREATE TABLE t(a INTEGER, b INTEGER, x INTEGER, PRIMARY KEY (a, b))",
            }
        ]


@pytest.fixture
def db(tmp_path):
    db_file = tmp_path / "test.db"
    with CompositePKDB.open(str(db_file)) as db:
        yield db


def test_insert_one_composite_primary_key(db):
    with pytest.raises(ValueError) as exc:
        db.insert_one("t", {"a": 1, "b": 2, "x": 3})
    assert "composite primary key" in str(exc.value)


def test_upsert_one_composite_primary_key(db):
    with pytest.raises(ValueError) as exc:
        db.upsert_one("t", {"a": 1, "b": 2, "x": 3})
    assert "composite primary key" in str(exc.value)


def test_upsert_many_composite_primary_key(db):
    rows = [{"a": 1, "b": 2, "x": 3}, {"a": 3, "b": 4, "x": 5}]
    with pytest.raises(ValueError) as exc:
        db.upsert_many("t", rows)
    assert "composite primary key" in str(exc.value)


def test_delete_one_composite_primary_key(db):
    with pytest.raises(ValueError) as exc:
        db.delete_one("t", 1)
    assert "composite primary key" in str(exc.value)


def test_update_one_composite_primary_key(db):
    with pytest.raises(ValueError) as exc:
        db.update_one("t", 1, {"x": 4})
    assert "composite primary key" in str(exc.value)


def test_query_dict_composite_primary_key(db):
    db.execute("INSERT INTO t(a, b, x) VALUES(?, ?, ?)", (1, 2, 3))
    with pytest.raises(ValueError) as exc:
        db.query_dict("SELECT * FROM t")
    assert "composite primary key" in str(exc.value)
