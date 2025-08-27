import asyncio
import sqlite3
from pathlib import Path
from typing import Callable, Optional, Union, Any

from aiosqlite import Connection
from logging import getLogger

logger = getLogger(__name__)


"""
This module contains override of aiosqlite connect and Connection implementation so to allow
daemonizing background thread of aiosqlite, so to not hang main app (mainly in tests) 

Please, on upgrade of aiosqlite, don't forget to review this module!
"""


class DaemonConnection(Connection):
    def __init__(
        self,
        connector: Callable[[], sqlite3.Connection],
        iter_chunk_size: int,
        loop: Optional[asyncio.AbstractEventLoop] = None,  # совместимость сигнатуры
        *,
        daemonize_thread: bool = False,
        creation_site: str = "",
    ) -> None:
        self.creation_site = creation_site
        super().__init__(connector, iter_chunk_size, loop)
        if daemonize_thread:
            self.daemon = True


def connect(
    database: Union[str, Path],
    *,
    iter_chunk_size=64,
    loop: Optional[asyncio.AbstractEventLoop] = None,
    daemonize_thread: bool = False,
    creation_site: str = "",
    **kwargs: Any,
) -> Connection:
    """Create and return a connection proxy to the sqlite database."""

    if loop is not None:
        logger.warning(
            "aiosqlite.connect() no longer uses the `loop` parameter",
            DeprecationWarning,
        )

    def connector() -> sqlite3.Connection:
        if isinstance(database, str):
            loc = database
        elif isinstance(database, bytes):
            loc = database.decode("utf-8")
        else:
            loc = str(database)

        return sqlite3.connect(loc, **kwargs)

    return DaemonConnection(
        connector,
        iter_chunk_size,
        loop,
        daemonize_thread=daemonize_thread,
        creation_site=creation_site,
    )
