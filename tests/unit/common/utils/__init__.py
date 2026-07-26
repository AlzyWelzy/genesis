"""Unit tests for :mod:`app.common.utils`.

One test module per utility module: ``test_datetime.py``, ``test_strings.py``,
``test_files.py``, ``test_crypto.py``, ``test_collections.py``.

These functions are pure, so they deserve dense edge-case coverage — empty
inputs, Unicode, boundary sizes, and for the crypto helpers, that comparison is
constant-time by construction rather than by luck.
"""
