# Cache methods

`SyncCacheDB` and `AsyncCacheDB` inherit the database API and add a persistent
key-value cache. Async calls are awaited. Values are serialized with `pickle`,
so cache databases must be trusted.

## `open(..., cache_keys_in_ram=False)`

`cache_keys_in_ram=True` maintains a process-local index of live keys:

```python
with SyncCacheDB.open("cache.db", cache_keys_in_ram=True) as cache:
    ...
```

It improves repeated negative checks but consumes memory. The index is
process-local and intentionally does not observe writes from another process.

## `get(key, default=None) -> Any`

Return the unpickled value, or `default` if the key is missing or expired:

```python
value = cache.get("user:1", default=None)
```

## `is_set(key) -> bool`

Return whether a key exists and has not expired, without unpickling it:

```python
if cache.is_set("report:today"):
    send(cache.get("report:today"))
```

## `set(key, value, expire_sec=None) -> None`

Store a pickle-serializable value. `None` means no expiration; a positive
number is a TTL in seconds; zero or a negative number is immediately expired:

```python
cache.set("token", "abc", expire_sec=300)
await async_cache.set("temporary", {"ok": True}, expire_sec=60)
```

## `delete(key) -> int`

Delete one key and return the affected row count:

```python
removed = cache.delete("token")
```

## `del_many(key_mask) -> int`

Delete keys matching a mask. `*` means any sequence of characters; `_`, `%`,
and backslashes are treated literally:

```python
cache.del_many("session:*")
```

## `keys(key_mask) -> list[str]`

Return non-expired keys matching the same mask:

```python
keys = cache.keys("session:*")
```

## `clear() -> int`

Delete all cache entries and return the affected row count:

```python
removed = cache.clear()
```

## `cache(expire_sec=None, key_func=None) -> decorator`

Return a memoization decorator. The default key includes the function name,
positional arguments, and keyword arguments. `key_func` receives the call
arguments and must return a string:

```python
@cache.cache(expire_sec=60, key_func=lambda user_id: f"user:{user_id}")
def load_user(user_id):
    return fetch_user(user_id)
```

`AsyncCacheDB.cache` returns an async wrapper and supports sync and async
callables. Exceptions are not cached. See [Cache API](../cache.md) for TTL,
cleanup, and RAM-index tradeoffs.
