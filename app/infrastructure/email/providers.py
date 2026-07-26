"""Concrete email providers and their factory.

Why this file exists
--------------------
The same reasoning as the storage providers: features depend on the contract in
:mod:`app.infrastructure.email.client`, and exactly one place decides which
transport the running process uses. That keeps "print to stdout in development,
SMTP in staging, an API in production" a configuration change rather than a code
change.

Add a transport by implementing
:class:`~app.infrastructure.email.client.EmailProvider` and adding a branch to
:func:`get_email_provider`.
"""

from app.core.config import settings
from app.core.logging import get_logger
from app.infrastructure.email.client import EmailMessage

logger = get_logger(__name__)


class ConsoleEmailProvider:
    """Logs messages instead of sending them.

    The default in local and test environments. Making this the default is a
    safety feature: a misconfigured development box cannot mail real customers.
    """

    async def send(self, message: EmailMessage) -> None:
        """Log the message envelope at INFO."""
        raise NotImplementedError

    async def send_batch(self, messages: list[EmailMessage]) -> None:
        """Log each message in turn."""
        raise NotImplementedError


class SMTPEmailProvider:
    """SMTP transport.

    Must use an async SMTP client (``aiosmtplib``); ``smtplib`` is blocking and
    would stall the event loop for the duration of the SMTP conversation. Add
    the dependency before implementing.
    """

    def __init__(self) -> None:
        self._host = settings.email.smtp_host
        self._port = settings.email.smtp_port

    async def send(self, message: EmailMessage) -> None:
        """Render the template and deliver over SMTP."""
        raise NotImplementedError

    async def send_batch(self, messages: list[EmailMessage]) -> None:
        """Deliver several messages over a single connection."""
        raise NotImplementedError


def render_template(template: str, context: dict[str, object]) -> tuple[str, str]:
    """Render a template into its HTML and plain-text bodies.

    Every message needs both parts: clients that block HTML fall back to text,
    and text-only-capable clients otherwise receive an empty message.

    Args:
        template: Template name, resolved under ``settings.email.template_dir``.
        context: Values interpolated into the template.

    Returns:
        A ``(html_body, text_body)`` pair.
    """
    raise NotImplementedError(
        "Choose a template engine (Jinja2), add it to dependencies, and render "
        "`<template>.html` / `<template>.txt` from settings.email.template_dir."
    )


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
