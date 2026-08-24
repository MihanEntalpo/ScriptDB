# Cache API

`SyncCacheDB` and `AsyncCacheDB` provide a persistent key-value cache backed by
SQLite. Values are serialized with `pickle`, so only read data from trusted
databases.

## Basic usage

### Sync

```python
from scriptdb import SyncCacheDB


with SyncCacheDB.open("cache.db") as cache:
    cache.set("answer", {"value": 42})
    print(cache.get("answer"))
```

### Async

```python
from scriptdb import AsyncCacheDB


async with AsyncCacheDB.open("cache.db") as cache:
    await cache.set("answer", {"value": 42})
    print(await cache.get("answer"))
```

## Methods

### `set(key, value, expire_sec=None)`

Store any pickle-serializable value. `None` means no expiration. A positive
`expire_sec` stores an absolute UTC expiration time. Zero or a negative value
is immediately expired:

```python
cache.set("forever", "value")
cache.set("temporary", "value", expire_sec=60)
cache.set("disabled", "value", expire_sec=0)
```

### `get(key, default=None)`

Return the value or the default when the key is missing or expired:

```python
user = cache.get("user:42", default={})
```

### `is_set(key)`

Check whether a non-expired key exists without deserializing its value:

```python
if cache.is_set("user:42"):
    refresh_user(cache.get("user:42"))
```

### `delete(key)`

Delete one key and return the affected row count:

```python
removed = cache.delete("user:42")
```

### `del_many(key_mask)`

Delete keys matching a mask. `*` means any sequence of characters. The `_`
character is treated literally:

```python
cache.del_many("user:*")
```

### `keys(key_mask)`

Return non-expired keys matching the same mask syntax:

```python
user_keys = cache.keys("user:*")
```

### `clear()`

Delete all keys and return the number removed:

```python
cache.clear()
```

## `cache()` decorator

Decorate a sync callable with `SyncCacheDB`:

```python
with SyncCacheDB.open("cache.db") as cache:
    @cache.cache(expire_sec=30)
    def square(value):
        print("calculated")
        return value * value

    square(4)  # calculates
    square(4)  # reads the cached result
```

Use `key_func` when the default key is not appropriate:

```python
@cache.cache(key_func=lambda user_id: f"user:{user_id}")
def load_user(user_id):
    return fetch_user(user_id)
```

`AsyncCacheDB.cache()` returns an async wrapper and supports both async and
sync callables:

```python
@cache.cache(expire_sec=60, key_func=lambda url: url)
async def fetch(url):
    return await http_get(url)
```

The decorator caches return values, not exceptions. Choose keys carefully so
different calls do not collide.

## RAM key index

Enable `cache_keys_in_ram=True` to keep live key metadata in memory:

```python
with SyncCacheDB.open("cache.db", cache_keys_in_ram=True) as cache:
    if cache.is_set("expensive:42"):
        value = cache.get("expensive:42")
```

This speeds up repeated negative existence checks and lets `get()` avoid a
database query for known-missing keys. The index is process-local and consumes
memory proportional to the number of keys. Do not use it as the source of
truth when another process modifies the same cache.

Expired entries are cleaned periodically. `expire_sec=None` entries remain
until explicitly deleted or cleared.
