# API reference

The API reference is split into small pages so it can be indexed reliably by
documentation tools and read independently.

## Database API

- [Lifecycle, migrations, and transactions](api/lifecycle.md)
- [SQL, CRUD, and upserts](api/crud.md)
- [Query helpers](api/queries.md)

## Supporting APIs

- [Cache methods](api/cache.md)
- [Hooks and class conversion](api/hooks.md)
- [SQL Builder](builder.md)
- [SQLite backend diagnostics](sqlite.md)

## Main classes

```python
from scriptdb import AsyncBaseDB, AsyncCacheDB, Builder, SyncBaseDB, SyncCacheDB
```

`SyncBaseDB` and `SyncCacheDB` provide blocking methods. `AsyncBaseDB` and
`AsyncCacheDB` provide the same operations as coroutines; use `await` and
`async for` where appropriate.

Common constructor options are `auto_create`, `row_factory`, `use_wal`, and
`legacy_sqlite_support`. Async classes additionally support
`daemonize_thread`; cache classes support `cache_keys_in_ram`.

All examples in the detailed pages include parameters, return values, and
lifecycle notes.
