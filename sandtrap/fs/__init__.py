"""Filesystem interception — delegates to monkeyfs."""

from monkeyfs import (
    FileSystem,
    VirtualFS,
    current_fs,
    patch,
    suspend,
)

__all__ = [
    "FileSystem",
    "VirtualFS",
    "current_fs",
    "patch",
    "suspend",
]
