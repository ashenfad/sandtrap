"""Subprocess-backed sandbox with kernel-level isolation."""

from .sandbox import ProcessSandbox
from .spool import SpoolClosedError, SpooledNamespace

__all__ = ["ProcessSandbox", "SpoolClosedError", "SpooledNamespace"]
