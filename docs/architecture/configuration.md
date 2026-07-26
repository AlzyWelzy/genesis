# Configuration

## The rule

**Every setting is declared in `app/core/settings.py`, built once in
`app/core/config.py`, and read from `settings` everywhere else.**

Nothing else calls `os.environ`. A value read from the environment deep inside a
service is invisible, untyped and impossible to validate at boot.

```python
from app.core.config import settings

engine = create_async_engine(str(settings.database.url))
```

## Structure

Nested models, one block per subsystem, populated with a `__` delimiter:

```text
APP__ENVIRONMENT=production
DATABASE__URL=postgresql+asyncpg://user:pass@host:5432/genesis
JWT__ACCESS_TOKEN_EXPIRE_MINUTES=15
RATE_LIMIT__ENABLED=true
```

Nesting rather than a flat namespace because each subsystem owns its block:
`app.infrastructure.database.session` reads `settings.database` and nothing
else, so the blast radius of a change is visible.

Only `DATABASE__URL` is required. Everything else has a default that is safe for
local development.

## Precedence

Highest wins:

1. **Process environment** — what an orchestrator injects.
2. **`.env.<environment>`** — environment-specific overlay, if present.
3. **`.env`** — the developer's local file.
4. **`secrets_dir`** — one file per secret.

## Secrets

The application is already able to move off `.env` files without a code change.
`SettingsConfigDict(secrets_dir=...)` reads one file per setting, which is the
convention used by both Docker secrets and Kubernetes secret volumes:

```text
/run/secrets/
  database__url
  jwt__private_key_path
  email__smtp_password
```

Set `SECRETS_DIR=/run/secrets` and the same fields are populated from files
instead of the environment. Nothing in the application changes.

Environment variables are the pragmatic default but are not actually private:
they appear in `docker inspect`, in process listings, in crash dumps and in
child processes. Mounted files are readable only by the process user and never
appear in a container's metadata.

Fields holding credentials are typed `SecretStr`, so they render as `**********`
in logs and tracebacks rather than in cleartext.

### Rules

- **Never commit a real secret.** `.env` and `keys/` are gitignored;
  `.env.example` documents the keys with no values.
- **Every environment gets its own credentials.** A key shared between staging
  and production means a staging token authenticates against production.
- **Rotate on exposure, and assume git history is forever.** A secret committed
  once is compromised even after the commit is reverted.

## Production safety

`Settings` carries a validator that refuses to build an unsafe production
configuration. Each check is a real incident class:

| Check | Why |
| --- | --- |
| `APP__DEBUG` must be false | A debug traceback leaks source, paths and often credentials |
| `APP__CORS_ORIGINS` must not contain `*` | Any site could make authenticated requests on a user's behalf |
| `APP__TRUSTED_HOSTS` must be set | Host-header poisoning of generated links and cache keys |
| `LOGGING__JSON_FORMAT` must be true | Unparseable logs are unsearchable during an incident |
| `LOGGING__LOG_REQUEST_BODY` must be false | Bodies contain passwords, tokens and personal data |
| `RATE_LIMIT__ENABLED` must be true | An unthrottled public API |
| `DATABASE__ECHO` must be false | Statement logging at production volume, with parameters |

All failures are reported together — fixing them one deploy at a time is a
guessing game.

Crashing at boot is the point. A misconfigured instance that refuses to start is
a failed deploy that halts a rolling release; one that starts is an exposure.

## Adding a setting

1. Add the field to the right nested model, with a type and a comment saying
   why it exists.
2. Give it a default that is safe locally, or make it required if there is no
   safe default.
3. Add it to `.env.example` with a representative value.
4. If it is unsafe in production, add a check to the validator.

Do not add a setting for something that never varies between environments —
that is a constant, and belongs in `app/common/constants.py`.

## In tests

Build `Settings` directly rather than touching the environment, so a
developer's local `.env` can never influence a test run:

```python
settings = Settings(
    app={"environment": "test"},
    database={"url": "postgresql+asyncpg://postgres:postgres@localhost/genesis_test"},
)
```
