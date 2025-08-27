from __future__ import annotations

import abc
import inspect
import logging
from pathlib import Path
from typing import Any, Callable, Dict, List, Tuple, TypeVar, Union, Set, TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - for type checkers only
    import sqlite3
    import aiosqlite

logger = logging.getLogger(__name__)

T = TypeVar("T", bound="AbstractBaseDB")

# shared SQL for migration tracking table
MIGRATIONS_TABLE_SQL = """
    CREATE TABLE IF NOT EXISTS applied_migrations (
        name       TEXT PRIMARY KEY,
        applied_at TEXT NOT NULL DEFAULT (datetime('now'))
    )
    """


def _get_migrations_table_sql() -> str:
    return MIGRATIONS_TABLE_SQL


def require_init(method: Callable) -> Callable:
    if inspect.iscoroutinefunction(method):

        async def async_wrapper(self, *args, **kwargs):
            if not getattr(self, "initialized", False) or getattr(self, "conn", None) is None:
                raise RuntimeError("you didn't call init")
            return await method(self, *args, **kwargs)

        return async_wrapper
    elif inspect.isasyncgenfunction(method):

        async def async_gen_wrapper(self, *args, **kwargs):
            if not getattr(self, "initialized", False) or getattr(self, "conn", None) is None:
                raise RuntimeError("you didn't call init")
            async for item in method(self, *args, **kwargs):
                yield item

        return async_gen_wrapper
    else:

        def sync_wrapper(self, *args, **kwargs):
            if not getattr(self, "initialized", False) or getattr(self, "conn", None) is None:
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
    def __init__(
        self, db_path: Union[str, Path], auto_create: bool = True, *, use_wal: bool = True
    ) -> None:
        path_obj = Path(db_path)
        if not auto_create and not path_obj.exists():
            raise RuntimeError(f"Database file {db_path} does not exist")
        self.db_path = str(path_obj)
        self.auto_create = auto_create
        self.use_wal = use_wal
        self.conn: Union[sqlite3.Connection, aiosqlite.Connection, None] = None
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

    def _validate_migrations(self, migrations_list: List[Dict[str, Any]], applied: Set[str]) -> List[Dict[str, Any]]:
        names: List[str] = []
        for mig in migrations_list:
            name = mig.get("name")
            if not isinstance(name, str):
                raise ValueError("Migration entry missing 'name'")
            names.append(name)
        dupes = {name for name in names if names.count(name) > 1}
        if dupes:
            raise ValueError(f"Duplicate migration names detected: {', '.join(sorted(dupes))}")
        unknown = applied - set(names)
        if unknown:
            missing = ", ".join(sorted(unknown))
            raise ValueError(f"Applied migration(s) not found: {missing}; database may be inconsistent")
        validated: List[Dict[str, Any]] = []
        for mig in migrations_list:
            kinds = [k for k in ("sql", "sqls", "function") if k in mig]
            if len(kinds) != 1:
                raise ValueError(f"Migration {mig['name']} must have exactly one of 'sql', 'sqls', or 'function'")
            if mig["name"] not in applied:
                validated.append(mig)
        return validated
