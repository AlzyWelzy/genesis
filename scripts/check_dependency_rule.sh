#!/usr/bin/env bash
# Enforce the architecture's load-bearing constraint.
#
# `core`, `common` and `infrastructure` must never import from `app.modules`.
# Dependencies point inward; an import the other way means business logic has
# ended up in the wrong layer.
#
# Checked mechanically because a rule enforced only by review is enforced
# intermittently, and because the first violation makes the second easier to
# justify. Shared by pre-commit and CI so both check exactly the same thing.
#
# See docs/architecture/dependency-rules.md

set -euo pipefail

INWARD_LAYERS=(app/core app/common app/infrastructure)

# Anchored to the start of a line (allowing indentation) so it matches real
# import statements only. Prose mentioning `app.modules`, and a commented-out
# example, are not dependencies.
#
# This is a textual scan, so it cannot tell code from a docstring: a doc example
# whose line begins with `from app.modules...` trips it. Write such examples as
# prose instead. Parsing the AST would fix this properly and is not worth the
# complexity for a guard that must stay obvious enough to trust.
IMPORT_PATTERN='^[[:space:]]*(from|import)[[:space:]]+app\.modules'

if violations=$(grep -rnE --include='*.py' "$IMPORT_PATTERN" \
    "${INWARD_LAYERS[@]}" 2>/dev/null); then
    echo "Dependency rule violated: inward layers must not import app.modules"
    echo
    echo "$violations"
    echo
    echo "See docs/architecture/dependency-rules.md"
    exit 1
fi

exit 0
