# Module reference

A file-by-file account of the codebase: what each module does, the decision it
encodes, and the failure it prevents.

The other documents in this directory explain rules and conventions. This one
explains *code* — read it when you need to know what something does or why it
looks the way it does.

Every module also carries a docstring covering the same ground; this page exists
so the whole picture can be read in one place, and so relationships between
modules are visible in a way per-file docs cannot show.

---

## Contents

- [Bootstrap](#bootstrap) — `main.py`, `api.py`
- [Core](#core) — configuration, logging, errors, security, middleware, lifespan
- [Common](#common) — types, pagination, filtering, sorting, DI, utilities
- [Infrastructure](#infrastructure) — database, redis, storage, email, queue, outbox, observability
- [Events](#events)
- [System](#system) — operational endpoints
- [Modules](#modules) — feature discovery
- [Cross-cutting flows](#cross-cutting-flows)

---

## Bootstrap

### `app/main.py`

The composition root — the only module allowed to know every top-level concern
at once. It builds the `FastAPI` object and does nothing else.

Boot sequence, in order:

1. Create the app with metadata and the lifespan handler.
2. `register_middleware(app)` — order is semantic, see below.
3. `register_exception_handlers(app)` — one envelope for every error.
4. Mount the system router **at the root**, outside the API prefix.
5. Mount the versioned API router.
6. `configure_openapi(app)`.

`create_app()` is a factory rather than only a module-level singleton so tests
can build an isolated instance after overriding settings.

**Why system routes are mounted at the root.** An orchestrator's probe URLs live
in deployment manifests, updated on a different cadence than the application. A
liveness probe that 404s because someone changed `APP__API_PREFIX` gets every
replica killed.

### `app/api.py`

Aggregates feature routers under `/api/v1`.

Routers are **discovered**, not listed. A hand-maintained `include_router` list
is one more thing to update per feature, and forgetting it produces a 404 that
looks like a routing bug.

The version lives here, not in feature routers, so introducing `/api/v2` is a
second router in this file rather than an edit to every module. Versioning is a
routing concern; if a service ever needs to know which version called it, the
boundary has been drawn in the wrong place.

---

## Core

Framework wiring. Nothing here may import from `app.modules`.

### `core/settings.py`

The entire configuration surface, as nested Pydantic models: `AppSettings`,
`DatabaseSettings`, `JWTSettings`, `SecuritySettings`, `RateLimitSettings`,
`RedisSettings`, `StorageSettings`, `EmailSettings`, `LoggingSettings`,
`ObservabilitySettings`.

Values come from, in decreasing priority: process environment →
`.env.<environment>` → `.env` → `secrets_dir`.

That last source is why moving off `.env` files needs no code change: point
`SECRETS_DIR` at a Docker/Kubernetes secrets mount and the same fields populate
from files.

**The production validator** (`_enforce_production_safety`) refuses to boot when
`APP__ENVIRONMENT=production` and any of these hold:

| Rejected | Because |
|---|---|
| `debug=true` | Starlette returns full tracebacks in HTTP responses |
| `"*"` in CORS origins | any site can make authenticated requests |
| no trusted hosts | Host-header poisoning of generated links |
| `json_format=false` | no aggregator can parse the logs |
| `log_request_body=true` | bodies contain passwords and tokens |
| `rate_limit.enabled=false` | unthrottled public API |
| `database.echo=true` | every statement logged |

Crashing at boot is the point: a misconfigured instance that refuses to start is
a failed deploy, whereas one that starts is a breach.

### `core/config.py`

Builds the singleton. Importing it reads and validates the environment, so a
missing `DATABASE__URL` crashes at import with a precise Pydantic error rather
than surfacing as a `None` much later.

### `core/context.py`

`ContextVar`s for `request_id`, `correlation_id`, `tenant_id`, `user_id`, plus
`tenant_scope`, `user_scope` and `request_scope` context managers.

Each async task gets its own view, so two concurrent requests never see each
other's values — which is why a module-level global cannot be used and why this
is not a disguised singleton.

`require_tenant_id()` **raises** rather than returning `None`. A tenant-scoped
query that silently runs unscoped returns every tenant's rows with a 200 status.
Failing loudly turns a silent breach into a stack trace.

`tenant_scope` restores the previous value on exit rather than clearing it, so a
job that dips into another tenant returns to its own.

### `core/logging.py`

Owns the root handler. JSON in deployed environments, readable lines locally.

`ContextFilter` attaches the request and correlation IDs to every record — as a
filter, because a convention that must be remembered at ten thousand call sites
will be missed at the one that matters.

Values passed via `extra=` are merged into the JSON object and recursively
redacted against `SENSITIVE_FIELD_NAMES`.

Pass context via `extra`, never formatted into the message: a message that
varies per call cannot be grouped, counted or alerted on.

### `core/exceptions.py`

The error taxonomy and the global handlers.

| Exception | Status | When |
|---|---|---|
| `ValidationError` | 422 | input is well-formed, a domain rule rejects it |
| `BusinessRuleError` | 409 | a system-state invariant would be violated |
| `NotFoundError` | 404 | absent, **or** present and not yours |
| `ConflictError` | 409 | uniqueness, version or race collision |
| `AuthenticationError` | 401 | identity not established |
| `AuthorizationError` | 403 | known, and refused |
| `RateLimitError` | 429 | allowance exceeded |
| `ServiceUnavailableError` | 503 | our dependency is down |
| `ExternalServiceError` | 502 | a third party is down |

The 502/503 split matters operationally: a 503 spike points at our
infrastructure, a 502 spike at someone else's.

`NotFoundError` is deliberately ambiguous between "absent" and "not yours".
Distinguishing them via 403-vs-404 tells an attacker which IDs are real.

4xx logs at INFO, 5xx at ERROR. Logging a 404 at ERROR is how alert fatigue
starts.

The unhandled-exception handler reads the request ID from the **ASGI scope**,
not the context var: `ServerErrorMiddleware` sits outside `RequestContext­Middleware`,
so by the time it runs the context has been reset — and a 500 with no
correlating ID is the least useful error a support team can receive.

### `core/security/`

Four modules, because each has different reasons to change.

**`keys.py`** — loads and caches PEM key material, and supports rotation. The
active key signs; retired public keys in `keys/retired/<kid>.pem` still verify.
Without that directory, rotation invalidates every outstanding token the moment
the key changes.

**`passwords.py`** — Argon2id via pwdlib. `verify_and_update_password` returns
an upgraded hash when parameters have strengthened; a successful login is the
only moment the plaintext exists, so it is the only moment a hash can be
migrated.

Policy defaults to length-only, following NIST 800-63B: forced composition rules
produce `Password1!`. `check_password_breached` uses the k-anonymity range API —
only a 5-character SHA-1 prefix leaves the process — and **fails open**, because
a breach-list outage must not block password resets.

**`tokens.py`** — signing and verification. Every token carries `iss`, `aud`,
`iat`, `exp`, `jti`, `type` and a token version, and a `kid` header.

The `type` claim stops an access token being replayed at a refresh endpoint.
The token version is what makes a *stateless* token revocable: bumping the
user's counter invalidates every token already issued to them.

`InvalidTokenError` carries no reason. Telling a client whether a token was
expired, forged or wrong-audience is free reconnaissance.

**`jwks.py`** — publishes public keys at `/.well-known/jwks.json` so other
services verify without a copied PEM and pick up rotations on their own.

### `core/principal.py`

The seam between core and Stage 2's auth module.

Core needs to know who is calling and what they may do; both live on a `User`
model that core may not import. So core declares a `Principal` **Protocol** —
`id`, `is_active`, `token_version`, `permissions`, `is_superuser` — and the auth
module registers a loader at startup. Structural typing means neither imports
the other.

`get_principal_loader()` **raises** when nothing is registered. The alternative —
treating an authenticated request as anonymous — converts a wiring mistake into
an authorization bypass.

### `core/middleware.py`

The single ordered list. Starlette applies middleware outside-in in *reverse*
registration order, so reading the calls bottom-to-top gives traversal order:

1. `RequestContextMiddleware` — outermost, so everything downstream has the IDs.
2. `TrustedHostMiddleware` — reject forged Host headers early.
3. `CORSMiddleware` — must be outside the app so CORS headers are attached to
   *error* responses too; a 500 without them appears in a browser as an opaque
   network failure.
4. `AccessLogMiddleware` — inside CORS, so the recorded duration is our work.
5. `GZipMiddleware` — innermost.

`RateLimitMiddleware` **returns** its 429 rather than raising. An exception
raised inside a `BaseHTTPMiddleware` never reaches the app's exception handlers,
which live in `ExceptionMiddleware` further down — raising produces a 500, which
tells a throttled caller the server broke.

Metrics are labelled with the **route template**, never the resolved path.
Labelling by path mints a series per ID and takes the metrics backend down
before it touches the application.

### `core/lifespan.py`

Acquire in dependency order, release in reverse, each teardown independently
guarded.

Startup fails fast on the database — an instance that cannot reach it must fail
the deploy, not serve errors. Redis is optional: cache and rate limiting degrade
rather than fail, so a Redis outage must not block a deploy.

### `core/openapi.py`

Stable operation IDs (`<tag>_<function>`), because these become method names in
generated clients and must not change when a route moves. Declares the bearer
security scheme, attaches the common error responses, and builds tag metadata
from the discovered routers.

---

## Common

Reusable, feature-agnostic code. No business logic; nothing imports `modules`.

### `common/types.py`

Base schemas and aliases.

`BaseSchema` sets `extra="forbid"` — a client typo (`emial`) becomes a 422
rather than a silently dropped field.

`SafeResponseSchema` masks `SecretStr` in **JSON** output. Pydantic masks it in
`model_dump()` but not `model_dump_json()`, and the JSON path is the one an API
uses.

### `common/pagination.py`

Offset pagination (`PaginationParams`, `Page`, `PageMeta`) and cursor pagination
(`CursorParams`, `CursorPage`, `encode_cursor`, `decode_cursor`).

`MAX_PAGE_SIZE` is enforced: an endpoint without a ceiling is a denial-of-service
vector.

Cursors are base64 **and HMAC-signed**, because base64 alone is encoding, not
protection — an unsigned cursor lets a client forge one and probe arbitrary key
ranges. They are versioned so a cursor minted before a sort-order change is
rejected cleanly rather than paging from the wrong place. Every failure mode
returns the same generic error, so the format cannot be probed.

### `common/sorting.py`

`build_order_by` resolves a client's `sort_by` against an **explicit allow-list**.
`getattr(Model, sort_by)` would let a caller sort by a column they cannot read,
and sort order alone is enough to binary-search a hidden value out of a table.

Always appends a unique tiebreaker: pagination over a non-unique sort duplicates
and skips rows between pages.

### `common/filtering.py`

`FilterSpec` maps a filter field to a column and an `Operator`; `build_filters`
translates a populated schema into predicates.

`LIKE` wildcards in user input are escaped, so someone searching for `100%`
matches the literal string rather than everything. `STARTS_WITH` is anchored so
a B-tree index can still serve it.

`FilterSpec(indexed=False)` is a deliberate acknowledgement that a filter will
scan; `unindexed_filters()` surfaces them so a review catches the `LIKE '%…%'`
that is fine on ten thousand rows and fatal on ten million.

### `common/dependencies.py`

The DI chain every module copies:

```
SessionDep → ClaimsDep → PrincipalDep → TenantDep → require_permission(...)
```

Separate steps because each fails differently:

| Failure | Status |
|---|---|
| no/malformed token | 401 |
| token for a deleted user | 401 |
| token predating a version bump | 401 |
| valid user, foreign tenant | **404** — existence is privileged |
| valid user, missing permission | 403 |

`ClaimsDep` does no I/O, so endpoints needing only identity stay cheap.
`PrincipalDep` is the first that touches the database.

`require_permission` requires **all** listed permissions. "Either" should be a
single composite permission, not a looser default.

`require_scopes` reads scopes from the token — coarse and slightly stale, right
for machine clients, wrong for anything revocable mid-session.

### `common/utils/`

Pure, synchronous helpers.

**`datetime.py`** — `utc_now()` is the only sanctioned clock read, which is what
makes time mockable with one patch. Day bounds are half-open: an inclusive
`23:59:59` drops the final second, which is why daily totals fail to sum.

**`strings.py`** — `slugify` (lossy and non-unique, so callers must check),
`mask`, `redact_sensitive`, `normalize_email` (lowercases; deliberately does
*not* strip Gmail `+tags`, which is anti-abuse policy, not formatting).

**`files.py`** — `sanitize_filename` strips traversal components and the
right-to-left override that disguises `photo‮gnp.exe` as `photo.png`.
`detect_content_type` sniffs magic numbers, because renaming `payload.html` to
`avatar.png` is the standard stored-XSS route. `safe_join` refuses to escape a
root.

**`crypto.py`** — `secrets`, never `random`. `constant_time_compare` for every
secret comparison. `hash_token` uses unsalted SHA-256, correct *here* and wrong
for passwords: a 256-bit random token cannot be brute-forced, so Argon2 would
add latency and nothing else.

**`collections.py`** — `chunk` exists for correctness, not convenience: it is how
a bulk operation avoids exceeding PostgreSQL's bind-parameter limit. `unique`
preserves order because `list(set(...))` produces unstable API responses.

---

## Infrastructure

Everything that talks to an external system, behind an interface.

### `infrastructure/database/`

**`base.py`** — the declarative base. Every model must attach to the same
`MetaData`, or Alembic cannot see it.

**`naming.py`** — the constraint naming convention. Without it PostgreSQL
auto-names constraints unpredictably and autogenerate cannot drop what it cannot
name, producing migrations that apply on a fresh database and fail on production.

**`types.py`** — `UTCDateTime`, `Money` (Decimal, never float),
`JSONDict` (JSONB, indexable), `CaseInsensitiveString` (email uniqueness without
the `citext` extension, which needs superuser), `EncryptedString` (Fernet at
rest) and `blind_index` (HMAC — an unkeyed hash of an email is trivially
reversible).

Encrypted columns are **not searchable or sortable**, and key rotation is a data
migration. Decide the rotation story before adopting one.

**`mixins.py`** — `UUIDPrimaryKeyMixin` (UUIDv7: time-ordered, so inserts keep
index locality, without a sequential key's enumerability), `TimestampMixin`
(server-side defaults, so migrations and hand-written SQL are correct too),
`TenantMixin`, `SoftDeleteMixin`, `AuditMixin`, `VersionMixin`.

`AuditMixin` deliberately uses no foreign key: an audit trail must survive the
deletion of the user it names.

`UUIDPrimaryKeyMixin` carries **both** a `default=uuid7` and an `init` event
listener, which is not redundancy. `default=` is an *insert* default: SQLAlchemy
evaluates it while flushing, so `Invoice().id` is `None` until then. Code doing
the thing the mixin exists to enable —

```python
invoice = Invoice(...)
await stage(session, InvoicePaid(invoice_id=invoice.id))  # None, silently
await repo.add(invoice)
```

— stages an event carrying `None`, with no error, because the column is happy to
be populated a moment later. The listener assigns the ID at construction and
closes that window; `default=` remains the backstop for paths that bypass
`__init__`, such as a bulk `insert()`. The listener uses `setdefault`, so an
explicitly supplied ID (a restore, a cross-environment import) still wins.

**`session.py`** — one engine per process, one session per unit of work.

Both entry points publish buffered domain events after the transaction settles:
`get_session` once the request handler returns (necessarily after the service
committed), `session_scope` after its own commit. Neither publishes on the
rollback path, because a subscriber reacting to something that did not happen is
worse than one that missed it. This is wired here rather than left to callers
because a forgotten `flush_pending_events` is invisible — the commit succeeds,
the response is a 200, and no handler ever runs.
`expire_on_commit=False` matters specifically for async: the default triggers a
lazy refresh on attribute access after commit, which raises `MissingGreenlet`
outside an await context.

**`repository.py`** — `BaseRepository` and `TenantRepository`.

Every query is built through `select()`, which `TenantRepository` overrides to
apply the tenant predicate. That makes scoping structural rather than a
convention; escaping it requires writing `select()` by hand, which is visible in
review and greppable.

`get()` goes through `select()` rather than `session.get()`, because
`session.get()` bypasses both tenant and soft-delete scoping — making it a
cross-tenant read by primary key.

`TenantRepository.add()` *overwrites* `tenant_id` rather than defaulting it. A
`tenant_id` deserialised from a request body is a cross-tenant write, and
stamping centrally is what makes the field impossible to influence from outside.

Covered by `tests/integration/test_repository.py`, which declares its models on
the real `Base` so they inherit the real naming convention and type mapping.
Their `_test_` prefix keeps them out of autogenerate (see `_include_object` in
`migrations/env.py`), and their tables are created inside the test's transaction,
so the rollback drops them.

### `infrastructure/redis/`

**`client.py`** — one pool for cache, pub/sub, queue and rate limiting.
`build_key` namespaces everything; a shared Redis without a namespace is how a
staging deploy silently reads production cache entries.

The client is **bound to the event loop that created it**. Anything creating a
fresh loop must `close_redis()` first.

`init_redis()` publishes the module globals only **after** the ping succeeds.
Assigning first and pinging second looks equivalent and is not: a failed ping
leaves a broken client cached, so the next call takes the "already initialised"
early return and reports success without ever pinging. Startup then proceeds
against a Redis that is not there, and the failure resurfaces later, at the point
of use, with nothing connecting it back to the cause.

**`cache.py`** — JSON, never pickle: unpickling attacker-controlled bytes is
arbitrary code execution. Read failures are swallowed and logged as misses,
because an unavailable cache should make the application slower, never broken.
`clear_prefix` uses `SCAN`, never `KEYS`, which blocks the entire Redis instance.

**`rate_limit.py`** — sliding window by default. A fixed window permits a burst
of 200 spanning the boundary, which is exactly the shape the limit prevents. The
sorted-set member includes a random suffix so two requests in the same
millisecond do not collapse into one.

The window check is a Lua script rather than a pipeline, for two reasons. A
pipeline groups round trips, but the *decision* still happens in Python, between
the count coming back and the entry going in — so two concurrent requests at the
boundary both read the same count and both proceed. And a rejected request must
**not** be recorded: recording it means a client that retries while blocked keeps
pushing its own window forward and never recovers, staying locked out until it
stops entirely for a full window, which a polling client never does. The lockout
is effectively permanent and looks, from outside, like the limiter is broken.

`reset_after` is measured from the oldest entry still in the window, not assumed
to be a whole window. A flat window length tells a client to wait far longer than
it needs to, which reads as a much harsher limit than the one configured.

Token bucket (`check_token_bucket`) for endpoints where a short burst is
legitimate but a high sustained rate is not. Implemented as a Lua script so
read-modify-write cannot interleave.

**`pubsub.py`** — `RedisPubSub` is fire-and-forget: a subscriber that is down
never learns. `RedisStreamsPubSub` keeps a capped log a reconnecting subscriber
can resume from, turning "lost on disconnect" into "lost after N messages".

**`events/base.py`** — `to_payload()` guarantees JSON-representable output, and
`_SCALAR_ENCODERS` is where that guarantee lives. Two things about it are not
stylistic. **Order matters**: `datetime` is a subclass of `date`, so it must be
matched first or every timestamp is silently truncated to a bare day — a data
loss no exception announces. And the table must cover everything a domain event
plausibly carries — `date`, `timedelta`, `bytes` were all missing — because the
failure is not local: an unencodable value raises when asyncpg writes the JSONB
outbox column, *inside* the business transaction, so adding a `due_date` to an
event breaks the write the event was describing.

**`utils/collections.py`** — `deep_merge` copies nested containers rather than
aliasing them. "Does not mutate its inputs" and "the result is safe to mutate"
are different properties, and only the first is obvious. A shallow `dict(base)`
satisfies the first while leaving every nested dict shared, so::

    config = deep_merge(DEFAULTS, overrides)
    config["db"]["host"] = "localhost"

rewrites `DEFAULTS` for the life of the process. Configuration merging is
precisely where a shared default is the base.

**`pagination.py`** — cursors are opaque, versioned and HMAC-signed. The
signature is appended with **no delimiter** and split off by its fixed length.
That is not a style choice: v1 separated payload from signature with `.` and
recovered the split using `rpartition`, which finds the *last* dot — and since
the signature is raw HMAC bytes, roughly one in sixteen contains `0x2e`. About
7% of cursors were then verified against the wrong payload and rejected as
forged, so a client paginating hit a spurious "invalid cursor" about one page in
fourteen: intermittent, unreproducible from any single example, and
indistinguishable in the logs from a genuine tampering attempt. Any delimiter
would have the same flaw; the length is what makes the boundary unambiguous.

**`tokens.py`** — claim construction happens *inside* the guarded block, not
only the signature check. A valid signature proves the payload is ours; it does
not prove every field is well-formed. A `tid` that is not a UUID, an `iat`
outside datetime's range, or a `scopes` claim that is a bare string (which
`tuple()` would silently explode into one scope per character) each become an
`InvalidTokenError` and a 401, rather than escaping as a `ValueError` and
becoming a 500 with a traceback.

**`utils/files.py`** — `safe_join` refuses both escape *and* a key that resolves
to the root itself. `""`, `"."` and `"a/.."` are all inside the root, so the
traversal check passes; the caller then writes to or unlinks a directory, which
surfaces as an uncaught `IsADirectoryError`. A storage key names an object and
can never name the root.

Two database exceptions are translated at the edge rather than falling through
to the catch-all handler. `StaleDataError` — an optimistic-lock version check
that lost its race — becomes a 409 `stale_data`, because the caller's request was
well-formed and the correct answer is "refetch and retry"; a 500 there means two
people editing one record produces an alert and a traceback. A unique-violation
`IntegrityError` becomes a 409 `already_exists`, matched on SQLSTATE `23505`
rather than on the driver's message, which is localised and version-dependent.
Racing a uniqueness check is not a bug to fix by checking harder: between any
`SELECT` and its `INSERT` there is a window, and the constraint is what closes
it. Every *other* integrity error stays a 500 — a foreign-key or check-constraint
failure means the application let through data it should have rejected. The
database's own message is never forwarded; it names tables, columns and
constraints.

The relay runs in `scripts/worker.py`, alongside the queue consumer. That is not
an implementation detail to be relocated casually: a staged event that no process
publishes is silently lost, which inverts the pattern into the failure it exists
to prevent. Running several is safe — claiming uses `FOR UPDATE SKIP LOCKED`.

The default publisher, `publish_to_queue`, enqueues the event as a job named
`outbox.<event name>`. The queue is the right destination rather than the
in-process event bus because the relay lives in a background process, so
publication has to cross a process boundary anyway — and the queue already
supplies acknowledgement, exponential backoff, a dead-letter destination and
name-based dispatch that does not require rebuilding the original Python class
from JSON. Outbox for "the intent survives the commit", queue for "the attempt is
retried"; neither alone is enough. The job carries `outbox:<message id>` as its
idempotency key, so a row republished after a crash between publishing and
marking it published is suppressed rather than delivered twice.

Ambient context crosses the process boundary as ordinary stream fields.
`Job.encode` captures `correlation_id`, `tenant_id` and `user_id` at enqueue
time — the last moment they exist, since the worker runs elsewhere — and
`Worker._dispatch` rebinds them with `request_scope` around the handler, per job
rather than per batch so concurrent jobs cannot see each other's tenant. The
outbox relay does the same from the message's own columns.

This is load-bearing rather than cosmetic. A `TenantRepository` reads the tenant
from the ambient context, so a job dispatched without one does not merely lose
its log correlation — it raises on `require_tenant_id()`, making tenant-scoped
background work impossible. The retry path re-serialises the fields from the job
rather than from ambient context, because it runs in the worker's failure path,
outside the scope the handler ran under; reading ambient there would give
attempt 1 a tenant and attempt 2 none.

### `infrastructure/storage/`

`presigned_url` / `verify_presigned` sign with `url_signing_secret()`, which
prefers `SECURITY__URL_SIGNING_KEY` and otherwise derives a key from the JWT
private key through HMAC with a domain separator. A presigned URL *is* the
authorisation — possession of it is the whole check — so signing with a service
name, namespace or any other publicly known value is not a weak signature, it is
no signature at all: anyone can mint a link for an arbitrary key with an
arbitrary expiry. Deriving keeps local development correct with no configuration;
set the key explicitly where links must outlive a signing-key rotation.

`S3StorageProvider.exists()` treats only a 404 (or `NoSuchKey` / `NotFound`) as
absence and re-raises everything else as `ExternalServiceError`. Reporting every
failure as "not there" means an outage, an expired credential or a bucket-policy
change makes every object appear missing — an "upload if absent" flow then
silently overwrites live data, and a read path returns 404 when the honest answer
is that storage is down and a retry would work.

Protocol plus local and S3 providers, selected by configuration.

The local provider runs every filesystem call through `asyncio.to_thread`, since
blocking I/O in an async path stalls the whole event loop — which presents as
"the service got slow", not "one upload got slow".

`safe_join` is what makes it safe: a key containing `../` would otherwise write
anywhere the process can write.

`copy`/`move` support the promote-from-temp-prefix flow. Copy-then-delete, never
the reverse: a failed copy leaves the source intact, whereas delete-first loses
the object. S3 copies server-side, so bytes never traverse this process.

Multipart uploads are a separate `MultipartCapable` protocol, so the local
provider is not forced to fake an API it does not need. An abandoned multipart
upload is **billed and invisible** in a normal listing, so `abort_multipart`
never raises and a lifecycle rule is the backstop.

### `infrastructure/email/`

Message model, provider protocol, Jinja2 templates.

Console is the default provider — a safety feature, so a misconfigured
development box cannot mail real customers.

Every message renders **both** HTML and plain text. Text-only clients otherwise
receive an empty message, and a message with no text alternative scores worse
with spam filters. `multipart/alternative` means "last renderable part wins", so
text is added first.

Templates use `StrictUndefined`: silently mailing "Hello ," is worse than
failing the send. Autoescaping is on, because an unescaped name is a
mail-client XSS vector.

**Suppression** is not optional. Mailbox providers score a sending domain on
bounce and complaint rates; mailing hard-bounced addresses degrades that score
until *all* mail from the domain — including password resets to willing
recipients — is rejected.

**Idempotency** derives a key from recipients, subject, template and context, so
protection is on by default. A queued send is retried under any realistic
failure, and a user receiving four identical password resets concludes the
system is broken. Fails open: a duplicate email is a smaller harm than a reset
that never arrives.

### `infrastructure/queue/`

Redis Streams, chosen over Celery/arq because Redis is already a dependency and
Streams provide consumer groups **with acknowledgement** — a worker that crashes
mid-job leaves the message pending and claimable rather than silently lost.

`worker.py` implements the retry policy. Three things a naive worker gets wrong,
each a production incident:

- **Retrying immediately** — the retries become the load keeping the dependency
  down. Backoff is exponential.
- **Retrying forever** — a permanent bug consumes a worker slot indefinitely.
  After `max_retries` the job is dead-lettered.
- **Losing in-flight work** — `reclaim_stalled` claims what a dead worker
  abandoned.

Jobs take serialisable arguments only — pass an ID, never an ORM object, because
by the time the job runs the session is closed. Jobs must be idempotent, and
must be enqueued **after** commit.

### `infrastructure/outbox/`

Closes the gap between committing and publishing.

```python
async with session_scope() as session:
    await repo.add(invoice)
    await stage(session, InvoicePaid(...))  # same transaction
    await session.commit()  # both, or neither
```

A crash between commit and publish is otherwise permanent silent loss: the
invoice exists, nothing will send the receipt, and no error was raised.
Publishing first is not a fix — a rollback then leaves subscribers reacting to
something that never happened.

The relay claims rows with `FOR UPDATE SKIP LOCKED`, so concurrent relays share
work rather than serialise. Guarantee is **at least once**; handlers must be
idempotent. Exhausted messages are kept, not deleted — the payload is often the
only record of what should have happened.

`pending_count()` is the metric to alert on: a climbing number means the relay
is down or slower than the write rate, and both become customer-visible within
minutes.

### `infrastructure/observability/`

`Metrics` is a protocol with a no-op default, so instrumentation is written once
and left in place regardless of whether a backend is configured.

The rule that matters: **never label a metric with an unbounded value.** A metric
labelled with a user ID on a million-user system is a million time series.

Both backends are optional extras and degrade to no-ops when absent — an
observability dependency must never be why an application refuses to start.

---

## Events

`DomainEvent` is frozen and past-tense: an event is a statement of fact, and a
handler that could mutate it would change what later handlers see.

`to_payload()` produces only JSON-representable values, because an event may
cross a process — or language — boundary. `Decimal` becomes a string, never a
float: an event carrying money must round-trip to the cent.

`EventBus.publish` runs handlers concurrently and **contains** their failures.
The publisher's work is already committed; one broken analytics subscriber must
not fail the request that triggered it. Failures are logged with the event name,
event ID and handler — swallowed is not the same as silent.

Handlers registered against a base class receive subclass events, which is how a
generic audit subscriber observes everything.

`publish_after_commit(session, *events, durable=...)` chooses between in-process
publication after commit (cheap, lost on a crash in the window) and the
transactional outbox (durable, one insert per event).

---

## System

Operational endpoints, mounted at the root.

| Endpoint | Question | Failure means |
|---|---|---|
| `/live` | is the process wedged? | container **killed and restarted** |
| `/ready` | can this instance serve? | removed from the load balancer |
| `/startup` | has boot finished? | orchestrator **waits** |
| `/health` | human summary | nothing automated |
| `/metrics` | Prometheus scrape | — |
| `/.well-known/jwks.json` | public keys | — |

`/live` checks **nothing external**, deliberately. If it checked the database, a
database blip would fail every replica's liveness probe simultaneously,
restarting the entire fleet — turning a recoverable dependency outage into a
total one, with a thundering herd on recovery.

`/ready` checks the database (fatal) and Redis (degraded, non-fatal, since the
application is designed to survive its absence).

`/startup` exists so a slow boot is not mistaken for a hung process. Without it,
a boot exceeding the liveness `failureThreshold` produces a crash loop whose only
cause is that startup was slow.

`/health` names the service and version but no hostnames or dependency
addresses — it is one of the most-scanned URLs on any public service.

---

## Modules

`app/modules/` is **empty**. Stage 2 has not started.

`registry.py` discovers features: every direct subpackage is a feature, and
`router`, `models`, `handlers` and `tasks` are imported when present.

This replaced four hand-maintained import lists whose failure modes were
asymmetric — a missing router import gives an obvious 404, but a missing model
import produces a migration that looks fine and creates nothing, and a missing
handler import produces an event system where nothing listens.

Import errors are **never** swallowed: a typo in a model module would otherwise
present as a mysteriously missing table.

---

## Cross-cutting flows

### A request

```
uvicorn
  → RequestContextMiddleware   mint request/correlation IDs → context + scope
  → TrustedHostMiddleware      reject forged Host
  → CORSMiddleware             preflight; headers on errors too
  → AccessLogMiddleware        time it, emit metrics by route template
  → RateLimitMiddleware        Redis sliding window; returns 429
  → GZipMiddleware
  → router                     path, status, response_model
  → dependencies               session → claims → principal → tenant → permission
  → service                    business rules; commits
  → repository                 SQL, tenant-scoped by construction
  → PostgreSQL
```

Errors leave through the handlers in `core/exceptions.py`, producing one
envelope regardless of origin.

### A durable side effect

```
service                stage(session, event) inside the transaction
  → commit             business change and outbox row, atomically
  → OutboxRelay        claims with FOR UPDATE SKIP LOCKED
  → publisher          event bus or queue
  → worker             idempotent handler
```

### Authentication (Stage 2 wiring)

```
Authorization: Bearer <jwt>
  → get_current_claims     signature, exp, iss, aud, type — no I/O
  → get_current_principal  loader seam → exists? active? token version current?
  → get_current_tenant_id  membership re-checked against the database
  → require_permission     resolved server-side, never read from the token
```

The auth module supplies the loader via `set_principal_loader`. Core never
imports it.
