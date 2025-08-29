import signal
import sys

import pytest
from scriptdb import SyncBaseDB


class EmptyDB(SyncBaseDB):
    def migrations(self):
        return []


def test_keyboard_interrupt_triggers_single_close(tmp_path):
    db_path = tmp_path / "ki.db"
    with pytest.raises(KeyboardInterrupt):
        with EmptyDB.open(db_path) as db:
            db.close()
            raise KeyboardInterrupt


def test_signal_handler_closes_db(tmp_path):
    if sys.platform == "win32":
        pytest.skip("signals not supported on Windows")
    db_path = tmp_path / "sig.db"
    with EmptyDB.open(db_path) as db:
        def handler(signum, frame):
            db.close()
        original = signal.signal(signal.SIGTERM, handler)
        try:
            signal.raise_signal(signal.SIGTERM)
            assert not db.initialized
        finally:
            signal.signal(signal.SIGTERM, original)
