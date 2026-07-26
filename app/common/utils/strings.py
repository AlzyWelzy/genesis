"""String manipulation helpers.

Why this file exists
--------------------
Slugs, truncation and masking look trivial and are quietly full of edge cases:
Unicode normalisation, combining characters, and collisions between two titles
that slugify identically. Implementing them once — with the edge cases
documented — stops four features from each getting them subtly wrong.

Everything here is pure and synchronous. No I/O, no configuration, no logging.
"""

import re
import unicodedata
from typing import Any, Final

from app.common.constants import REDACTED, SENSITIVE_FIELD_NAMES

_NON_SLUG_CHARS: Final[re.Pattern[str]] = re.compile(r"[^a-z0-9]+")
_CAMEL_BOUNDARY: Final[re.Pattern[str]] = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_ACRONYM_BOUNDARY: Final[re.Pattern[str]] = re.compile(r"(?<=[A-Z])(?=[A-Z][a-z])")


def slugify(value: str, *, max_length: int = 64) -> str:
    """Convert arbitrary text into a URL-safe slug.

    Non-ASCII characters are transliterated where a decomposition exists and
    dropped otherwise, so callers must treat the result as **non-unique**:
    "Café" and "Cafe" both slugify to ``cafe``. Any slug used as an identifier
    needs a uniqueness check and a suffix strategy at the persistence layer.

    Args:
        value: Free-form text.
        max_length: Truncation limit, applied without leaving a trailing dash.

    Returns:
        A lowercase, hyphen-separated slug. Empty when the input contains no
        slug-safe characters at all.
    """
    # NFKD splits accented characters into base + combining mark, so the marks
    # can be dropped while the base letter survives: "é" -> "e".
    normalized = unicodedata.normalize("NFKD", value)
    ascii_only = normalized.encode("ascii", "ignore").decode("ascii")
    slug = _NON_SLUG_CHARS.sub("-", ascii_only.lower()).strip("-")
    if len(slug) > max_length:
        slug = slug[:max_length].rstrip("-")
    return slug


def truncate(value: str, max_length: int, *, suffix: str = "…") -> str:
    """Shorten a string to ``max_length`` characters including the suffix.

    Truncates on a word boundary when one falls within the last quarter of the
    budget; otherwise it cuts mid-word rather than discarding most of the text.

    Counts code points, not grapheme clusters — an emoji sequence can still be
    split. Use a grapheme-aware library if that matters for display.

    Args:
        value: The text to shorten.
        max_length: Maximum length of the result, including ``suffix``.
        suffix: Appended when truncation occurs.

    Returns:
        The original string when it already fits, otherwise the truncated form.

    Raises:
        ValueError: When ``max_length`` cannot fit the suffix.
    """
    if len(value) <= max_length:
        return value
    if max_length < len(suffix):
        raise ValueError("max_length must be at least the length of the suffix")

    budget = max_length - len(suffix)
    cut = value[:budget]
    space = cut.rfind(" ")
    if space > budget * 0.75:
        cut = cut[:space]
    return cut.rstrip() + suffix


def mask(value: str, *, visible: int = 4) -> str:
    """Partially redact a secret, keeping the last ``visible`` characters.

    For support tooling and logs where a human must confirm *which* key or card
    is in play without the value being recoverable.

    Short values are replaced entirely: revealing four of five characters is
    not redaction.

    Args:
        value: The secret.
        visible: Trailing characters to keep.

    Returns:
        The masked value.
    """
    if visible <= 0 or len(value) <= visible * 2:
        return "*" * len(value)
    return "*" * (len(value) - visible) + value[-visible:]


def redact_sensitive(data: dict[str, Any]) -> dict[str, Any]:
    """Recursively replace sensitive values in a mapping with a placeholder.

    Used before logging request or response bodies. Key matching is
    case-insensitive against
    :data:`app.common.constants.SENSITIVE_FIELD_NAMES`, and nested dicts and
    lists are walked — a password one level down is still a password.

    Args:
        data: The mapping to sanitise. Not modified in place.

    Returns:
        A copy with sensitive values replaced by
        :data:`~app.common.constants.REDACTED`.
    """
    return {key: _redact_value(key, value) for key, value in data.items()}


def _redact_value(key: str, value: Any) -> Any:
    """Redact one key/value pair, recursing into containers."""
    if key.lower() in SENSITIVE_FIELD_NAMES:
        return REDACTED
    if isinstance(value, dict):
        return redact_sensitive(value)
    if isinstance(value, list | tuple):
        return [_redact_value(key, item) for item in value]
    return value


def normalize_email(value: str) -> str:
    """Normalise an email address for storage and comparison.

    Lowercases the whole address and strips surrounding whitespace. The local
    part is technically case-sensitive per RFC 5321, but no real provider
    treats it that way, and preserving case creates duplicate accounts for the
    same human.

    Provider-specific canonicalisation (stripping Gmail dots and ``+`` tags) is
    deliberately **not** done here: it is an anti-abuse policy decision, not a
    formatting one, and applying it silently breaks legitimate aliases.

    Args:
        value: The raw address.

    Returns:
        The normalised address.
    """
    return value.strip().lower()


def to_snake_case(value: str) -> str:
    """Convert camelCase or PascalCase to snake_case.

    Handles acronym boundaries, so ``HTTPResponse`` becomes ``http_response``
    rather than ``h_t_t_p_response``.
    """
    with_acronyms = _ACRONYM_BOUNDARY.sub("_", value)
    return _CAMEL_BOUNDARY.sub("_", with_acronyms).lower()


def to_camel_case(value: str) -> str:
    """Convert snake_case to camelCase.

    Useful as a Pydantic ``alias_generator`` when an API must present camelCase
    to JavaScript clients while models stay Pythonic. This project uses
    snake_case on the wire (see ``docs/architecture/api-guidelines.md``), so
    this exists for integrating with external APIs that do not.
    """
    head, *rest = value.split("_")
    return head + "".join(word.capitalize() for word in rest)


__all__ = [
    "mask",
    "normalize_email",
    "redact_sensitive",
    "slugify",
    "to_camel_case",
    "to_snake_case",
    "truncate",
]
