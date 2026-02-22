"""Filesystem interception — delegates to monkeyfs."""

from monkeyfs import (
    FileSystem,
    MemoryFS,
    install,
    suspend_fs_interception,
    use_fs,
)
from monkeyfs.context import current_fs

__all__ = [
    "FileSystem",
    "MemoryFS",
    "current_fs",
    "install",
    "suspend_fs_interception",
    "use_fs",
]
