"""Unified persistence errors for the Gateway data layer."""

from __future__ import annotations


class PersistenceError(Exception):
    """Base class for gateway persistence failures."""


class PersistenceConflictError(PersistenceError):
    """Unique constraint or optimistic concurrency conflict."""


class PersistenceUnavailableError(PersistenceError):
    """Database locked, connection failed, or otherwise unavailable."""
