"""Async SQLite database with migrations for lightweight scripts."""

import sqlite3

MIN_SQLITE_VERSION = (3, 21, 0)

if sqlite3.sqlite_version_info < MIN_SQLITE_VERSION:  # pragma: no cover - env guard
    raise RuntimeError("ScriptDB requires SQLite >= 3.21.0")

from .basedb import BaseDB, run_every_seconds, run_every_queries
from .cachedb import CacheDB

__all__ = ["BaseDB", "run_every_seconds", "run_every_queries", "CacheDB"]
__version__ = "0.1.0"
