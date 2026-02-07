"""Filesystem interception for sblite sandbox."""

from .context import current_fs, suspend_fs_interception, use_fs
from .patch import install
from .protocol import FileSystem

__all__ = [
    "FileSystem",
    "current_fs",
    "install",
    "suspend_fs_interception",
    "use_fs",
]
