"""Hypothesis profiles for the property suite.

Three profiles, because the right number of examples differs by situation:

``dev`` (default)
    Small and fast, so running the whole suite while working stays quick and
    nobody is tempted to skip it.

``ci``
    Ten times the examples. CI has the time, and it is the gate that matters.

``thorough``
    A deliberate hunt — ``--hypothesis-profile=thorough`` or
    ``HYPOTHESIS_PROFILE=thorough``. Worth running before a release, or when a
    bug suggests a whole class of input was never explored.

``deadline=None`` throughout: these properties call real code whose runtime
varies with the generated input, so a per-example deadline turns that variance
into flaky failures that say nothing about correctness.
"""

import os

from hypothesis import HealthCheck, settings

# Spelled out per profile rather than unpacked from a shared dict: the shared
# dict reads as less duplication and costs the type checker every keyword.
settings.register_profile(
    "dev",
    max_examples=50,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
settings.register_profile(
    "ci",
    max_examples=500,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
settings.register_profile(
    "thorough",
    max_examples=5000,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)

settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "dev"))
