# tests/conftest.py
import sys
import os
import time
import signal
import threading
import traceback
import faulthandler


def _live_threads():
    main = threading.main_thread()
    return [t for t in threading.enumerate() if t is not main]


def _classify_threads(live):
    """Return grouped threads:
    (aiosqlite_nd, aiosqlite_d, others_nd, others_d) with stacks."""
    frames = sys._current_frames()
    aiosqlite_nd, aiosqlite_d, others_nd, others_d = [], [], [], []

    for t in live:
        fr = frames.get(t.ident)
        stack = traceback.extract_stack(fr) if fr else []
        in_aiosqlite_stack = any(
            "aiosqlite" in (frm.filename or "") or "aiosqlite" in (frm.name or "") for frm in stack
        )
        in_aiosqlite_name = "aiosqlite" in t.name.lower()
        target = (aiosqlite_nd, aiosqlite_d) if (in_aiosqlite_stack or in_aiosqlite_name) else (others_nd, others_d)
        (target[1] if t.daemon else target[0]).append((t, stack))
    return aiosqlite_nd, aiosqlite_d, others_nd, others_d


def _schedule_sigterm(delay_sec: float = 3.0):
    """Spawn a daemon thread that sends SIGTERM to the current process after a delay."""

    def _killer(pid: int, d: float):
        time.sleep(d)
        os.kill(pid, signal.SIGTERM)

    t = threading.Thread(target=_killer, args=(os.getpid(), delay_sec), name="pytest-sigterm-killer", daemon=True)
    t.start()


def _print_group(tr, title, items):
    """
    Print group of threads for debugging purpose
    """
    if not items:
        return
    tr.write_sep("-", f"{title} ({len(items)})")
    for t, _ in items:
        tr.write_line(
            f"cls={type(t).__name__} name={t.name!r} ident={t.ident} "
            f"daemon={t.daemon} alive={t.is_alive()} created_at={getattr(t, 'creation_site', '')}"
        )


def pytest_sessionfinish(session, exitstatus):
    """
    At the very end, report lingering threads (grouped by daemon flag) and schedule SIGTERM if aiosqlite workers remain.
    """
    tr = session.config.pluginmanager.getplugin("terminalreporter")
    if not tr:
        return

    live = _live_threads()
    if not live:
        return

    aiosqlite_nd, aiosqlite_d, others_nd, others_d = _classify_threads(live)

    if len(aiosqlite_nd) + len(others_nd) > 0:
        tr.write_sep("=", "LIVE THREADS AT SESSION END (grouped)")
        _print_group(tr, "aiosqlite (non-daemon)", aiosqlite_nd)
        _print_group(tr, "aiosqlite (daemon)", aiosqlite_d)
        _print_group(tr, "others (non-daemon)", others_nd)
        _print_group(tr, "others (daemon)", others_d)

        tr.write_line("")  # spacing
        tr.write_line("[faulthandler] stacks of all threads:")
        faulthandler.dump_traceback(file=sys.stderr, all_threads=True)

        if aiosqlite_nd or aiosqlite_d:
            tr.write_sep("-", "NOTICE")
            tr.write_line(
                "Detected lingering aiosqlite worker threads. "
                "Make sure to `await db.close()` (or use `async with`). "
                "If this still doesn’t resolve the issue, consider enabling "
                "`daemonize_thread=True` in the constructor or during open()."
            )
            # Avoid hanging in threading._shutdown if something still blocks.
            _schedule_sigterm(3.0)
