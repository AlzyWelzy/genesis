"""Metrics, tracing and error reporting seams.

Why this package exists now, mostly unimplemented
-------------------------------------------------
Instrumentation is the classic thing teams defer and then cannot retrofit:
adding spans and metrics later means touching every layer at once, during the
incident that proved they were needed.

The compromise here is to build the *seams* — a metrics interface with a no-op
default, a tracing initialiser, an error-reporting hook — and wire them into the
lifespan from day one. Turning them on becomes a configuration change and a
dependency install, not an architectural change.

Everything here is off by default and costs nothing when disabled.
"""
