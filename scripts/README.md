# Scripts

Operational entry points: one-off tasks, maintenance jobs and developer tooling
that are not part of the HTTP API.

## Contents

| Script | Purpose |
|---|---|
| [`generate_keys.py`](generate_keys.py) | Generate the Ed25519 JWT signing key pair |

## Rules

- **Reuse the application layers.** A script must call the same services the API
  calls — through `session_scope()` from
  [`app/infrastructure/database/session.py`](../app/infrastructure/database/session.py),
  not with its own connection and hand-written SQL. A script that reimplements
  a business rule will drift from the API version, and the drift will be
  discovered as corrupted data.
- **Never write DDL here.** Schema changes are migrations. Always.
- **Idempotent where possible.** Scripts get re-run after a partial failure.
- **`--dry-run` for anything destructive**, and make it the default when the
  script can modify or delete production data.
- **Log what happened**, including counts. "Updated 4,182 rows" is the
  difference between a confident deploy and an anxious one.
- **Argparse, not positional guesswork.** A script run at 3am under pressure
  needs `--help` to be honest.

## Future entry points

- `worker.py` — the background job consumer (see
  [`app/infrastructure/queue/`](../app/infrastructure/queue/)).
- `seed.py` — populate a local database with development data. Development
  only; it must refuse to run when `APP__ENVIRONMENT=production`.
- `healthcheck.py` — container health probe.
