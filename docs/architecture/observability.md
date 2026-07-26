# Observability

Three signals, three different questions. Confusing them produces dashboards
nobody trusts.

| Signal | Question | Status |
| --- | --- | --- |
| **Logs** | What happened to *this* request? | Implemented |
| **Metrics** | What is happening to the *system*? | Seam built, off |
| **Traces** | *Where* did this request spend its time? | Seam built, off |

Metrics and tracing are off by default and cost nothing when disabled. The seams
exist from day one because retrofitting instrumentation means touching every
layer at once — usually during the incident that proved it was needed.

## Logs

Structured, one JSON object per line in any environment with an aggregator; a
readable renderer for a terminal. The format is configuration, never a call-site
choice.

**Always pass context via `extra`, never by formatting it into the message:**

```python
logger.info("Invoice paid", extra={"invoice_id": str(invoice.id)})  # right
logger.info(f"Invoice {invoice.id} paid")  # wrong
```

A message that varies per call cannot be grouped, counted or alerted on. The
first form gives "how many `Invoice paid` events today, filtered by tenant"; the
second gives a million distinct strings.

**Correlation is automatic.** `ContextFilter` attaches `request_id`,
`correlation_id` and `tenant_id` to every record. That is the difference between
"an error happened" and "here are the fourteen lines belonging to the request
that failed" — and it costs the caller nothing, which is why it is a filter and
not a convention.

**Redaction is a safety net, not a licence.** Anything whose key matches
`SENSITIVE_FIELD_NAMES` is masked recursively before writing. It catches the
accidental `extra={"payload": body}`; it does not make logging secrets safe.

**Levels:** DEBUG for development detail, INFO for business events and 4xx,
WARNING for degraded-but-working (rate limiter failing open), ERROR for 5xx and
anything needing a human, `logger.exception` for unhandled errors.

Logging a 404 at ERROR is how alert fatigue starts, and alert fatigue is how a
real 500 gets ignored.

## Metrics

`Metrics` is a protocol with a `NoOpMetrics` default, so instrumentation is
written once and left in place whether or not a backend is configured. That is
what stops instrumentation being stripped out "because we don't use it yet".

**Cardinality is the rule that matters.** Never label a metric with an unbounded
value — user IDs, tenant IDs, request IDs, raw URL paths. Each distinct value is
a new time series; a metric labelled with a user ID on a million-user system is
a million series, and it takes down the metrics backend long before the
application. Use the *route template* (`/invoices/{invoice_id}`), never the
resolved path.

Instrument the four signals worth alerting on before anything else: request
rate, error rate, duration by route, and saturation (pool in use, queue depth).
Do it in middleware, not per endpoint, so coverage cannot drift as endpoints are
added.

## Traces

Once a request touches the API, PostgreSQL, Redis, object storage and a third
party, "the request was slow" stops being useful. A trace shows which of the
five, and in a fan-out, what actually ran in parallel.

Auto-instrumentation covers the boundaries that matter (FastAPI, SQLAlchemy,
Redis, httpx). Hand-written spans are only for business operations worth naming.

**Sampling:** full sampling is affordable in development and ruinous in
production — at scale the tracing bill exceeds the rest of the stack. The
default is 10%, with the caveat that the interesting requests (errors, slow
outliers) are exactly the ones a naive sampler drops. Tail-based sampling, which
decides after the fact, is the fix.

## Health

Three endpoints, three different consumers. The distinction causes outages when
it is missed:

- **`/live`** — is the process wedged? Failure gets the container **restarted**,
  so it checks *nothing external*. A liveness probe that checked the database
  would restart the entire fleet during a database blip, converting a
  recoverable dependency outage into a total one plus a thundering herd on
  recovery.
- **`/ready`** — can this instance serve traffic? Failure removes it from the
  load balancer but leaves it running. This is where dependencies are checked.
- **`/health`** — human-readable summary for dashboards. Never wire an
  orchestrator to it.

Redis is non-fatal in readiness: cache and rate limiting both degrade rather
than fail, so an instance without Redis can still serve requests. Marking it
fatal would take the service down for a component it is designed to survive.

Probe paths are excluded from the access log and the rate limiter. They fire
every few seconds per replica and would otherwise dominate log volume — and a
throttled probe gets healthy pods killed.

## What to add next

1. Ship logs to an aggregator and confirm `request_id` is indexed. Everything
   else is less useful until searching by request ID works.
2. Turn on metrics; alert on error rate and p99 duration.
3. Add tracing when a request first crosses a service boundary.
4. Add an error tracker (`OBSERVABILITY__SENTRY_DSN` is already declared).
