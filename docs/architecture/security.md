# Security

Mechanism lives in [`app/core/security/`](../../app/core/security/); policy —
who may log in, what a session means — is a Stage 2 feature. This document
covers the mechanism and the decisions behind it.

## Tokens

**Ed25519 (EdDSA)**, asymmetric, so services that only *verify* never need the
signing key. Smaller and faster than RSA at equivalent strength, with no
parameter choices to get wrong.

| Token | Lifetime | Revocable | Carries |
| --- | --- | --- | --- |
| Access | 15 minutes | Via token version | `sub`, `tv`, `tid`, `scopes` |
| Refresh | 30 days | Yes, server-side session record | `sub`, `tv`, `tid` |

Every token carries `iss`, `aud`, `iat`, `exp`, `jti` and `type`, and a `kid`
header naming the signing key.

### Why the split

Stateless tokens cannot be revoked — that is their entire performance advantage
and their entire weakness. Two mitigations, both implemented:

**Short access tokens.** The lifetime *is* the revocation window. Fifteen
minutes means a stolen token is useful for at most fifteen minutes.

**Token versioning.** Every access token carries the user's `tv` claim; the user
record holds the current value. Bumping it invalidates every token already
issued to that user, instantly. This is what makes "log out everywhere",
"password changed" and "account suspended" actually take effect, at the cost of
one cacheable lookup per request.

Refresh tokens are long-lived, so they are backed by a server-side session
record and revoked by deleting it. The `jti` claim is that record's identifier.

### The `type` claim

Without it, a refresh token — which lives thirty days — authenticates as an
access token, because the signature is equally valid. `decode_token` takes an
`expected_type` and every caller passes it.

### Verification

`decode_token` always checks signature, expiry, issuer and audience, and
requires `exp`, `iat`, `sub`, `jti` and `type` to be present. The signing key is
selected by the `kid` header, so rotated keys keep working.

`InvalidTokenError` carries **no reason**. Telling a client whether a token was
expired, forged or for the wrong audience is free reconnaissance.

A verified token is a statement about *identity*, never *authorization*. That
the signature is valid says nothing about whether the subject still exists, is
active, or may perform the action. Those checks happen in dependencies.

## Key rotation

Naively swapping the key file invalidates every outstanding token the instant it
happens — every user logged out mid-deploy. The design avoids that:

```text
keys/
  private.pem          active signing key
  public.pem           active verification key
  retired/
    2025-q4.pem        still verifies, no longer signs
```

Tokens name their key via `kid`; retired public keys stay in `retired/` and are
still accepted and still published in JWKS.

**Procedure:**

1. Generate a new pair; set `JWT__ACTIVE_KEY_ID` to the new kid.
2. Move the previous *public* key to `keys/retired/<old-kid>.pem`.
3. Deploy. New tokens use the new key; old tokens still verify.
4. After the longest token lifetime has elapsed, delete the retired key.

Skipping step 2 is the flag-day failure above.

## JWKS

Public keys are published at `/.well-known/jwks.json`. Once more than one thing
verifies these tokens — a gateway, another service, a partner — copying a PEM
into each does not scale and makes rotation a manual cross-system operation.

Exposes public keys only. That is by design and is not a leak: a public key is
what verifiers need and cannot sign anything.

## Passwords

**Argon2id** via `pwdlib`'s recommended parameters. Memory-hard, so an attacker
with GPUs gains far less than against bcrypt or any SHA construction.

**Transparent rehashing.** `verify_and_update_password` returns an upgraded hash
when the parameters have moved on. A successful login is the only moment the
plaintext exists in memory, so it is the only moment a stored hash can be
migrated — persist what it returns.

**Policy: length, not composition.** The default enforces a 12-character
minimum and nothing else, following NIST 800-63B. Forced composition rules
produce `Password1!` and measurably weaker passwords; the switches exist in
configuration because auditors sometimes require them.

**An upper bound of 128 characters.** Argon2's cost scales with input, so an
unbounded password field lets anyone burn a worker's CPU with a megabyte of text.

Breach-corpus checking is configured and not yet implemented; it blocks the
passwords actually being tried in credential stuffing, which no composition rule
does. Use the k-anonymity range API — send the first five SHA-1 hex characters
only, never the password or its full hash.

## Hashing tokens vs passwords

Different problems, different tools.

`app.common.utils.crypto.hash_token` uses unsalted SHA-256, which is *correct*
for a 256-bit random token: it cannot be brute-forced or rainbow-tabled, so
Argon2 would add latency and nothing else. Passwords have low entropy and need
the slow, salted hash.

Storing token hashes means a database leak does not hand over usable tokens.

## Comparisons

Every secret comparison uses `constant_time_compare`. A plain `==` returns early
on the first differing byte, and that timing difference is enough to recover a
token byte by byte.

## Transport and headers

- CORS origins are explicit; `*` with credentials is rejected in production.
- `TrustedHostMiddleware` binds Host headers in production.
- 401 responses always carry `WWW-Authenticate`; 429 always carries
  `Retry-After`.
- TLS terminates at the proxy. The application assumes it is behind one and
  reads client IPs from forwarded headers, never the socket address.

## Not yet built

Stage 2, in dependency order: login and session management, refresh-token
rotation with reuse detection, account lockout, email verification, password
reset, permissions and audit logging.
