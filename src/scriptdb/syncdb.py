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
    _atomic_migration_script,
    _row_columns,
)
from .dbbuilder import _SQLBuilder, _quote_ident
from ._rowfactory import (
    RowFactorySetting,
    RowType,
    dict_row_factory,
    first_column_value,
    normalize_row_factory,
    supports_init_arg,
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
        legacy_sqlite_support: bool,
    ) -> None:
        self._cls = cls
        self._db_path = db_path
        self._auto_create = auto_create
        self._use_wal = use_wal
        self._db: Optional[T] = None
        self._row_factory, _ = normalize_row_factory(row_factory)
        self._legacy_sqlite_support = legacy_sqlite_support

    def _open(self) -> T:
        kwargs: Dict[str, Any] = {}
        if supports_row_factory(self._cls):
            kwargs["row_factory"] = self._row_factory
        if supports_init_arg(self._cls, "legacy_sqlite_support"):
            kwargs["legacy_sqlite_support"] = self._legacy_sqlite_support
        instance = self._cls(self._db_path, **kwargs)  # type: ignore[call-arg]
        if "row_factory" not in kwargs and hasattr(instance, "_set_row_factory"):
            instance._set_row_factory(self._row_factory)  # type: ignore[attr-defined]
        if "legacy_sqlite_support" not in kwargs:
            instance.legacy_sqlite_support = self._legacy_sqlite_support  # type: ignore[attr-defined]
        instance.auto_create = self._auto_create  # type: ignore[attr-defined]
        instance.use_wal = self._use_wal  # type: ignore[attr-defined]
        try:
            instance.init()
        except BaseException:
            with contextlib.suppress(BaseException):
                instance.close()
            raise
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
        legacy_sqlite_support: bool = False,
    ) -> None:
        super().__init__(
            db_path,
            auto_create,
            use_wal=use_wal,
            legacy_sqlite_support=legacy_sqlite_support,
        )
        self.conn: sqlite3.Connection = cast(sqlite3.Connection, None)
        self._periodic_threads: List[threading.Thread] = []
        self._stop_event = threading.Event()
        self._close_lock = threading.RLock()
        self._operation_lock = threading.RLock()
        self._upsert_lock = self._operation_lock
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
        self._operation_lock.acquire()
        try:
            self.conn.execute("BEGIN")
            self._in_transaction = True
        except BaseException:
            self._operation_lock.release()
            raise

    @require_init
    def commit(self) -> None:
        if not self._in_transaction:
            raise RuntimeError("No active transaction to commit")
        self.conn.commit()
        self._in_transaction = False
        self._operation_lock.release()

    @require_init
    def rollback(self) -> None:
        if not self._in_transaction:
            raise RuntimeError("No active transaction to roll back")
        self.conn.rollback()
        self._in_transaction = False
        self._operation_lock.release()

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
        legacy_sqlite_support: bool = False,
    ) -> _SyncDBOpenContext[T]:
        path_obj = Path(db_path)
        if not auto_create and not path_obj.exists():
            raise RuntimeError(f"Database file {db_path} does not exist")
        return _SyncDBOpenContext(
            cls,
            str(path_obj),
            auto_create,
            use_wal,
            row_factory,
            legacy_sqlite_support,
        )

    def init(self) -> None:
        if self.conn is not None or self.initialized:
            raise RuntimeError("database is already initialized")
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._stop_event.clear()
        self._pk_cache.clear()
        for hook in self._query_hooks:
            hook["count"] = 0
            hook["running"] = False
        if getattr(self, "use_wal", False):
            self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self._configure_row_factory()
        self._is_closed = False
        self.initialized = True
        try:
            self._ensure_migrations_table()
            self._apply_migrations()
        except BaseException:
            self.initialized = False
            self.conn.close()
            self.conn = cast(sqlite3.Connection, None)
            raise

        for seconds, method in self._periodic_specs:

            def runner(method=method, seconds=seconds):
                while not self._stop_event.wait(seconds):
                    try:
                        with self._operation_lock:
                            if not self.initialized:
                                return
                            logger.debug("Launching method %s", method.__name__)
                            method()
                            logger.debug(
                                "Method %s finished, next run in %s seconds",
                                method.__name__,
                                seconds,
                            )
                    except Exception:
                        logger.exception("Periodic method %s failed", method.__name__)

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
                script = _atomic_migration_script([sql], name)
                try:
                    logger.debug("Applying migration by executing SQL script: %s", script)
                    self.conn.executescript(script)
                except BaseException as exc:
                    if self.conn.in_transaction:
                        self.conn.rollback()
                    if isinstance(exc, Exception):
                        raise RuntimeError(f"Error while applying migration {name}: {exc}") from exc
                    raise
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
                    rendered.append(sql)
                script = _atomic_migration_script(rendered, name)
                try:
                    logger.debug("Applying migration by executing SQL script: %s", script)
                    self.conn.executescript(script)
                except BaseException as exc:
                    if self.conn.in_transaction:
                        self.conn.rollback()
                    if isinstance(exc, Exception):
                        raise RuntimeError(f"Error while applying migration {name}: {exc}") from exc
                    raise
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
                    self.conn.execute("BEGIN")
                    self._in_transaction = True
                    func(*call_args)
                    sql = "INSERT INTO applied_migrations(name) VALUES (?)"
                    self.conn.execute(sql, (name,))
                    self.conn.commit()
                    self._in_transaction = False
                except BaseException as exc:
                    if self.conn.in_transaction:
                        self.conn.rollback()
                    self._in_transaction = False
                    if isinstance(exc, Exception):
                        raise RuntimeError(f"Error while applying migration {name}: {exc}") from exc
                    raise

    @require_init
    def _primary_key(self, table: str) -> str:
        if table not in self._pk_cache:
            sql = "SELECT name, pk FROM pragma_table_info(?)"
            logger.debug("Executing SQL: %s", sql)
            cur = self.conn.execute(sql, (table,))
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
            if hook["running"]:
                continue
            hook["count"] += 1
            if hook["count"] >= hook["interval"]:
                hook["count"] = 0
                logger.debug("Launching method %s", hook["method"].__name__)
                hook["running"] = True
                try:
                    hook["method"]()
                except Exception:
                    logger.exception("Query hook %s failed", hook["method"].__name__)
                finally:
                    hook["running"] = False

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
        cur = self._execute_many_atomic(sql, seq_params)
        self._maybe_commit()
        self._on_query()
        return cur

    def _execute_many_atomic(self, sql: str, seq_params: Iterable[Sequence[Any]]) -> sqlite3.Cursor:
        savepoint = "scriptdb_batch"
        self.conn.execute(f"SAVEPOINT {savepoint}")
        try:
            cur = self.conn.executemany(sql, seq_params)
            self.conn.execute(f"RELEASE SAVEPOINT {savepoint}")
            return cur
        except BaseException:
            self.conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            self.conn.execute(f"RELEASE SAVEPOINT {savepoint}")
            raise

    @require_init
    def insert_one(self, table: str, row: Dict[str, Any]) -> Any:
        pk_col = self._primary_key(table)
        table_sql = _quote_ident(table)
        if not row:
            cur = self.conn.execute(f"INSERT INTO {table_sql} DEFAULT VALUES")
            self._maybe_commit()
            self._on_query()
            pk = cur.lastrowid
            cur.close()
            return pk
        cols = _row_columns([row])
        col_clause = ", ".join(_quote_ident(column) for column in cols)
        placeholders = ", ".join("?" for _ in cols)
        sql = f"INSERT INTO {table_sql} ({col_clause}) VALUES ({placeholders})"
        logger.debug("Executing SQL: %s; params: %s", sql, row)
        cur = self.conn.execute(sql, tuple(row[column] for column in cols))
        self._maybe_commit()
        self._on_query()
        pk = row.get(pk_col, cur.lastrowid)
        cur.close()
        return pk

    @require_init
    def insert_many(self, table: str, rows: List[Dict[str, Any]]) -> None:
        if not rows:
            return
        cols = _row_columns(rows)
        col_clause = ", ".join(_quote_ident(column) for column in cols)
        placeholders = ", ".join("?" for _ in cols)
        sql = f"INSERT INTO {_quote_ident(table)} ({col_clause}) VALUES ({placeholders})"
        values = [tuple(row[column] for column in cols) for row in rows]
        self._execute_many_atomic(sql, values)
        self._maybe_commit()
        self._on_query()

    @require_init
    def upsert_one(self, table: str, row: Dict[str, Any]) -> Any:
        with self._upsert_lock:
            if self._use_legacy_upsert():
                self._warn_legacy_upsert_once()
                return self._legacy_upsert_one_locked(table, row)
            sqlite_backend.ensure_upsert_supported()
            pk_col = self._primary_key(table)
            if not row:
                return self.insert_one(table, row)
            cols = _row_columns([row])
            col_clause = ", ".join(_quote_ident(column) for column in cols)
            placeholders = ", ".join("?" for _ in cols)
            insert_sql = f"INSERT INTO {_quote_ident(table)} ({col_clause}) VALUES ({placeholders})"
            values = tuple(row[column] for column in cols)
            if pk_col not in row:
                cur = self.execute(insert_sql, values)
                return cur.lastrowid
            update_cols = [c for c in cols if c != pk_col]
            if update_cols:
                assignments = ", ".join(
                    f"{_quote_ident(c)}=excluded.{_quote_ident(c)}" for c in update_cols
                )
                upsert_sql = f"{insert_sql} ON CONFLICT({_quote_ident(pk_col)}) DO UPDATE SET {assignments}"
            else:
                upsert_sql = f"{insert_sql} ON CONFLICT({_quote_ident(pk_col)}) DO NOTHING"
            cur = self.execute(upsert_sql, values)
            return row.get(pk_col, cur.lastrowid)

    @require_init
    def upsert_many(self, table: str, rows: List[Dict[str, Any]]) -> None:
        with self._upsert_lock:
            if self._use_legacy_upsert():
                self._warn_legacy_upsert_once()
                self._legacy_upsert_many_locked(table, rows)
                return
            sqlite_backend.ensure_upsert_supported()
            if not rows:
                return
            pk_col = self._primary_key(table)
            cols = _row_columns(rows)
            col_clause = ", ".join(_quote_ident(column) for column in cols)
            placeholders = ", ".join("?" for _ in cols)
            insert_sql = f"INSERT INTO {_quote_ident(table)} ({col_clause}) VALUES ({placeholders})"
            update_cols = [c for c in cols if c != pk_col]
            if update_cols:
                assignments = ", ".join(
                    f"{_quote_ident(c)}=excluded.{_quote_ident(c)}" for c in update_cols
                )
                upsert_sql = f"{insert_sql} ON CONFLICT({_quote_ident(pk_col)}) DO UPDATE SET {assignments}"
            else:
                upsert_sql = f"{insert_sql} ON CONFLICT({_quote_ident(pk_col)}) DO NOTHING"
            values = [tuple(row[column] for column in cols) for row in rows]
            self._execute_many_atomic(upsert_sql, values)
            self._maybe_commit()
            self._on_query()

    def _legacy_upsert_one_locked(self, table: str, row: Dict[str, Any], *, emit_query_hook: bool = True) -> Any:
        pk_col = self._primary_key(table)
        table_sql = _quote_ident(table)
        if not row:
            cur = self.conn.execute(f"INSERT INTO {table_sql} DEFAULT VALUES")
            self._maybe_commit()
            if emit_query_hook:
                self._on_query()
            pk = cur.lastrowid
            cur.close()
            return pk
        cols = _row_columns([row])
        col_clause = ", ".join(_quote_ident(column) for column in cols)
        placeholders = ", ".join("?" for _ in cols)
        values = tuple(row[column] for column in cols)
        insert_sql = f"INSERT INTO {table_sql} ({col_clause}) VALUES ({placeholders})"
        if pk_col not in row:
            cur = self.conn.execute(insert_sql, values)
            self._maybe_commit()
            if emit_query_hook:
                self._on_query()
            pk = row.get(pk_col, cur.lastrowid)
            cur.close()
            return pk

        update_cols = [c for c in cols if c != pk_col]
        started_transaction = False
        if not self._in_transaction:
            self.conn.execute("BEGIN")
            self._in_transaction = True
            started_transaction = True
        try:
            if update_cols:
                assignments = ", ".join(f"{_quote_ident(c)}=?" for c in update_cols)
                update_sql = f"UPDATE {table_sql} SET {assignments} WHERE {_quote_ident(pk_col)}=?"
                update_values = tuple(row[column] for column in update_cols) + (row[pk_col],)
                cur = self.conn.execute(update_sql, update_values)
                updated = cur.rowcount
                cur.close()
                if updated == 0:
                    cur = self.conn.execute(
                        f"INSERT OR IGNORE INTO {table_sql} ({col_clause}) VALUES ({placeholders})",
                        values,
                    )
                    inserted = cur.rowcount
                    cur.close()
                    if inserted == 0:
                        cur = self.conn.execute(update_sql, update_values)
                        cur.close()
            else:
                cur = self.conn.execute(
                    f"INSERT OR IGNORE INTO {table_sql} ({col_clause}) VALUES ({placeholders})",
                    values,
                )
                cur.close()
        except Exception:
            if started_transaction:
                self.conn.rollback()
                self._in_transaction = False
            raise

        if started_transaction:
            self.conn.commit()
            self._in_transaction = False
        if emit_query_hook:
            self._on_query()
        return row.get(pk_col)

    def _legacy_upsert_many_locked(self, table: str, rows: List[Dict[str, Any]]) -> None:
        if not rows:
            return
        _row_columns(rows)
        started_transaction = False
        if not self._in_transaction:
            self.conn.execute("BEGIN")
            self._in_transaction = True
            started_transaction = True
        try:
            for row in rows:
                self._legacy_upsert_one_locked(table, row, emit_query_hook=False)
        except Exception:
            if started_transaction:
                self.conn.rollback()
                self._in_transaction = False
            raise
        if started_transaction:
            self.conn.commit()
            self._in_transaction = False
        self._on_query()

    @require_init
    def delete_one(self, table: str, pk: Any) -> int:
        pk_col = self._primary_key(table)
        sql = f"DELETE FROM {_quote_ident(table)} WHERE {_quote_ident(pk_col)}=?"
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
        sql = f"DELETE FROM {_quote_ident(table)} WHERE {where}"
        cur = self.conn.execute(sql, ps)
        self._maybe_commit()
        self._on_query()
        return cur.rowcount

    @require_init
    def update_one(self, table: str, pk: Any, row: Dict[str, Any]) -> int:
        if not row:
            return 0
        pk_col = self._primary_key(table)
        cols = _row_columns([row])
        assignments = ", ".join(f"{_quote_ident(column)}=?" for column in cols)
        sql = f"UPDATE {_quote_ident(table)} SET {assignments} WHERE {_quote_ident(pk_col)}=?"
        values = tuple(row[column] for column in cols) + (pk,)
        cur = self.conn.execute(sql, values)
        self._maybe_commit()
        self._on_query()
        return cur.rowcount

    @require_init
    def query_one(
        self,
        sql: str,
        params: Union[Sequence[Any], Mapping[str, Any], None] = None,
        postprocess_func: Optional[Callable[[RowType], Any]] = None,
    ) -> Optional[Any]:
        """Fetch single row with parameters.

        ``postprocess_func`` can transform the row before returning it.
        """
        ps = params if params is not None else ()
        logger.debug("Executing SQL: %s; params: %s", sql, ps)
        cur = self.conn.execute(sql, ps)
        row = cur.fetchone()
        cur.close()
        self._on_query()
        if row is None:
            return None
        row_typed = cast(RowType, row)
        if postprocess_func is not None:
            return postprocess_func(row_typed)
        return row_typed

    @require_init
    def query_many(
        self,
        sql: str,
        params: Union[Sequence[Any], Mapping[str, Any], None] = None,
        postprocess_func: Optional[Callable[[RowType], Any]] = None,
    ) -> List[Any]:
        """Fetch all rows with parameters.

        ``postprocess_func`` can transform each row before returning them.
        """
        ps = params if params is not None else ()
        logger.debug("Executing SQL: %s; params: %s", sql, ps)
        cur = self.conn.execute(sql, ps)
        rows = cur.fetchall()
        cur.close()
        self._on_query()
        if postprocess_func is not None:
            return [postprocess_func(cast(RowType, row)) for row in rows]
        return cast(List[RowType], rows)

    @require_init
    def query_many_gen(
        self,
        sql: str,
        params: Union[Sequence[Any], Mapping[str, Any], None] = None,
        postprocess_func: Optional[Callable[[RowType], Any]] = None,
    ) -> Generator[Any, None, None]:
        """Generator fetching rows one by one.

        ``postprocess_func`` can transform each row before yielding it.
        """
        ps = params if params is not None else ()
        logger.debug("Executing SQL: %s; params: %s", sql, ps)
        cur = self.conn.execute(sql, ps)
        try:
            for row in cur:
                row_typed = cast(RowType, row)
                if postprocess_func is None:
                    yield row_typed
                else:
                    yield postprocess_func(row_typed)
        finally:
            cur.close()
        self._on_query()

    @require_init
    def query_scalar(
        self,
        sql: str,
        params: Union[Sequence[Any], Mapping[str, Any], None] = None,
        postprocess_func: Optional[Callable[[RowType], Any]] = None,
    ) -> Any:
        row = self.query_one(sql, params, postprocess_func=postprocess_func)
        return None if row is None else first_column_value(cast(RowType, row), self._rows_as_dict)

    @require_init
    def query_column(
        self,
        sql: str,
        params: Union[Sequence[Any], Mapping[str, Any], None] = None,
        postprocess_func: Optional[Callable[[RowType], Any]] = None,
    ) -> List[Any]:
        rows = self.query_many(sql, params, postprocess_func=postprocess_func)
        return [first_column_value(cast(RowType, row), self._rows_as_dict) for row in rows]

    @require_init
    def query_dict(
        self,
        sql: str,
        params: Union[Sequence[Any], Mapping[str, Any], None] = None,
        *,
        key: Union[str, Callable[[RowType], Any], None] = None,
        value: Union[str, Callable[[RowType], Any], None] = None,
        postprocess_func: Optional[Callable[[RowType], Any]] = None,
    ) -> Dict[Any, Any]:
        rows = self.query_many(sql, params, postprocess_func=postprocess_func)
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

        return {get_key(cast(RowType, row)): get_value(cast(RowType, row)) for row in rows}

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
            current = threading.current_thread()
            for t in self._periodic_threads:
                if t is not current:
                    t.join()
            self._periodic_threads.clear()
            with self._operation_lock:
                if self.conn:
                    if self._in_transaction:
                        self.conn.rollback()
                        self._in_transaction = False
                        self._operation_lock.release()
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
