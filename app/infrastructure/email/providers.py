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

from email.message import EmailMessage as MIMEMessage
from functools import lru_cache

from jinja2 import Environment, FileSystemLoader, StrictUndefined, TemplateNotFound

from app.core.config import settings
from app.core.exceptions import ExternalServiceError
from app.core.logging import get_logger
from app.infrastructure.email.client import EmailMessage

logger = get_logger(__name__)


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
        """Render the message and log its envelope."""
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
            raise ValueError("EMAIL__SMTP_HOST is required when using the smtp provider")

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

        import aiosmtplib

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

            for message in messages:
                html, text = render_template(message.template, message.context)
                mime = build_mime(message, html, text)
                await client.send_message(
                    mime, recipients=[*message.to, *message.cc, *message.bcc]
                )
                logger.info(
                    "Email sent",
                    extra={
                        "email_to": message.to,
                        "email_template": message.template,
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
    """

    def __init__(self) -> None:
        self.sent: list[EmailMessage] = []

    async def send(self, message: EmailMessage) -> None:
        """Record the message."""
        self.sent.append(message)

    async def send_batch(self, messages: list[EmailMessage]) -> None:
        """Record every message."""
        self.sent.extend(messages)

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
