"""Date and time helpers.

Why this file exists
--------------------
``datetime.now()`` returns a *naive* datetime in the machine's local timezone.
Stored in one region and read in another it silently shifts by hours; compared
against an aware datetime it raises outright. The single most valuable rule in
a distributed application is that every timestamp is UTC and aware, and this
module is how that rule is enforced — nothing else calls ``datetime.now()``.

Routing every clock read through :func:`utc_now` also makes time mockable: a
test patches one function instead of hunting every call site.
"""

from datetime import UTC, date, datetime, timedelta

#: Days that are not working days. Monday is 0, Sunday is 6.
_WEEKEND = (5, 6)


def utc_now() -> datetime:
    """Return the current time as a timezone-aware UTC datetime.

    The only sanctioned way to read the clock. ``datetime.utcnow()`` is
    deprecated and returns a naive value; ``datetime.now()`` is local time.
    """
    return datetime.now(UTC)


def ensure_utc(value: datetime) -> datetime:
    """Return ``value`` as an aware UTC datetime.

    Naive inputs are *assumed* to be UTC rather than rejected, because data
    arriving from legacy tables and third-party payloads is routinely naive.
    Aware inputs are converted.

    Args:
        value: A naive or aware datetime.

    Returns:
        The equivalent aware UTC datetime.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def to_iso(value: datetime) -> str:
    """Serialise a datetime to an ISO 8601 string in UTC."""
    return ensure_utc(value).isoformat()


def from_iso(value: str) -> datetime:
    """Parse an ISO 8601 string into an aware UTC datetime.

    Accepts a trailing ``Z``, which ``fromisoformat`` handles from 3.11 onward
    but which older serialisers still emit in forms worth normalising.

    Args:
        value: An ISO 8601 timestamp.

    Returns:
        The aware UTC datetime.

    Raises:
        ValueError: When the string is not valid ISO 8601.
    """
    return ensure_utc(datetime.fromisoformat(value))


def start_of_day(value: date | datetime) -> datetime:
    """Return midnight UTC at the start of ``value``'s day.

    Note that "day" is timezone-dependent: for user-facing reports the caller
    must convert to the user's timezone *before* bucketing, or a user in UTC+13
    sees their evening activity attributed to tomorrow.

    Args:
        value: A date, or a datetime whose date part is used.

    Returns:
        Midnight UTC on that date.
    """
    day = value.date() if isinstance(value, datetime) else value
    return datetime(day.year, day.month, day.day, tzinfo=UTC)


def end_of_day(value: date | datetime) -> datetime:
    """Return the exclusive upper bound of ``value``'s day (next midnight UTC).

    Exclusive on purpose. An inclusive ``23:59:59`` bound drops every event in
    the final second, and at microsecond precision that is a real loss — it is
    why daily totals fail to sum to the monthly one.

    **Compare with ``<``, never ``<=``, and never SQL ``BETWEEN``.** The returned
    instant belongs to the *next* day, so::

        WHERE ts BETWEEN start_of_day(d) AND end_of_day(d)   -- wrong

    counts a row landing exactly on midnight in both days' totals, because SQL
    ``BETWEEN`` is inclusive at both ends. The correct form is half-open::

        WHERE ts >= start_of_day(d) AND ts < end_of_day(d)   -- right

    which is also what makes consecutive days tile the timeline exactly once.

    Args:
        value: A date, or a datetime whose date part is used.

    Returns:
        Midnight UTC on the following day.
    """
    return start_of_day(value) + timedelta(days=1)


def add_business_days(value: date, days: int) -> date:
    """Advance a date by ``days`` working days, skipping weekends.

    Holidays are not considered — they are jurisdiction-specific and belong in
    a feature that knows which calendar applies.

    Args:
        value: The starting date.
        days: Working days to add. Negative counts backwards; zero returns the
            input unchanged even if it falls on a weekend.

    Returns:
        The resulting date.
    """
    if days == 0:
        return value

    step = timedelta(days=1 if days > 0 else -1)
    remaining = abs(days)
    current = value
    while remaining:
        current += step
        if current.weekday() not in _WEEKEND:
            remaining -= 1
    return current


def humanize_timedelta(delta: timedelta) -> str:
    """Render a duration as a short human string, e.g. ``"3h 12m"``.

    For logs, admin tooling and internal dashboards. User-facing text must be
    localised, which is a presentation concern rather than a utility one.

    Args:
        delta: The duration. Negative durations are rendered with a leading
            minus sign.

    Returns:
        The formatted duration, at most two significant units.
    """
    total = int(delta.total_seconds())
    if total == 0:
        return "0s"

    sign = "-" if total < 0 else ""
    total = abs(total)

    days, remainder = divmod(total, 86_400)
    hours, remainder = divmod(remainder, 3_600)
    minutes, seconds = divmod(remainder, 60)

    parts = [
        (days, "d"),
        (hours, "h"),
        (minutes, "m"),
        (seconds, "s"),
    ]
    # Two units is the readability sweet spot: "3h 12m" is useful,
    # "3h 12m 7s 0ms" is noise.
    significant = [f"{value}{unit}" for value, unit in parts if value][:2]
    return sign + " ".join(significant)
