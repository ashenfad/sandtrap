"""Filesystem interception — delegates to monkeyfs."""

from monkeyfs import (
    FileSystem,
    IsolatedFS,
    VirtualFS,
    current_fs,
    patch,
    suspend,
)

__all__ = [
    "FileSystem",
    "IsolatedFS",
    "VirtualFS",
    "current_fs",
    "patch",
    "suspend",
]
