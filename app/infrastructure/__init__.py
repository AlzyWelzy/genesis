"""Adapters to external systems.

Everything that speaks to something outside the process — PostgreSQL, Redis,
object storage, SMTP, the job queue — lives here, behind an interface.

The rule this layer exists to enforce: business logic depends on *abstractions*
(``StorageProvider``, ``Cache``, ``EmailProvider``), never on a concrete client.
That is what makes a service testable without a network and what keeps a
provider swap from touching feature code.
"""
