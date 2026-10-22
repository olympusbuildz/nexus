"""Backend protocol and registry."""

from nexus.backends.registry import active_backend_name, build_backend, describe_catalog

__all__ = ["active_backend_name", "build_backend", "describe_catalog"]
