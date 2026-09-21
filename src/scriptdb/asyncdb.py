import abc
import asyncio
import signal
import logging
import contextlib
import inspect
import re
import traceback
import weakref

from . import daemonizable_aiosqlite
from . import sqlite_backend

try:
    import aiosqlite
except ImportError as exc:
    raise ImportError("aiosqlite is required for async support; install with 'scriptdb[async]'") from exc
from pathlib import Path
from typing import (
    Any,
    Callable,
    Dict,
    List,
    Set,
    Optional,
    Sequence,
    Iterable,
    Mapping,
    Union,
    Type,
    TypeVar,
    AsyncGenerator,
    Generic,
    cast,
    Tuple,
)

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

sqlite3 = sqlite_backend.sqlite3


def _capture_creation_site() -> str:
    """
    Return short path to call location
    <file>:<line> in <func>
    """
    try:
        stack = traceback.extract_stack()
        # remove current frame and site-packages/aiosqlite frames
        filtered = [
            fr
            for fr in stack
            if "site-packages" not in (fr.filename or "")
            and not fr.filename.endswith("asyncdb.py")
            and "aiosqlite" not in (fr.filename or "")
        ]
        if not filtered:
            filtered = stack[-5:]

        head = filtered[-1]
        head_str = f"{head.filename}:{head.lineno} in {head.name}"
    except Exception:
        head_str = "unknown:unknown"
    return head_str


T = TypeVar("T", bound="AsyncBaseDB")

logger = logging.getLogger(__name__)
_SIGNAL_STATES: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, Dict[str, Any]]" = weakref.WeakKeyDictionary()


class _AsyncRLock:
    """A small task-reentrant asyncio lock used to isolate transactions."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._owner: Optional[asyncio.Task] = None
        self._depth = 0

    async def acquire(self) -> None:
        task = asyncio.current_task()
        if task is not None and task is self._owner:
            self._depth += 1
            return
        await self._lock.acquire()
        self._owner = task
        self._depth = 1

    def release(self) -> None:
        if asyncio.current_task() is not self._owner:
            raise RuntimeError("operation lock released by a different task")
        self._depth -= 1
        if self._depth == 0:
            self._owner = None
            self._lock.release()

    async def __aenter__(self) -> "_AsyncRLock":
        await self.acquire()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        self.release()


class _AsyncDBOpenContext(Generic[T]):
    """Internal helper returned by :meth:`AsyncBaseDB.open`.

    This object is both awaitable and an async context manager, allowing the
    database to be opened with either ``await MyDB.open(...)`` or
    ``async with MyDB.open(...) as db``. It lazily constructs and initializes
    the database instance on first use and makes sure the instance is properly
    closed when leaving the context manager.
    """

    def __init__(
        self,
        cls: Type[T],
        db_path: str,
        auto_create: bool,
        use_wal: bool,
        daemonize_thread: bool = False,
        row_factory: RowFactorySetting = sqlite3.Row,
        legacy_sqlite_support: bool = False,
        handle_signals: bool = False,
    ) -> None:
        self._cls = cls
        self._db_path = db_path
        self._auto_create = auto_create
        self._use_wal = use_wal
        self._db: Optional[T] = None
        self._daemonize_thread = daemonize_thread
        self._row_factory, _ = normalize_row_factory(row_factory)
        self._legacy_sqlite_support = legacy_sqlite_support
        self._handle_signals = handle_signals

    async def _open(self) -> T:
        kwargs: Dict[str, Any] = {}
        if supports_row_factory(self._cls):
            kwargs["row_factory"] = self._row_factory
        if supports_init_arg(self._cls, "legacy_sqlite_support"):
            kwargs["legacy_sqlite_support"] = self._legacy_sqlite_support
        if supports_init_arg(self._cls, "handle_signals"):
            kwargs["handle_signals"] = self._handle_signals
        instance = self._cls(self._db_path, **kwargs)  # type: ignore[call-arg]
        if "row_factory" not in kwargs and hasattr(instance, "_set_row_factory"):
            instance._set_row_factory(self._row_factory)  # type: ignore[attr-defined]
        if "legacy_sqlite_support" not in kwargs:
            instance.legacy_sqlite_support = self._legacy_sqlite_support  # type: ignore[attr-defined]
        if "handle_signals" not in kwargs:
            instance.handle_signals = self._handle_signals  # type: ignore[attr-defined]
        instance.auto_create = self._auto_create  # type: ignore[attr-defined]
        instance.use_wal = self._use_wal  # type: ignore[attr-defined]
        instance.daemonize_thread = self._daemonize_thread
        try:
            await instance.init()
            instance._register_signal_handlers()
        except BaseException:
            with contextlib.suppress(BaseException):
                await instance.close()
            raise
        self._db = instance
        return instance

    def __await__(self):
        return self._open().__await__()

    async def __aenter__(self) -> T:
        return await self._open()

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._db is not None:
            await self._db.close()


class AsyncBaseDB(AbstractBaseDB):
    """
    Abstract async SQLite-backed database with migration support via aiosqlite.

    Subclasses must implement migrations() -> List[Dict[str, Any]]:
      each dict must have:
        - "name": str (unique identifier)
        - exactly one of:
            * "sql": str
            * "sqls": Sequence[str]
            * "function": Callable[[aiosqlite.Connection], Any]

    Usage:
        db = await YourDB.open("app.db")
    """

    def __init__(
        self,
        db_path: Union[str, Path],
        auto_create: bool = True,
        *,
        use_wal: bool = True,
        row_factory: RowFactorySetting = sqlite3.Row,
        daemonize_thread: bool = False,
        legacy_sqlite_support: bool = False,
        handle_signals: bool = False,
    ) -> None:
        super().__init__(
            db_path,
            auto_create,
            use_wal=use_wal,
            legacy_sqlite_support=legacy_sqlite_support,
        )
        self.conn: aiosqlite.Connection = cast(aiosqlite.Connection, None)
        self.daemonize_thread: bool = daemonize_thread
        self.handle_signals = handle_signals
        self._periodic_tasks: List[asyncio.Task] = []
        self._query_tasks: List[asyncio.Task] = []
        self._close_lock = asyncio.Lock()
        self._operation_lock = _AsyncRLock()
        self._upsert_lock = self._operation_lock
        self._signal_loop: Optional[asyncio.AbstractEventLoop] = None
        self._signals_registered = False
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

    def _register_signal_handlers(self) -> None:
        if not self.handle_signals or self._signals_registered:
            return
        loop = asyncio.get_running_loop()
        state = _SIGNAL_STATES.get(loop)
        if state is None:
            instances: "weakref.WeakSet[AsyncBaseDB]" = weakref.WeakSet()
            previous: Dict[signal.Signals, Any] = {}
            installed: Dict[signal.Signals, Any] = {}
            state = {"instances": instances, "previous": previous, "installed": installed}
            _SIGNAL_STATES[loop] = state

            def _handler() -> None:
                for instance in list(instances):
                    asyncio.create_task(instance.close())

            try:
                for sig in (signal.SIGINT, signal.SIGTERM):
                    old_handle = getattr(loop, "_signal_handlers", {}).get(sig)
                    if old_handle is not None and not old_handle.cancelled():
                        previous[sig] = ("loop", old_handle._callback, old_handle._args)
                    else:
                        previous[sig] = ("signal", signal.getsignal(sig))
                    loop.add_signal_handler(sig, _handler)
                    installed[sig] = getattr(loop, "_signal_handlers", {}).get(sig)
            except BaseException:
                for sig, installed_handle in installed.items():
                    current = getattr(loop, "_signal_handlers", {}).get(sig)
                    if current is not installed_handle:
                        continue
                    loop.remove_signal_handler(sig)
                    previous_handler = previous.get(sig)
                    if previous_handler is None:
                        continue
                    kind, callback, *rest = previous_handler
                    if kind == "loop":
                        loop.add_signal_handler(sig, callback, *rest[0])
                    else:
                        signal.signal(sig, callback)
                _SIGNAL_STATES.pop(loop, None)
                raise
        state["instances"].add(self)
        self._signal_loop = loop
        self._signals_registered = True

    def _unregister_signal_handlers(self) -> None:
        if not self._signals_registered or self._signal_loop is None:
            return
        loop = self._signal_loop
        state = _SIGNAL_STATES.get(loop)
        if state is not None:
            state["instances"].discard(self)
            if not state["instances"]:
                for sig in (signal.SIGINT, signal.SIGTERM):
                    current = getattr(loop, "_signal_handlers", {}).get(sig)
                    if current is not state["installed"].get(sig):
                        continue
                    loop.remove_signal_handler(sig)
                    previous = state["previous"].get(sig)
                    if previous is None:
                        continue
                    kind, callback, *rest = previous
                    if kind == "loop":
                        loop.add_signal_handler(sig, callback, *rest[0])
                    else:
                        signal.signal(sig, callback)
                _SIGNAL_STATES.pop(loop, None)
        self._signals_registered = False
        self._signal_loop = None

    async def _maybe_commit(self) -> None:
        if not self._in_transaction:
            await self.conn.commit()

    @require_init
    async def begin(self) -> None:
        if self._in_transaction:
            raise RuntimeError("A transaction is already in progress")
        await self._operation_lock.acquire()
        try:
            await self.conn.execute("BEGIN")
            self._in_transaction = True
        except BaseException:
            self._operation_lock.release()
            raise

    @require_init
    async def commit(self) -> None:
        if not self._in_transaction:
            raise RuntimeError("No active transaction to commit")
        await self.conn.commit()
        self._in_transaction = False
        self._operation_lock.release()

    @require_init
    async def rollback(self) -> None:
        if not self._in_transaction:
            raise RuntimeError("No active transaction to roll back")
        await self.conn.rollback()
        self._in_transaction = False
        self._operation_lock.release()

    @contextlib.asynccontextmanager
    async def transaction(self):
        if not self.initialized or self.conn is None:
            raise self._not_initialized_error()
        await self.begin()
        committed = False
        try:
            yield
            committed = True
        except BaseException:
            if self._in_transaction:
                await self.rollback()
            raise
        finally:
            if committed and self._in_transaction:
                await self.commit()

    @classmethod
    def open(
        cls: Type[T],
        db_path: Union[str, Path],
        *,
        auto_create: bool = True,
        use_wal: bool = True,
        daemonize_thread: bool = False,
        row_factory: RowFactorySetting = sqlite3.Row,
        legacy_sqlite_support: bool = False,
        handle_signals: bool = False,
    ) -> _AsyncDBOpenContext[T]:
        """
        Factory returning an awaitable context manager for the database instance.

        Usage:
            ``db = await YourDB.open("app.db")`` or
            ``async with YourDB.open("app.db") as db: ...``

        Parameters:
            db_path: Filesystem path to the SQLite database.
            auto_create: Whether to create the database file if it does not exist.
            use_wal: Enable SQLite's WAL journal mode. Pass ``False`` to disable.
            daemonize_thread: Make background thread to be daemonize, set it to True if program hangs on exit
            handle_signals: Register SIGINT/SIGTERM handlers that close the database.

        Returns:
            Awaitable context manager yielding an initialized subclass instance of type T.
        """
        path_obj = Path(db_path)
        if not auto_create and not path_obj.exists():
            raise RuntimeError(f"Database file {db_path} does not exist")
        return _AsyncDBOpenContext(
            cls,
            str(path_obj),
            auto_create,
            use_wal,
            daemonize_thread,
            row_factory,
            legacy_sqlite_support,
            handle_signals,
        )

    @abc.abstractmethod
    def migrations(self) -> List[Dict[str, Any]]:
        """
        Return ordered list of migration dicts.
        Each dict must include:
          - name: str
          - exactly one of:
              * sql: str
              * sqls: Sequence[str]
              * function: Callable
        """
        raise NotImplementedError

    async def init(self) -> None:
        """
        Initialize the database connection and apply pending migrations.
        """
        if self.conn is not None or self.initialized:
            raise RuntimeError("database is already initialized")
        if self._is_closed:
            self._close_lock = asyncio.Lock()
            self._operation_lock = _AsyncRLock()
            self._upsert_lock = self._operation_lock
        self.conn = await daemonizable_aiosqlite.connect(
            self.db_path, daemonize_thread=self.daemonize_thread, creation_site=_capture_creation_site()
        )
        self._pk_cache.clear()
        for hook in self._query_hooks:
            hook["count"] = 0
            hook["running"] = False

        if getattr(self, "use_wal", False):
            await self.conn.execute("PRAGMA journal_mode=WAL")
        await self.conn.execute("PRAGMA foreign_keys=ON")
        self._configure_row_factory()
        self._is_closed = False
        self.initialized = True
        try:
            await self._ensure_migrations_table()
            await self._apply_migrations()
        except BaseException:
            self.initialized = False
            await self.conn.close()
            self.conn = cast(aiosqlite.Connection, None)
            raise

        for seconds, method in self._periodic_specs:

            async def runner(method=method, seconds=seconds):
                while True:
                    try:
                        logger.debug("Launching method %s", method.__name__)
                        await method()
                        logger.debug(
                            "Method %s finished, next run in %s seconds",
                            method.__name__,
                            seconds,
                        )
                        if not self.initialized:
                            return
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        logger.exception("Periodic method %s failed", method.__name__)
                    await asyncio.sleep(seconds)

            task = asyncio.create_task(runner())
            self._periodic_tasks.append(task)

            def _cleanup(t: asyncio.Task, tasks=self._periodic_tasks) -> None:
                with contextlib.suppress(ValueError):
                    tasks.remove(t)

            task.add_done_callback(_cleanup)

    async def _ensure_migrations_table(self) -> None:
        sql = _get_migrations_table_sql()
        logger.debug("Executing SQL: %s", sql)
        await self.conn.execute(sql)
        await self._maybe_commit()

    async def _applied_versions(self) -> Set[str]:
        sql = "SELECT name FROM applied_migrations"
        logger.debug("Executing SQL: %s", sql)
        cur = await self.conn.execute(sql)
        rows = await cur.fetchall()
        await cur.close()
        return {row["name"] for row in rows}

    async def _apply_migrations(self) -> None:
        migrations_list = self.migrations()
        applied = await self._applied_versions()
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
                    logger.debug(
                        "Applying migration by executing SQL script: %s",
                        script,
                    )
                    await self.conn.executescript(script)
                except BaseException as exc:
                    if self.conn.in_transaction:
                        await self.conn.rollback()
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
                    await self.conn.executescript(script)
                except BaseException as exc:
                    if self.conn.in_transaction:
                        await self.conn.rollback()
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
                if not callable(func) or not inspect.iscoroutinefunction(target):
                    raise TypeError(f"'function' for migration {name} must be an async function")
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
                    await self.conn.execute("BEGIN")
                    self._in_transaction = True
                    await func(*call_args)
                    sql = "INSERT INTO applied_migrations(name) VALUES (?)"
                    await self.conn.execute(sql, (name,))
                    await self.conn.commit()
                    self._in_transaction = False
                except BaseException as exc:
                    if self.conn.in_transaction:
                        await self.conn.rollback()
                    self._in_transaction = False
                    if isinstance(exc, Exception):
                        raise RuntimeError(f"Error while applying migration {name}: {exc}") from exc
                    raise

    @require_init
    async def _primary_key(self, table: str) -> str:
        """Return the name of ``table``'s primary key column, caching lookups."""
        if table not in self._pk_cache:
            sql = "SELECT name, pk FROM pragma_table_info(?)"
            logger.debug("Executing SQL: %s", sql)
            cur = await self.conn.execute(sql, (table,))
            rows = await cur.fetchall()
            await cur.close()
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
        """Run registered query hooks once their configured interval is reached."""
        for hook in self._query_hooks:
            if hook["running"]:
                continue
            hook["count"] += 1
            if hook["count"] >= hook["interval"]:
                hook["count"] = 0
                hook["running"] = True

                async def runner(method=hook["method"], interval=hook["interval"], hook_state=hook):
                    try:
                        logger.debug("Launching method %s", method.__name__)
                        await method()
                        logger.debug(
                            "Method %s finished, next run after %s queries",
                            method.__name__,
                            interval,
                        )
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        logger.exception("Query hook %s failed", method.__name__)
                    finally:
                        hook_state["running"] = False

                task = asyncio.create_task(runner())
                self._query_tasks.append(task)

                def _cleanup(t: asyncio.Task, tasks=self._query_tasks) -> None:
                    with contextlib.suppress(ValueError):
                        tasks.remove(t)

                task.add_done_callback(_cleanup)

    @require_init
    async def execute(self, sql: str, params: Union[Sequence[Any], Mapping[str, Any], None] = None) -> aiosqlite.Cursor:
        """
        Execute a statement with positional or named parameters and commit.
        Returns aiosqlite.Cursor.

        Example:
            cur = await db.execute(
                "INSERT INTO t(x) VALUES(?)", (1,)
            )
            print(cur.lastrowid)
        """
        ps = params if params is not None else ()
        logger.debug("Executing SQL: %s; params: %s", sql, ps)
        cur = await self.conn.execute(sql, ps)
        await self._maybe_commit()
        self._on_query()
        return cur

    @require_init
    async def execute_many(self, sql: str, seq_params: Iterable[Sequence[Any]]) -> aiosqlite.Cursor:
        """
        Execute many positional statements and commit.
        Returns aiosqlite.Cursor.

        Example:
            cur = await db.execute_many(
                "INSERT INTO t(x) VALUES(?)",
                [(1,), (2,), (3,)]
            )
            print(cur.rowcount)
        """
        logger.debug("Executing many SQL: %s; params: %s", sql, seq_params)
        cur = await self._execute_many_atomic(sql, seq_params)
        await self._maybe_commit()
        self._on_query()
        return cur

    async def _execute_many_atomic(
        self, sql: str, seq_params: Iterable[Sequence[Any]]
    ) -> aiosqlite.Cursor:
        savepoint = "scriptdb_batch"
        await self.conn.execute(f"SAVEPOINT {savepoint}")
        try:
            cur = await self.conn.executemany(sql, seq_params)
            await self.conn.execute(f"RELEASE SAVEPOINT {savepoint}")
            return cur
        except BaseException:
            await self.conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            await self.conn.execute(f"RELEASE SAVEPOINT {savepoint}")
            raise

    @require_init
    async def insert_one(self, table: str, row: Dict[str, Any]) -> Any:
        """
        Insert a single row into ``table``. Returns primary key of the new row.

        Example:
            pk = await db.insert_one("t", {"x": 1})
            print(pk)
        """
        pk_col = await self._primary_key(table)
        table_sql = _quote_ident(table)
        if not row:
            cur = await self.conn.execute(f"INSERT INTO {table_sql} DEFAULT VALUES")
            await self._maybe_commit()
            self._on_query()
            pk = cur.lastrowid
            await cur.close()
            return pk
        cols = _row_columns([row])
        col_clause = ", ".join(_quote_ident(column) for column in cols)
        placeholders = ", ".join("?" for _ in cols)
        sql = f"INSERT INTO {table_sql} ({col_clause}) VALUES ({placeholders})"
        logger.debug("Executing SQL: %s; params: %s", sql, row)
        cur = await self.conn.execute(sql, tuple(row[column] for column in cols))
        await self._maybe_commit()
        self._on_query()
        pk = row.get(pk_col, cur.lastrowid)
        await cur.close()
        return pk

    @require_init
    async def insert_many(self, table: str, rows: List[Dict[str, Any]]) -> None:
        """
        Insert multiple rows into ``table``.

        Example:
            await db.insert_many("t", [{"x": 1}, {"x": 2}])
        """
        if not rows:
            return
        cols = _row_columns(rows)
        col_clause = ", ".join(_quote_ident(column) for column in cols)
        placeholders = ", ".join("?" for _ in cols)
        sql = f"INSERT INTO {_quote_ident(table)} ({col_clause}) VALUES ({placeholders})"
        values = [tuple(row[column] for column in cols) for row in rows]
        await self._execute_many_atomic(sql, values)
        await self._maybe_commit()
        self._on_query()

    @require_init
    async def upsert_one(self, table: str, row: Dict[str, Any]) -> Any:
        """
        Insert or update a single row based on the table's primary key.
        Returns the primary key of the affected row.

        If the primary key column is omitted, the row is inserted and the
        generated key is returned.

        Example:
            pk = await db.upsert_one("t", {"id": 1, "x": 2})
        """
        async with self._upsert_lock:
            if self._use_legacy_upsert():
                self._warn_legacy_upsert_once()
                return await self._legacy_upsert_one_locked(table, row)
            sqlite_backend.ensure_upsert_supported()
            pk_col = await self._primary_key(table)
            if not row:
                return await self.insert_one(table, row)
            cols = _row_columns([row])
            col_clause = ", ".join(_quote_ident(column) for column in cols)
            placeholders = ", ".join("?" for _ in cols)
            insert_sql = f"INSERT INTO {_quote_ident(table)} ({col_clause}) VALUES ({placeholders})"
            values = tuple(row[column] for column in cols)

            # When no primary key is supplied, behave like a simple insert.
            if pk_col not in row:
                cur = await self.execute(insert_sql, values)
                return cur.lastrowid

            update_cols = [c for c in cols if c != pk_col]
            if update_cols:
                assignments = ", ".join(
                    f"{_quote_ident(c)}=excluded.{_quote_ident(c)}" for c in update_cols
                )
                upsert_sql = f"{insert_sql} ON CONFLICT({_quote_ident(pk_col)}) DO UPDATE SET {assignments}"
            else:
                upsert_sql = f"{insert_sql} ON CONFLICT({_quote_ident(pk_col)}) DO NOTHING"

            cur = await self.execute(upsert_sql, values)
            return row.get(pk_col, cur.lastrowid)

    @require_init
    async def upsert_many(self, table: str, rows: List[Dict[str, Any]]) -> None:
        """
        Insert or update multiple rows based on the table's primary key.

        Example:
            await db.upsert_many("t", [{"id": 1, "x": 2}, {"id": 2, "x": 3}])
        """
        async with self._upsert_lock:
            if self._use_legacy_upsert():
                self._warn_legacy_upsert_once()
                await self._legacy_upsert_many_locked(table, rows)
                return
            sqlite_backend.ensure_upsert_supported()
            if not rows:
                return
            pk_col = await self._primary_key(table)
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
            await self._execute_many_atomic(upsert_sql, values)
        await self._maybe_commit()
        self._on_query()

    async def _legacy_upsert_one_locked(self, table: str, row: Dict[str, Any], *, emit_query_hook: bool = True) -> Any:
        pk_col = await self._primary_key(table)
        table_sql = _quote_ident(table)
        if not row:
            cur = await self.conn.execute(f"INSERT INTO {table_sql} DEFAULT VALUES")
            await self._maybe_commit()
            if emit_query_hook:
                self._on_query()
            lastrowid = cur.lastrowid
            await cur.close()
            return lastrowid
        cols = _row_columns([row])
        col_clause = ", ".join(_quote_ident(column) for column in cols)
        placeholders = ", ".join("?" for _ in cols)
        values = tuple(row[column] for column in cols)
        insert_sql = f"INSERT INTO {table_sql} ({col_clause}) VALUES ({placeholders})"
        if pk_col not in row:
            cur = await self.conn.execute(insert_sql, values)
            await self._maybe_commit()
            if emit_query_hook:
                self._on_query()
            lastrowid = cur.lastrowid
            await cur.close()
            return row.get(pk_col, lastrowid)

        update_cols = [c for c in cols if c != pk_col]
        started_transaction = False
        if not self._in_transaction:
            await self.conn.execute("BEGIN")
            self._in_transaction = True
            started_transaction = True
        try:
            if update_cols:
                assignments = ", ".join(f"{_quote_ident(c)}=?" for c in update_cols)
                update_sql = f"UPDATE {table_sql} SET {assignments} WHERE {_quote_ident(pk_col)}=?"
                update_values = tuple(row[column] for column in update_cols) + (row[pk_col],)
                cur = await self.conn.execute(update_sql, update_values)
                updated = cur.rowcount
                await cur.close()
                if updated == 0:
                    cur = await self.conn.execute(
                        f"INSERT OR IGNORE INTO {table_sql} ({col_clause}) VALUES ({placeholders})",
                        values,
                    )
                    inserted = cur.rowcount
                    await cur.close()
                    if inserted == 0:
                        cur = await self.conn.execute(update_sql, update_values)
                        await cur.close()
            else:
                cur = await self.conn.execute(
                    f"INSERT OR IGNORE INTO {table_sql} ({col_clause}) VALUES ({placeholders})",
                    values,
                )
                await cur.close()
        except Exception:
            if started_transaction:
                await self.conn.rollback()
                self._in_transaction = False
            raise

        if started_transaction:
            await self.conn.commit()
            self._in_transaction = False
        if emit_query_hook:
            self._on_query()
        return row.get(pk_col)

    async def _legacy_upsert_many_locked(self, table: str, rows: List[Dict[str, Any]]) -> None:
        if not rows:
            return
        _row_columns(rows)
        started_transaction = False
        if not self._in_transaction:
            await self.conn.execute("BEGIN")
            self._in_transaction = True
            started_transaction = True
        try:
            for row in rows:
                await self._legacy_upsert_one_locked(table, row, emit_query_hook=False)
        except Exception:
            if started_transaction:
                await self.conn.rollback()
                self._in_transaction = False
            raise
        if started_transaction:
            await self.conn.commit()
            self._in_transaction = False
        self._on_query()

    @require_init
    async def update_one(self, table: str, pk: Any, data: Dict[str, Any]) -> int:
        """
        Update a single row identified by primary key with the provided columns.
        Returns number of updated rows (0 or 1).

        Example:
            await db.update_one("t", 1, {"x": 2})
        """
        if not data:
            return 0
        pk_col = await self._primary_key(table)
        cols = _row_columns([data])
        assignments = ", ".join(f"{_quote_ident(column)} = ?" for column in cols)
        values = tuple(data[column] for column in cols) + (pk,)
        sql = f"UPDATE {_quote_ident(table)} SET {assignments} WHERE {_quote_ident(pk_col)} = ?"
        cur = await self.execute(sql, values)
        return cur.rowcount

    @require_init
    async def delete_one(self, table: str, pk: Any) -> int:
        """
        Delete a single row from ``table`` by primary key. Returns number of
        deleted rows (0 or 1).

        Example:
            await db.delete_one("t", 1)
        """
        pk_col = await self._primary_key(table)
        sql = f"DELETE FROM {_quote_ident(table)} WHERE {_quote_ident(pk_col)} = ?"
        cur = await self.execute(sql, (pk,))
        return cur.rowcount

    @require_init
    async def delete_many(
        self,
        table: str,
        where: str,
        params: Union[Sequence[Any], Mapping[str, Any], None] = None,
    ) -> int:
        """
        Delete multiple rows from ``table`` matching ``where`` condition.
        Returns number of deleted rows.

        Example:
            await db.delete_many("t", "x > ?", (10,))
        """
        sql = f"DELETE FROM {_quote_ident(table)} WHERE {where}"
        cur = await self.execute(sql, params)
        return cur.rowcount

    @require_init
    async def query_many(
        self,
        sql: str,
        params: Union[Sequence[Any], Mapping[str, Any], None] = None,
        postprocess_func: Optional[Callable[[RowType], Any]] = None,
    ) -> List[Any]:
        """
        Fetch all rows with parameters. Returns ``List`` of the configured row type.

        ``postprocess_func`` can transform each row before returning them.

        Example:
            rows = await db.query_many(
                "SELECT x FROM t WHERE x > ?", (0,)
            )
            for row in rows:
                print(row["x"])
        """
        ps = params if params is not None else ()
        logger.debug("Executing SQL: %s; params: %s", sql, ps)
        cur = await self.conn.execute(sql, ps)
        rows = list(await cur.fetchall())
        await cur.close()
        self._on_query()
        if postprocess_func is not None:
            return [postprocess_func(cast(RowType, row)) for row in rows]
        return cast(List[RowType], rows)

    @require_init
    async def query_many_gen(
        self,
        sql: str,
        params: Union[Sequence[Any], Mapping[str, Any], None] = None,
        postprocess_func: Optional[Callable[[RowType], Any]] = None,
    ) -> AsyncGenerator[Any, None]:
        """
        Async generator fetching rows one by one. Yields objects produced by the
        configured row factory. ``postprocess_func`` can transform each row
        before yielding it.

        Example:
            async for row in db.query_many_gen(
                "SELECT x FROM t WHERE x > ?", (0,)
            ):
                print(row["x"])
        """
        ps = params if params is not None else ()
        logger.debug("Executing SQL: %s; params: %s", sql, ps)
        async with self.conn.execute(sql, ps) as cur:
            self._on_query()
            async for row in cur:
                row_typed = cast(RowType, row)
                if postprocess_func is None:
                    yield row_typed
                else:
                    yield postprocess_func(row_typed)

    @require_init
    async def query_one(
        self,
        sql: str,
        params: Union[Sequence[Any], Mapping[str, Any], None] = None,
        postprocess_func: Optional[Callable[[RowType], Any]] = None,
    ) -> Optional[Any]:
        """
        Fetch single row with parameters. Returns row of configured type or ``None``.

        ``postprocess_func`` can transform the row before returning it.

        Example:
            row = await db.query_one(
                "SELECT x FROM t WHERE id = :id", {"id": 1}
            )
            if row:
                print(row["x"])
        """
        ps = params if params is not None else ()
        logger.debug("Executing SQL: %s; params: %s", sql, ps)
        cur = await self.conn.execute(sql, ps)
        row = await cur.fetchone()
        await cur.close()
        self._on_query()
        if row is None:
            return None
        row_typed = cast(RowType, row)
        if postprocess_func is not None:
            return postprocess_func(row_typed)
        return row_typed

    @require_init
    async def query_scalar(
        self,
        sql: str,
        params: Union[Sequence[Any], Mapping[str, Any], None] = None,
        postprocess_func: Optional[Callable[[RowType], Any]] = None,
    ) -> Any:
        """Execute a query and return the first column of the first row.

        ``postprocess_func`` can transform the fetched row before extracting
        the first column.

        Example:
            count = await db.query_scalar(
                "SELECT COUNT(*) FROM t WHERE x > ?", (0,)
            )
        """
        row = await self.query_one(sql, params, postprocess_func=postprocess_func)
        return None if row is None else first_column_value(cast(RowType, row), self._rows_as_dict)

    @require_init
    async def query_column(
        self,
        sql: str,
        params: Union[Sequence[Any], Mapping[str, Any], None] = None,
        postprocess_func: Optional[Callable[[RowType], Any]] = None,
    ) -> List[Any]:
        """Execute a query and return a list of the first column from each row.

        ``postprocess_func`` can transform each row before column extraction.

        Example:
            ids = await db.query_column(
                "SELECT id FROM t WHERE x > ?", (0,)
            )
        """
        rows = await self.query_many(sql, params, postprocess_func=postprocess_func)
        return [first_column_value(cast(RowType, row), self._rows_as_dict) for row in rows]

    @require_init
    async def query_dict(
        self,
        sql: str,
        params: Union[Sequence[Any], Mapping[str, Any], None] = None,
        *,
        key: Union[str, Callable[[RowType], Any], None] = None,
        value: Union[str, Callable[[RowType], Any], None] = None,
        postprocess_func: Optional[Callable[[RowType], Any]] = None,
    ) -> Dict[Any, Any]:
        """Execute a query and return a dictionary built from rows.

        ``key`` may be a column name, a callable receiving each row, or ``None``
        to automatically use the table's primary key column.

        ``value`` controls the dictionary's values. When ``None`` (default) each
        row is stored. If a string, the named column from each row is used. If a
        callable, its return value is stored for each row. ``postprocess_func``
        can transform each row before key/value selection.

        Examples:
            # Use primary key column automatically
            users = await db.query_dict("SELECT * FROM users")

            # Explicit column names for key and value
            names = await db.query_dict(
                "SELECT id, name FROM users", key="id", value="name"
            )

            # Callables for custom key and value
            users = await db.query_dict(
                "SELECT * FROM users",
                key=lambda row: row["id"],
                value=lambda row: f"{row['first_name']} {row['last_name']}",
            )
        """
        rows = await self.query_many(sql, params, postprocess_func=postprocess_func)

        if key is None:
            match = re.search(
                r"from\s+(?:\"([A-Za-z_][\w]*)\"|'([A-Za-z_][\w]*)'|([A-Za-z_][\w]*))",
                sql,
                re.IGNORECASE,
            )
            if not match:
                raise ValueError(
                    "Cannot determine table name from sql, so cannot deduce primary "
                    "key, please provide non-empty 'key' argument"
                )
            table = match.group(1) or match.group(2) or match.group(3)
            key = await self._primary_key(table)

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

    async def close(self) -> None:
        """Close the database connection.

        The method is idempotent and guarded by a lock so it can be safely
        invoked multiple times, including concurrently via signal handlers
        while an ``async with`` context is exiting.
        """
        async with self._close_lock:
            if not self.initialized and self.conn is None:
                self._is_closed = True
                return
            if self._signals_registered and self._signal_loop is not None:
                self._unregister_signal_handlers()
            current_task = asyncio.current_task()
            for task in self._periodic_tasks:
                if task is current_task:
                    continue
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            self._periodic_tasks.clear()

            for task in list(self._query_tasks):
                if task is current_task:
                    continue
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            self._query_tasks.clear()
            async with self._operation_lock:
                if self.conn:
                    if self._in_transaction:
                        await self.conn.rollback()
                        self._in_transaction = False
                        self._operation_lock.release()
                    await self.conn.close()
                    self.conn = cast(aiosqlite.Connection, None)
                self.initialized = False
                self._is_closed = True

    async def __aenter__(self: T) -> T:
        if not self.initialized:
            try:
                await self.init()
            except BaseException:
                with contextlib.suppress(BaseException):
                    await self.close()
                raise
        self._register_signal_handlers()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()


# Alias for backward compatibility
BaseDB = AsyncBaseDB
