"""Pure utility functions.

Grouped by subject: ``datetime``, ``strings``, ``files``, ``crypto``,
``collections``.

Every function here must be pure and synchronous unless it is inherently
streaming: no configuration reads, no I/O, no logging, no global state. That is
what makes them trivially testable and safe to call from any layer.

Import the module, not the function (``from app.common.utils import strings``),
so call sites read as ``strings.slugify(...)`` — several of these module names
shadow standard-library ones.
"""
