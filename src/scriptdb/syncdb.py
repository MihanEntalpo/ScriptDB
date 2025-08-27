import sqlite3
import logging
import threading
import re
import inspect
from pathlib import Path
from typing import (
    Any,
    Callable,
    Dict,
    Iterable,
    List,
    Mapping,
    Optional,
    Sequence,
    Set,
    Type,
    TypeVar,
    Generator,
    Union,
    Generic,
    cast,
)

from .abstractdb import AbstractBaseDB, require_init, _get_migrations_table_sql

logger = logging.getLogger(__name__)

T = TypeVar("T", bound="SyncBaseDB")


class _SyncDBOpenContext(Generic[T]):
    def __init__(self, cls: Type[T], db_path: str, auto_create: bool, use_wal: bool) -> None:
        self._cls = cls
        self._db_path = db_path
        self._auto_create = auto_create
        self._use_wal = use_wal
        self._db: Optional[T] = None

    def _open(self) -> T:
        instance: T = self._cls(self._db_path)  # type: ignore
        instance.auto_create = self._auto_create  # type: ignore[attr-defined]
        instance.use_wal = self._use_wal  # type: ignore[attr-defined]
        instance.init()
        self._db = instance
        return instance

    def __enter__(self) -> T:
        return self._open()

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._db is not None:
            self._db.close()


class SyncBaseDB(AbstractBaseDB):
    def __init__(
        self, db_path: Union[str, Path], auto_create: bool = True, *, use_wal: bool = True
    ) -> None:
        super().__init__(db_path, auto_create, use_wal=use_wal)
        self.conn: sqlite3.Connection = cast(sqlite3.Connection, None)
        self._periodic_threads: List[threading.Thread] = []
        self._stop_event = threading.Event()
        self._upsert_lock = threading.Lock()

    @classmethod
    def open(
        cls: Type[T], db_path: Union[str, Path], *, auto_create: bool = True, use_wal: bool = True
    ) -> _SyncDBOpenContext[T]:
        path_obj = Path(db_path)
        if not auto_create and not path_obj.exists():
            raise RuntimeError(f"Database file {db_path} does not exist")
        return _SyncDBOpenContext(cls, str(path_obj), auto_create, use_wal)

    def init(self) -> None:
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        if getattr(self, "use_wal", False):
            self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.row_factory = sqlite3.Row
        self._ensure_migrations_table()
        self._apply_migrations()
        self.initialized = True

        for seconds, method in self._periodic_specs:

            def runner(method=method, seconds=seconds):
                while not self._stop_event.wait(seconds):
                    logger.info("Launching method %s", method.__name__)
                    method()
                    logger.info(
                        "Method %s finished, next run in %s seconds",
                        method.__name__,
                        seconds,
                    )

            t = threading.Thread(target=runner, daemon=True)
            t.start()
            self._periodic_threads.append(t)

    def _ensure_migrations_table(self) -> None:
        sql = _get_migrations_table_sql()
        logger.debug("Executing SQL: %s", sql)
        self.conn.execute(sql)
        self.conn.commit()

    def _applied_versions(self) -> Set[str]:
        sql = "SELECT name FROM applied_migrations"
        logger.debug("Executing SQL: %s", sql)
        cur = self.conn.execute(sql)
        rows = cur.fetchall()
        cur.close()
        return {row["name"] for row in rows}

    def _apply_migrations(self) -> None:
        migrations_list = self.migrations()
        applied = self._applied_versions()
        for mig in self._validate_migrations(migrations_list, applied):
            name = mig["name"]
            if "sql" in mig:
                try:
                    logger.debug("Applying migration by executing SQL script: %s", mig["sql"])
                    self.conn.executescript(mig["sql"])
                except Exception as exc:
                    raise RuntimeError(f"Error while applying migration {name}: {exc}") from exc
            elif "sqls" in mig:
                sqls = mig["sqls"]
                if (
                    not isinstance(sqls, Sequence)
                    or isinstance(sqls, (str, bytes))
                    or not all(isinstance(s, str) for s in sqls)
                ):
                    raise TypeError(f"'sqls' for migration {name} must be a sequence of strings")
                try:
                    for sql in sqls:
                        logger.debug("Applying migration by executing SQL script: %s", sql)
                        self.conn.executescript(sql)
                except Exception as exc:
                    raise RuntimeError(f"Error while applying migration {name}: {exc}") from exc
            else:
                func = mig["function"]
                if not callable(func):
                    raise TypeError(f"'function' for migration {name} must be callable")
                if inspect.iscoroutinefunction(func):
                    raise TypeError(f"'function' for migration {name} must be synchronous")
                try:
                    func(self, migrations_list, name)
                except Exception as exc:
                    raise RuntimeError(f"Error while applying migration {name}: {exc}") from exc
            sql = "INSERT INTO applied_migrations(name) VALUES (?)"
            logger.debug("Executing SQL: %s; params: (%s,)", sql, name)
            self.conn.execute(sql, (name,))
            self.conn.commit()

    @require_init
    def _primary_key(self, table: str) -> str:
        if table not in self._pk_cache:
            sql = f"PRAGMA table_info({table})"
            logger.debug("Executing SQL: %s", sql)
            cur = self.conn.execute(sql)
            rows = cur.fetchall()
            cur.close()
            pk_cols = [row["name"] for row in rows if row["pk"]]
            if not pk_cols:
                raise ValueError(f"Table {table} has no primary key")
            if len(pk_cols) > 1:
                raise ValueError(f"Table {table} has composite primary key")
            self._pk_cache[table] = pk_cols[0]
        return self._pk_cache[table]

    def _on_query(self) -> None:
        for hook in self._query_hooks:
            hook["count"] += 1
            if hook["count"] >= hook["interval"]:
                hook["count"] = 0
                logger.info("Launching method %s", hook["method"].__name__)
                hook["method"]()

    @require_init
    def execute(
        self,
        sql: str,
        params: Union[Sequence[Any], Mapping[str, Any], None] = None,
    ) -> sqlite3.Cursor:
        ps = params if params is not None else ()
        logger.debug("Executing SQL: %s; params: %s", sql, ps)
        cur = self.conn.execute(sql, ps)
        self.conn.commit()
        self._on_query()
        return cur

    @require_init
    def execute_many(
        self,
        sql: str,
        seq_params: Iterable[Sequence[Any]],
    ) -> sqlite3.Cursor:
        logger.debug("Executing many SQL: %s; params: %s", sql, seq_params)
        cur = self.conn.executemany(sql, seq_params)
        self.conn.commit()
        self._on_query()
        return cur

    @require_init
    def insert_one(self, table: str, row: Dict[str, Any]) -> Any:
        pk_col = self._primary_key(table)
        cols = ", ".join(row.keys())
        placeholders = ", ".join([f":{c}" for c in row])
        sql = f"INSERT INTO {table} ({cols}) VALUES ({placeholders})"
        logger.debug("Executing SQL: %s; params: %s", sql, row)
        cur = self.conn.execute(sql, row)
        self.conn.commit()
        self._on_query()
        pk = row.get(pk_col, cur.lastrowid)
        cur.close()
        return pk

    @require_init
    def insert_many(self, table: str, rows: List[Dict[str, Any]]) -> None:
        if not rows:
            return
        cols = rows[0].keys()
        col_clause = ", ".join(cols)
        placeholders = ", ".join([f":{c}" for c in cols])
        sql = f"INSERT INTO {table} ({col_clause}) VALUES ({placeholders})"
        self.conn.executemany(sql, rows)
        self.conn.commit()
        self._on_query()

    @require_init
    def upsert_one(self, table: str, row: Dict[str, Any]) -> Any:
        with self._upsert_lock:
            pk_col = self._primary_key(table)
            cols = row.keys()
            col_clause = ", ".join(cols)
            placeholders = ", ".join([f":{c}" for c in cols])
            insert_sql = f"INSERT INTO {table} ({col_clause}) VALUES ({placeholders})"
            if pk_col not in row:
                cur = self.execute(insert_sql, row)
                return cur.lastrowid
            update_cols = [c for c in cols if c != pk_col]
            update_sql = ""
            if update_cols:
                assignments = ", ".join([f"{c}=:{c}" for c in update_cols])
                update_sql = f"UPDATE {table} SET {assignments} WHERE {pk_col}=:{pk_col}"
            try:
                self.conn.execute(insert_sql, row)
            except sqlite3.IntegrityError:
                if update_sql:
                    self.conn.execute(update_sql, row)
                else:
                    return row[pk_col]
            self.conn.commit()
            self._on_query()
            return row.get(pk_col, self.conn.execute("SELECT last_insert_rowid()").fetchone()[0])

    @require_init
    def upsert_many(self, table: str, rows: List[Dict[str, Any]]) -> None:
        with self._upsert_lock:
            if not rows:
                return
            pk_col = self._primary_key(table)
            cols = rows[0].keys()
            col_clause = ", ".join(cols)
            placeholders = ", ".join([f":{c}" for c in cols])
            insert_sql = f"INSERT INTO {table} ({col_clause}) VALUES ({placeholders})"
            update_cols = [c for c in cols if c != pk_col]
            assignments = ", ".join([f"{c}=:{c}" for c in update_cols])
            update_sql = f"UPDATE {table} SET {assignments} WHERE {pk_col}=:{pk_col}" if update_cols else ""
            for row in rows:
                try:
                    self.conn.execute(insert_sql, row)
                except sqlite3.IntegrityError:
                    if update_sql:
                        self.conn.execute(update_sql, row)
            self.conn.commit()
            self._on_query()
        self._on_query()

    @require_init
    def delete_one(self, table: str, pk: Any) -> int:
        pk_col = self._primary_key(table)
        sql = f"DELETE FROM {table} WHERE {pk_col}=?"
        cur = self.conn.execute(sql, (pk,))
        self.conn.commit()
        self._on_query()
        return cur.rowcount

    @require_init
    def delete_many(
        self,
        table: str,
        where: str,
        params: Union[Sequence[Any], Mapping[str, Any], None] = None,
    ) -> int:
        ps = params if params is not None else ()
        sql = f"DELETE FROM {table} WHERE {where}"
        cur = self.conn.execute(sql, ps)
        self.conn.commit()
        self._on_query()
        return cur.rowcount

    @require_init
    def update_one(self, table: str, pk: Any, row: Dict[str, Any]) -> int:
        pk_col = self._primary_key(table)
        assignments = ", ".join([f"{c}=:{c}" for c in row])
        sql = f"UPDATE {table} SET {assignments} WHERE {pk_col}=:pk"
        row = dict(row)
        row["pk"] = pk
        cur = self.conn.execute(sql, row)
        self.conn.commit()
        self._on_query()
        return cur.rowcount

    @require_init
    def query_one(
        self,
        sql: str,
        params: Union[Sequence[Any], Mapping[str, Any], None] = None,
    ) -> Optional[sqlite3.Row]:
        ps = params if params is not None else ()
        logger.debug("Executing SQL: %s; params: %s", sql, ps)
        cur = self.conn.execute(sql, ps)
        row = cur.fetchone()
        cur.close()
        self._on_query()
        return row

    @require_init
    def query_many(
        self,
        sql: str,
        params: Union[Sequence[Any], Mapping[str, Any], None] = None,
    ) -> List[sqlite3.Row]:
        ps = params if params is not None else ()
        logger.debug("Executing SQL: %s; params: %s", sql, ps)
        cur = self.conn.execute(sql, ps)
        rows = cur.fetchall()
        cur.close()
        self._on_query()
        return rows

    @require_init
    def query_many_gen(
        self,
        sql: str,
        params: Union[Sequence[Any], Mapping[str, Any], None] = None,
    ) -> Generator[sqlite3.Row, None, None]:
        ps = params if params is not None else ()
        logger.debug("Executing SQL: %s; params: %s", sql, ps)
        cur = self.conn.execute(sql, ps)
        try:
            for row in cur:
                yield row
        finally:
            cur.close()
        self._on_query()

    @require_init
    def query_scalar(
        self,
        sql: str,
        params: Union[Sequence[Any], Mapping[str, Any], None] = None,
    ) -> Any:
        row = self.query_one(sql, params)
        return None if row is None else row[0]

    @require_init
    def query_column(
        self,
        sql: str,
        params: Union[Sequence[Any], Mapping[str, Any], None] = None,
    ) -> List[Any]:
        rows = self.query_many(sql, params)
        return [row[0] for row in rows]

    @require_init
    def query_dict(
        self,
        sql: str,
        params: Union[Sequence[Any], Mapping[str, Any], None] = None,
        *,
        key: Union[str, Callable[[sqlite3.Row], Any], None] = None,
        value: Union[str, Callable[[sqlite3.Row], Any], None] = None,
    ) -> Dict[Any, Any]:
        rows = self.query_many(sql, params)
        if key is None:
            match = re.search(
                r"from\s+(?:\"([A-Za-z_][\w]*)\"|'([A-Za-z_][\w]*)'|([A-Za-z_][\w]*))",
                sql,
                re.IGNORECASE,
            )
            if not match:
                raise ValueError(
                    "Cannot determine table name from sql, so cannot deduce primary "
                    "key, please provide non-empty 'key' argument",
                )
            table = match.group(1) or match.group(2) or match.group(3)
            key = self._primary_key(table)
        if isinstance(key, str):
            key_str = key

            def get_key(row: sqlite3.Row) -> Any:
                return row[key_str]
        else:

            def get_key(row: sqlite3.Row) -> Any:
                return key(row)

        if value is None:

            def get_value(row: sqlite3.Row) -> Any:
                return row
        elif isinstance(value, str):
            value_str = value

            def get_value(row: sqlite3.Row) -> Any:
                return row[value_str]
        else:

            def get_value(row: sqlite3.Row) -> Any:
                return value(row)

        return {get_key(row): get_value(row) for row in rows}

    @require_init
    def close(self) -> None:
        self._stop_event.set()
        for t in self._periodic_threads:
            t.join(timeout=0)
        self._periodic_threads.clear()
        if self.conn:
            self.conn.close()
        self.initialized = False

    def __enter__(self: T) -> T:
        if not self.initialized:
            self.init()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()


BaseDB = SyncBaseDB


def __getattr__(name):
    if name == "CacheDB":
        from .synccachedb import SyncCacheDB

        return SyncCacheDB
    raise AttributeError(name)
