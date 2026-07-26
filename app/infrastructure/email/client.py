"""Email contract and message model.

Why this file exists
--------------------
Sending mail is a side effect on a third-party system that can be slow, can
fail and must be mockable. Defining an explicit message model plus a provider
protocol means a service can express "send this template to this address" as a
pure value, and whether it goes to SMTP, a transactional API, or a list in a
test is decided elsewhere.

Never send email inline in a request handler. A provider outage would turn into
request timeouts; enqueue the send instead (see :mod:`app.infrastructure.queue`).
"""

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class EmailAttachment:
    """A file attached to an outgoing message."""

    filename: str
    content: bytes
    content_type: str = "application/octet-stream"


@dataclass(frozen=True, slots=True)
class EmailMessage:
    """A renderable, transport-agnostic outgoing message.

    Carries a template name and its context rather than a rendered body, so
    rendering stays the provider layer's concern and the message itself remains
    trivially serialisable — which is what allows it to be queued.

    Attributes:
        to: Recipient addresses.
        subject: Message subject line.
        template: Template name resolved against the configured template dir.
        context: Values interpolated into the template.
    """

    to: list[str]
    subject: str
    template: str
    context: dict[str, Any] = field(default_factory=dict)
    cc: list[str] = field(default_factory=list)
    bcc: list[str] = field(default_factory=list)
    attachments: list[EmailAttachment] = field(default_factory=list)
    reply_to: str | None = None

    #: Overrides the derived key. Set when two logically distinct messages
    #: would otherwise hash identically — the same notification sent twice on
    #: purpose, for instance.
    idempotency_key: str | None = None


class EmailProvider(Protocol):
    """Transport contract every email backend must satisfy."""

    async def send(self, message: EmailMessage) -> None:
        """Deliver a single message.

        Implementations should raise on permanent failures so the caller (or
        the queue's retry policy) can react; transient failures are worth
        retrying with backoff rather than surfacing.
        """
        ...

    async def send_batch(self, messages: list[EmailMessage]) -> None:
        """Deliver several messages, reusing one connection where possible."""
        ...


class SuppressionReason(StrEnum):
    """Why an address must not be mailed.

    Recorded so the distinction survives: a hard bounce is a permanent fact
    about the address, whereas a complaint is a decision by the recipient. They
    have different review processes and different legal weight.
    """

    #: The address does not exist. Permanent; never retry.
    HARD_BOUNCE = "hard_bounce"

    #: The recipient marked a message as spam. Continuing to send is the single
    #: fastest way to have a sending domain blocklisted.
    COMPLAINT = "complaint"

    #: The recipient opted out. In several jurisdictions, honouring this is a
    #: legal obligation rather than a courtesy.
    UNSUBSCRIBED = "unsubscribed"

    #: Manually blocked by an operator.
    MANUAL = "manual"


class SuppressionList(Protocol):
    """Addresses that must not receive mail.

    Why this is not optional
    ------------------------
    Mailbox providers score a sending domain on bounce and complaint rates.
    Repeatedly mailing an address that hard-bounced, or someone who marked a
    message as spam, pushes that score down until *all* mail from the domain —
    including password resets to willing recipients — lands in spam or is
    rejected outright. Recovering a burned sending reputation takes weeks.

    So the check runs before delivery, on every message, and a suppressed
    recipient is dropped rather than attempted.
    """

    async def is_suppressed(self, address: str) -> bool:
        """Whether this address must not be mailed."""
        ...

    async def suppress(self, address: str, reason: SuppressionReason) -> None:
        """Add an address, recording why."""
        ...

    async def release(self, address: str) -> None:
        """Remove an address.

        For an operator correcting a mistake, or a re-subscription. Never call
        it automatically in response to a send failure — that would defeat the
        entire mechanism.
        """
        ...


class InMemorySuppressionList:
    """Dict-backed suppression list for tests and local development.

    Not shared between processes. Production needs a database-backed
    implementation so a bounce recorded by a webhook is visible to every sender.
    """

    def __init__(self) -> None:
        self._entries: dict[str, SuppressionReason] = {}

    async def is_suppressed(self, address: str) -> bool:
        """Whether the address is listed."""
        return address.lower() in self._entries

    async def suppress(self, address: str, reason: SuppressionReason) -> None:
        """Add an address."""
        self._entries[address.lower()] = reason

    async def release(self, address: str) -> None:
        """Remove an address."""
        self._entries.pop(address.lower(), None)

    def reason_for(self, address: str) -> SuppressionReason | None:
        """Why an address is suppressed, for assertions and support tooling."""
        return self._entries.get(address.lower())


#: Process-wide suppression list. Replaced at startup and in tests.
_suppression_list: SuppressionList = InMemorySuppressionList()


def set_suppression_list(implementation: SuppressionList) -> None:
    """Install the process-wide suppression list."""
    global _suppression_list  # noqa: PLW0603 - process-wide singleton by design
    _suppression_list = implementation


def get_suppression_list() -> SuppressionList:
    """Return the active suppression list."""
    return _suppression_list


async def filter_suppressed(recipients: Sequence[str]) -> tuple[list[str], list[str]]:
    """Split recipients into those that may be mailed and those that may not.

    Returns:
        An ``(allowed, suppressed)`` pair. A message whose ``allowed`` list is
        empty must not be sent at all — handing an empty recipient list to a
        provider is an error in most transports and a silent no-op in the rest.
    """
    suppression = get_suppression_list()
    allowed: list[str] = []
    suppressed: list[str] = []
    for address in recipients:
        if await suppression.is_suppressed(address):
            suppressed.append(address)
        else:
            allowed.append(address)
    return allowed, suppressed


def message_idempotency_key(message: EmailMessage) -> str:
    """Derive a stable key identifying a logically-identical message.

    A queued send is retried at least once under any realistic failure, and a
    user who receives the same password-reset mail four times reasonably
    concludes the system is broken. An explicit
    :attr:`EmailMessage.idempotency_key` always wins; otherwise the key is
    derived from recipients, subject, template and context — the things that
    make two messages *the same message*.

    Deriving rather than requiring means the protection is on by default. Set
    the key explicitly whenever a genuinely distinct message could hash the
    same, such as two identical notifications minutes apart.
    """
    if message.idempotency_key:
        return message.idempotency_key

    payload = json.dumps(
        {
            "to": sorted(message.to),
            "subject": message.subject,
            "template": message.template,
            "context": message.context,
        },
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(payload.encode()).hexdigest()
