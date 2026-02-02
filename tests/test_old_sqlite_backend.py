import ctypes
import json
import os
import platform
from pathlib import Path
import subprocess
import sys
import textwrap

import pytest

from scriptdb import sqlite_backend

ROOT = Path(__file__).resolve().parents[1]
OLD_SQLITE_DIR = ROOT / "tests" / "old-sqlite"
OLD_SQLITE_LIB = OLD_SQLITE_DIR / "libsqlite3.so.0.8.6"


@pytest.mark.skipif(
    sys.platform != "linux" or platform.machine() != "x86_64",
    reason="old sqlite build is provided for Linux x86_64 only",
)
@pytest.mark.skipif(not OLD_SQLITE_LIB.exists(), reason="old sqlite shared library is missing")
def test_old_sqlite_upsert_requires_newer_version() -> None:
    try:
        old_sqlite = ctypes.CDLL(str(OLD_SQLITE_LIB))
    except OSError as exc:
        pytest.skip(f"old sqlite library failed to load: {exc}")

    if not hasattr(old_sqlite, "sqlite3_create_window_function"):
        pytest.skip("old sqlite library lacks sqlite3_create_window_function required by Python sqlite3")

    script = textwrap.dedent(
        f"""
        import json
        from pathlib import Path
        import sys

        repo_root = Path({str(ROOT)!r})
        sys.path.insert(0, str(repo_root / "src"))

        from scriptdb import sqlite_backend

        payload = {{
            "backend": sqlite_backend.SQLITE_BACKEND,
            "too_old": sqlite_backend.SQLITE_TOO_OLD,
            "version": sqlite_backend.SQLITE_VERSION,
            "upsert_supported": True,
            "error": "",
        }}
        try:
            sqlite_backend.ensure_upsert_supported()
        except Exception as exc:
            payload["upsert_supported"] = False
            payload["error"] = str(exc)
        print(json.dumps(payload))
        """
    ).strip()

    env = os.environ.copy()
    env["LD_PRELOAD"] = str(OLD_SQLITE_LIB)
    env["LD_LIBRARY_PATH"] = f"{OLD_SQLITE_DIR}:{env.get('LD_LIBRARY_PATH', '')}".rstrip(":")
    env["PYTHONNOUSERSITE"] = "1"

    result = subprocess.run(
        [sys.executable, "-S", "-c", script],
        capture_output=True,
        text=True,
        env=env,
        check=True,
    )
    payload = json.loads(result.stdout)

    assert payload["too_old"] is True
    assert payload["upsert_supported"] is False
    assert payload["version"].startswith("3.22")
    assert sqlite_backend.UPSERT_UNSUPPORTED_MESSAGE in payload["error"]


@pytest.mark.skipif(
    sys.platform != "linux" or platform.machine() != "x86_64",
    reason="old sqlite build is provided for Linux x86_64 only",
)
@pytest.mark.skipif(not OLD_SQLITE_LIB.exists(), reason="old sqlite shared library is missing")
def test_suite_runs_with_old_sqlite() -> None:
    if os.environ.get("SCRIPTDB_OLD_SQLITE_SUBPROCESS") == "1":
        pytest.skip("avoid recursive pytest execution")

    try:
        old_sqlite = ctypes.CDLL(str(OLD_SQLITE_LIB))
    except OSError as exc:
        pytest.skip(f"old sqlite library failed to load: {exc}")

    if not hasattr(old_sqlite, "sqlite3_create_window_function"):
        pytest.skip("old sqlite library lacks sqlite3_create_window_function required by Python sqlite3")

    env = os.environ.copy()
    env["LD_PRELOAD"] = str(OLD_SQLITE_LIB)
    env["LD_LIBRARY_PATH"] = f"{OLD_SQLITE_DIR}:{env.get('LD_LIBRARY_PATH', '')}".rstrip(":")
    env["PYTHONNOUSERSITE"] = "1"
    env["SCRIPTDB_OLD_SQLITE_SUBPROCESS"] = "1"

    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--ignore=tests/test_old_sqlite_backend.py"],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )

    if result.returncode != 0:
        raise AssertionError(
            "pytest failed under old sqlite\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )
