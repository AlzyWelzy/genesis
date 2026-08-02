"""Concrete email providers and their factory.

Why this file exists
--------------------
The same reasoning as the storage providers: features depend on the contract in
:mod:`app.infrastructure.email.client`, and exactly one place decides which
transport the running process uses. That keeps "print to stdout in development,
SMTP in staging, an API in production" a configuration change rather than a
code change.

The console provider is the **default** deliberately. It is a safety feature: a
misconfigured development box cannot mail real customers.
"""

from dataclasses import replace
from email.message import EmailMessage as MIMEMessage
from functools import lru_cache

import aiosmtplib
from jinja2 import Environment, FileSystemLoader, StrictUndefined, TemplateNotFound

from app.core.config import settings
from app.core.exceptions import ExternalServiceError
from app.core.logging import get_logger
from app.infrastructure.email.client import (
    EmailMessage,
    filter_suppressed,
    message_idempotency_key,
)
from app.infrastructure.redis.client import build_key, get_redis

logger = get_logger(__name__)

#: How long a send is remembered for deduplication. Long enough to cover any
#: realistic retry chain, short enough that a genuinely repeated notification
#: a day later still goes out.
_IDEMPOTENCY_TTL_SECONDS = 24 * 60 * 60


async def prepare_recipients(message: EmailMessage) -> EmailMessage | None:
    """Drop suppressed recipients, returning ``None`` if none remain.

    Runs before every delivery. See
    :class:`~app.infrastructure.email.client.SuppressionList` for why this is
    not optional: mailing hard-bounced addresses degrades the sending domain's
    reputation until legitimate mail stops arriving.

    Returns:
        The message with suppressed recipients removed, or ``None`` when there
        is no one left to send to.
    """
    allowed, suppressed = await filter_suppressed(message.to)
    if suppressed:
        logger.info(
            "Skipping suppressed recipients",
            extra={"suppressed_count": len(suppressed), "template": message.template},
        )
    if not allowed:
        logger.info(
            "Message dropped: every recipient is suppressed",
            extra={"template": message.template},
        )
        return None

    allowed_cc, _ = await filter_suppressed(message.cc)
    allowed_bcc, _ = await filter_suppressed(message.bcc)
    return replace(message, to=allowed, cc=allowed_cc, bcc=allowed_bcc)


async def claim_send(message: EmailMessage) -> bool:
    """Claim the right to send a message exactly once.

    A queued send is retried at least once under any realistic failure. Without
    this, a transient SMTP error means the user receives the same password
    reset several times and reasonably concludes the system is broken.

    ``SET NX`` is atomic, so two workers processing a duplicate job cannot both
    win the claim.

    **Fails open.** If Redis is unavailable the send proceeds: a duplicate
    email is a far smaller harm than a password reset that never arrives.

    Returns:
        ``True`` when this caller should send.
    """
    key = build_key("email:sent", message_idempotency_key(message))
    try:
        claimed = await get_redis().set(key, "1", nx=True, ex=_IDEMPOTENCY_TTL_SECONDS)
    except Exception:  # noqa: BLE001 - delivery matters more than deduplication
        logger.warning("Email idempotency unavailable; sending without the guard")
        return True

    if not claimed:
        logger.info("Duplicate email suppressed", extra={"template": message.template})
    return bool(claimed)


async def release_send_claim(message: EmailMessage) -> None:
    """Give back a claim whose send did not happen.

    Without this the guard inverts into the failure it exists to prevent. The
    claim is taken *before* the transport runs, so a send that then fails leaves
    the key in place for the whole idempotency window — and the queue's retry,
    the entire reason the guard is needed, is suppressed. A transient SMTP error
    stops being "the user gets a duplicate" and becomes "the password reset
    never arrives", silently, for hours.

    Best effort. A failed release costs a suppressed retry, which is exactly
    where things stood before, so it is logged rather than raised — the caller
    is already unwinding a more important error.
    """
    key = build_key("email:sent", message_idempotency_key(message))
    try:
        await get_redis().delete(key)
    except Exception:  # noqa: BLE001 - already unwinding a more important error
        logger.warning(
            "Could not release the email idempotency claim; the retry will be "
            "suppressed until it expires",
            extra={"template": message.template},
        )


@lru_cache(maxsize=1)
def _environment() -> Environment:
    """Build and cache the Jinja environment.

    ``StrictUndefined`` turns a missing context variable into an error instead
    of an empty string. Silently rendering "Hello ," to a customer is worse
    than failing the send and alerting someone.

    ``autoescape`` is on for HTML: every value in an email context is
    ultimately user data, and an unescaped name is a mail-client XSS vector.
    """
    return Environment(
        loader=FileSystemLoader(settings.email.template_dir),
        autoescape=True,
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )


def render_template(template: str, context: dict[str, object]) -> tuple[str, str]:
    """Render a template into its HTML and plain-text bodies.

    Every message needs both parts. Clients that block HTML fall back to text,
    text-only clients otherwise receive an empty message, and a message with no
    text alternative scores worse with spam filters.

    Args:
        template: Template name, resolved under ``settings.email.template_dir``.
            Given ``"welcome"``, renders ``welcome.html`` and ``welcome.txt``.
        context: Values interpolated into the template.

    Returns:
        An ``(html_body, text_body)`` pair.

    Raises:
        ExternalServiceError: When either part is missing. Both are required,
            so a half-present template is a deployment error worth failing on.
    """
    environment = _environment()
    try:
        html = environment.get_template(f"{template}.html").render(**context)
        text = environment.get_template(f"{template}.txt").render(**context)
    except TemplateNotFound as exc:
        raise ExternalServiceError(
            f"Email template '{template}' is missing its .html or .txt part."
        ) from exc
    return html, text


def build_mime(message: EmailMessage, html: str, text: str) -> MIMEMessage:
    """Assemble a multipart/alternative MIME message.

    Text is added first and HTML second: ``multipart/alternative`` semantics
    are "last part the client can render wins", so reversing them makes every
    HTML-capable client show the plain-text version.

    Args:
        message: The message metadata.
        html: Rendered HTML body.
        text: Rendered plain-text body.

    Returns:
        The MIME message, ready to hand to a transport.
    """
    mime = MIMEMessage()
    mime["Subject"] = message.subject
    mime["From"] = f"{settings.email.from_name} <{settings.email.from_address}>"
    mime["To"] = ", ".join(message.to)
    if message.cc:
        mime["Cc"] = ", ".join(message.cc)
    if reply_to := message.reply_to or settings.email.reply_to:
        mime["Reply-To"] = reply_to

    mime.set_content(text)
    mime.add_alternative(html, subtype="html")

    for attachment in message.attachments:
        maintype, _, subtype = attachment.content_type.partition("/")
        mime.add_attachment(
            attachment.content,
            maintype=maintype or "application",
            subtype=subtype or "octet-stream",
            filename=attachment.filename,
        )
    return mime


class ConsoleEmailProvider:
    """Logs messages instead of sending them.

    The default in local and test environments, so a misconfigured development
    box cannot mail real customers.

    Renders the template even though nothing is sent — a template that fails to
    render should fail in development, not first in staging.
    """

    async def send(self, message: EmailMessage) -> None:
        """Render the message and log its envelope.

        Applies suppression and idempotency exactly as a real transport does,
        so development exercises the same code path production will.
        """
        prepared = await prepare_recipients(message)
        if prepared is None or not await claim_send(prepared):
            return

        message = prepared
        html, text = render_template(message.template, message.context)
        logger.info(
            "Email (console provider, not sent)",
            extra={
                "email_to": message.to,
                "email_subject": message.subject,
                "email_template": message.template,
                "email_text_preview": text[:200],
                "email_html_bytes": len(html),
            },
        )

    async def send_batch(self, messages: list[EmailMessage]) -> None:
        """Log each message in turn."""
        for message in messages:
            await self.send(message)


class SMTPEmailProvider:
    """SMTP transport, using ``aiosmtplib``.

    ``smtplib`` is blocking and would stall the event loop for the duration of
    the SMTP conversation — which includes DNS, TLS negotiation and the remote
    server's own latency.
    """

    def __init__(self) -> None:
        if not settings.email.smtp_host:
            raise ValueError(
                "EMAIL__SMTP_HOST is required when using the smtp provider"
            )

    async def send(self, message: EmailMessage) -> None:
        """Render the template and deliver over SMTP."""
        await self.send_batch([message])

    async def send_batch(self, messages: list[EmailMessage]) -> None:
        """Deliver several messages over a single connection.

        One connection for the batch: SMTP setup is a TCP handshake plus TLS
        plus authentication, which dwarfs the cost of the messages themselves.
        """
        if not messages:
            return

        password = (
            settings.email.smtp_password.get_secret_value()
            if settings.email.smtp_password
            else None
        )
        client = aiosmtplib.SMTP(
            hostname=settings.email.smtp_host,
            port=settings.email.smtp_port,
            start_tls=settings.email.smtp_use_tls,
        )
        try:
            await client.connect()
            if settings.email.smtp_username and password:
                await client.login(settings.email.smtp_username, password)

            for original in messages:
                prepared = await prepare_recipients(original)
                if prepared is None or not await claim_send(prepared):
                    continue

                try:
                    html, text = render_template(prepared.template, prepared.context)
                    mime = build_mime(prepared, html, text)
                    await client.send_message(
                        mime,
                        recipients=[*prepared.to, *prepared.cc, *prepared.bcc],
                    )
                except Exception:
                    # This message did not go out, so it must not stay claimed.
                    # Holding the claim through a failure suppresses the very
                    # retry that is supposed to recover it.
                    await release_send_claim(prepared)
                    raise

                logger.info(
                    "Email sent",
                    extra={
                        "email_to": prepared.to,
                        "email_template": prepared.template,
                    },
                )
        except Exception as exc:
            # Raised so the queue's retry policy can act. A send that fails
            # silently is indistinguishable from one that succeeded.
            raise ExternalServiceError("Email delivery failed") from exc
        finally:
            await client.quit()


class CollectingEmailProvider:
    """Captures messages in a list instead of sending them.

    For tests. Lets an assertion check that a message *would* have been sent,
    and to whom, with no transport and no rendering side effects to mock.

    **Applies suppression and idempotency, exactly as a real transport does.**
    Those two are not transport concerns — they decide whether a message is sent
    at all — so a fake that skipped them would let a test assert that mail went
    to an address production refuses, or that a retried job sent once when it
    would have sent twice.

    This fake is installed by an autouse fixture, so it is what nearly every
    test in the suite actually exercises. It previously appended
    unconditionally, which made both guarantees untestable rather than merely
    untested: the assertion that would have proved them could not pass. See
    ``tests/parity``.

    Rendering is still skipped, deliberately and visibly: a captured message is
    kept as the ``EmailMessage`` it was, so an assertion can inspect the template
    name and context rather than parsing HTML.
    """

    def __init__(self) -> None:
        self.sent: list[EmailMessage] = []

    async def send(self, message: EmailMessage) -> None:
        """Record the message, if suppression and idempotency permit it."""
        prepared = await prepare_recipients(message)
        if prepared is None or not await claim_send(prepared):
            return
        self.sent.append(prepared)

    async def send_batch(self, messages: list[EmailMessage]) -> None:
        """Record each message that passes the same guards."""
        for message in messages:
            await self.send(message)

    def clear(self) -> None:
        """Discard captured messages. For test teardown."""
        self.sent.clear()


def get_email_provider() -> ConsoleEmailProvider | SMTPEmailProvider:
    """Build the provider selected by configuration.

    Returns:
        The provider matching ``settings.email.provider``.

    Raises:
        ValueError: When the configured provider name is unknown.
    """
    match settings.email.provider:
        case "console":
            return ConsoleEmailProvider()
        case "smtp":
            return SMTPEmailProvider()
        case unknown:
            raise ValueError(f"Unknown email provider: {unknown}")


#: Process-wide provider, built during startup by the lifespan.
email: ConsoleEmailProvider | SMTPEmailProvider | CollectingEmailProvider | None = None


def set_email_provider(
    provider: ConsoleEmailProvider | SMTPEmailProvider | CollectingEmailProvider,
) -> None:
    """Install the process-wide email provider."""
    global email  # noqa: PLW0603 - process-wide singleton by design
    email = provider


def get_email() -> ConsoleEmailProvider | SMTPEmailProvider | CollectingEmailProvider:
    """Return the initialised email provider.

    Raises:
        RuntimeError: When called before the lifespan built it.
    """
    if email is None:
        raise RuntimeError("Email provider is not initialised; the lifespan builds it.")
    return email
