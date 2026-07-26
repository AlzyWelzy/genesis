"""Date and time helpers.

Why this file exists
--------------------
``datetime.now()`` returns a *naive* datetime in the machine's local timezone.
Stored in one region and read in another, it silently shifts by hours; compared
against an aware datetime, it raises. The single most valuable rule in a
distributed application is that every timestamp is UTC and aware, and this
module is how that rule is enforced — nothing else calls ``datetime.now()``.

Using :func:`utc_now` everywhere also makes time mockable: a test patches one
function instead of hunting every call site.
"""

from datetime import UTC, date, datetime, timedelta


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

    Raises:
        ValueError: When the string is not valid ISO 8601.
    """
    return ensure_utc(datetime.fromisoformat(value))


def start_of_day(value: date | datetime) -> datetime:
    """Return midnight UTC at the start of ``value``'s day.

    Note that "day" is timezone-dependent: for user-facing reports the caller
    must convert to the user's timezone *before* bucketing, or a user in UTC+13
    will see their evening activity attributed to tomorrow.
    """
    raise NotImplementedError


def end_of_day(value: date | datetime) -> datetime:
    """Return the exclusive upper bound of ``value``'s day (next midnight UTC).

    Exclusive on purpose: ``23:59:59`` as an inclusive bound drops events in the
    final second, and microsecond precision makes that a real loss.
    """
    raise NotImplementedError


def add_business_days(value: date, days: int) -> date:
    """Advance a date by ``days`` working days, skipping weekends.

    Holidays are not considered — they are jurisdiction-specific and belong in
    a feature that knows which calendar applies.
    """
    raise NotImplementedError


def humanize_timedelta(delta: timedelta) -> str:
    """Render a duration as a short human string ("3h 12m").

    For logs, admin tooling and internal dashboards. User-facing text must be
    localised, which is a presentation concern, not a utility one.
    """
    raise NotImplementedError
