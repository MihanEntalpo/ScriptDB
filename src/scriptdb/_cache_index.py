"""Shared helpers for cache databases that track keys in RAM."""
from __future__ import annotations

from bisect import bisect_left
from datetime import datetime, timezone
from functools import wraps
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple, cast

import logging
import threading
from sys import getsizeof


logger = logging.getLogger(__name__)


def _ram_locked(method):
    """Decorator that guards cache-index mutations with ``_ram_lock``."""

    @wraps(method)
    def wrapper(self, *args, **kwargs):  # type: ignore[override]
        if not getattr(self, "_cache_keys_in_ram", False):
            return method(self, *args, **kwargs)
        with self._ram_lock:
            return method(self, *args, **kwargs)

    return wrapper


class _CacheKeyIndexMixin:
    """Mixin that manages an in-memory index of cache keys and expirations."""

    _MISSING = object()

    def __init__(self, *args, cache_keys_in_ram: bool = False, **kwargs) -> None:
        """Initialize RAM-index storage; subclasses forward ``cache_keys_in_ram``."""
        self._cache_keys_in_ram = cache_keys_in_ram
        self._ram_lock = threading.Lock()
        self._ram_keys: Dict[str, Optional[datetime]] = {}
        self._ram_entries: List[Tuple[str, Optional[datetime]]] = []
        self._ram_scores: List[float] = []
        super().__init__(*args, **kwargs)

    @staticmethod
    def _parse_expire(value: Optional[str]) -> Optional[datetime]:
        """Convert an ISO timestamp to ``datetime`` for RAM bookkeeping."""
        return datetime.fromisoformat(value) if value else None

    @staticmethod
    def _expire_score(expire: Optional[datetime]) -> float:
        """Return ordering value for expires; used internally for sorting."""
        return expire.timestamp() if expire is not None else float("inf")

    def _reload_ram_index(self, rows: Iterable[Mapping[str, Any]]) -> None:
        """Rebuild the RAM index from DB rows; call from subclass initialization."""
        if not self._cache_keys_in_ram:
            return
        now = datetime.now(timezone.utc)
        mapping: Dict[str, Optional[datetime]] = {}
        entries: List[Tuple[str, Optional[datetime]]] = []
        for row in rows:
            key = str(row["key"])
            expire = self._parse_expire(cast(Optional[str], row["expire_utc"]))
            if expire is not None and expire <= now:
                continue
            mapping[key] = expire
            entries.append((key, expire))
        entries.sort(key=lambda item: self._expire_score(item[1]), reverse=True)
        scores = [-self._expire_score(expire) for _, expire in entries]
        self._ram_reset(mapping, entries, scores)
        if logger.isEnabledFor(logging.DEBUG):
            size = getsizeof(mapping) + getsizeof(entries) + getsizeof(scores)
            size += sum(getsizeof(key) + getsizeof(expire) for key, expire in mapping.items())
            size += sum(
                getsizeof(entry) + getsizeof(entry[0]) + getsizeof(entry[1])
                for entry in entries
            )
            size += sum(getsizeof(score) for score in scores)
            logger.debug(
                "Reloaded RAM cache index with %d keys (~%.1f KiB)",
                len(entries),
                size / 1024,
            )

    @_ram_locked
    def _ram_reset(
        self,
        mapping: Dict[str, Optional[datetime]],
        entries: List[Tuple[str, Optional[datetime]]],
        scores: List[float],
    ) -> None:
        """Replace RAM index containers; call only from mixin helpers."""
        if not self._cache_keys_in_ram:
            return
        self._ram_keys = mapping
        self._ram_entries = entries
        self._ram_scores = scores

    @_ram_locked
    def _ram_has_key(self, key: str, now: datetime) -> Optional[bool]:
        """Check RAM index for ``key``; call from cache ``get``/``is_set`` methods."""
        if not self._cache_keys_in_ram:
            return None
        stored = self._ram_keys.get(key, self._MISSING)
        if stored is self._MISSING:
            return False
        expire = cast(Optional[datetime], stored)
        if expire is not None and expire <= now:
            self._ram_remove_entry_unlocked(key)
            return False
        return True

    @_ram_locked
    def _ram_mark_miss(self, key: str) -> None:
        """Remove stale ``key`` after cache miss; call from cache ``get`` paths."""
        if not self._cache_keys_in_ram:
            return
        self._ram_remove_entry_unlocked(key)

    @_ram_locked
    def _ram_on_set(self, key: str, expire: Optional[datetime], now: datetime) -> None:
        """Update RAM structures after ``set``; invoked by cache ``set`` implementations."""
        if not self._cache_keys_in_ram:
            return
        self._ram_purge_expired_unlocked(now)
        self._ram_remove_entry_unlocked(key)
        if expire is None or expire > now:
            self._ram_insert_unlocked(key, expire)

    @_ram_locked
    def _ram_on_delete(self, key: str, now: datetime) -> None:
        """Update RAM structures after ``delete``; invoked by cache ``delete`` methods."""
        if not self._cache_keys_in_ram:
            return
        self._ram_purge_expired_unlocked(now)
        self._ram_remove_entry_unlocked(key)

    @_ram_locked
    def _ram_on_del_many(self, keys: Sequence[str], now: datetime) -> None:
        """Update RAM structures after bulk delete; call from cache ``del_many``."""
        if not self._cache_keys_in_ram:
            return
        self._ram_purge_expired_unlocked(now)
        for key in keys:
            self._ram_remove_entry_unlocked(key)

    @_ram_locked
    def _ram_on_clear(self) -> None:
        """Clear RAM index entirely; call from cache ``clear`` implementations."""
        if not self._cache_keys_in_ram:
            return
        self._ram_keys.clear()
        self._ram_entries.clear()
        self._ram_scores.clear()

    @_ram_locked
    def _ram_purge_expired(self, now: datetime) -> None:
        """Remove expired entries; call from cache maintenance hooks (e.g. cleanup)."""
        if not self._cache_keys_in_ram:
            return
        self._ram_purge_expired_unlocked(now)

    def _ram_insert_unlocked(self, key: str, expire: Optional[datetime]) -> None:
        """Insert entry without locking; caller must hold ``_ram_lock``."""
        if not self._cache_keys_in_ram:
            return
        score = self._expire_score(expire)
        neg_score = -score
        idx = bisect_left(self._ram_scores, neg_score)
        self._ram_scores.insert(idx, neg_score)
        self._ram_entries.insert(idx, (key, expire))
        self._ram_keys[key] = expire

    def _ram_remove_entry_unlocked(self, key: str) -> None:
        """Remove entry without locking; caller must hold ``_ram_lock``."""
        if not self._cache_keys_in_ram:
            return
        stored = self._ram_keys.pop(key, self._MISSING)
        if stored is self._MISSING:
            return
        expire = cast(Optional[datetime], stored)
        neg_score = -self._expire_score(expire)
        idx = bisect_left(self._ram_scores, neg_score)
        while idx < len(self._ram_scores) and self._ram_scores[idx] == neg_score:
            entry_key, _ = self._ram_entries[idx]
            if entry_key == key:
                del self._ram_entries[idx]
                del self._ram_scores[idx]
                break
            idx += 1

    def _ram_purge_expired_unlocked(self, now: datetime) -> None:
        """Delete expired entries without locking; caller must hold ``_ram_lock``."""
        if not self._cache_keys_in_ram:
            return
        if not self._ram_scores:
            return
        neg_now = -now.timestamp()
        cutoff = bisect_left(self._ram_scores, neg_now)
        if cutoff >= len(self._ram_entries):
            return
        for key, _ in self._ram_entries[cutoff:]:
            self._ram_keys.pop(key, None)
        del self._ram_entries[cutoff:]
        del self._ram_scores[cutoff:]
