"""Filesystem interception — delegates to monkeyfs."""

from monkeyfs import (
    FileSystem,
    VirtualFS,
    current_fs,
    install,
    patch,
    suspend_fs_interception,
)

__all__ = [
    "FileSystem",
    "VirtualFS",
    "current_fs",
    "install",
    "suspend_fs_interception",
    "patch",
]
