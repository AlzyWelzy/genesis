"""Collection and iterable helpers.

Why this file exists
--------------------
A handful of shapes recur constantly in service code: grouping rows by a
foreign key, chunking a list into database-sized batches, keeping the first
occurrence of each key. Written inline they are three-line comprehensions that
each reader has to decode; named, they state intent.

:func:`chunk` in particular exists for a correctness reason, not convenience —
it is how a bulk operation avoids sending 50,000 parameters in one statement
and blowing past PostgreSQL's parameter limit.

Everything here is pure, synchronous and generic.
"""

from collections.abc import Callable, Hashable, Iterable, Iterator, Sequence
from typing import Any


def chunk[T](items: Sequence[T], size: int) -> Iterator[Sequence[T]]:
    """Split a sequence into consecutive chunks of at most ``size``.

    Bulk inserts, batched API calls and anything that would otherwise send an
    unbounded number of bind parameters in a single statement.

    Args:
        items: The sequence to split.
        size: Maximum chunk length. Must be positive.

    Yields:
        Slices of ``items``.

    Raises:
        ValueError: When ``size`` is not positive.
    """
    raise NotImplementedError


def group_by[T, K: Hashable](
    items: Iterable[T], key: Callable[[T], K]
) -> dict[K, list[T]]:
    """Group items into a dict of lists keyed by ``key(item)``.

    The standard fix for an N+1 query: fetch the children in one statement,
    group them by parent ID in memory, then attach.

    Args:
        items: The items to group.
        key: Extracts the grouping key from an item.

    Returns:
        A mapping of key to the items sharing it, in encounter order.
    """
    raise NotImplementedError


def index_by[T, K: Hashable](items: Iterable[T], key: Callable[[T], K]) -> dict[K, T]:
    """Build a lookup dict from items, keyed by ``key(item)``.

    Assumes keys are unique; later items overwrite earlier ones silently. Use
    :func:`group_by` when duplicates are possible and meaningful.
    """
    raise NotImplementedError


def unique[T, K: Hashable](
    items: Iterable[T], key: Callable[[T], K] | None = None
) -> list[T]:
    """Remove duplicates while preserving order.

    Order preservation matters: ``list(set(...))`` is faster but returns an
    arbitrary order, which turns into flaky tests and unstable API responses.
    """
    raise NotImplementedError


def partition[T](
    items: Iterable[T], predicate: Callable[[T], bool]
) -> tuple[list[T], list[T]]:
    """Split items into those matching a predicate and those that do not.

    Returns:
        A ``(matching, non_matching)`` pair.
    """
    raise NotImplementedError


def flatten[T](nested: Iterable[Iterable[T]]) -> list[T]:
    """Concatenate one level of nesting into a single list."""
    raise NotImplementedError


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge ``override`` into ``base``, returning a new dict.

    Nested dicts merge key by key; every other type is replaced outright,
    including lists — merging lists has no single obvious meaning (append?
    union? by index?), so replacement is the predictable choice.

    Neither input is modified.
    """
    raise NotImplementedError


def compact[K: Hashable, V](mapping: dict[K, V | None]) -> dict[K, V]:
    """Drop ``None`` values from a mapping.

    For building PATCH payloads and partial updates, where "field absent" and
    "field explicitly set to null" must stay distinguishable.
    """
    raise NotImplementedError
