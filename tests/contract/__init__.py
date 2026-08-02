"""Tests that lock the promises made to clients.

Why this directory exists
-------------------------
Some of this codebase's guarantees are not about behaviour at all — they are
about *stability*. ``AGENTS.md`` and ``docs/architecture/error-handling.md`` both
state that an error ``code`` is "part of the public API contract: adding one is
safe, changing one is a breaking change".

Nothing enforced that. A rename during an otherwise sensible refactor — say
``not_found`` to ``resource_not_found`` — would have passed every test, every
lint and every type check, then silently broken every client branching on it.
The application would be *more* correct by its own tests and less usable by
everyone consuming it.

That is the same shape as the other bugs this project has shipped: a documented
guarantee with no mechanism behind it. The mechanism is here.

How this works
--------------
The manifests below are **locked values, not derived ones**. Deriving them from
the code would make the test tautological — it would pass whatever the code
happened to say, which is exactly what it must not do.

When one of these fails, that is the point. Decide deliberately:

* **Adding** a code or field is backwards compatible. Add it to the manifest in
  the same commit.
* **Renaming or removing** one is a breaking change. Version the API or keep the
  old name as well; do not simply update the manifest to make the test quiet.
"""
