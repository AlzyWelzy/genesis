"""Outbound email.

``client`` defines the message model and provider contract; ``providers``
implements the transports and selects one from configuration; ``templates``
holds the bodies.

Email is always sent through the queue, never inline in a request — a slow
provider must not become a slow API.
"""
