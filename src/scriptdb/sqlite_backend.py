from __future__ import annotations

import importlib
import importlib.metadata
import sys
import warnings
from dataclasses import dataclass
from types import ModuleType
from typing import Optional, Tuple

MIN_SQLITE_VERSION: Tuple[int, int, int] = (3, 24, 0)
MIN_SQLITE_VERSION_STR = ".".join(str(part) for part in MIN_SQLITE_VERSION)


@dataclass(frozen=True)
class SQLiteBackendInfo:
    """Details about the selected SQLite backend and optional pysqlite packages."""

    backend: str
    sqlite_version: str
    sqlite_version_info: Tuple[int, int, int]
    module_path: Optional[str]
    pysqlite_distribution_installed: bool
    pysqlite_binary_distribution_installed: bool
    pysqlite_import_error: Optional[str]


def _distribution_installed(name: str) -> bool:
    try:
        importlib.metadata.distribution(name)
    except importlib.metadata.PackageNotFoundError:
        return False
    except Exception:
        # Metadata is diagnostic only and must never affect backend selection.
        return False
    return True


def _pysqlite_distribution_status() -> Tuple[bool, bool]:
    return _distribution_installed("pysqlite3"), _distribution_installed("pysqlite3-binary")


def _old_sqlite_warning(info: SQLiteBackendInfo) -> str:
    version = info.sqlite_version
    message = (
        f"Selected SQLite backend: {info.backend}\n"
        f"SQLite version: {version}\n"
        f"Module: {info.module_path or '<unknown>'}\n\n"
        f"pysqlite3 was successfully imported, but it provides SQLite {version}, "
        f"which is older than the required {MIN_SQLITE_VERSION_STR}."
    )

    if info.pysqlite_distribution_installed and info.pysqlite_binary_distribution_installed:
        return (
            f"{message}\n"
            "Both `pysqlite3` and `pysqlite3-binary` are installed. These distributions provide "
            "the same `pysqlite3` import namespace and their files may conflict.\n"
            "Remove both packages and reinstall only `pysqlite3-binary`:\n\n"
            "    python -m pip uninstall -y pysqlite3 pysqlite3-binary\n"
            "    python -m pip install --no-cache-dir --only-binary=:all: pysqlite3-binary"
        )

    if info.pysqlite_distribution_installed and not info.pysqlite_binary_distribution_installed:
        return (
            f"{message}\n"
            "The ordinary `pysqlite3` package may have been compiled against an old system SQLite. "
            "Replace it with the binary distribution:\n\n"
            "    python -m pip uninstall -y pysqlite3\n"
            "    python -m pip install --no-cache-dir --only-binary=:all: pysqlite3-binary"
        )

    return (
        f"{message}\n"
        "The loaded pysqlite3 installation may contain stale or overwritten files. "
        "Cleanly reinstall only the binary distribution:\n\n"
        "    python -m pip uninstall -y pysqlite3 pysqlite3-binary\n"
        "    python -m pip install --no-cache-dir --only-binary=:all: pysqlite3-binary"
    )


def _pysqlite_import_warning(info: SQLiteBackendInfo) -> str:
    installed = []
    if info.pysqlite_distribution_installed:
        installed.append("pysqlite3")
    if info.pysqlite_binary_distribution_installed:
        installed.append("pysqlite3-binary")
    distributions = " and ".join(f"`{name}`" for name in installed)
    return (
        f"{distributions} appears to be installed, but importing pysqlite3 failed:\n\n"
        f"{info.pysqlite_import_error}\n\nFalling back to stdlib sqlite3."
    )


def _load_sqlite_module() -> Tuple[ModuleType, str, bool, SQLiteBackendInfo]:
    pysqlite_installed, pysqlite_binary_installed = _pysqlite_distribution_status()
    backend = "sqlite3"
    import_error: Optional[str] = None

    try:
        module = importlib.import_module("pysqlite3.dbapi2")
        backend = "pysqlite3"
        sys.modules["sqlite3"] = module
    except ImportError as first_error:
        try:
            module = importlib.import_module("pysqlite3")
            backend = "pysqlite3"
            sys.modules["sqlite3"] = module
        except ImportError as second_error:
            if pysqlite_installed or pysqlite_binary_installed:
                import_error = (
                    f"pysqlite3.dbapi2: {type(first_error).__name__}: {first_error}\n"
                    f"pysqlite3: {type(second_error).__name__}: {second_error}"
                )
            module = importlib.import_module("sqlite3")

    version_info = getattr(module, "sqlite_version_info", (0, 0, 0))
    normalized_version_info: Tuple[int, int, int] = tuple(version_info)  # type: ignore[assignment]
    version_str = getattr(module, "sqlite_version", ".".join(str(part) for part in normalized_version_info))
    info = SQLiteBackendInfo(
        backend=backend,
        sqlite_version=version_str,
        sqlite_version_info=normalized_version_info,
        module_path=getattr(module, "__file__", None),
        pysqlite_distribution_installed=pysqlite_installed,
        pysqlite_binary_distribution_installed=pysqlite_binary_installed,
        pysqlite_import_error=import_error,
    )
    too_old = normalized_version_info < MIN_SQLITE_VERSION

    if import_error:
        warnings.warn(
            _pysqlite_import_warning(info),
            RuntimeWarning,
            stacklevel=2,
        )

    if too_old:
        if backend == "pysqlite3":
            warnings.warn(_old_sqlite_warning(info), RuntimeWarning, stacklevel=2)
        else:
            warnings.warn(
                (
                    f"SQLite {version_str} is older than the recommended {MIN_SQLITE_VERSION_STR}; "
                    "upsert_one and upsert_many will not work. "
                    "Install it with: python -m pip install 'scriptdb[pysqlite]'"
                ),
                RuntimeWarning,
                stacklevel=2,
            )

    return module, backend, too_old, info


sqlite3, SQLITE_BACKEND, SQLITE_TOO_OLD, SQLITE_BACKEND_INFO = _load_sqlite_module()
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
    "SQLITE_BACKEND_INFO",
    "SQLITE_TOO_OLD",
    "SQLITE_VERSION",
    "SQLITE_VERSION_INFO",
    "UPSERT_UNSUPPORTED_MESSAGE",
    "MIN_SQLITE_VERSION",
    "MIN_SQLITE_VERSION_STR",
    "ensure_upsert_supported",
    "SQLiteBackendInfo",
    "sqlite3",
]
