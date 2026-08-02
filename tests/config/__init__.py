"""Tests over the configuration space itself.

Why this directory exists
-------------------------
A setting is not code, so no amount of testing the code exercises it. The
production safety validator is the only thing standing between a plausible
environment file and an instance that boots into a silently broken state — and
until these tests existed, nothing checked the validator itself.

The gap that motivated it: a production deployment with ``STORAGE__PROVIDER=local``
and ``EMAIL__PROVIDER=console`` booted happily. Every upload went to
container-local disk, invisible to the other replicas and lost on the next
restart; every email was logged instead of sent, so no password reset ever
arrived. Neither produced an error, a failed request or a metric. The first
signal would have been a customer asking where their file went.

The validator's own docstring states the principle: "a misconfigured instance
that refuses to start is a failed deploy; one that starts is a breach." These
tests hold it to that, one setting at a time, and — just as importantly — check
that a *correct* production configuration still boots. A validator that rejects
everything is as useless as one that rejects nothing, and much easier to write by
accident.
"""
