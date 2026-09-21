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
    async with EmptyDB.open(db_path, daemonize_thread=True, handle_signals=True) as db:
        signal.raise_signal(signal.SIGTERM)
        # allow signal handler to run the close coroutine
        await asyncio.sleep(0.1)
        assert not db.initialized


@pytest.mark.asyncio
async def test_signal_handlers_are_not_replaced_by_default(tmp_path):
    if sys.platform == "win32":
        pytest.skip("signals not supported on Windows in asyncio")
    loop = asyncio.get_running_loop()
    handled = asyncio.Event()
    loop.add_signal_handler(signal.SIGTERM, handled.set)
    try:
        db_path = tmp_path / "application-signal-handler.db"
        async with EmptyDB.open(db_path, daemonize_thread=True) as db:
            signal.raise_signal(signal.SIGTERM)
            await asyncio.wait_for(handled.wait(), timeout=1)
            assert db.initialized
        handled.clear()
        signal.raise_signal(signal.SIGTERM)
        await asyncio.wait_for(handled.wait(), timeout=1)
    finally:
        loop.remove_signal_handler(signal.SIGTERM)


@pytest.mark.asyncio
async def test_opt_in_signal_handler_restores_application_handler(tmp_path):
    if sys.platform == "win32":
        pytest.skip("signals not supported on Windows in asyncio")
    loop = asyncio.get_running_loop()
    handled = asyncio.Event()
    loop.add_signal_handler(signal.SIGTERM, handled.set)
    try:
        db = await EmptyDB.open(tmp_path / "restore.db", daemonize_thread=True, handle_signals=True)
        await db.close()
        signal.raise_signal(signal.SIGTERM)
        await asyncio.wait_for(handled.wait(), timeout=1)
    finally:
        loop.remove_signal_handler(signal.SIGTERM)


@pytest.mark.asyncio
async def test_opt_in_signal_handler_closes_multiple_databases(tmp_path):
    if sys.platform == "win32":
        pytest.skip("signals not supported on Windows in asyncio")
    first = await EmptyDB.open(tmp_path / "first.db", daemonize_thread=True, handle_signals=True)
    second = await EmptyDB.open(tmp_path / "second.db", daemonize_thread=True, handle_signals=True)
    signal.raise_signal(signal.SIGTERM)
    await asyncio.sleep(0.1)
    assert not first.initialized
    assert not second.initialized


@pytest.mark.asyncio
async def test_close_does_not_overwrite_new_application_signal_handler(tmp_path):
    if sys.platform == "win32":
        pytest.skip("signals not supported on Windows in asyncio")
    loop = asyncio.get_running_loop()
    handled = asyncio.Event()
    db = await EmptyDB.open(
        tmp_path / "application-replaced-handler.db",
        daemonize_thread=True,
        handle_signals=True,
    )
    loop.add_signal_handler(signal.SIGTERM, handled.set)
    try:
        await db.close()
        signal.raise_signal(signal.SIGTERM)
        await asyncio.wait_for(handled.wait(), timeout=1)
    finally:
        loop.remove_signal_handler(signal.SIGTERM)


@pytest.mark.asyncio
async def test_partial_signal_registration_failure_restores_handlers_and_closes_db(
    tmp_path, monkeypatch
):
    if sys.platform == "win32":
        pytest.skip("signals not supported on Windows in asyncio")
    loop = asyncio.get_running_loop()
    instances = []

    class TrackedDB(EmptyDB):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            instances.append(self)

    def application_sigint_handler():
        pass

    def application_sigterm_handler():
        pass

    loop.add_signal_handler(signal.SIGINT, application_sigint_handler)
    loop.add_signal_handler(signal.SIGTERM, application_sigterm_handler)
    original_add_signal_handler = loop.add_signal_handler
    failed = False

    def fail_once(sig, callback, *args):
        nonlocal failed
        if sig == signal.SIGTERM and not failed:
            failed = True
            raise RuntimeError("cannot install SIGTERM handler")
        return original_add_signal_handler(sig, callback, *args)

    monkeypatch.setattr(loop, "add_signal_handler", fail_once)
    try:
        with pytest.raises(RuntimeError, match="cannot install SIGTERM handler"):
            await TrackedDB.open(
                tmp_path / "partial-registration.db",
                daemonize_thread=True,
                handle_signals=True,
            )
        handlers = getattr(loop, "_signal_handlers")
        assert handlers[signal.SIGINT]._callback is application_sigint_handler
        assert handlers[signal.SIGTERM]._callback is application_sigterm_handler
        assert instances[0].conn is None
    finally:
        loop.remove_signal_handler(signal.SIGINT)
        loop.remove_signal_handler(signal.SIGTERM)
