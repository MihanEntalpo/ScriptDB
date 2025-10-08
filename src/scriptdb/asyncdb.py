import abc
import sqlite3
import asyncio
import signal
import logging
import contextlib
import inspect
import re
import traceback

from . import daemonizable_aiosqlite

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
)

from .abstractdb import AbstractBaseDB, require_init, _get_migrations_table_sql
from .dbbuilder import _SQLBuilder
from ._rowfactory import (
    RowFactorySetting,
    RowType,
    dict_row_factory,
    first_column_value,
    normalize_row_factory,
    supports_row_factory,
)


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
    ) -> None:
        self._cls = cls
        self._db_path = db_path
        self._auto_create = auto_create
        self._use_wal = use_wal
        self._db: Optional[T] = None
        self._daemonize_thread = daemonize_thread
        self._row_factory, _ = normalize_row_factory(row_factory)

    async def _open(self) -> T:
        if supports_row_factory(self._cls):
            instance = self._cls(self._db_path, row_factory=self._row_factory)  # type: ignore
        else:
            instance = self._cls(self._db_path)  # type: ignore
            if hasattr(instance, "_set_row_factory"):
                instance._set_row_factory(self._row_factory)  # type: ignore[attr-defined]
        instance.auto_create = self._auto_create  # type: ignore[attr-defined]
        instance.use_wal = self._use_wal  # type: ignore[attr-defined]
        instance.daemonize_thread = self._daemonize_thread
        await instance.init()
        instance._register_signal_handlers()
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
    ) -> None:
        super().__init__(db_path, auto_create, use_wal=use_wal)
        self.conn: aiosqlite.Connection = cast(aiosqlite.Connection, None)
        self.daemonize_thread: bool = daemonize_thread
        self._periodic_tasks: List[asyncio.Task] = []
        self._query_tasks: List[asyncio.Task] = []
        self._upsert_lock = asyncio.Lock()
        self._close_lock = asyncio.Lock()
        self._signal_loop: Optional[asyncio.AbstractEventLoop] = None
        self._signals_registered = False
        self._row_factory_setting: RowFactorySetting = sqlite3.Row
        self._rows_as_dict = False
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
        if self._signals_registered:
            return
        loop = asyncio.get_running_loop()

        def _handler() -> None:
            asyncio.create_task(self.close())

        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, _handler)
        self._signal_loop = loop
        self._signals_registered = True

    @classmethod
    def open(
        cls: Type[T],
        db_path: Union[str, Path],
        *,
        auto_create: bool = True,
        use_wal: bool = True,
        daemonize_thread: bool = False,
        row_factory: RowFactorySetting = sqlite3.Row,
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

        Returns:
            Awaitable context manager yielding an initialized subclass instance of type T.
        """
        path_obj = Path(db_path)
        if not auto_create and not path_obj.exists():
            raise RuntimeError(f"Database file {db_path} does not exist")
        return _AsyncDBOpenContext(cls, str(path_obj), auto_create, use_wal, daemonize_thread, row_factory)

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
        self.conn = await daemonizable_aiosqlite.connect(
            self.db_path, daemonize_thread=self.daemonize_thread, creation_site=_capture_creation_site()
        )

        if getattr(self, "use_wal", False):
            await self.conn.execute("PRAGMA journal_mode=WAL")
        self._configure_row_factory()
        await self._ensure_migrations_table()
        await self._apply_migrations()
        self.initialized = True

        for seconds, method in self._periodic_specs:

            async def runner(method=method, seconds=seconds):
                while True:
                    logger.debug("Launching method %s", method.__name__)
                    await method()
                    logger.debug(
                        "Method %s finished, next run in %s seconds",
                        method.__name__,
                        seconds,
                    )
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
        await self.conn.commit()

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
                try:
                    logger.debug(
                        "Applying migration by executing SQL script: %s",
                        sql,
                    )
                    await self.conn.executescript(sql)
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
                    rendered.append(sql)
                try:
                    await self.conn.execute("BEGIN")
                    try:
                        for sql in rendered:
                            logger.debug(
                                "Applying migration by executing SQL script: %s",
                                sql,
                            )
                            await self.conn.execute(sql)
                    except BaseException:
                        await self.conn.rollback()
                        raise
                    else:
                        await self.conn.commit()
                except Exception as exc:
                    raise RuntimeError(f"Error while applying migration {name}: {exc}") from exc
            else:
                func = mig["function"]
                if not callable(func) or not inspect.iscoroutinefunction(func):
                    raise TypeError(f"'function' for migration {name} must be an async function")
                try:
                    await func(self, migrations_list, name)
                except Exception as exc:
                    raise RuntimeError(f"Error while applying migration {name}: {exc}") from exc

            sql = "INSERT INTO applied_migrations(name) VALUES (?)"
            logger.debug("Executing SQL: %s; params: (%s,)", sql, name)
            await self.conn.execute(sql, (name,))
            await self.conn.commit()

    @require_init
    async def _primary_key(self, table: str) -> str:
        """Return the name of ``table``'s primary key column, caching lookups."""
        if table not in self._pk_cache:
            sql = f"PRAGMA table_info({table})"
            logger.debug("Executing SQL: %s", sql)
            cur = await self.conn.execute(sql)
            rows = await cur.fetchall()
            await cur.close()
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
            hook["count"] += 1
            if hook["count"] >= hook["interval"]:
                hook["count"] = 0

                async def runner(method=hook["method"], interval=hook["interval"]):
                    logger.debug("Launching method %s", method.__name__)
                    await method()
                    logger.debug(
                        "Method %s finished, next run after %s queries",
                        method.__name__,
                        interval,
                    )

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
        await self.conn.commit()
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
        cur = await self.conn.executemany(sql, seq_params)
        await self.conn.commit()
        self._on_query()
        return cur

    @require_init
    async def insert_one(self, table: str, row: Dict[str, Any]) -> Any:
        """
        Insert a single row into ``table``. Returns primary key of the new row.

        Example:
            pk = await db.insert_one("t", {"x": 1})
            print(pk)
        """
        pk_col = await self._primary_key(table)
        cols = ", ".join(row.keys())
        placeholders = ", ".join([f":{c}" for c in row])
        sql = f"INSERT INTO {table} ({cols}) VALUES ({placeholders})"
        logger.debug("Executing SQL: %s; params: %s", sql, row)
        cur = await self.conn.execute(sql, row)
        await self.conn.commit()
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
        cols = rows[0].keys()
        col_clause = ", ".join(cols)
        placeholders = ", ".join([f":{c}" for c in cols])
        sql = f"INSERT INTO {table} ({col_clause}) VALUES ({placeholders})"
        await self.conn.executemany(sql, rows)
        await self.conn.commit()
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
            pk_col = await self._primary_key(table)
            cols = row.keys()
            col_clause = ", ".join(cols)
            placeholders = ", ".join([f":{c}" for c in cols])
            insert_sql = f"INSERT INTO {table} ({col_clause}) VALUES ({placeholders})"

            # When no primary key is supplied, behave like a simple insert.
            if pk_col not in row:
                cur = await self.execute(insert_sql, row)
                return cur.lastrowid

            update_cols = [c for c in cols if c != pk_col]
            update_sql = ""
            if update_cols:
                assignments = ", ".join([f"{c}=:{c}" for c in update_cols])
                update_sql = f"UPDATE {table} SET {assignments} WHERE {pk_col} = :{pk_col}"

            try:
                cur = await self.execute(insert_sql, row)
                return row.get(pk_col, cur.lastrowid)
            except sqlite3.IntegrityError:
                if update_sql:
                    await self.execute(update_sql, row)
                return row[pk_col]

    @require_init
    async def upsert_many(self, table: str, rows: List[Dict[str, Any]]) -> None:
        """
        Insert or update multiple rows based on the table's primary key.

        Example:
            await db.upsert_many("t", [{"id": 1, "x": 2}, {"id": 2, "x": 3}])
        """
        async with self._upsert_lock:
            if not rows:
                return
            pk_col = await self._primary_key(table)
            cols = rows[0].keys()
            col_clause = ", ".join(cols)
            placeholders = ", ".join([f":{c}" for c in cols])
            insert_sql = f"INSERT INTO {table} ({col_clause}) VALUES ({placeholders})"
            update_cols = [c for c in cols if c != pk_col]
            update_sql = ""
            if update_cols:
                assignments = ", ".join([f"{c}=:{c}" for c in update_cols])
                update_sql = f"UPDATE {table} SET {assignments} WHERE {pk_col} = :{pk_col}"
            for row in rows:
                try:
                    logger.debug("Executing SQL: %s; params: %s", insert_sql, row)
                    await self.conn.execute(insert_sql, row)
                except sqlite3.IntegrityError:
                    if update_sql:
                        logger.debug("Executing SQL: %s; params: %s", update_sql, row)
                        await self.conn.execute(update_sql, row)
            await self.conn.commit()
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
        assignments = ", ".join([f"{c} = :{c}" for c in data])
        params = dict(data)
        params["pk"] = pk
        sql = f"UPDATE {table} SET {assignments} WHERE {pk_col} = :pk"
        cur = await self.execute(sql, params)
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
        sql = f"DELETE FROM {table} WHERE {pk_col} = ?"
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
        sql = f"DELETE FROM {table} WHERE {where}"
        cur = await self.execute(sql, params)
        return cur.rowcount

    @require_init
    async def query_many(
        self, sql: str, params: Union[Sequence[Any], Mapping[str, Any], None] = None
    ) -> List[RowType]:
        """
        Fetch all rows with parameters. Returns ``List`` of the configured row type.

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
        return cast(List[RowType], rows)

    @require_init
    async def query_many_gen(
        self, sql: str, params: Union[Sequence[Any], Mapping[str, Any], None] = None
    ) -> AsyncGenerator[RowType, None]:
        """
        Async generator fetching rows one by one. Yields objects produced by the
        configured row factory.

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
                yield cast(RowType, row)

    @require_init
    async def query_one(
        self, sql: str, params: Union[Sequence[Any], Mapping[str, Any], None] = None
    ) -> Optional[RowType]:
        """
        Fetch single row with parameters. Returns row of configured type or ``None``.

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
        return cast(Optional[RowType], row)

    @require_init
    async def query_scalar(
        self,
        sql: str,
        params: Union[Sequence[Any], Mapping[str, Any], None] = None,
    ) -> Any:
        """Execute a query and return the first column of the first row.

        Example:
            count = await db.query_scalar(
                "SELECT COUNT(*) FROM t WHERE x > ?", (0,)
            )
        """
        row = await self.query_one(sql, params)
        return None if row is None else first_column_value(row, self._rows_as_dict)

    @require_init
    async def query_column(
        self,
        sql: str,
        params: Union[Sequence[Any], Mapping[str, Any], None] = None,
    ) -> List[Any]:
        """Execute a query and return a list of the first column from each row.

        Example:
            ids = await db.query_column(
                "SELECT id FROM t WHERE x > ?", (0,)
            )
        """
        rows = await self.query_many(sql, params)
        return [first_column_value(row, self._rows_as_dict) for row in rows]

    @require_init
    async def query_dict(
        self,
        sql: str,
        params: Union[Sequence[Any], Mapping[str, Any], None] = None,
        *,
        key: Union[str, Callable[[RowType], Any], None] = None,
        value: Union[str, Callable[[RowType], Any], None] = None,
    ) -> Dict[Any, Any]:
        """Execute a query and return a dictionary built from rows.

        ``key`` may be a column name, a callable receiving each row, or ``None``
        to automatically use the table's primary key column.

        ``value`` controls the dictionary's values. When ``None`` (default) each
        row is stored. If a string, the named column from each row is used. If a
        callable, its return value is stored for each row.

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
        rows = await self.query_many(sql, params)

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

        return {get_key(row): get_value(row) for row in rows}

    async def close(self) -> None:
        """Close the database connection.

        The method is idempotent and guarded by a lock so it can be safely
        invoked multiple times, including concurrently via signal handlers
        while an ``async with`` context is exiting.
        """
        async with self._close_lock:
            if not self.initialized and self.conn is None:
                return
            if self._signals_registered and self._signal_loop is not None:
                for sig in (signal.SIGINT, signal.SIGTERM):
                    self._signal_loop.remove_signal_handler(sig)
                self._signals_registered = False
                self._signal_loop = None
            for task in self._periodic_tasks:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            self._periodic_tasks.clear()

            for task in list(self._query_tasks):
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            self._query_tasks.clear()
            if self.conn:
                await self.conn.close()
                self.conn = cast(aiosqlite.Connection, None)
            self.initialized = False

    async def __aenter__(self: T) -> T:
        if not self.initialized:
            await self.init()
        self._register_signal_handlers()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()


# Alias for backward compatibility
BaseDB = AsyncBaseDB
