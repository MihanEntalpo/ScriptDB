"""Synchronous cache database implementation."""

import asyncio
import inspect
import pickle
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, List, Optional

from .abstractdb import run_every_seconds, require_init
from .syncdb import SyncBaseDB


class SyncCacheDB(SyncBaseDB):
    """Synchronous cache database with expiration support."""

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
        row = self.query_one("SELECT value, expire_utc FROM cache WHERE key=?", (key,))
        if row is None:
            return default
        expire_utc = row["expire_utc"]
        if expire_utc is not None:
            if datetime.fromisoformat(expire_utc) <= datetime.now(timezone.utc):
                return default
        return pickle.loads(row["value"])

    @require_init
    def is_set(self, key: str) -> bool:
        exists = self.query_scalar(
            "SELECT 1 FROM cache WHERE key=? AND (expire_utc IS NULL OR expire_utc > ?)",
            (key, datetime.now(timezone.utc).isoformat()),
        )
        return exists is not None

    @require_init
    def set(self, key: str, value: Any, expire_sec: Optional[int] = None) -> None:
        now = datetime.now(timezone.utc)
        if expire_sec is None:
            expire_utc = None
        elif expire_sec > 0:
            expire_utc = now + timedelta(seconds=expire_sec)
        else:
            expire_utc = now
        blob = sqlite3.Binary(pickle.dumps(value))
        row = {
            "key": key,
            "value": blob,
            "expire_utc": expire_utc.isoformat() if expire_utc else None,
        }
        self.upsert_one("cache", row)

    @require_init
    def delete(self, key: str) -> int:
        cur = self.execute("DELETE FROM cache WHERE key=?", (key,))
        return cur.rowcount

    @require_init
    def del_many(self, key_mask: str) -> int:
        pattern = key_mask.replace("_", "\\_").replace("*", "%")
        cur = self.execute("DELETE FROM cache WHERE key LIKE ? ESCAPE '\\'", (pattern,))
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
        self.execute(
            "DELETE FROM cache WHERE expire_utc IS NOT NULL AND expire_utc <= ?",
            (datetime.now(timezone.utc).isoformat(),),
        )


