"""Helpers to convert between async and sync database base classes."""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, Protocol, TypeVar, cast


if TYPE_CHECKING:  # pragma: no cover - only for type checkers
    from .asyncdb import AsyncBaseDB
    from .syncdb import SyncBaseDB


TAsync = TypeVar("TAsync", bound="AsyncBaseDB")
TSync = TypeVar("TSync", bound="SyncBaseDB")


class _MigrationProvider(Protocol):
    def migrations(self) -> list[dict[str, Any]]:
        ...


def _ensure_sql_migrations(migrations_list: list[dict[str, Any]], source: str):
    from .dbbuilder import _SQLBuilder

    for mig in migrations_list:
        name = mig.get("name", "<unknown>")
        if "function" in mig:
            raise TypeError(
                f"Cannot convert migrations with callables: migration {name} in {source} uses 'function'"
            )
        if "sql" in mig:
            if not isinstance(mig["sql"], (str, _SQLBuilder)):
                raise TypeError(
                    f"'sql' for migration {name} must be a string or SQL builder instance"
                )
        elif "sqls" in mig:
            sqls = mig["sqls"]
            if not isinstance(sqls, Sequence) or isinstance(sqls, (str, bytes)):
                raise TypeError(
                    f"'sqls' for migration {name} must be a sequence of strings or SQL builder instances"
                )
            for sql in sqls:
                if not isinstance(sql, (str, _SQLBuilder)):
                    raise TypeError(
                        f"'sqls' for migration {name} must contain only strings or SQL builder instances"
                    )
    return migrations_list


def _validated_migrations(
    source_cls: type[_MigrationProvider], target_self: _MigrationProvider
):
    dummy = cast(_MigrationProvider, object.__new__(source_cls))
    if hasattr(dummy, "__dict__") and hasattr(target_self, "__dict__"):
        dummy.__dict__.update(getattr(target_self, "__dict__", {}))

    migrations_list = source_cls.migrations(dummy)
    _ensure_sql_migrations(migrations_list, source_cls.__name__)
    return source_cls.migrations(target_self)


def sync_from_async(async_cls: type[TAsync]) -> type["SyncBaseDB"]:
    """Create a synchronous subclass that reuses an async class's migrations."""

    from .asyncdb import AsyncBaseDB
    from .syncdb import SyncBaseDB

    if not issubclass(async_cls, AsyncBaseDB):
        raise TypeError("async_cls must inherit from AsyncBaseDB")

    def __init__(self, *args, **kwargs):
        SyncBaseDB.__init__(self, *args, **kwargs)

    name = f"Sync{async_cls.__name__}"
    def migrations(self):
        return _validated_migrations(async_cls, self)

    return type(
        name,
        (SyncBaseDB,),
        {
            "__name__": name,
            "__qualname__": name,
            "__module__": __name__,
            "__init__": __init__,
            "migrations": migrations,
        },
    )


def async_from_sync(sync_cls: type[TSync]) -> type["AsyncBaseDB"]:
    """Create an async subclass that reuses a sync class's migrations."""

    from .asyncdb import AsyncBaseDB
    from .syncdb import SyncBaseDB

    if not issubclass(sync_cls, SyncBaseDB):
        raise TypeError("sync_cls must inherit from SyncBaseDB")

    def __init__(self, *args, **kwargs):
        AsyncBaseDB.__init__(self, *args, **kwargs)

    name = f"Async{sync_cls.__name__}"
    def migrations(self):
        return _validated_migrations(sync_cls, self)

    return type(
        name,
        (AsyncBaseDB,),
        {
            "__name__": name,
            "__qualname__": name,
            "__module__": __name__,
            "__init__": __init__,
            "migrations": migrations,
        },
    )
