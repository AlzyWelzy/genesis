# 0005. Ed25519 token signing with key rotation

**Status:** Accepted
**Date:** 2026-07-26

## Context

The platform issues bearer tokens. Three things had to be decided before the
first token: the signing algorithm, how keys rotate, and how revocation works
given that stateless tokens cannot be revoked.

## Decision

- **Ed25519 (EdDSA)** asymmetric signing.
- **`kid`-based rotation**: tokens name their signing key; retired public keys
  remain in `keys/retired/` and stay valid for verification.
- **JWKS** published at `/.well-known/jwks.json`.
- **Short access tokens (15 min) plus a token-version claim** for revocation;
  refresh tokens backed by a revocable server-side session record.

## Alternatives considered

**HS256 (symmetric).** Simplest — one secret, no key files. Rejected because
every service that verifies a token would also be able to *mint* one. That is
acceptable in a single service and unacceptable the moment a gateway, a worker
or a partner needs to verify.

**RS256.** The most widely supported asymmetric option, and the safe choice for
maximum interoperability. Rejected in favour of Ed25519: smaller keys and
signatures, faster verification, and no parameter choices to get wrong (RSA key
size, padding scheme — PKCS#1 v1.5 versus PSS — are all opportunities for error).
RS256 remains configurable for legacy interop.

**Opaque tokens with a server-side session lookup.** Revocation becomes trivial
and instantaneous, which is a genuine advantage. Rejected because it requires a
datastore hit on every single request, and it cannot be verified by another
service without calling back to this one. The token-version claim recovers most
of the revocation benefit at the cost of one cacheable lookup.

**Long-lived access tokens.** Rejected: a stateless token cannot be revoked, so
its lifetime *is* the revocation window. A one-hour access token means a stolen
token is useful for an hour.

**Single key, no rotation.** Rejected: rotation then becomes a flag day. Swapping
the key invalidates every outstanding token instantly — every user logged out
mid-deploy, every mobile client erroring. The `kid` header and a retired-keys
directory make rotation a routine deploy.

## Consequences

Easy: other services verifying tokens without shared secrets, rotating keys
without downtime, revoking all of a user's sessions by bumping one integer.

Hard: key material must be managed — generated per environment, kept out of git,
and delivered by a secret manager in production. `keys/` is gitignored and
`scripts/generate_keys.py` makes generation a single command.

Operational commitment: the rotation procedure in
[`security.md`](../security.md) must be followed in order. Skipping the step
that moves the old public key to `retired/` reintroduces the flag day.
