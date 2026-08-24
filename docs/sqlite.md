# SQLite troubleshooting

## Backend selection

ScriptDB selects the first importable backend in this order:

1. `pysqlite3.dbapi2`;
2. `pysqlite3`;
3. standard-library `sqlite3`.

Selection is based on the module that actually imports and its actual
`sqlite_version_info`; installed distribution metadata is diagnostic only.

Inspect the selected backend from Python:

```python
from scriptdb import sqlite_backend

print("backend:", sqlite_backend.SQLITE_BACKEND)
print("version:", sqlite_backend.SQLITE_VERSION)
print("version info:", sqlite_backend.SQLITE_VERSION_INFO)
print("module:", sqlite_backend.sqlite3.__file__)
print("too old:", sqlite_backend.SQLITE_TOO_OLD)

info = sqlite_backend.SQLITE_BACKEND_INFO
print("pysqlite3 installed:", info.pysqlite_distribution_installed)
print("pysqlite3-binary installed:", info.pysqlite_binary_distribution_installed)
print("import error:", info.pysqlite_import_error)
```

## Normal cases

If pysqlite is not installed, ScriptDB uses stdlib `sqlite3`. If that SQLite
version is old, the warning recommends:

```bash
python -m pip install 'scriptdb[pysqlite]'
```

If pysqlite imports successfully and reports SQLite 3.24.0 or newer, no
backend warning is emitted.

## Old ordinary `pysqlite3`

The ordinary `pysqlite3` package may be compiled against the operating
system's old SQLite. ScriptDB reports the selected backend, version, module
path, and recommends replacing it with the binary distribution:

```bash
python -m pip uninstall -y pysqlite3
python -m pip install --no-cache-dir --only-binary=:all: pysqlite3-binary
```

## Conflicting distributions

`pysqlite3` and `pysqlite3-binary` both install the `pysqlite3` namespace,
including `pysqlite3.dbapi2` and the native `_sqlite3` extension. Installing
both can mix or overwrite files. If both are reported as installed while the
loaded backend is old, remove both and install only the binary package:

```bash
python -m pip uninstall -y pysqlite3 pysqlite3-binary

python -m pip install \
    --no-cache-dir \
    --only-binary=:all: \
    pysqlite3-binary
```

## Stale or overwritten files

If metadata reports only `pysqlite3-binary` but the imported module still
reports an unexpectedly old SQLite, do not assume the metadata is correct.
The site-packages directory may contain files left by an earlier installation.
Use the same clean reinstall:

```bash
python -m pip uninstall -y pysqlite3 pysqlite3-binary

python -m pip install \
    --no-cache-dir \
    --only-binary=:all: \
    pysqlite3-binary
```

ScriptDB diagnoses the problem but does not modify the Python environment.

## Broken import

If metadata says a pysqlite distribution is installed but importing it raises
`ImportError`, ScriptDB preserves the original error in
`SQLITE_BACKEND_INFO.pysqlite_import_error`, emits a warning, and falls back
to stdlib `sqlite3` when possible. Normal startup is not aborted solely
because optional pysqlite is broken.

## Independent inspection

Use this command to compare stdlib and pysqlite versions:

```bash
python - <<'PY'
import sqlite3
print("stdlib:", sqlite3.sqlite_version, sqlite3.__file__)

try:
    import pysqlite3
    print("pysqlite3:", pysqlite3.sqlite_version, pysqlite3.__file__)
except ImportError as exc:
    print("pysqlite3 import failed:", exc)
PY

python -m pip show pysqlite3
python -m pip show pysqlite3-binary
python -m pip freeze | grep -i sqlite
```

## Legacy compatibility mode

If upgrading SQLite is impossible, open the database with
`legacy_sqlite_support=True`:

```python
with AppDB.open("app.db", legacy_sqlite_support=True) as db:
    db.upsert_one("items", {"id": 1, "value": "legacy"})
```

This emulates upserts with older SQLite-compatible statements. It is slower
than native `ON CONFLICT` upsert and only activates when the selected backend
is actually below SQLite 3.24.0.
