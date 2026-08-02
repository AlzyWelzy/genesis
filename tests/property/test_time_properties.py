"""Invariants of the datetime helpers, over generated instants.

Time is the classic source of bugs that reproduce twice a year. The generators
here span daylight-saving transitions, leap days and the far past and future,
because those are the dates nobody writes into an example.
"""

from datetime import UTC, date, datetime, timedelta

from hypothesis import assume, given
from hypothesis import strategies as st

from app.common.utils import datetime as dt_utils

AWARE = st.datetimes(timezones=st.just(UTC))
NAIVE = st.datetimes()
DATES = st.dates()

#: Bounded, for the business-day helper: its loop walks one day at a time, so
#: year 1 and year 9999 make the property slow without making it stronger.
ORDINARY_DATES = st.dates(min_value=date(1900, 1, 1), max_value=date(2200, 1, 1))


class TestEnsureUtc:
    @given(AWARE)
    def test_an_aware_utc_datetime_is_unchanged(self, moment: datetime) -> None:
        assert dt_utils.ensure_utc(moment) == moment

    @given(NAIVE)
    def test_a_naive_datetime_gains_utc(self, moment: datetime) -> None:
        """A naive timestamp is the single most common date bug there is."""
        result = dt_utils.ensure_utc(moment)

        assert result.tzinfo is not None
        assert result.utcoffset() == timedelta(0)

    @given(NAIVE)
    def test_it_is_idempotent(self, moment: datetime) -> None:
        once = dt_utils.ensure_utc(moment)
        assert dt_utils.ensure_utc(once) == once


class TestIsoRoundTrip:
    @given(AWARE)
    def test_every_instant_round_trips(self, moment: datetime) -> None:
        """A timestamp crosses a process boundary as text and must survive it."""
        assert dt_utils.from_iso(dt_utils.to_iso(moment)) == moment

    @given(AWARE)
    def test_the_encoded_form_is_always_utc(self, moment: datetime) -> None:
        """Mixed offsets on the wire are how comparisons go wrong downstream."""
        assert dt_utils.to_iso(moment).endswith(("+00:00", "Z"))


class TestDayBoundaries:
    @given(DATES)
    def test_the_start_of_a_day_precedes_its_end(self, value: date) -> None:
        assume(value < date.max)
        assert dt_utils.start_of_day(value) < dt_utils.end_of_day(value)

    @given(DATES)
    def test_both_boundaries_are_aware(self, value: date) -> None:
        assume(value < date.max)
        assert dt_utils.start_of_day(value).tzinfo is not None
        assert dt_utils.end_of_day(value).tzinfo is not None

    @given(DATES)
    def test_the_start_falls_on_the_requested_day(self, value: date) -> None:
        """Off by one here silently shifts every daily report."""
        assert dt_utils.start_of_day(value).date() == value

    @given(DATES)
    def test_the_day_range_is_half_open(self, value: date) -> None:
        """Adjacent ranges must tile the timeline exactly once.

        Half-open is what makes that true: the end of one day *is* the start of
        the next, so every instant belongs to exactly one day. It is also why
        callers must compare with ``<`` and never SQL ``BETWEEN`` — see the
        warning on ``end_of_day``.
        """
        assume(value < date.max)
        tomorrow = value + timedelta(days=1)

        assert dt_utils.end_of_day(value) == dt_utils.start_of_day(tomorrow)
        assert dt_utils.start_of_day(value) < dt_utils.end_of_day(value)

    @given(AWARE)
    def test_a_datetime_is_accepted_as_well_as_a_date(self, moment: datetime) -> None:
        assert dt_utils.start_of_day(moment).date() == moment.date()


class TestBusinessDays:
    @given(ORDINARY_DATES, st.integers(min_value=1, max_value=60))
    def test_the_result_is_never_a_weekend(self, start: date, days: int) -> None:
        """The entire point of the function.

        ``days=0`` is excluded deliberately: the docstring states it returns the
        input unchanged even on a weekend, so it is the one input for which this
        property does not apply.
        """
        assert dt_utils.add_business_days(start, days).weekday() < 5

    @given(ORDINARY_DATES)
    def test_zero_days_returns_the_input_untouched(self, start: date) -> None:
        """The documented exception, pinned so it cannot drift into a surprise."""
        assert dt_utils.add_business_days(start, 0) == start

    @given(ORDINARY_DATES, st.integers(min_value=1, max_value=30))
    def test_adding_days_moves_forward(self, start: date, days: int) -> None:
        assert dt_utils.add_business_days(start, days) > start

    @given(ORDINARY_DATES, st.integers(min_value=1, max_value=30))
    def test_subtracting_days_moves_backward(self, start: date, days: int) -> None:
        assert dt_utils.add_business_days(start, -days) < start

    @given(ORDINARY_DATES, st.integers(min_value=1, max_value=20))
    def test_the_gap_is_at_least_the_days_requested(
        self, start: date, days: int
    ) -> None:
        """Skipping weekends means the calendar gap is never *shorter*."""
        result = dt_utils.add_business_days(start, days)

        assert (result - start).days >= days

    @given(ORDINARY_DATES, st.integers(min_value=1, max_value=10))
    def test_adding_is_monotonic(self, start: date, days: int) -> None:
        """More business days must never land earlier."""
        assert dt_utils.add_business_days(start, days) <= dt_utils.add_business_days(
            start, days + 1
        )


class TestHumanize:
    @given(st.timedeltas())
    def test_it_never_raises(self, delta: timedelta) -> None:
        """Rendered into user-facing text, so every duration must be handled."""
        dt_utils.humanize_timedelta(delta)

    @given(st.timedeltas())
    def test_it_always_returns_something(self, delta: timedelta) -> None:
        """An empty string in a sentence reads as a rendering bug to a user."""
        assert dt_utils.humanize_timedelta(delta).strip() != ""
