"""Reusable code shared across features.

Pagination, response envelopes, shared enums, type aliases and pure utility
functions.

Two rules keep this package from decaying into a junk drawer:

1. **No business logic.** Nothing here may know what a customer or an invoice
   is.
2. **No inward imports.** ``common`` must not import from :mod:`app.modules`,
   and only from :mod:`app.core` for configuration and constants.

Code belongs here once a *third* feature needs it — not in anticipation.
"""
