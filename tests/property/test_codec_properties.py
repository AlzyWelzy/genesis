"""Round-trip and tamper-evidence invariants for every encoder in the codebase.

An encoder has two obligations that pull against each other: **everything it
produces must decode**, and **nothing it did not produce may decode**. Example
tests routinely establish the second and merely sample the first — which is
exactly how the pagination cursor shipped rejecting 7% of the cursors it had just
minted, with every hand-written case falling in the other 93%.
"""

import contextlib
import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import UUID

from hypothesis import assume, given
from hypothesis import strategies as st

from app.common.pagination import decode_cursor, encode_cursor
from app.core.exceptions import ValidationError
from app.events.base import DomainEvent

#: Values a cursor legitimately carries: sort keys are scalars.
CURSOR_VALUES = st.dictionaries(
    st.text(min_size=1, max_size=40),
    st.one_of(
        st.text(max_size=100),
        st.integers(),
        st.booleans(),
        st.none(),
        st.floats(allow_nan=False, allow_infinity=False),
    ),
    max_size=8,
)
SECRETS = st.text(min_size=1, max_size=64)


class TestCursorRoundTrip:
    @given(CURSOR_VALUES, SECRETS)
    def test_every_cursor_we_mint_decodes(self, values: dict, secret: str) -> None:
        """The obligation example tests keep missing.

        v1 appended the signature after a ``.`` and split with ``rpartition``,
        which finds the *last* dot — and a signature is raw HMAC bytes, so one in
        sixteen contained ``0x2e``. Roughly 7% of cursors were rejected as
        forged, at random, and no single example reproduced it reliably.
        """
        cursor = encode_cursor(values, secret=secret)
        assert decode_cursor(cursor, secret=secret) == values

    @given(CURSOR_VALUES, SECRETS)
    def test_a_cursor_is_url_safe(self, values: dict, secret: str) -> None:
        cursor = encode_cursor(values, secret=secret)
        assert not set(cursor) & {"+", "/", "=", "?", "&", "#"}

    @given(CURSOR_VALUES, SECRETS, SECRETS)
    def test_a_foreign_secret_never_verifies(
        self, values: dict, secret: str, other: str
    ) -> None:
        """Tamper-evidence: the other half of the obligation."""
        assume(secret != other)
        cursor = encode_cursor(values, secret=secret)

        try:
            decode_cursor(cursor, secret=other)
        except ValidationError:
            return
        raise AssertionError("a cursor verified under the wrong secret")

    @given(st.text(max_size=200), SECRETS)
    def test_arbitrary_input_is_rejected_not_crashed(
        self, junk: str, secret: str
    ) -> None:
        """A cursor is client-supplied, so every byte sequence must be handled."""
        with contextlib.suppress(ValidationError):
            decode_cursor(junk, secret=secret)

    @given(CURSOR_VALUES, SECRETS, st.integers(min_value=0, max_value=200))
    def test_truncation_is_rejected(self, values: dict, secret: str, cut: int) -> None:
        cursor = encode_cursor(values, secret=secret)
        assume(cut < len(cursor))

        with contextlib.suppress(ValidationError):
            decode_cursor(cursor[:cut], secret=secret)


#: Values a domain event legitimately carries.
EVENT_VALUES = st.recursive(
    st.one_of(
        st.text(max_size=50),
        st.integers(),
        st.booleans(),
        st.none(),
        st.floats(allow_nan=False, allow_infinity=False),
        st.uuids(),
        st.datetimes(timezones=st.just(UTC)),
        st.dates(),
        st.timedeltas(),
        st.binary(max_size=32),
        st.decimals(allow_nan=False, allow_infinity=False),
    ),
    lambda children: st.one_of(
        st.lists(children, max_size=5),
        st.dictionaries(st.text(min_size=1, max_size=20), children, max_size=5),
    ),
    max_leaves=15,
)


def _carrier(value: object) -> DomainEvent:
    """Build a one-field event carrying an arbitrary value."""
    from dataclasses import dataclass
    from typing import ClassVar

    @dataclass(frozen=True, slots=True, kw_only=True)
    class Carrier(DomainEvent):
        name: ClassVar[str] = "test.carrier"
        val: object

    return Carrier(val=value)


class TestEventPayload:
    @given(EVENT_VALUES)
    def test_every_payload_is_json_serialisable(self, value: object) -> None:
        """The guarantee ``to_payload`` makes in its own docstring.

        Breaking it is not a local failure: an unencodable value raises when
        asyncpg writes the JSONB outbox column, *inside* the business
        transaction, so the event breaks the write it was describing.
        """
        json.dumps(_carrier(value).to_payload())

    @given(EVENT_VALUES)
    def test_serialising_is_stable(self, value: object) -> None:
        """The same event must not produce two different payloads."""
        event = _carrier(value)
        assert event.to_payload() == event.to_payload()

    @given(st.datetimes(timezones=st.just(UTC)))
    def test_a_datetime_never_degrades_to_a_date(self, moment: datetime) -> None:
        """``datetime`` subclasses ``date``, so encoder order decides this.

        Matched the wrong way round, every timestamp silently loses its time
        component — a data loss that raises nothing.
        """
        encoded = _carrier(moment).to_payload()["payload"]["val"]

        assert encoded == moment.isoformat()
        assert "T" in encoded

    @given(st.decimals(allow_nan=False, allow_infinity=False))
    def test_a_decimal_never_becomes_a_float(self, amount: Decimal) -> None:
        """Money must round-trip to the cent; binary floats cannot."""
        encoded = _carrier(amount).to_payload()["payload"]["val"]

        assert isinstance(encoded, str)
        assert Decimal(encoded) == amount

    @given(st.uuids())
    def test_a_uuid_round_trips(self, value: UUID) -> None:
        assert UUID(_carrier(value).to_payload()["payload"]["val"]) == value

    @given(st.dates())
    def test_a_date_round_trips(self, value: date) -> None:
        encoded = _carrier(value).to_payload()["payload"]["val"]
        assert date.fromisoformat(encoded) == value

    @given(st.timedeltas())
    def test_a_timedelta_round_trips_exactly(self, value: timedelta) -> None:
        """Exactly, not approximately.

        ``total_seconds()`` returns a float, and a few hundred years of duration
        already exceeds a float's precision for microseconds — so a value came
        back a microsecond adrift, silently. The same trap as encoding money as
        a float, which this module already refuses.
        """
        encoded = _carrier(value).to_payload()["payload"]["val"]

        assert isinstance(encoded, str)
        seconds = Decimal(encoded)
        whole = int(seconds.to_integral_value(rounding="ROUND_FLOOR"))
        micros = int((seconds - whole) * 1_000_000)
        assert timedelta(seconds=whole, microseconds=micros) == value
