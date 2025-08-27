"""Async SQLite database with migrations for lightweight scripts."""

import sqlite3

MIN_SQLITE_VERSION = (3, 21, 0)

if sqlite3.sqlite_version_info < MIN_SQLITE_VERSION:  # pragma: no cover - env guard
    raise RuntimeError("ScriptDB requires SQLite >= 3.21.0")

from .abstractdb import AbstractBaseDB, run_every_seconds, run_every_queries  # noqa: E402
from .syncdb import SyncBaseDB  # noqa: E402

__all__ = [
    "AbstractBaseDB",
    "AsyncBaseDB",  # lazy-imported via __getattr__
    "SyncBaseDB",
    "run_every_seconds",
    "run_every_queries",
    "AsyncCacheDB",  # lazy-imported via __getattr__
    "SyncCacheDB",  # lazy-imported via __getattr__
]
__version__ = "1.0.2"


# Lazy-load objects that require optional dependencies so that importing
# :mod:`scriptdb` does not immediately pull them in.  The synchronous API is
# always available, while the async and cache implementations are resolved on
# first access.
_LAZY_ATTRS = {
    "AsyncBaseDB": ("asyncdb", "AsyncBaseDB"),
    "AsyncCacheDB": ("asynccachedb", "AsyncCacheDB"),
    "SyncCacheDB": ("synccachedb", "SyncCacheDB"),
}


def __getattr__(name: str):
    """Dynamically import optional components.

    ``AsyncBaseDB`` depends on :mod:`aiosqlite`, so we delay importing it until
    needed.  The cache database variants live in their own modules and are
    loaded on demand as well.  All other public names are imported eagerly
    above.
    """

    if name in _LAZY_ATTRS:
        module_name, attr_name = _LAZY_ATTRS[name]
        try:
            module = __import__(f"{__name__}.{module_name}", fromlist=[attr_name])
        except ImportError as exc:  # pragma: no cover - runtime guard
            if name == "AsyncBaseDB":
                raise ImportError("aiosqlite is required for async support; install with 'scriptdb[async]'") from exc
            raise
        return getattr(module, attr_name)

    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
