import abc
import sqlite3
import asyncio
import inspect
import logging
import contextlib
import aiosqlite
from typing import Any, Callable, Dict, List, Set, Optional, Sequence, Iterable, Mapping, Union, Type, TypeVar, AsyncGenerator, Tuple

# Type for self-returning class methods
T = TypeVar('T', bound='BaseDB')

logger = logging.getLogger(__name__)

def require_init(method: Callable) -> Callable:
    """
    Decorator to ensure the database is initialized before method execution.

    Raises:
        RuntimeError: if `init()` was not called before invocation.
    """
    if inspect.iscoroutinefunction(method):
        async def async_wrapper(self, *args, **kwargs):
            if not getattr(self, 'initialized', False) or self.conn is None:
                raise RuntimeError("you didn't call init")
            return await method(self, *args, **kwargs)
        return async_wrapper
    elif inspect.isasyncgenfunction(method):
        async def async_gen_wrapper(self, *args, **kwargs):
            if not getattr(self, 'initialized', False) or self.conn is None:
                raise RuntimeError("you didn't call init")
            async for item in method(self, *args, **kwargs):
                yield item
        return async_gen_wrapper
    else:
        def sync_wrapper(self, *args, **kwargs):
            if not getattr(self, 'initialized', False) or self.conn is None:
                raise RuntimeError("you didn't call init")
            return method(self, *args, **kwargs)
        return sync_wrapper


def run_every_seconds(seconds: int) -> Callable:
    """Run decorated async method in background every ``seconds``.

    Useful for periodic cleaners or updaters that should work while the
    database connection stays open.

    Example:
        class MyDB(BaseDB):
            @run_every_seconds(60)
            async def cleanup(self):
                ...  # cleanup every minute
    """

    def decorator(method: Callable) -> Callable:
        if not inspect.iscoroutinefunction(method):
            raise TypeError("run_every_seconds can only decorate async functions")
        setattr(method, "_run_every_seconds", seconds)
        return method

    return decorator


def run_every_queries(queries: int) -> Callable:
    """Run decorated async method after every ``queries`` database calls.

    Handy for tasks like checkpointing or vacuuming triggered by activity.

    Example:
        class MyDB(BaseDB):
            @run_every_queries(1000)
            async def checkpoint(self):
                await self.execute("PRAGMA wal_checkpoint")
    """

    def decorator(method: Callable) -> Callable:
        if not inspect.iscoroutinefunction(method):
            raise TypeError("run_every_queries can only decorate async functions")
        setattr(method, "_run_every_queries", queries)
        return method

    return decorator

class BaseDB(abc.ABC):
    """
    Abstract async SQLite-backed database with migration support via aiosqlite.

    Subclasses must implement migrations() -> List[Dict[str, Any]]:
      each dict must have:
        - "name": str (unique identifier)
        - either "sql": str
          or     "function": Callable[[aiosqlite.Connection], Any]

    Usage:
        db = await YourDB.open("app.db")
    """

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self.conn: Optional[aiosqlite.Connection] = None
        self.initialized: bool = False
        self._periodic_specs: List[Tuple[int, Callable]] = []
        self._periodic_tasks: List[asyncio.Task] = []
        self._query_hooks: List[Dict[str, Any]] = []

        for name in dir(self):
            attr = getattr(self, name)
            seconds = getattr(attr, "_run_every_seconds", None)
            if seconds is not None:
                self._periodic_specs.append((seconds, attr))
            queries = getattr(attr, "_run_every_queries", None)
            if queries is not None:
                self._query_hooks.append({"interval": queries, "method": attr, "count": 0})

    @classmethod
    async def open(cls: Type[T], db_path: str) -> T:
        """
        Factory to create and initialize the database instance.

        Returns:
            Initialized subclass instance of type T.
        """
        instance: T = cls(db_path)  # type: ignore
        await instance.init()
        return instance

    @abc.abstractmethod
    def migrations(self) -> List[Dict[str, Any]]:
        """
        Return ordered list of migration dicts.
        Each dict must include:
          - name: str
          - either sql: str or function: Callable
        """
        raise NotImplementedError

    async def init(self) -> None:
        """
        Initialize the database connection and apply pending migrations.
        """
        self.conn = await aiosqlite.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        await self._ensure_migrations_table()
        await self._apply_migrations()
        self.initialized = True

        for seconds, method in self._periodic_specs:
            async def runner(method=method, seconds=seconds):
                while True:
                    logger.info("Launching method %s", method.__name__)
                    await method()
                    logger.info(
                        "Method %s finished, next run in %s seconds",
                        method.__name__,
                        seconds,
                    )
                    await asyncio.sleep(seconds)

            task = asyncio.create_task(runner())
            self._periodic_tasks.append(task)

    async def _ensure_migrations_table(self) -> None:
        await self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS applied_migrations (
                name       TEXT PRIMARY KEY,
                applied_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
        await self.conn.commit()

    async def _applied_versions(self) -> Set[str]:
        cur = await self.conn.execute("SELECT name FROM applied_migrations")
        rows = await cur.fetchall()
        await cur.close()
        return {row["name"] for row in rows}

    async def _apply_migrations(self) -> None:
        migrations_list = self.migrations()
        names = [mig.get("name") for mig in migrations_list]
        dupes = {name for name in names if names.count(name) > 1}
        if dupes:
            raise ValueError(f"Duplicate migration names detected: {', '.join(sorted(dupes))}")

        applied = await self._applied_versions()
        for mig in migrations_list:
            name = mig.get("name")
            if not name:
                raise ValueError("Migration entry missing 'name'")
            if name in applied:
                continue

            if "sql" in mig:
                await self.conn.executescript(mig["sql"])
            elif "function" in mig:
                func = mig["function"]
                if not callable(func):
                    raise ValueError(f"'function' for migration {name} is not callable")
                result = func(self.conn)
                if asyncio.iscoroutine(result):
                    await result
            else:
                raise ValueError(f"Migration {name} must have either 'sql' or 'function'")

            await self.conn.execute(
                "INSERT INTO applied_migrations(name) VALUES (?)", (name,)
            )
            await self.conn.commit()

    def _on_query(self) -> None:
        for hook in self._query_hooks:
            hook["count"] += 1
            if hook["count"] >= hook["interval"]:
                hook["count"] = 0

                async def runner(method=hook["method"], interval=hook["interval"]):
                    logger.info("Launching method %s", method.__name__)
                    await method()
                    logger.info(
                        "Method %s finished, next run after %s queries",
                        method.__name__,
                        interval,
                    )

                asyncio.create_task(runner())

    @require_init
    async def execute(
        self,
        sql: str,
        params: Union[Sequence[Any], Mapping[str, Any], None] = None
    ) -> aiosqlite.Cursor:
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
        cur = await self.conn.execute(sql, ps)
        await self.conn.commit()
        self._on_query()
        return cur

    @require_init
    async def execute_many(
        self,
        sql: str,
        seq_params: Iterable[Sequence[Any]]
    ) -> aiosqlite.Cursor:
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
        cur = await self.conn.executemany(sql, seq_params)
        await self.conn.commit()
        self._on_query()
        return cur

    @require_init
    async def query_many(
        self,
        sql: str,
        params: Union[Sequence[Any], Mapping[str, Any], None] = None
    ) -> List[sqlite3.Row]:
        """
        Fetch all rows with parameters. Returns List[sqlite3.Row].

        Example:
            rows = await db.query_many(
                "SELECT x FROM t WHERE x > ?", (0,)
            )
            for row in rows:
                print(row["x"])
        """
        ps = params if params is not None else ()
        cur = await self.conn.execute(sql, ps)
        rows = await cur.fetchall()
        await cur.close()
        self._on_query()
        return rows

    @require_init
    async def query_many_gen(
        self,
        sql: str,
        params: Union[Sequence[Any], Mapping[str, Any], None] = None
    ) -> AsyncGenerator[sqlite3.Row, None]:
        """
        Async generator fetching rows one by one. Yields sqlite3.Row objects.

        Example:
            async for row in db.query_many_gen(
                "SELECT x FROM t WHERE x > ?", (0,)
            ):
                print(row["x"])
        """
        ps = params if params is not None else ()
        async with self.conn.execute(sql, ps) as cur:
            self._on_query()
            async for row in cur:
                yield row

    @require_init
    async def query_one(
        self,
        sql: str,
        params: Union[Sequence[Any], Mapping[str, Any], None] = None
    ) -> Optional[sqlite3.Row]:
        """
        Fetch single row with parameters. Returns sqlite3.Row or None.

        Example:
            row = await db.query_one(
                "SELECT x FROM t WHERE id = :id", {"id": 1}
            )
            if row:
                print(row["x"])
        """
        ps = params if params is not None else ()
        cur = await self.conn.execute(sql, ps)
        row = await cur.fetchone()
        await cur.close()
        self._on_query()
        return row

    @require_init
    async def close(self) -> None:
        """
        Close the database connection.
        """
        for task in self._periodic_tasks:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._periodic_tasks.clear()
        if self.conn:
            await self.conn.close()
        self.initialized = False
