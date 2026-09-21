from __future__ import annotations

import abc
import inspect
import logging
import re
import warnings
import contextlib
import math
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Sequence, Tuple, TypeVar, Union, Set, TYPE_CHECKING

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


_FIRST_KEYWORD_PATTERN = re.compile(r"^\s*(?:--[^\n]*\n|/\*.*?\*/\s*)*(\w+)", re.DOTALL)


def _first_keyword(sql: str) -> str:
    match = _FIRST_KEYWORD_PATTERN.search(sql)
    return match.group(1).lower() if match else ""


def _split_sql_script(sql: str) -> List[str]:
    """Split a SQLite script without breaking triggers or quoted semicolons."""

    from . import sqlite_backend

    statements: List[str] = []
    buffer = ""
    for char in sql:
        buffer += char
        if char == ";" and sqlite_backend.sqlite3.complete_statement(buffer):
            statement = buffer.strip()
            if statement:
                statements.append(statement)
            buffer = ""
    if buffer.strip():
        statements.append(buffer.strip() + ";")
    return statements


def _atomic_migration_script(sql_parts: Sequence[str], name: str) -> str:
    """Build one transaction containing the migration and its tracking row."""

    body = "\n".join(part if part.rstrip().endswith(";") else f"{part};" for part in sql_parts)
    statements = _split_sql_script(body)
    normalized: List[str] = []
    saw_begin = False
    saw_end = False
    for statement in statements:
        keyword = _first_keyword(statement)
        if keyword == "begin":
            saw_begin = True
            continue
        if keyword in {"commit", "end"}:
            saw_end = True
            continue
        if keyword == "rollback":
            raise ValueError(f"Migration {name} must not contain ROLLBACK")
        normalized.append(statement)
    if saw_begin and not saw_end:
        raise ValueError(f"Migration {name} contains a BEGIN without COMMIT/END")
    encoded_name = name.encode("utf-8").hex()
    marker = (
        "INSERT INTO applied_migrations(name) "
        f"VALUES (CAST(X'{encoded_name}' AS TEXT));"
    )
    return "BEGIN;\n" + "\n".join(normalized) + f"\n{marker}\nCOMMIT;"


def _row_columns(rows: Sequence[Mapping[str, Any]]) -> Tuple[str, ...]:
    """Validate bulk row shapes and return their common ordered columns."""

    if not rows:
        return ()
    columns = tuple(rows[0].keys())
    if not columns:
        raise ValueError("row must contain at least one column")
    if any(not isinstance(column, str) or not column for column in columns):
        raise ValueError("column names must be non-empty strings")
    expected = set(columns)
    for index, row in enumerate(rows[1:], start=1):
        if set(row.keys()) != expected:
            raise ValueError(f"row {index} has different columns from the first row")
    return columns


def require_init(method: Callable) -> Callable:
    def ensure_open(self) -> None:
        if not getattr(self, "initialized", False) or getattr(self, "conn", None) is None:
            error_builder = getattr(self, "_not_initialized_error", None)
            raise error_builder() if callable(error_builder) else RuntimeError("you didn't call init")

    if inspect.iscoroutinefunction(method):

        async def async_wrapper(self, *args, **kwargs):
            ensure_open(self)
            lock = getattr(self, "_operation_lock", None)
            if lock is None:
                return await method(self, *args, **kwargs)
            async with lock:
                ensure_open(self)
                return await method(self, *args, **kwargs)

        return async_wrapper
    elif inspect.isasyncgenfunction(method):

        async def async_gen_wrapper(self, *args, **kwargs):
            ensure_open(self)
            lock = getattr(self, "_operation_lock", None)
            if lock is None:
                async for item in method(self, *args, **kwargs):
                    yield item
                return
            async with lock:
                ensure_open(self)
                async for item in method(self, *args, **kwargs):
                    yield item

        return async_gen_wrapper
    elif inspect.isgeneratorfunction(method):

        def gen_wrapper(self, *args, **kwargs):
            ensure_open(self)
            lock = getattr(self, "_operation_lock", None)
            context = lock if lock is not None else contextlib.nullcontext()
            with context:
                ensure_open(self)
                yield from method(self, *args, **kwargs)

        return gen_wrapper
    else:

        def sync_wrapper(self, *args, **kwargs):
            ensure_open(self)
            lock = getattr(self, "_operation_lock", None)
            context = lock if lock is not None else contextlib.nullcontext()
            with context:
                ensure_open(self)
                return method(self, *args, **kwargs)

        return sync_wrapper


def run_every_seconds(seconds: float) -> Callable:
    if (
        isinstance(seconds, bool)
        or not isinstance(seconds, (int, float))
        or not math.isfinite(seconds)
        or seconds <= 0
    ):
        raise ValueError("seconds must be a positive number")

    def decorator(method: Callable) -> Callable:
        setattr(method, "_run_every_seconds", seconds)
        return method

    return decorator


def run_every_queries(queries: int) -> Callable:
    if isinstance(queries, bool) or not isinstance(queries, int) or queries <= 0:
        raise ValueError("queries must be a positive integer")

    def decorator(method: Callable) -> Callable:
        setattr(method, "_run_every_queries", queries)
        return method

    return decorator


def _is_signature_binding_error(exc: BaseException) -> bool:
    """Return True if ``exc`` originates from ``inspect.Signature.bind`` helpers."""

    tb = exc.__traceback__
    while tb is not None:
        frame = tb.tb_frame
        module = frame.f_globals.get("__name__")
        if module == "inspect" and frame.f_code.co_name in {
            "_bind",
            "bind",
            "_bind_partial",
            "bind_partial",
        }:
            return True
        tb = tb.tb_next
    return False


class AbstractBaseDB(abc.ABC):
    def __init__(
        self,
        db_path: Union[str, Path],
        auto_create: bool = True,
        *,
        use_wal: bool = True,
        legacy_sqlite_support: bool = False,
    ) -> None:
        path_obj = Path(db_path)
        if not auto_create and not path_obj.exists():
            raise RuntimeError(f"Database file {db_path} does not exist")
        self.db_path = str(path_obj)
        self.auto_create = auto_create
        self.use_wal = use_wal
        self.legacy_sqlite_support = legacy_sqlite_support
        self.conn: Union[sqlite3.Connection, aiosqlite.Connection, None] = None
        self.initialized: bool = False
        self._is_closed: bool = False
        self._periodic_specs: List[Tuple[float, Callable]] = []
        self._query_hooks: List[Dict[str, Any]] = []
        self._pk_cache: Dict[str, str] = {}
        self._legacy_upsert_warning_emitted = False

        for name in dir(self):
            attr = getattr(self, name)
            seconds = getattr(attr, "_run_every_seconds", None)
            if seconds is not None:
                self._periodic_specs.append((seconds, attr))
            queries = getattr(attr, "_run_every_queries", None)
            if queries is not None:
                self._query_hooks.append({"interval": queries, "method": attr, "count": 0, "running": False})

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

    def _not_initialized_error(self) -> RuntimeError:
        if getattr(self, "_is_closed", False):
            return RuntimeError("connection already closed")
        return RuntimeError("you didn't call init")

    def _use_legacy_upsert(self) -> bool:
        from . import sqlite_backend

        return bool(self.legacy_sqlite_support and sqlite_backend.SQLITE_TOO_OLD)

    def _warn_legacy_upsert_once(self) -> None:
        if self._legacy_upsert_warning_emitted or not self._use_legacy_upsert():
            return
        self._legacy_upsert_warning_emitted = True
        warnings.warn(
            (
                "legacy_sqlite_support=True enabled compatibility upsert mode for an older SQLite build; "
                "ScriptDB will emulate upsert without ON CONFLICT DO UPDATE."
            ),
            RuntimeWarning,
            stacklevel=3,
        )
