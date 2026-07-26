"""Tests for the security primitives.

These are the highest-value unit tests in the foundation: every one of them
covers a failure that would be a vulnerability rather than a bug. They assert
on *rejection* at least as much as on success — a token system that accepts
valid tokens but also accepts forged ones passes a happy-path test suite.
"""

import base64
import uuid

import jwt as pyjwt
import pytest

from app.core.exceptions import ValidationError
from app.core.security import (
    ACCESS_TOKEN_TYPE,
    REFRESH_TOKEN_TYPE,
    InvalidTokenError,
    build_jwks,
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    validate_password_policy,
    verify_and_update_password,
    verify_password,
)


class TestPasswordHashing:
    """Argon2id hashing behaviour."""

    def test_hash_is_not_the_plaintext(self) -> None:
        password = "correct horse battery staple"
        hashed = hash_password(password)
        assert password not in hashed
        assert hashed.startswith("$argon2id$")

    def test_hashes_are_salted(self) -> None:
        """The same password must hash differently every time.

        Identical hashes would mean no salt, which makes the whole table
        vulnerable to a single precomputed rainbow table.
        """
        assert hash_password("same") != hash_password("same")

    def test_verify_accepts_correct_and_rejects_wrong(self) -> None:
        hashed = hash_password("s3cret-passphrase")
        assert verify_password("s3cret-passphrase", hashed) is True
        assert verify_password("s3cret-passphras", hashed) is False

    def test_verify_returns_false_for_malformed_hash(self) -> None:
        """A corrupt stored hash must not raise.

        Raising would let a caller distinguish "no such user" from "this user
        exists but their hash is broken", which leaks account existence.
        """
        assert verify_password("anything", "not-a-hash") is False

    def test_verify_and_update_reports_current_hash_as_current(self) -> None:
        hashed = hash_password("another-good-passphrase")
        verified, updated = verify_and_update_password(
            "another-good-passphrase", hashed
        )
        assert verified is True
        assert updated is None


class TestPasswordPolicy:
    """Policy enforcement, independent of hashing."""

    def test_rejects_short_password_with_reasons(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            validate_password_policy("short")
        assert exc_info.value.code == "password_policy_violation"
        assert exc_info.value.details["requirements"]

    def test_accepts_long_passphrase_without_composition_rules(self) -> None:
        """Length alone must suffice under the default policy (NIST 800-63B)."""
        validate_password_policy("a sufficiently long passphrase")

    def test_rejects_absurdly_long_password(self) -> None:
        """An unbounded password is a CPU exhaustion vector against Argon2."""
        with pytest.raises(ValidationError):
            validate_password_policy("x" * 5000)


class TestTokens:
    """JWT creation and verification."""

    def test_round_trip_preserves_claims(self) -> None:
        tenant_id = uuid.uuid7()
        token = create_access_token(
            "user-1", token_version=7, tenant_id=tenant_id, scopes=("invoices:read",)
        )
        claims = decode_token(token, expected_type=ACCESS_TOKEN_TYPE)

        assert claims.subject == "user-1"
        assert claims.token_version == 7
        assert claims.tenant_id == tenant_id
        assert claims.scopes == ("invoices:read",)
        assert claims.token_id

    def test_refresh_token_rejected_where_access_expected(self) -> None:
        """The type claim must prevent cross-endpoint replay.

        Without it, a refresh token — which lives for thirty days — would
        authenticate as an access token.
        """
        refresh = create_refresh_token("user-1")
        with pytest.raises(InvalidTokenError):
            decode_token(refresh, expected_type=ACCESS_TOKEN_TYPE)

    def test_refresh_token_carries_no_scopes(self) -> None:
        """A refresh token's only power must be obtaining an access token."""
        claims = decode_token(
            create_refresh_token("user-1"), expected_type=REFRESH_TOKEN_TYPE
        )
        assert claims.scopes == ()

    def test_tampered_signature_is_rejected(self) -> None:
        token = create_access_token("user-1")
        header, payload, _ = token.split(".")
        with pytest.raises(InvalidTokenError):
            decode_token(f"{header}.{payload}.AAAA")

    def test_modified_payload_is_rejected(self) -> None:
        """Editing a claim must invalidate the signature.

        The scenario that matters: a caller re-encoding the payload with
        elevated scopes and keeping the original signature.
        """
        token = create_access_token("user-1", scopes=("invoices:read",))
        header, _, signature = token.split(".")
        forged_payload = (
            base64.urlsafe_b64encode(b'{"sub":"user-1","scopes":["admin"]}')
            .rstrip(b"=")
            .decode()
        )
        with pytest.raises(InvalidTokenError):
            decode_token(f"{header}.{forged_payload}.{signature}")

    def test_unsigned_token_is_rejected(self) -> None:
        """The `alg: none` attack must not work.

        Historically the single most common JWT vulnerability: a library that
        honours the token's own algorithm header accepts an unsigned token.
        """
        forged = pyjwt.encode({"sub": "attacker"}, key="", algorithm="none")
        with pytest.raises(InvalidTokenError):
            decode_token(forged)

    def test_token_carries_kid_header_for_rotation(self) -> None:
        """Without a kid, a verifier cannot select among rotated keys."""
        header = pyjwt.get_unverified_header(create_access_token("user-1"))
        assert header["kid"] == "primary"

    def test_wrong_audience_is_rejected(self) -> None:
        """A token minted for another service must not be accepted here."""
        from app.core.security.keys import get_signing_key

        foreign = pyjwt.encode(
            {
                "sub": "user-1",
                "iss": "genesis",
                "aud": "some-other-service",
                "iat": 1,
                "exp": 99999999999,
                "jti": "x",
                "type": ACCESS_TOKEN_TYPE,
            },
            get_signing_key().private_pem,
            algorithm="EdDSA",
        )
        with pytest.raises(InvalidTokenError):
            decode_token(foreign)


class TestJWKS:
    """Public key publication."""

    def test_publishes_active_key_as_okp(self) -> None:
        document = build_jwks()
        assert len(document["keys"]) == 1

        key = document["keys"][0]
        assert key["kty"] == "OKP"
        assert key["crv"] == "Ed25519"
        assert key["kid"] == "primary"
        assert key["use"] == "sig"

    def test_never_exposes_private_material(self) -> None:
        """A JWKS containing 'd' would publish the signing key itself."""
        for key in build_jwks()["keys"]:
            assert "d" not in key


class TestTokenClaims:
    def test_token_version_is_carried(self) -> None:
        """This claim is what makes logout-everywhere possible."""
        claims = decode_token(create_access_token("user-1", token_version=7))
        assert claims.token_version == 7

    def test_tenant_and_scopes_are_carried(self) -> None:
        from uuid import uuid7

        tenant_id = uuid7()
        claims = decode_token(
            create_access_token("u", tenant_id=tenant_id, scopes=("invoices:read",))
        )
        assert claims.tenant_id == tenant_id
        assert claims.scopes == ("invoices:read",)

    def test_each_token_gets_a_distinct_jti(self) -> None:
        """A shared jti would make per-token revocation impossible."""
        assert (
            decode_token(create_access_token("u")).token_id
            != decode_token(create_access_token("u")).token_id
        )

    def test_caller_claims_cannot_override_registered_ones(self) -> None:
        """Otherwise a caller could forge `sub` through the extra-claims dict."""
        claims = decode_token(create_access_token("real", claims={"sub": "admin"}))
        assert claims.subject == "real"


class TestTokenRejection:
    def test_access_token_rejected_where_refresh_expected(self) -> None:
        from app.core.security import REFRESH_TOKEN_TYPE

        with pytest.raises(InvalidTokenError):
            decode_token(create_access_token("u"), expected_type=REFRESH_TOKEN_TYPE)

    def test_expired_token_is_rejected(self) -> None:
        from datetime import timedelta

        from app.core.security import create_token

        expired = create_token(
            "u", token_type=ACCESS_TOKEN_TYPE, expires_in=timedelta(seconds=-10)
        )
        with pytest.raises(InvalidTokenError):
            decode_token(expired)

    def test_garbage_is_rejected(self) -> None:
        with pytest.raises(InvalidTokenError):
            decode_token("not-a-token")

    def test_unknown_kid_is_rejected(self) -> None:
        """Trying every key on an unknown kid would let an attacker probe them."""
        import base64
        import json

        from app.core.config import settings

        _, payload, signature = create_access_token("u").split(".")
        forged_header = (
            base64.urlsafe_b64encode(
                json.dumps({"alg": settings.jwt.algorithm, "kid": "nope"}).encode()
            )
            .decode()
            .rstrip("=")
        )
        with pytest.raises(InvalidTokenError):
            decode_token(f"{forged_header}.{payload}.{signature}")
