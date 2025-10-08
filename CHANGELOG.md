# Changelog of the ScriptDB

## 1.0.8 - Added RAM key index, also reduce routine logging noise

- Added RAM key index for sync and async cachedb, with cache_keys_in_ram=True it can speadup keys existing checks with some additional RAM usage
- Lowered periodic task lifecycle logging in `AsyncBaseDB` and `SyncBaseDB` from INFO to DEBUG

## 1.0.7 - Expanded tests and reliability fixes

- Added tests for insert_many, query_dict edge cases, zero-expiry keys and PK-only upserts
- Improved update/upsert handling for empty data and removed duplicate query hooks
- Introduced DB builder module, unified DDL builder API and added identifier injection tests
- Replaced deprecated loop parameter warnings and enabled migration rollback on interrupts

## 1.0.6 - Added simple SQL DDL queries builder

- Added handy SQL DDL queries building tools
 
## 1.0.5 - Handle database close on interrupts

## 1.0.4 - Added tests and docs

## 1.0.3 - Typing and linting

### Added

* PyPI classifiers and packaged type hints via `py.typed`
* Ruff and mypy linting with CI integration and developer docs + reformatting
* Added daemonizing aiosqlite so tests (or some apps) would not hang

## 1.0.2 - Lazy async imports

### Added

* Deferred loading of async components so `SyncBaseDB` works without `aiosqlite`
* Clear guidance when `aiosqlite` is missing
* Tests covering public exports and sync-only operation

## 1.0.1 - Slight changes

### Added

* Moved aiosqlite to [async] dependency
* Added Changelog, updated README.md
* Added gitlab actions

## 1.0.0 - First working version

### Added

* Full sync and async BaseDB with migrations support
* CacheDB implemented via sync and async interfaces

