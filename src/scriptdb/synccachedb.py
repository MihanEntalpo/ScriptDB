"""Synchronous cache database implementation."""

import asyncio
import inspect
import pickle
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Union

from ._cache_index import _CacheKeyIndexMixin
from ._rowfactory import supports_row_factory
from .abstractdb import run_every_seconds, require_init
from .syncdb import SyncBaseDB, _SyncDBOpenContext, RowFactorySetting


class SyncCacheDB(_CacheKeyIndexMixin, SyncBaseDB):
    """Synchronous cache database with optional in-memory key index."""

    def __init__(
        self,
        db_path: Union[str, Path],
        auto_create: bool = True,
        *,
        use_wal: bool = True,
        row_factory: RowFactorySetting = sqlite3.Row,
        cache_keys_in_ram: bool = False,
    ) -> None:
        super().__init__(
            db_path,
            auto_create,
            row_factory=row_factory,
            use_wal=use_wal,
            cache_keys_in_ram=cache_keys_in_ram,
        )

    @classmethod
    def open(
        cls,
        db_path: Union[str, Path],
        *,
        auto_create: bool = True,
        row_factory: RowFactorySetting = sqlite3.Row,
        use_wal: bool = True,
        cache_keys_in_ram: bool = False,
    ) -> "_SyncCacheDBOpenContext":
        path_obj = Path(db_path)
        if not auto_create and not path_obj.exists():
            raise RuntimeError(f"Database file {db_path} does not exist")
        return _SyncCacheDBOpenContext(
            cls,
            str(path_obj),
            auto_create,
            use_wal,
            row_factory,
            cache_keys_in_ram,
        )

    def init(self) -> None:
        super().init()
        if self._cache_keys_in_ram:
            rows = self.query_many("SELECT key, expire_utc FROM cache")
            self._reload_ram_index(rows)

    def migrations(self):
        return [
            {
                "name": "create_cache_table",
                "sql": (
                    "CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, value BLOB NOT NULL, expire_utc DATETIME)"
                ),
            }
        ]

    @require_init
    def get(self, key: str, default: Any = None) -> Any:
        now = datetime.now(timezone.utc)
        cached_presence = self._ram_has_key(key, now)
        if cached_presence is False:
            return default
        row = self.query_one("SELECT value, expire_utc FROM cache WHERE key=?", (key,))
        if row is None:
            self._ram_mark_miss(key)
            return default
        expire_utc = row["expire_utc"]
        if expire_utc is not None:
            expire_dt = datetime.fromisoformat(expire_utc)
            if expire_dt <= now:
                self._ram_mark_miss(key)
                return default
        return pickle.loads(row["value"])

    @require_init
    def is_set(self, key: str) -> bool:
        cached_presence = self._ram_has_key(key, datetime.now(timezone.utc))
        if cached_presence is not None:
            return cached_presence
        exists = self.query_scalar(
            "SELECT 1 FROM cache WHERE key=? AND (expire_utc IS NULL OR expire_utc > ?)",
            (key, datetime.now(timezone.utc).isoformat()),
        )
        return exists is not None

    @require_init
    def set(self, key: str, value: Any, expire_sec: Optional[int] = None) -> None:
        now = datetime.now(timezone.utc)
        if expire_sec is None:
            expire_dt: Optional[datetime] = None
        elif expire_sec > 0:
            expire_dt = now + timedelta(seconds=expire_sec)
        else:
            expire_dt = now
        blob = sqlite3.Binary(pickle.dumps(value))
        row = {
            "key": key,
            "value": blob,
            "expire_utc": expire_dt.isoformat() if expire_dt else None,
        }
        self.upsert_one("cache", row)
        self._ram_on_set(key, expire_dt, now)

    @require_init
    def delete(self, key: str) -> int:
        now = datetime.now(timezone.utc)
        cur = self.execute("DELETE FROM cache WHERE key=?", (key,))
        self._ram_on_delete(key, now)
        return cur.rowcount

    @require_init
    def del_many(self, key_mask: str) -> int:
        pattern = key_mask.replace("_", "\\_").replace("*", "%")
        keys_to_remove: List[str] = []
        if self._cache_keys_in_ram:
            keys_to_remove = [
                str(key)
                for key in self.query_column(
                    "SELECT key FROM cache WHERE key LIKE ? ESCAPE '\\'",
                    (pattern,),
                )
            ]
        cur = self.execute("DELETE FROM cache WHERE key LIKE ? ESCAPE '\\'", (pattern,))
        if self._cache_keys_in_ram:
            self._ram_on_del_many(keys_to_remove, datetime.now(timezone.utc))
        return cur.rowcount

    @require_init
    def keys(self, key_mask: str) -> List[str]:
        pattern = key_mask.replace("_", "\\_").replace("*", "%")
        return self.query_column(
            ("SELECT key FROM cache WHERE key LIKE ? ESCAPE '\\' AND (expire_utc IS NULL OR expire_utc > ?)"),
            (pattern, datetime.now(timezone.utc).isoformat()),
        )

    @require_init
    def clear(self) -> int:
        cur = self.execute("DELETE FROM cache")
        self._ram_on_clear()
        return cur.rowcount

    def cache(
        self,
        expire_sec: Optional[int] = None,
        key_func: Optional[Callable[..., str]] = None,
    ) -> Callable:
        def decorator(func: Callable) -> Callable:
            def wrapper(*args, **kwargs):
                key = key_func(*args, **kwargs) if key_func else f"{func.__name__}:{args}:{kwargs}"
                _sentinel = object()
                value = self.get(key, _sentinel)
                if value is not _sentinel:
                    return value
                if inspect.iscoroutinefunction(func):
                    result = asyncio.run(func(*args, **kwargs))
                else:
                    result = func(*args, **kwargs)
                self.set(key, result, expire_sec)
                return result

            return wrapper

        return decorator

    @run_every_seconds(5)
    @require_init
    def _cleanup(self) -> None:
        now = datetime.now(timezone.utc)
        self.execute(
            "DELETE FROM cache WHERE expire_utc IS NOT NULL AND expire_utc <= ?",
            (now.isoformat(),),
        )
        self._ram_purge_expired(now)


class _SyncCacheDBOpenContext(_SyncDBOpenContext["SyncCacheDB"]):
    def __init__(
        self,
        cls,
        db_path: str,
        auto_create: bool,
        use_wal: bool,
        row_factory: RowFactorySetting,
        cache_keys_in_ram: bool,
    ) -> None:
        super().__init__(cls, db_path, auto_create, use_wal, row_factory)
        self._cache_keys_in_ram = cache_keys_in_ram

    def _open(self) -> "SyncCacheDB":
        kwargs: Dict[str, Any] = {
            "auto_create": self._auto_create,
            "use_wal": self._use_wal,
            "cache_keys_in_ram": self._cache_keys_in_ram,
        }
        if supports_row_factory(self._cls):
            kwargs["row_factory"] = self._row_factory
            instance = self._cls(self._db_path, **kwargs)  # type: ignore[call-arg]
        else:
            instance = self._cls(self._db_path, **kwargs)  # type: ignore[call-arg]
            if hasattr(instance, "_set_row_factory"):
                instance._set_row_factory(self._row_factory)  # type: ignore[attr-defined]
        instance.init()
        self._db = instance
        return instance
