import contextlib
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
    Tuple,
)

from . import sqlite_backend
from .abstractdb import (
    AbstractBaseDB,
    require_init,
    _get_migrations_table_sql,
    _is_signature_binding_error,
    _script_has_transaction,
    _script_starts_unfinished_transaction,
)
from .dbbuilder import _SQLBuilder
from ._rowfactory import (
    RowFactorySetting,
    RowType,
    dict_row_factory,
    first_column_value,
    normalize_row_factory,
    supports_row_factory,
)

logger = logging.getLogger(__name__)

sqlite3: Any = sqlite_backend.sqlite3

T = TypeVar("T", bound="SyncBaseDB")


class _SyncDBOpenContext(Generic[T]):
    def __init__(
        self,
        cls: Type[T],
        db_path: str,
        auto_create: bool,
        use_wal: bool,
        row_factory: RowFactorySetting,
    ) -> None:
        self._cls = cls
        self._db_path = db_path
        self._auto_create = auto_create
        self._use_wal = use_wal
        self._db: Optional[T] = None
        self._row_factory, _ = normalize_row_factory(row_factory)

    def _open(self) -> T:
        if supports_row_factory(self._cls):
            instance = self._cls(self._db_path, row_factory=self._row_factory)  # type: ignore
        else:
            instance = self._cls(self._db_path)  # type: ignore
            if hasattr(instance, "_set_row_factory"):
                instance._set_row_factory(self._row_factory)  # type: ignore[attr-defined]
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
        self,
        db_path: Union[str, Path],
        auto_create: bool = True,
        *,
        row_factory: RowFactorySetting = sqlite3.Row,
        use_wal: bool = True,
    ) -> None:
        super().__init__(db_path, auto_create, use_wal=use_wal)
        self.conn: sqlite3.Connection = cast(sqlite3.Connection, None)
        self._periodic_threads: List[threading.Thread] = []
        self._stop_event = threading.Event()
        self._upsert_lock = threading.Lock()
        self._close_lock = threading.Lock()
        self._row_factory_setting: RowFactorySetting = sqlite3.Row
        self._rows_as_dict = False
        self._in_transaction = False
        self._set_row_factory(row_factory)

    def _set_row_factory(self, row_factory: RowFactorySetting) -> None:
        normalized, rows_as_dict = normalize_row_factory(row_factory)
        self._row_factory_setting = normalized
        self._rows_as_dict = rows_as_dict
        self._configure_row_factory()

    def _configure_row_factory(self) -> None:
        if self.conn is None:
            return
        conn = cast(Any, self.conn)
        if self._rows_as_dict:
            conn.row_factory = dict_row_factory
        else:
            conn.row_factory = sqlite3.Row

    def _maybe_commit(self) -> None:
        if not self._in_transaction:
            self.conn.commit()

    @require_init
    def begin(self) -> None:
        if self._in_transaction:
            raise RuntimeError("A transaction is already in progress")
        self.conn.execute("BEGIN")
        self._in_transaction = True

    @require_init
    def commit(self) -> None:
        if not self._in_transaction:
            raise RuntimeError("No active transaction to commit")
        self.conn.commit()
        self._in_transaction = False

    @require_init
    def rollback(self) -> None:
        if not self._in_transaction:
            raise RuntimeError("No active transaction to roll back")
        self.conn.rollback()
        self._in_transaction = False

    @contextlib.contextmanager
    def transaction(self):
        if not self.initialized or self.conn is None:
            raise self._not_initialized_error()
        self.begin()
        committed = False
        try:
            yield
            committed = True
        except BaseException:
            if self._in_transaction:
                self.rollback()
            raise
        finally:
            if committed and self._in_transaction:
                self.commit()

    @classmethod
    def open(
        cls: Type[T],
        db_path: Union[str, Path],
        *,
        auto_create: bool = True,
        row_factory: RowFactorySetting = sqlite3.Row,
        use_wal: bool = True,
    ) -> _SyncDBOpenContext[T]:
        path_obj = Path(db_path)
        if not auto_create and not path_obj.exists():
            raise RuntimeError(f"Database file {db_path} does not exist")
        return _SyncDBOpenContext(cls, str(path_obj), auto_create, use_wal, row_factory)

    def init(self) -> None:
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        if getattr(self, "use_wal", False):
            self.conn.execute("PRAGMA journal_mode=WAL")
        self._configure_row_factory()
        self._is_closed = False
        self.initialized = True
        try:
            self._ensure_migrations_table()
            self._apply_migrations()
        except Exception:
            self.initialized = False
            raise

        for seconds, method in self._periodic_specs:

            def runner(method=method, seconds=seconds):
                while not self._stop_event.wait(seconds):
                    logger.debug("Launching method %s", method.__name__)
                    method()
                    logger.debug(
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
        self._maybe_commit()

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
                sql = mig["sql"]
                if isinstance(sql, _SQLBuilder):
                    sql = str(sql)
                if not isinstance(sql, str):
                    raise TypeError(
                        f"'sql' for migration {name} must be a string or SQL builder instance"
                    )
                try:
                    logger.debug("Applying migration by executing SQL script: %s", sql)
                    self.conn.executescript(sql)
                except Exception as exc:
                    raise RuntimeError(f"Error while applying migration {name}: {exc}") from exc
            elif "sqls" in mig:
                sqls = mig["sqls"]
                if not isinstance(sqls, Sequence) or isinstance(sqls, (str, bytes)):
                    raise TypeError(
                        f"'sqls' for migration {name} must be a sequence of strings or SQL builder instances"
                    )
                rendered: List[str] = []
                for sql in sqls:
                    if isinstance(sql, _SQLBuilder):
                        sql = str(sql)
                    if not isinstance(sql, str):
                        raise TypeError(
                            f"'sqls' for migration {name} must contain only strings or SQL builder instances"
                        )
                    if _script_starts_unfinished_transaction(sql):
                        raise ValueError(
                            f"'sqls' for migration {name} contains a BEGIN without COMMIT/ROLLBACK"
                        )
                    rendered.append(sql)
                body = "\n".join(sql if sql.rstrip().endswith(";") else f"{sql};" for sql in rendered)
                user_wrapped = any(_script_has_transaction(sql) for sql in rendered)
                script = body if user_wrapped else f"BEGIN;\n{body}\nCOMMIT;"
                try:
                    logger.debug("Applying migration by executing SQL script: %s", script)
                    self.conn.executescript(script)
                except Exception as exc:
                    if not user_wrapped:
                        self.conn.execute("ROLLBACK")
                    raise RuntimeError(f"Error while applying migration {name}: {exc}") from exc
            else:
                func = mig["function"]
                if isinstance(func, str):
                    try:
                        func = getattr(self, func)
                    except AttributeError as exc:
                        raise ValueError(
                            f"'function' for migration {name} references unknown attribute '{func}'"
                        ) from exc
                target = getattr(func, "__func__", func)
                if not callable(func):
                    raise TypeError(f"'function' for migration {name} must be callable")
                if inspect.iscoroutinefunction(target):
                    raise TypeError(f"'function' for migration {name} must be synchronous")
                bound = inspect.ismethod(func)
                call_args: Tuple[Any, ...]
                signature_hint: str
                if bound:
                    call_args = (migrations_list, name)
                    signature_hint = "(migrations, name)"
                else:
                    call_args = (self, migrations_list, name)
                    signature_hint = "(db, migrations, name)"
                try:
                    inspect.signature(func).bind(*call_args)
                except TypeError as exc:
                    if not _is_signature_binding_error(exc):
                        raise
                    raise TypeError(
                        f"'function' for migration {name} must accept parameters {signature_hint}"
                    ) from exc
                try:
                    func(*call_args)
                except Exception as exc:
                    raise RuntimeError(f"Error while applying migration {name}: {exc}") from exc
            sql = "INSERT INTO applied_migrations(name) VALUES (?)"
            logger.debug("Executing SQL: %s; params: (%s,)", sql, name)
            self.conn.execute(sql, (name,))
            self._maybe_commit()

    @require_init
    def _primary_key(self, table: str) -> str:
        if table not in self._pk_cache:
            sql = f"PRAGMA table_info({table})"
            logger.debug("Executing SQL: %s", sql)
            cur = self.conn.execute(sql)
            rows = cur.fetchall()
            cur.close()
            if not rows:
                raise ValueError(f"Table {table} does not exist")
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
                logger.debug("Launching method %s", hook["method"].__name__)
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
        self._maybe_commit()
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
        self._maybe_commit()
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
        self._maybe_commit()
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
        self._maybe_commit()
        self._on_query()

    @require_init
    def upsert_one(self, table: str, row: Dict[str, Any]) -> Any:
        sqlite_backend.ensure_upsert_supported()
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
            if update_cols:
                assignments = ", ".join([f"{c}=excluded.{c}" for c in update_cols])
                upsert_sql = f"{insert_sql} ON CONFLICT({pk_col}) DO UPDATE SET {assignments}"
            else:
                upsert_sql = f"{insert_sql} ON CONFLICT({pk_col}) DO NOTHING"
            cur = self.execute(upsert_sql, row)
            return row.get(pk_col, cur.lastrowid)

    @require_init
    def upsert_many(self, table: str, rows: List[Dict[str, Any]]) -> None:
        sqlite_backend.ensure_upsert_supported()
        with self._upsert_lock:
            if not rows:
                return
            pk_col = self._primary_key(table)
            cols = rows[0].keys()
            col_clause = ", ".join(cols)
            placeholders = ", ".join([f":{c}" for c in cols])
            insert_sql = f"INSERT INTO {table} ({col_clause}) VALUES ({placeholders})"
            update_cols = [c for c in cols if c != pk_col]
            if update_cols:
                assignments = ", ".join([f"{c}=excluded.{c}" for c in update_cols])
                upsert_sql = f"{insert_sql} ON CONFLICT({pk_col}) DO UPDATE SET {assignments}"
            else:
                upsert_sql = f"{insert_sql} ON CONFLICT({pk_col}) DO NOTHING"
            self.conn.executemany(upsert_sql, rows)
            self._maybe_commit()
            self._on_query()

    @require_init
    def delete_one(self, table: str, pk: Any) -> int:
        pk_col = self._primary_key(table)
        sql = f"DELETE FROM {table} WHERE {pk_col}=?"
        cur = self.conn.execute(sql, (pk,))
        self._maybe_commit()
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
        self._maybe_commit()
        self._on_query()
        return cur.rowcount

    @require_init
    def update_one(self, table: str, pk: Any, row: Dict[str, Any]) -> int:
        if not row:
            return 0
        pk_col = self._primary_key(table)
        assignments = ", ".join([f"{c}=:{c}" for c in row])
        sql = f"UPDATE {table} SET {assignments} WHERE {pk_col}=:pk"
        row = dict(row)
        row["pk"] = pk
        cur = self.conn.execute(sql, row)
        self._maybe_commit()
        self._on_query()
        return cur.rowcount

    @require_init
    def query_one(
        self,
        sql: str,
        params: Union[Sequence[Any], Mapping[str, Any], None] = None,
    ) -> Optional[RowType]:
        ps = params if params is not None else ()
        logger.debug("Executing SQL: %s; params: %s", sql, ps)
        cur = self.conn.execute(sql, ps)
        row = cur.fetchone()
        cur.close()
        self._on_query()
        return cast(Optional[RowType], row)

    @require_init
    def query_many(
        self,
        sql: str,
        params: Union[Sequence[Any], Mapping[str, Any], None] = None,
    ) -> List[RowType]:
        ps = params if params is not None else ()
        logger.debug("Executing SQL: %s; params: %s", sql, ps)
        cur = self.conn.execute(sql, ps)
        rows = cur.fetchall()
        cur.close()
        self._on_query()
        return cast(List[RowType], rows)

    @require_init
    def query_many_gen(
        self,
        sql: str,
        params: Union[Sequence[Any], Mapping[str, Any], None] = None,
    ) -> Generator[RowType, None, None]:
        ps = params if params is not None else ()
        logger.debug("Executing SQL: %s; params: %s", sql, ps)
        cur = self.conn.execute(sql, ps)
        try:
            for row in cur:
                yield cast(RowType, row)
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
        return None if row is None else first_column_value(row, self._rows_as_dict)

    @require_init
    def query_column(
        self,
        sql: str,
        params: Union[Sequence[Any], Mapping[str, Any], None] = None,
    ) -> List[Any]:
        rows = self.query_many(sql, params)
        return [first_column_value(row, self._rows_as_dict) for row in rows]

    @require_init
    def query_dict(
        self,
        sql: str,
        params: Union[Sequence[Any], Mapping[str, Any], None] = None,
        *,
        key: Union[str, Callable[[RowType], Any], None] = None,
        value: Union[str, Callable[[RowType], Any], None] = None,
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

            def get_key(row: RowType) -> Any:
                return row[key_str]
        else:

            def get_key(row: RowType) -> Any:
                return key(row)

        if value is None:

            def get_value(row: RowType) -> Any:
                return row
        elif isinstance(value, str):
            value_str = value

            def get_value(row: RowType) -> Any:
                return row[value_str]
        else:

            def get_value(row: RowType) -> Any:
                return value(row)

        return {get_key(row): get_value(row) for row in rows}

    def close(self) -> None:
        """Close the database connection.

        The method is idempotent and protected by a lock so it can be invoked
        multiple times safely from different threads or signal handlers.
        """
        with self._close_lock:
            if not self.initialized and self.conn is None:
                self._is_closed = True
                return
            self._stop_event.set()
            for t in self._periodic_threads:
                t.join(timeout=0)
            self._periodic_threads.clear()
            if self.conn:
                self._in_transaction = False
                self.conn.close()
                self.conn = cast(sqlite3.Connection, None)
            self.initialized = False
            self._is_closed = True

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
