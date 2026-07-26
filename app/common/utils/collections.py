"""Collection and iterable helpers.

Why this file exists
--------------------
A handful of shapes recur constantly in service code: grouping rows by a
foreign key, chunking a list into database-sized batches, keeping the first
occurrence of each key. Written inline they are three-line comprehensions each
reader has to decode; named, they state intent.

:func:`chunk` in particular exists for a correctness reason rather than
convenience — it is how a bulk operation avoids sending 50,000 bind parameters
in one statement and hitting PostgreSQL's limit.

Everything here is pure, synchronous and generic.
"""

from collections.abc import Callable, Hashable, Iterable, Iterator, Sequence
from typing import Any


def chunk[T](items: Sequence[T], size: int) -> Iterator[Sequence[T]]:
    """Split a sequence into consecutive chunks of at most ``size``.

    For bulk inserts, batched API calls, and anything that would otherwise send
    an unbounded number of bind parameters in a single statement.

    Args:
        items: The sequence to split.
        size: Maximum chunk length.

    Yields:
        Slices of ``items``. Nothing is yielded for an empty input.

    Raises:
        ValueError: When ``size`` is not positive — a zero or negative size
            would otherwise loop forever.
    """
    if size <= 0:
        raise ValueError("chunk size must be positive")
    for start in range(0, len(items), size):
        yield items[start : start + size]


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
    grouped: dict[K, list[T]] = {}
    for item in items:
        grouped.setdefault(key(item), []).append(item)
    return grouped


def index_by[T, K: Hashable](items: Iterable[T], key: Callable[[T], K]) -> dict[K, T]:
    """Build a lookup dict from items, keyed by ``key(item)``.

    Assumes keys are unique; later items overwrite earlier ones silently. Use
    :func:`group_by` when duplicates are possible and meaningful.
    """
    return {key(item): item for item in items}


def unique[T, K: Hashable](
    items: Iterable[T], key: Callable[[T], K] | None = None
) -> list[T]:
    """Remove duplicates while preserving order.

    Order preservation matters: ``list(set(...))`` is faster but returns an
    arbitrary order, which becomes flaky tests and unstable API responses.

    Args:
        items: The items to deduplicate.
        key: Optional identity function. Defaults to the item itself, which
            requires the items to be hashable.

    Returns:
        The first occurrence of each distinct key, in encounter order.
    """
    seen: set[Any] = set()
    result: list[T] = []
    for item in items:
        identity = key(item) if key else item
        if identity not in seen:
            seen.add(identity)
            result.append(item)
    return result


def partition[T](
    items: Iterable[T], predicate: Callable[[T], bool]
) -> tuple[list[T], list[T]]:
    """Split items into those matching a predicate and those that do not.

    One pass, and the predicate is evaluated exactly once per item — which
    matters when it is expensive or has side effects.

    Args:
        items: The items to split.
        predicate: Tested against each item.

    Returns:
        A ``(matching, non_matching)`` pair.
    """
    matching: list[T] = []
    non_matching: list[T] = []
    for item in items:
        (matching if predicate(item) else non_matching).append(item)
    return matching, non_matching


def flatten[T](nested: Iterable[Iterable[T]]) -> list[T]:
    """Concatenate one level of nesting into a single list."""
    return [item for inner in nested for item in inner]


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge ``override`` into ``base``, returning a new dict.

    Nested dicts merge key by key; every other type is replaced outright,
    **including lists**. Merging lists has no single obvious meaning (append?
    union? by index?), so replacement is the predictable choice.

    Neither input is modified.

    Args:
        base: The starting mapping.
        override: Values that win on conflict.

    Returns:
        A new merged mapping.
    """
    merged = dict(base)
    for key, value in override.items():
        existing = merged.get(key)
        if isinstance(existing, dict) and isinstance(value, dict):
            merged[key] = deep_merge(existing, value)
        else:
            merged[key] = value
    return merged


def compact[K: Hashable, V](mapping: dict[K, V | None]) -> dict[K, V]:
    """Drop ``None`` values from a mapping.

    For building PATCH payloads and partial updates. Note the caveat this
    implies: once compacted, "field absent" and "field explicitly set to null"
    are indistinguishable. Where that distinction matters, use Pydantic's
    ``exclude_unset`` instead of this function.

    Args:
        mapping: The mapping to compact.

    Returns:
        A new mapping without ``None`` values.
    """
    return {key: value for key, value in mapping.items() if value is not None}
