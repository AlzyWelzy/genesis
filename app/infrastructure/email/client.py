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

from dataclasses import dataclass, field
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


# TODO: add idempotency keys so a queue retry cannot double-send.
# TODO: add a suppression-list check (bounces, unsubscribes) before delivery —
# repeatedly mailing a hard-bounced address damages domain reputation.
