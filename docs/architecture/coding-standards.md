# Coding standards

Most of this is enforced by `ruff` and `ty` — see `pyproject.toml`. This
document covers what a linter cannot check.

## Enforced automatically

```bash
uv run ruff check --fix .   # 33 rule families
uv run ruff format .
uv run ty check
```

A lint rule is a code review comment that never forgets and costs nothing. If a
rule is wrong for the project, change it in `pyproject.toml` with a reason —
never scatter `# noqa` to work around it. Every `noqa` in this codebase carries
a justification after the code:

```python
host = ("0.0.0.0",)  # noqa: S104 - development server only
```

## Typing

Every function is annotated, arguments and return. `ANN` enforces it.

Modern syntax throughout — the target is 3.14, so there is no reason for the
old spellings:

```python
def find(ids: list[UUID]) -> Invoice | None: ...  # not List, not Optional


type InvoiceId = UUID  # not TypeAlias


def first[T](items: list[T]) -> T | None: ...  # not TypeVar
```

`Any` is a smell outside generic plumbing. If a payload is genuinely
unstructured, say `dict[str, Any]` and validate it at the boundary.

## Docstrings

Google convention, enforced by `D`. Every public module, class and function.

The module docstring answers **why the file exists**, not what it contains — a
list of contents is visible from the code. State the problem the module solves
and the decision it encodes:

```python
"""Distributed rate limiting.

Why this file exists
--------------------
An in-process rate limiter is close to useless behind a load balancer: with
four replicas the effective limit is four times the configured one, and it
resets on every deploy. The counter has to live somewhere shared.
"""
```

Function docstrings document arguments, returns and raises when they are not
obvious. Skip `Args:` when the signature already says everything.

**Comment the surprising, not the obvious.** `# increment the counter` above
`counter += 1` is noise. The valuable comment explains a constraint the reader
cannot infer:

```python
# Use a unique member per request: two requests in the same millisecond would
# otherwise collide into one sorted-set entry and undercount.
```

## Naming

| Kind | Style |
| --- | --- |
| Modules, functions, variables | `snake_case` |
| Classes | `PascalCase` |
| Constants | `UPPER_SNAKE_CASE`, annotated `Final` |
| Private | leading underscore |
| Booleans | `is_` / `has_` / `should_` |

Say what a thing is, not what type it is: `invoices`, not `invoice_list`.
Avoid abbreviations except the universal ones (`id`, `url`, `db`).

Async functions are named for what they do, not that they are async — the
`await` at the call site already says so.

## Async

Never call a blocking function in an async path. It blocks the entire event
loop, so one slow call stalls every concurrent request in the process — which
presents as "the whole service got slow", not "one endpoint got slow".

```python
# Wrong: blocks the loop
response = requests.get(url)
time.sleep(1)
data = open(path).read()

# Right
response = await client.get(url)
await asyncio.sleep(1)
data = await asyncio.to_thread(path.read_text)
```

`ASYNC` catches the common cases. It cannot catch a blocking third-party
library, so check before adopting one — `boto3` and `smtplib` are blocking,
which is why this project uses `aioboto3` and `aiosmtplib`.

## Errors

Raise domain errors from `app.core.exceptions`; never `HTTPException` outside a
router. See [`error-handling.md`](error-handling.md).

Never catch an exception you cannot handle. `except Exception: pass` converts a
bug into corrupted state discovered days later. If you must catch broadly, log
it and say why in a `noqa` comment:

```python
except Exception:  # noqa: BLE001 - any failure means "not ready"
    return False
```

## Imports

Absolute, always — `TID252` bans relative imports. They make a file's
dependencies unreadable and break silently when a module moves.

Import modules, not names, when the module name gives useful context:

```python
from app.common.utils import strings

strings.slugify(title)  # clearer than a bare slugify()
```

Several utility modules shadow stdlib names (`datetime`, `collections`), which
makes this more than a style preference.

## Functions

Small, single-purpose, and named for what they return. If you need "and" to
describe it, split it.

Keyword-only arguments for anything a caller could get in the wrong order:

```python
def create_token(subject: str, *, token_type: str, expires_in: timedelta) -> str:
```

`create_token(user_id, "access", delta)` is a bug waiting to happen; the
keyword form cannot be misordered.

Never a mutable default (`B006`). Use `None` and build inside.

## Files

Split at roughly 400 lines — a `service.py` becomes a `service/` package with
the same public import path. Never split earlier for its own sake; a 300-line
file that reads top to bottom beats four files that must be read together.

## Pull requests

- One logical change. A refactor and a feature in one diff cannot be reviewed
  or reverted independently.
- Tests for new behaviour; a regression test for a fix, failing before it.
- Documentation updated **in the same commit**. Documentation corrected in a
  follow-up is documentation that stays wrong for a week.
- CI green — `ruff`, `ty` and `pytest` all pass before review, not after.
