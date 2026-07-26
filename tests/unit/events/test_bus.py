"""Tests for domain events and the in-process bus.

The containment test is the important one: the whole point of an event is that
the publisher does not depend on its subscribers, and a subscriber that can
fail the publisher's request breaks exactly that guarantee.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import ClassVar

import pytest

from app.events.base import DomainEvent, IntegrationEvent
from app.events.bus import EventBus


class Tier(StrEnum):
    PRO = "pro"


@dataclass(frozen=True, slots=True, kw_only=True)
class InvoicePaid(DomainEvent):
    name: ClassVar[str] = "billing.invoice_paid"

    invoice_id: uuid.UUID
    amount: Decimal
    tier: Tier
    paid_at: datetime


@dataclass(frozen=True, slots=True, kw_only=True)
class UserRegistered(DomainEvent):
    name: ClassVar[str] = "identity.user_registered"

    email: str


@pytest.fixture
def bus() -> EventBus:
    """A fresh bus per test — subscriptions are otherwise global state."""
    return EventBus()


def _invoice_paid() -> InvoicePaid:
    return InvoicePaid(
        invoice_id=uuid.uuid7(),
        amount=Decimal("42.50"),
        tier=Tier.PRO,
        paid_at=datetime(2026, 1, 15, 10, 30, tzinfo=UTC),
    )


class TestDomainEvent:
    def test_envelope_carries_identity_and_timing(self) -> None:
        event = _invoice_paid()
        envelope = event.to_payload()

        assert envelope["name"] == "billing.invoice_paid"
        assert envelope["version"] == 1
        assert envelope["event_id"] == str(event.event_id)
        assert envelope["occurred_at"] == event.occurred_at.isoformat()

    def test_payload_is_json_serialisable(self) -> None:
        """An event may cross a process — or language — boundary."""
        import json

        envelope = _invoice_paid().to_payload()
        assert json.loads(json.dumps(envelope)) == envelope

    def test_payload_converts_domain_types_to_strings(self) -> None:
        payload = _invoice_paid().payload()
        assert isinstance(payload["invoice_id"], str)
        assert payload["tier"] == "pro"
        assert payload["paid_at"] == "2026-01-15T10:30:00+00:00"

    def test_payload_excludes_envelope_fields(self) -> None:
        payload = _invoice_paid().payload()
        assert "event_id" not in payload
        assert "occurred_at" not in payload

    def test_events_are_immutable(self) -> None:
        """A handler must not be able to change what later handlers see."""
        event = _invoice_paid()
        with pytest.raises((AttributeError, TypeError)):
            event.amount = Decimal("0")  # ty: ignore[invalid-assignment]

    def test_each_occurrence_gets_a_distinct_id(self) -> None:
        assert _invoice_paid().event_id != _invoice_paid().event_id

    def test_integration_event_marks_the_process_boundary(self) -> None:
        assert issubclass(IntegrationEvent, DomainEvent)


class TestSubscription:
    async def test_handler_receives_the_published_event(self, bus: EventBus) -> None:
        received: list[InvoicePaid] = []

        @bus.subscribe(InvoicePaid)
        async def handle(event: InvoicePaid) -> None:
            received.append(event)

        event = _invoice_paid()
        await bus.publish(event)
        assert received == [event]

    async def test_only_matching_handlers_run(self, bus: EventBus) -> None:
        invoice_calls: list[str] = []
        user_calls: list[str] = []

        @bus.subscribe(InvoicePaid)
        async def on_invoice(event: InvoicePaid) -> None:
            invoice_calls.append("invoice")

        @bus.subscribe(UserRegistered)
        async def on_user(event: UserRegistered) -> None:
            user_calls.append("user")

        await bus.publish(_invoice_paid())
        assert invoice_calls == ["invoice"]
        assert user_calls == []

    async def test_base_class_subscription_receives_subclasses(
        self, bus: EventBus
    ) -> None:
        """This is what lets one audit subscriber observe every event."""
        seen: list[str] = []

        @bus.subscribe(DomainEvent)
        async def audit(event: DomainEvent) -> None:
            seen.append(event.name)

        await bus.publish(_invoice_paid())
        await bus.publish(UserRegistered(email="a@b.c"))
        assert seen == ["billing.invoice_paid", "identity.user_registered"]

    async def test_multiple_handlers_all_run(self, bus: EventBus) -> None:
        calls: list[str] = []

        @bus.subscribe(InvoicePaid)
        async def first(event: InvoicePaid) -> None:
            calls.append("first")

        @bus.subscribe(InvoicePaid)
        async def second(event: InvoicePaid) -> None:
            calls.append("second")

        await bus.publish(_invoice_paid())
        assert sorted(calls) == ["first", "second"]

    def test_synchronous_handler_is_rejected(self, bus: EventBus) -> None:
        """A sync handler would block the loop for every publisher."""
        with pytest.raises(TypeError, match="must be async"):

            @bus.subscribe(InvoicePaid)
            def blocking(event: InvoicePaid) -> None:  # ty: ignore[invalid-argument-type]
                pass

    def test_register_is_equivalent_to_the_decorator(self, bus: EventBus) -> None:
        async def handle(event: InvoicePaid) -> None:
            pass

        bus.register(InvoicePaid, handle)
        assert bus.handler_count(InvoicePaid) == 1


class TestFailureContainment:
    async def test_a_failing_handler_does_not_reach_the_publisher(
        self, bus: EventBus
    ) -> None:
        """The publisher's work is already committed; a listener cannot undo it."""

        @bus.subscribe(InvoicePaid)
        async def broken(event: InvoicePaid) -> None:
            raise RuntimeError("analytics is down")

        await bus.publish(_invoice_paid())  # must not raise

    async def test_a_failing_handler_does_not_block_the_others(
        self, bus: EventBus
    ) -> None:
        survived: list[str] = []

        @bus.subscribe(InvoicePaid)
        async def broken(event: InvoicePaid) -> None:
            raise RuntimeError("boom")

        @bus.subscribe(InvoicePaid)
        async def healthy(event: InvoicePaid) -> None:
            survived.append("ran")

        await bus.publish(_invoice_paid())
        assert survived == ["ran"]

    async def test_failure_is_logged_with_context(
        self, bus: EventBus, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Swallowed is not the same as silent."""

        @bus.subscribe(InvoicePaid)
        async def broken(event: InvoicePaid) -> None:
            raise RuntimeError("boom")

        with caplog.at_level("ERROR"):
            await bus.publish(_invoice_paid())

        assert "Event handler failed" in caplog.text

    async def test_publishing_with_no_handlers_is_a_no_op(
        self, bus: EventBus
    ) -> None:
        await bus.publish(_invoice_paid())


class TestPublishMany:
    async def test_events_are_delivered_in_order(self, bus: EventBus) -> None:
        """A later event may depend on an earlier one having been handled."""
        order: list[str] = []

        @bus.subscribe(DomainEvent)
        async def record(event: DomainEvent) -> None:
            order.append(event.name)

        await bus.publish_many(
            [UserRegistered(email="a@b.c"), _invoice_paid()]
        )
        assert order == ["identity.user_registered", "billing.invoice_paid"]


class TestIsolation:
    def test_clear_removes_subscriptions(self, bus: EventBus) -> None:
        @bus.subscribe(InvoicePaid)
        async def handle(event: InvoicePaid) -> None:
            pass

        assert bus.handler_count(InvoicePaid) == 1
        bus.clear()
        assert bus.handler_count(InvoicePaid) == 0
