"""Operational endpoints.

Why this is not a feature module
--------------------------------
Health probes, key distribution and metrics are not business features: they have
no tenant, no authentication, no service layer, and they must keep answering
when the business features are broken. Putting them in :mod:`app.modules` would
mix operational surface with product surface and imply they follow the
router/service/repository pattern, which they deliberately do not.

They are also mounted differently. An orchestrator expects ``/health`` at the
root, not ``/api/v1/health``, and a liveness probe that 404s because the API
prefix changed will get every replica killed.
"""
