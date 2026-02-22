"""Filesystem interception — delegates to monkeyfs."""

from monkeyfs import (
    FileSystem,
    VirtualFS,
    install,
    suspend_fs_interception,
    use_fs,
)
from monkeyfs.context import current_fs

__all__ = [
    "FileSystem",
    "VirtualFS",
    "current_fs",
    "install",
    "suspend_fs_interception",
    "use_fs",
]
