"""Genesis — application package.

Layers
------
* :mod:`app.core` — framework wiring and configuration.
* :mod:`app.infrastructure` — adapters to external systems.
* :mod:`app.modules` — business features.
* :mod:`app.common` — reusable, dependency-free helpers.
* :mod:`app.events` — domain events and the in-process bus.

Dependencies point inward: ``core`` and ``common`` never import ``modules``.
See ``docs/architecture.md``.
"""

__version__ = "0.1.0"
