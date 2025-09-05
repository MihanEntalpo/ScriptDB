import asyncio
import pytest
from scriptdb import daemonizable_aiosqlite as dai


def test_connect_with_loop_triggers_warning():
    loop = asyncio.new_event_loop()
    try:
        with pytest.warns(DeprecationWarning):
            conn = dai.connect(b":memory:", loop=loop)
            loop.run_until_complete(conn.__aenter__())
            assert isinstance(conn, dai.DaemonConnection)
            loop.run_until_complete(conn.close())
    finally:
        loop.close()


def test_connect_accepts_path(tmp_path):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        db_path = tmp_path / "test.db"
        conn = dai.connect(db_path)
        loop.run_until_complete(conn.__aenter__())
        assert isinstance(conn, dai.DaemonConnection)
        loop.run_until_complete(conn.close())
    finally:
        loop.close()
        asyncio.set_event_loop(None)
