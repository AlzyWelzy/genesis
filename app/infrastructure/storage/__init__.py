"""Object storage.

``client`` defines the provider contract; ``providers`` implements it for the
local filesystem and for S3-compatible services, and selects one from
configuration. Features depend on the contract only.
"""
