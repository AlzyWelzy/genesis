"""Password hashing and policy.

Why this file exists
--------------------
Password handling is where a backend either quietly does the right thing or
quietly does something catastrophic, and the difference is invisible in a code
review unless it is centralised. One module decides the algorithm, one module
decides the policy, and no feature ever calls a hashing library directly.

Choices made here, and why
--------------------------
**Argon2id.** Memory-hard, so an attacker with GPUs gains far less than against
SHA-family or bcrypt. ``PasswordHash.recommended()`` tracks pwdlib's current
parameters rather than freezing numbers that will look negligent in three years.

**Transparent rehashing.** Parameters should get stronger over time, and the
only moment a stored hash can be upgraded is the moment the plaintext is
available — a successful login. :func:`verify_and_update_password` returns the
upgraded hash for the caller to persist.

**Length over composition.** The policy defaults enforce a minimum length and
nothing else, which follows NIST 800-63B: forced composition rules ("one
symbol") produce `Password1!` and measurably weaker passwords. Composition
switches exist in configuration because auditors sometimes require them.

**An upper bound.** Argon2's cost scales with input length, so an unbounded
password field lets anyone burn a worker's CPU with a megabyte of text.

This module contains no authentication flow: no user lookup, no credential
check, no lockout logic. Those are Stage 2 concerns and belong in a feature.
"""

import re
from typing import Final

from pwdlib import PasswordHash

from app.core.config import settings
from app.core.exceptions import ValidationError

#: Argon2id via pwdlib's recommended construction. Changing the parameters is a
#: single edit here; ``verify_and_update`` migrates stored hashes on next login.
_password_hash: Final[PasswordHash] = PasswordHash.recommended()

_UPPERCASE: Final[re.Pattern[str]] = re.compile(r"[A-Z]")
_LOWERCASE: Final[re.Pattern[str]] = re.compile(r"[a-z]")
_DIGIT: Final[re.Pattern[str]] = re.compile(r"\d")
_SYMBOL: Final[re.Pattern[str]] = re.compile(r"[^A-Za-z0-9]")


def hash_password(password: str) -> str:
    """Hash a plaintext password for storage.

    Args:
        password: The plaintext password. Never log or persist this value.

    Returns:
        An encoded Argon2id hash including the algorithm parameters and salt,
        so a future parameter change stays verifiable against old hashes.
    """
    return _password_hash.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    """Check a plaintext password against a stored hash.

    Returns ``False`` rather than raising when the stored hash is malformed or
    uses an unknown algorithm, so a caller cannot distinguish "no such user"
    from "corrupt hash" — a distinction that leaks account existence.

    Args:
        password: The plaintext password supplied by the client.
        password_hash: The previously stored hash.

    Returns:
        ``True`` when the password matches.
    """
    try:
        return _password_hash.verify(password, password_hash)
    except Exception:  # noqa: BLE001 - malformed hashes must not leak details
        return False


def verify_and_update_password(
    password: str, password_hash: str
) -> tuple[bool, str | None]:
    """Verify a password and, when its hash is outdated, return an upgraded one.

    A successful verification is the only moment the plaintext exists in memory,
    so it is the only moment a stored hash can be migrated to stronger
    parameters. When the second element is not ``None``, the caller must persist
    it — this is the whole mechanism by which hashing strength improves over the
    life of the system.

    Args:
        password: The plaintext password supplied by the client.
        password_hash: The previously stored hash.

    Returns:
        A ``(verified, updated_hash)`` pair. ``updated_hash`` is ``None`` when
        the stored hash is already current.
    """
    try:
        return _password_hash.verify_and_update(password, password_hash)
    except Exception:  # noqa: BLE001 - malformed hashes must not leak details
        return False, None


def validate_password_policy(password: str) -> None:
    """Enforce the configured password policy.

    Every failed rule is reported at once. Revealing them one at a time turns
    choosing a password into a guessing game and is a genuine source of user
    frustration and support load.

    Args:
        password: The candidate plaintext password.

    Raises:
        ValidationError: When the password violates the policy. ``details``
            carries the machine-readable list of unmet requirements.
    """
    policy = settings.security
    failures: list[str] = []

    if len(password) < policy.password_min_length:
        failures.append(f"must be at least {policy.password_min_length} characters")
    if len(password) > policy.password_max_length:
        failures.append(f"must be at most {policy.password_max_length} characters")
    if policy.password_require_uppercase and not _UPPERCASE.search(password):
        failures.append("must contain an uppercase letter")
    if policy.password_require_lowercase and not _LOWERCASE.search(password):
        failures.append("must contain a lowercase letter")
    if policy.password_require_digit and not _DIGIT.search(password):
        failures.append("must contain a digit")
    if policy.password_require_symbol and not _SYMBOL.search(password):
        failures.append("must contain a symbol")

    if failures:
        raise ValidationError(
            "Password does not meet the required policy.",
            code="password_policy_violation",
            details={"requirements": failures},
        )

    # TODO: when SECURITY__PASSWORD_CHECK_BREACHED is on, check the password
    # against a breach corpus using the k-anonymity range API (send the first 5
    # SHA-1 hex characters only — never the password or its full hash) and fail
    # open on a lookup error. A breach check blocks the passwords actually being
    # tried in credential stuffing, which no composition rule does.
