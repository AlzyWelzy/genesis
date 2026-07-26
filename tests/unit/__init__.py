"""Unit tests — pure logic, no I/O.

No database, no network, no filesystem, no clock. These must run in
milliseconds so they can be executed on every save.

Mirrors ``app/common`` and ``app/core``. Service tests live under
``tests/modules/`` alongside the feature they belong to.
"""
