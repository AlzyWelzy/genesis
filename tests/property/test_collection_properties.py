"""Invariants of the collection helpers.

These functions are small enough to look obviously correct, which is exactly why
properties earn their keep here: ``deep_merge`` looked obviously correct and
returned a result aliasing its own inputs, while the example test named
``does_not_mutate_inputs`` passed because it asserted the other half of the
property.
"""

from typing import Any

from hypothesis import given
from hypothesis import strategies as st

from app.common.utils import collections as cols

ITEMS = st.lists(st.integers(), max_size=50)

#: JSON-shaped nested mappings — what `deep_merge` is actually used for.
NESTED = st.recursive(
    st.one_of(st.integers(), st.text(max_size=10), st.booleans(), st.none()),
    lambda children: st.one_of(
        st.lists(children, max_size=4),
        st.dictionaries(st.text(min_size=1, max_size=6), children, max_size=4),
    ),
    max_leaves=12,
)
MAPPINGS = st.dictionaries(st.text(min_size=1, max_size=6), NESTED, max_size=5)


def _shares_container(left: object, right: object) -> bool:
    """Whether two structures share any mutable container by identity.

    Identity, not equality: two structurally equal dicts are fine, whereas the
    same dict object reachable from both sides is the aliasing bug.
    """
    if isinstance(left, dict) and isinstance(right, dict):
        if left is right and left:
            return True
        left_map: dict[Any, Any] = left
        right_map: dict[Any, Any] = right
        return any(
            _shares_container(left_map[key], right_map[key])
            for key in left_map.keys() & right_map.keys()
        )
    if isinstance(left, list) and isinstance(right, list):
        if left is right and left:
            return True
        return any(_shares_container(a, b) for a, b in zip(left, right, strict=False))
    return False


class TestChunk:
    @given(ITEMS, st.integers(min_value=1, max_value=20))
    def test_chunking_loses_and_duplicates_nothing(
        self, items: list[int], size: int
    ) -> None:
        """A batch helper that drops a row corrupts an import silently."""
        flattened = [item for group in cols.chunk(items, size) for item in group]
        assert flattened == items

    @given(ITEMS, st.integers(min_value=1, max_value=20))
    def test_no_chunk_exceeds_the_requested_size(
        self, items: list[int], size: int
    ) -> None:
        """The caller chose the size to stay under a bind-parameter limit."""
        assert all(len(group) <= size for group in cols.chunk(items, size))

    @given(ITEMS, st.integers(min_value=1, max_value=20))
    def test_only_the_final_chunk_may_be_short(
        self, items: list[int], size: int
    ) -> None:
        """A short chunk in the middle means a batch was split incorrectly."""
        groups = list(cols.chunk(items, size))
        assert all(len(group) == size for group in groups[:-1])


class TestDeepMerge:
    @given(MAPPINGS, MAPPINGS)
    def test_neither_input_is_modified(self, base: dict, override: dict) -> None:
        import copy

        base_before, override_before = copy.deepcopy(base), copy.deepcopy(override)
        cols.deep_merge(base, override)

        assert base == base_before
        assert override == override_before

    @given(MAPPINGS, MAPPINGS)
    def test_the_result_shares_no_mutable_structure_with_its_inputs(
        self, base: dict, override: dict
    ) -> None:
        """The property the example test missed.

        "Does not mutate its inputs" and "the result is safe to mutate" are
        different claims, and a shallow copy satisfies only the first. Merging
        configuration is where a shared default is the base, so aliasing there
        rewrites the defaults for the whole process.
        """
        merged = cols.deep_merge(base, override)

        assert not _shares_container(merged, base)
        assert not _shares_container(merged, override)

    @given(MAPPINGS)
    def test_merging_with_nothing_returns_an_equal_mapping(self, base: dict) -> None:
        assert cols.deep_merge(base, {}) == base

    @given(MAPPINGS)
    def test_merging_a_mapping_with_itself_is_a_fixed_point(self, base: dict) -> None:
        """Every leaf conflicts, and the override side must win each one."""
        assert cols.deep_merge(base, base) == base


class TestUniqueAndGrouping:
    @given(ITEMS)
    def test_unique_preserves_first_occurrence_order(self, items: list[int]) -> None:
        """Order is what makes the result reproducible."""
        assert cols.unique(items) == list(dict.fromkeys(items))

    @given(ITEMS)
    def test_unique_contains_exactly_the_distinct_items(self, items: list[int]) -> None:
        assert set(cols.unique(items)) == set(items)

    @given(ITEMS)
    def test_group_by_partitions_without_loss(self, items: list[int]) -> None:
        grouped = cols.group_by(items, key=lambda value: value % 3)
        assert sum(len(group) for group in grouped.values()) == len(items)

    @given(ITEMS)
    def test_partition_splits_exhaustively(self, items: list[int]) -> None:
        matching, rest = cols.partition(items, lambda value: value > 0)

        assert len(matching) + len(rest) == len(items)
        assert all(value > 0 for value in matching)
        assert all(value <= 0 for value in rest)

    @given(st.lists(st.lists(st.integers(), max_size=5), max_size=5))
    def test_flatten_concatenates_in_order(self, nested: list[list[int]]) -> None:
        assert cols.flatten(nested) == [item for group in nested for item in group]
