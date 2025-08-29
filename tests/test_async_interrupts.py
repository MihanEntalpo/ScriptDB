import asyncio
import signal
import sys

import pytest
from scriptdb import AsyncBaseDB


class EmptyDB(AsyncBaseDB):
    def migrations(self):
        return []


@pytest.mark.asyncio
async def test_keyboard_interrupt_triggers_single_close(tmp_path):
    db_path = tmp_path / "ki.db"
    with pytest.raises(KeyboardInterrupt):
        async with EmptyDB.open(db_path, daemonize_thread=True) as db:
            await db.close()
            raise KeyboardInterrupt


@pytest.mark.asyncio
async def test_signal_handler_closes_db(tmp_path):
    if sys.platform == "win32":
        pytest.skip("signals not supported on Windows in asyncio")
    db_path = tmp_path / "sig.db"
    async with EmptyDB.open(db_path, daemonize_thread=True) as db:
        signal.raise_signal(signal.SIGTERM)
        # allow signal handler to run the close coroutine
        await asyncio.sleep(0.1)
        assert not db.initialized
