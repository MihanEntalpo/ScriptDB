import abc
import inspect
import logging
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Mapping, Union, Type, TypeVar, Generator, Tuple

logger = logging.getLogger(__name__)

T = TypeVar('T', bound='AbstractBaseDB')

# shared SQL for migration tracking table
MIGRATIONS_TABLE_SQL = (
    """
    CREATE TABLE IF NOT EXISTS applied_migrations (
        name       TEXT PRIMARY KEY,
        applied_at TEXT NOT NULL DEFAULT (datetime('now'))
    )
    """
)


def _get_migrations_table_sql() -> str:
    return MIGRATIONS_TABLE_SQL


def require_init(method: Callable) -> Callable:
    if inspect.iscoroutinefunction(method):
        async def async_wrapper(self, *args, **kwargs):
            if not getattr(self, 'initialized', False) or getattr(self, 'conn', None) is None:
                raise RuntimeError("you didn't call init")
            return await method(self, *args, **kwargs)
        return async_wrapper
    elif inspect.isasyncgenfunction(method):
        async def async_gen_wrapper(self, *args, **kwargs):
            if not getattr(self, 'initialized', False) or getattr(self, 'conn', None) is None:
                raise RuntimeError("you didn't call init")
            async for item in method(self, *args, **kwargs):
                yield item
        return async_gen_wrapper
    else:
        def sync_wrapper(self, *args, **kwargs):
            if not getattr(self, 'initialized', False) or getattr(self, 'conn', None) is None:
                raise RuntimeError("you didn't call init")
            return method(self, *args, **kwargs)
        return sync_wrapper


def run_every_seconds(seconds: int) -> Callable:
    def decorator(method: Callable) -> Callable:
        setattr(method, "_run_every_seconds", seconds)
        return method
    return decorator


def run_every_queries(queries: int) -> Callable:
    def decorator(method: Callable) -> Callable:
        setattr(method, "_run_every_queries", queries)
        return method
    return decorator


class AbstractBaseDB(abc.ABC):
    def __init__(self, db_path: str, auto_create: bool = True, *, use_wal: bool = True) -> None:
        if not auto_create and not Path(db_path).exists():
            raise RuntimeError(f"Database file {db_path} does not exist")
        self.db_path = db_path
        self.auto_create = auto_create
        self.use_wal = use_wal
        self.conn = None
        self.initialized: bool = False
        self._periodic_specs: List[Tuple[int, Callable]] = []
        self._query_hooks: List[Dict[str, Any]] = []
        self._pk_cache: Dict[str, str] = {}

        for name in dir(self):
            attr = getattr(self, name)
            seconds = getattr(attr, "_run_every_seconds", None)
            if seconds is not None:
                self._periodic_specs.append((seconds, attr))
            queries = getattr(attr, "_run_every_queries", None)
            if queries is not None:
                self._query_hooks.append({"interval": queries, "method": attr, "count": 0})

    @abc.abstractmethod
    def migrations(self) -> List[Dict[str, Any]]:
        raise NotImplementedError
