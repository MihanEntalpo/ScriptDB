# Changelog of the ScriptDB

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

