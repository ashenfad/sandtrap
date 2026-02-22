"""Filesystem interception — delegates to monkeyfs."""

from monkeyfs import (
    FileSystem,
    VirtualFS,
    current_fs,
    install,
    patch,
    suspend,
)

__all__ = [
    "FileSystem",
    "VirtualFS",
    "current_fs",
    "install",
    "suspend",
    "patch",
]
