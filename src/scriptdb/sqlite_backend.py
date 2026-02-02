from __future__ import annotations

import importlib
import sys
import warnings
from types import ModuleType
from typing import Tuple

MIN_SQLITE_VERSION: Tuple[int, int, int] = (3, 24, 0)
MIN_SQLITE_VERSION_STR = ".".join(str(part) for part in MIN_SQLITE_VERSION)


def _load_sqlite_module() -> Tuple[ModuleType, str, bool]:
    backend = "sqlite3"

    try:
        module = importlib.import_module("pysqlite3.dbapi2")
        backend = "pysqlite3"
        sys.modules["sqlite3"] = module
    except ImportError:
        try:
            module = importlib.import_module("pysqlite3")
            backend = "pysqlite3"
            sys.modules["sqlite3"] = module
        except ImportError:
            module = importlib.import_module("sqlite3")

    version_info = getattr(module, "sqlite_version_info", (0, 0, 0))
    too_old = tuple(version_info) < MIN_SQLITE_VERSION

    if too_old:
        version_str = getattr(module, "sqlite_version", ".".join(str(part) for part in version_info))
        warnings.warn(
            (
                f"SQLite {version_str} is older than the recommended {MIN_SQLITE_VERSION_STR}; "
                "upsert_one and upsert_many will not work. "
                "Install scriptdb[pysqlite] to bundle a modern SQLite build."
            ),
            RuntimeWarning,
            stacklevel=2,
        )

    return module, backend, too_old


sqlite3, SQLITE_BACKEND, SQLITE_TOO_OLD = _load_sqlite_module()
SQLITE_VERSION_INFO: Tuple[int, int, int] = tuple(getattr(sqlite3, "sqlite_version_info", (0, 0, 0)))  # type: ignore[assignment]
SQLITE_VERSION = getattr(sqlite3, "sqlite_version", ".".join(str(part) for part in SQLITE_VERSION_INFO))

UPSERT_UNSUPPORTED_MESSAGE = (
    f"SQLite version is too old for ON CONFLICT upsert (requires >= {MIN_SQLITE_VERSION_STR}). "
    "Install scriptdb[pysqlite] or upgrade SQLite; we warned you that some features will not work."
)


def ensure_upsert_supported() -> None:
    if SQLITE_TOO_OLD:
        raise RuntimeError(UPSERT_UNSUPPORTED_MESSAGE)


__all__ = [
    "SQLITE_BACKEND",
    "SQLITE_TOO_OLD",
    "SQLITE_VERSION",
    "SQLITE_VERSION_INFO",
    "UPSERT_UNSUPPORTED_MESSAGE",
    "MIN_SQLITE_VERSION",
    "MIN_SQLITE_VERSION_STR",
    "ensure_upsert_supported",
    "sqlite3",
]
