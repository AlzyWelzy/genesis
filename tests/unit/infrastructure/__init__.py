"""Unit tests for :mod:`app.infrastructure`.

Covers the parts that need no external system: the in-memory fakes, template
rendering, cursor encoding and the metrics seam. Anything requiring a real
PostgreSQL or Redis lives in ``tests/integration/``.
"""
