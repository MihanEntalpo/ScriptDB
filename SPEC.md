# ScriptDB Specification

## Overview

ScriptDB is a small Python package that wraps SQLite behind a minimal,
script-friendly API. Its purpose is to give small utilities, ETL jobs,
automation scripts, and integrations a persistent storage layer without
introducing a full ORM, an external database server, or framework-specific
infrastructure.

The project is intentionally narrow in scope:

- SQLite only.
- Small, explicit API surface.
- Sync and async interfaces with near-identical behavior.
- Built-in migration support.
- Helper methods for common CRUD and query patterns.
- Optional cache-oriented database implementations.
- Packaging and typing suitable for PyPI distribution.

ScriptDB is not designed to be an ORM, a query builder for arbitrary SQL
workloads, or a general-purpose data platform. It is a pragmatic wrapper over
SQLite intended to remove repetitive boilerplate in scripts.

## Primary Goals

- Provide a very small persistence layer for script-like applications.
- Keep raw SQL usage simple and visible rather than abstracting it away.
- Support both synchronous and asynchronous codebases with matching APIs.
- Let applications define migrations directly in Python subclasses.
- Stay lightweight and easy to install.
- Preserve compatibility with older environments when practical.

## Non-Goals

- No ORM model layer.
- No relationship management system.
- No SQL dialect abstraction beyond SQLite.
- No schema reflection framework beyond what is needed for helper methods.
- No dependency on web frameworks or application frameworks.

## Repository Layout

- `src/scriptdb`
  Core package implementation.
- `tests`
  Behavioral and regression test suite.
- `README.md`
  User-facing documentation and quick-start usage.
- `CHANGELOG.md`
  Version history and release notes.
- `pyproject.toml`
  Packaging metadata, dependencies, tool configuration.
- `Makefile`
  Convenience commands for linting and tests.
- `AGENTS.md`
  Repository-specific instructions for coding agents.
- `SPEC.md`
  This architecture and behavior reference.

## Package Structure

### Public Entry Point

`src/scriptdb/__init__.py`

Responsibilities:

- Exposes the public package API.
- Imports synchronous primitives eagerly.
- Lazy-loads optional async and cache components so that importing
  `scriptdb` does not require `aiosqlite` immediately.
- Re-exports SQLite backend metadata such as selected backend name and
  SQLite version.

Public exports:

- `AbstractBaseDB`
- `SyncBaseDB`
- `AsyncBaseDB`
- `SyncCacheDB`
- `AsyncCacheDB`
- `Builder`
- `run_every_seconds`
- `run_every_queries`
- `sqlite_backend_name`
- `sqlite_version`

### Shared Base Layer

`src/scriptdb/abstractdb.py`

This file contains shared logic used by both sync and async database classes.

Core responsibilities:

- Defines `AbstractBaseDB`.
- Validates constructor arguments related to file creation behavior.
- Tracks lifecycle state:
  - `initialized`
  - `_is_closed`
  - `conn`
- Collects periodic and query-based hooks declared via decorators.
- Validates migration declarations.
- Provides the `require_init` guard decorator for methods that require an open
  connection.
- Provides legacy SQLite upsert compatibility helpers.

Important decorators:

- `require_init`
  Prevents usage before initialization or after close.
- `run_every_seconds`
  Marks a method to be executed periodically after initialization.
- `run_every_queries`
  Marks a method to be executed after a configured number of queries.

Migration validation rules:

- Every migration must have a string `name`.
- Migration names must be unique.
- The database must not contain applied migrations missing from the current
  subclass definition.
- Each migration must define exactly one of:
  - `sql`
  - `sqls`
  - `function`

Legacy SQLite compatibility:

- `legacy_sqlite_support` is stored on the base class.
- When enabled and the active SQLite build is too old for native upsert
  syntax, upsert helpers switch to compatibility mode.
- Compatibility mode emits a `RuntimeWarning` once per database instance.

### SQLite Backend Resolver

`src/scriptdb/sqlite_backend.py`

This module chooses the SQLite Python backend and centralizes version checks.

Responsibilities:

- Prefer `pysqlite3.dbapi2` when available.
- Fallback to `pysqlite3`.
- Fallback again to stdlib `sqlite3`.
- Publish backend metadata:
  - `SQLITE_BACKEND`
  - `SQLITE_VERSION`
  - `SQLITE_VERSION_INFO`
  - `SQLITE_TOO_OLD`
- Warn if the detected SQLite is older than the minimum supported native
  upsert version.
- Provide `ensure_upsert_supported()` for native upsert enforcement.

Minimum version policy:

- Native upsert support requires SQLite `>= 3.24.0`.
- On older SQLite builds:
  - native upsert path raises by default;
  - legacy compatibility mode can emulate upserts when
    `legacy_sqlite_support=True`.

### Synchronous Database Layer

`src/scriptdb/syncdb.py`

`SyncBaseDB` is the synchronous implementation using the chosen `sqlite3`
module.

Core responsibilities:

- Open and initialize SQLite connections.
- Apply migrations.
- Configure row factories.
- Expose synchronous CRUD/query helpers.
- Manage transaction state.
- Run periodic hooks in background daemon threads.
- Serialize upserts using a lock.
- Close connections safely and idempotently.

Connection behavior:

- Uses `sqlite3.connect(..., check_same_thread=False)`.
- Enables WAL by default unless `use_wal=False`.
- Supports `row_factory` configuration:
  - `sqlite3.Row`
  - `dict`

Initialization flow:

1. Connect to SQLite.
2. Optionally enable WAL.
3. Configure row factory.
4. Mark instance initialized.
5. Ensure the migration tracking table exists.
6. Apply unapplied migrations.
7. Start periodic threads for methods marked with `run_every_seconds`.

Transaction behavior:

- Automatic commit is enabled unless a manual transaction is active.
- Explicit methods:
  - `begin()`
  - `commit()`
  - `rollback()`
  - `transaction()` context manager

Query/command helpers:

- `execute(sql, params=None)`
- `execute_many(sql, seq_params)`
- `insert_one(table, row)`
- `insert_many(table, rows)`
- `upsert_one(table, row)`
- `upsert_many(table, rows)`
- `update_one(table, pk, row)`
- `delete_one(table, pk)`
- `delete_many(table, where, params=None)`
- `query_one(sql, params=None, postprocess_func=None)`
- `query_many(sql, params=None, postprocess_func=None)`
- `query_many_gen(sql, params=None, postprocess_func=None)`
- `query_scalar(sql, params=None, postprocess_func=None)`
- `query_column(sql, params=None, postprocess_func=None)`
- `query_dict(sql, params=None, key=None, value=None, postprocess_func=None)`
- `close()`

Primary key assumptions:

- Many helper methods assume a single-column primary key.
- Table metadata is inspected via `PRAGMA table_info(...)`.
- Tables without a primary key raise.
- Composite primary keys are allowed at schema level but helper methods that
  require a single key raise `ValueError`.

Native upsert behavior:

- Uses `INSERT ... ON CONFLICT(pk) DO UPDATE` or `DO NOTHING`.
- Requires SQLite >= 3.24.0.

Legacy upsert behavior:

- Activated only when both conditions are true:
  - `legacy_sqlite_support=True`
  - backend reports `SQLITE_TOO_OLD`
- Emulates upsert via:
  - `UPDATE ... WHERE pk=:pk`
  - if no row updated, `INSERT OR IGNORE`
  - if insert was ignored because of a race, retry `UPDATE`
- Keeps lock-based serialization around upserts.
- Bulk upsert is implemented as a loop inside one transaction when possible.

### Asynchronous Database Layer

`src/scriptdb/asyncdb.py`

`AsyncBaseDB` is the asynchronous implementation using `aiosqlite`.

Design intent:

- Mirror `SyncBaseDB` behavior as closely as possible.
- Preserve similar method names and semantics.
- Allow codebases to switch between sync and async with minimal migration
  effort.

Core responsibilities:

- Open async SQLite connections.
- Apply migrations asynchronously.
- Provide async CRUD/query helpers.
- Manage async transactions.
- Schedule periodic coroutines using `asyncio.create_task`.
- Register signal handlers to close open databases on process termination.
- Support daemonized `aiosqlite` worker threads through an internal wrapper.

Initialization flow:

1. Connect via `daemonizable_aiosqlite.connect`.
2. Optionally enable WAL.
3. Configure row factory.
4. Mark instance initialized.
5. Ensure the migration tracking table exists.
6. Apply migrations.
7. Start periodic async tasks for methods marked with `run_every_seconds`.

Transaction behavior:

- Explicit methods:
  - `begin()`
  - `commit()`
  - `rollback()`
  - `transaction()` async context manager
- Auto-commit is skipped when a manual transaction is active.

Query/command helpers:

- Async equivalents of the sync API.
- `query_many_gen` returns an async generator.

Query hook scheduling:

- Methods marked with `run_every_queries` are launched as async tasks after
  the configured query count is reached.

Upsert behavior:

- Mirrors sync behavior.
- Uses an `asyncio.Lock`.
- Supports the same legacy fallback path and warning model.

Signal handling:

- Registers handlers for `SIGINT` and `SIGTERM`.
- On signal, schedules `close()`.
- Removes registered handlers during close.

### Cache Database Layer

Files:

- `src/scriptdb/synccachedb.py`
- `src/scriptdb/asynccachedb.py`
- `src/scriptdb/_cache_index.py`

Purpose:

- Provide a persistent key-value cache stored in SQLite.
- Support optional TTL expiration.
- Support an optional RAM index to avoid repeated existence checks against the
  database.

Schema:

The cache layer defines its own migration:

- `cache`
  - `key TEXT PRIMARY KEY`
  - `value BLOB NOT NULL`
  - `expire_utc DATETIME`

Storage model:

- Values are serialized with `pickle`.
- Stored as SQLite blobs via `sqlite3.Binary`.
- Expiration timestamps are stored as ISO strings in UTC.

Public cache API:

- `get(key, default=None)`
- `is_set(key)`
- `set(key, value, expire_sec=None)`
- `delete(key)`
- `del_many(key_mask)`
- `keys(key_mask)`
- `clear()`
- `cache(...)` decorator

Behavior notes:

- `expire_sec=None` means no expiration.
- `expire_sec <= 0` means effectively expired immediately.
- Cleanup is performed periodically every 5 seconds using `run_every_seconds`.
- `cache()` decorator can memoize sync or async callables, depending on the
  cache class.

RAM index subsystem:

`_CacheKeyIndexMixin` manages an optional in-memory index of live cache keys.

Responsibilities:

- Track keys and expirations in RAM.
- Purge expired entries.
- Short-circuit negative existence checks.
- Keep memory structures sorted by expiration score.
- Protect RAM structures with a thread lock.

Data structures:

- `_ram_keys`
  Mapping of key -> expiration or sentinel.
- `_ram_entries`
  Sorted list of `(key, expire)` pairs.
- `_ram_scores`
  Parallel score list for fast insertion and purge.

Tradeoff:

- `cache_keys_in_ram=True` improves repeated key existence checks at the cost
  of additional memory and bookkeeping.

Legacy SQLite interaction:

- Cache `set()` internally relies on `upsert_one("cache", row)`.
- Therefore cache databases also support `legacy_sqlite_support=True`.

### Row Factory Helpers

`src/scriptdb/_rowfactory.py`

Purpose:

- Normalize row factory configuration.
- Convert SQLite rows to dictionaries when requested.
- Support helper functions that work for both row types.

Supported row types:

- `sqlite3.Row`
- `dict`

Important helpers:

- `normalize_row_factory`
- `dict_row_factory`
- `first_column_value`
- `supports_row_factory`
- `supports_init_arg`

### SQL Builder

`src/scriptdb/dbbuilder.py`

Purpose:

- Provide a small fluent API for generating SQLite DDL safely.
- Avoid repetitive string concatenation in migration definitions.

Main builder types:

- `_SQLBuilder`
- `CreateTableBuilder`
- `AlterTableBuilder`
- `DropTableBuilder`
- `CreateIndexBuilder`
- `DropIndexBuilder`
- `Builder` facade

Capabilities include:

- Create tables.
- Create tables from representative dictionaries.
- Define primary keys and fields with SQLite type inference.
- Add uniqueness and check constraints.
- Add references.
- Alter tables:
  - add column
  - drop/remove column
  - rename table
  - rename column
- Create/drop indexes.

Design constraints:

- Focused on schema-building convenience, not general SQL generation.
- Produces raw SQL strings consumed by migration entries.

### Async/Sync Conversion

`src/scriptdb/conversion.py`

Purpose:

- Generate async wrappers from sync DB classes.
- Generate sync wrappers from async DB classes.

Constraints:

- Source class must inherit from the expected base class.
- Conversion only supports SQL migrations:
  - `sql`
  - `sqls`
  - builder instances
- Callable `function` migrations are rejected because async/sync callable
  semantics are implementation-specific.

### Daemonizable aiosqlite Wrapper

`src/scriptdb/daemonizable_aiosqlite.py`

Purpose:

- Wrap `aiosqlite` so its worker thread can optionally run as a daemon.

Reason:

- Some environments and tests can hang on process exit because of
  non-daemonized worker threads.

Behavior:

- Exposes `connect(...)`.
- Returns a custom `DaemonConnection`.
- Supports `daemonize_thread=True`.

## Migration System

Migrations are declared by subclassing `SyncBaseDB` or `AsyncBaseDB` and
implementing `migrations(self)`.

Each migration is a dictionary with:

- `name`
- exactly one of:
  - `sql`
  - `sqls`
  - `function`

### `sql`

A single SQL string or SQL builder instance.

Behavior:

- Executed with `executescript`.
- Suitable for one or many statements separated by semicolons.

### `sqls`

A sequence of SQL strings or SQL builder instances.

Behavior:

- Rendered into a single script.
- Script is wrapped in `BEGIN/COMMIT` automatically unless the user already
  provided explicit transaction control.
- If a script begins a transaction but does not close it, validation fails.

### `function`

A Python callable invoked during migration.

Sync constraints:

- Must be synchronous.
- Can be a bound method name or callable.

Async constraints:

- Must be awaitable when used from `AsyncBaseDB`.

Signature expectations:

- Bound methods: `(migrations, name)`
- Unbound callables: `(db, migrations, name)`

Applied migrations tracking:

- Stored in `applied_migrations`.
- `name` is the primary key.
- `applied_at` stores a UTC timestamp.

## Public API Semantics

### Opening Databases

Sync:

- `with MyDB.open(path) as db: ...`
- `db = MyDB.open(path)._open()` internally, though end users should normally
  prefer the context manager.

Async:

- `db = await MyDB.open(path)`
- `async with MyDB.open(path) as db: ...`

Common constructor/open options:

- `auto_create`
- `use_wal`
- `row_factory`
- `legacy_sqlite_support`

Async-only options:

- `daemonize_thread`

Cache-only options:

- `cache_keys_in_ram`

### Query Result Shapes

By default:

- Rows are `sqlite3.Row`.

With `row_factory=dict`:

- Query helpers return plain dictionaries.

Affected query helpers:

- `query_one`
- `query_many`
- `query_many_gen`
- `query_dict`

Derived query helpers:

- `query_scalar`
- `query_column`

### `query_dict`

Purpose:

- Convert a result set into a Python dictionary.

Key selection:

- Explicit string column name.
- Callable.
- Inferred from table primary key if `key=None` and the SQL has a simple
  `FROM table` form.

Value selection:

- Entire row.
- Specific column name.
- Callable.

Known limitation:

- SQL parsing for automatic table inference is intentionally simple.

## Compatibility Model

### Python

- Minimum supported Python is `3.8`.

### SQLite

- Native upsert path expects SQLite `>= 3.24.0`.
- Older SQLite builds are still partially usable:
  - most operations work;
  - native upsert path raises by default;
  - optional legacy compatibility mode enables slower emulated upserts.

### Optional Dependencies

- `aiosqlite` for async support.
- `pysqlite3-binary` as an optional modern SQLite backend on older systems.

## Testing Strategy

The test suite covers:

- Sync and async base database behavior.
- Cache database behavior.
- Module exports.
- SQL builder behavior.
- Conversion helpers.
- Interrupt and close behavior.
- Composite primary key error handling.
- Old SQLite compatibility behavior.
- Daemonized `aiosqlite` behavior.

Notable test files:

- `tests/test_sync_basedb.py`
- `tests/test_async_basedb.py`
- `tests/test_sync_cachedb.py`
- `tests/test_async_cachedb.py`
- `tests/test_sqlite_backend.py`
- `tests/test_old_sqlite_backend.py`
- `tests/test_dbbuilder.py`
- `tests/test_module_exports.py`

The repository also contains a vendored old SQLite shared library under
`tests/old-sqlite` for compatibility tests on supported Linux environments.

## Tooling and Release Process

### Tooling

Configured in `pyproject.toml`:

- Ruff
- mypy
- pytest
- pytest-asyncio
- pytest-cov

### Development Commands

Typical validation flow:

- `pip install -e .[test]`
- `ruff check .`
- `mypy src/scriptdb`
- `pytest --cov=scriptdb --cov-report=term-missing`

### Packaging

- Uses setuptools with `src/` layout.
- Ships `py.typed`.
- Intended for PyPI publication.

Release-related scripts:

- `build.sh`
  Build and upload to PyPI/TestPyPI.
- `release-github.sh`
  Create and publish GitHub releases based on the package version.

## Architectural Invariants

The following properties should be preserved unless the project is being
deliberately redesigned:

- Sync and async APIs should stay behaviorally aligned.
- Importing `scriptdb` should not require `aiosqlite` unless async classes are
  actually accessed.
- Migrations should remain subclass-defined and explicit.
- Helper methods that rely on a single primary key should fail clearly on
  unsupported schemas.
- Legacy SQLite support should remain opt-in.
- Cache layer behavior should be consistent across sync and async variants.
- SQLite remains the only database backend.
- Public API changes should be reflected in README, CHANGELOG, tests, and this
  specification.

## Known Limitations

- Composite primary keys are not supported by helper methods requiring a single
  primary key.
- SQL parsing for `query_dict(key=None)` is intentionally limited.
- Cache values use `pickle`, so they are Python-specific and should be treated
  as trusted data.
- Legacy upsert mode is slower than native SQLite upsert support.
- Daemonized async worker threads trade safer process shutdown for the
  possibility of abrupt termination.

## Maintenance Requirements

When making changes to the codebase:

- Update this file whenever architecture, public behavior, supported options,
  or significant implementation constraints change.
- Update `README.md` for user-visible changes.
- Update `CHANGELOG.md` for release-worthy behavior changes.
- Keep tests aligned with the documented behavior.

This document is intended to be the high-level source of truth for repository
structure, component roles, and behavioral expectations.
