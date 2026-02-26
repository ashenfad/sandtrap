"""seccomp syscall filtering (Linux).

Installs an allowlist of syscalls needed for Python file I/O while
blocking process spawning and network operations.  Requires either
the ``seccomp`` or ``pyseccomp`` PyPI package (both need ``libseccomp``
at runtime).  Graceful no-op if unavailable.
"""

from __future__ import annotations

import sys
import warnings

# Syscalls needed by the Python runtime + file I/O + pipe IPC.
_ALLOWED_SYSCALLS = [
    # Process exit
    "exit",
    "exit_group",
    # Signals
    "rt_sigaction",
    "rt_sigreturn",
    "rt_sigprocmask",
    "sigaltstack",
    # Memory management (allocator, GC, mmap)
    "brk",
    "mmap",
    "munmap",
    "mprotect",
    "mremap",
    "madvise",
    # File I/O
    "read",
    "write",
    "readv",
    "writev",
    "open",
    "openat",
    "openat2",
    "close",
    "close_range",
    "lseek",
    "pread64",
    "pwrite64",
    # File metadata
    "stat",
    "fstat",
    "lstat",
    "newfstatat",
    "statx",
    "access",
    "faccessat",
    "faccessat2",
    # Directory traversal
    "getcwd",
    "getdents",
    "getdents64",
    "chdir",
    "fchdir",
    "readlink",
    "readlinkat",
    # File creation/modification
    "mkdir",
    "mkdirat",
    "unlink",
    "unlinkat",
    "rename",
    "renameat",
    "renameat2",
    "truncate",
    "ftruncate",
    "chmod",
    "fchmod",
    "fchmodat",
    "symlink",
    "symlinkat",
    "link",
    "linkat",
    # Pipe/IPC (for multiprocessing.Pipe)
    "pipe",
    "pipe2",
    "poll",
    "ppoll",
    "select",
    "pselect6",
    "epoll_create",
    "epoll_create1",
    "epoll_ctl",
    "epoll_wait",
    "epoll_pwait",
    "sendmsg",
    "recvmsg",
    "sendto",
    "recvfrom",
    # Process/thread basics
    "getpid",
    "getppid",
    "gettid",
    "getuid",
    "getgid",
    "geteuid",
    "getegid",
    "getrlimit",
    "setrlimit",
    "prlimit64",
    "getrusage",
    "clock_gettime",
    "clock_getres",
    "gettimeofday",
    "nanosleep",
    "clock_nanosleep",
    "futex",
    "set_robust_list",
    "get_robust_list",
    "arch_prctl",
    "prctl",
    "set_tid_address",
    "rseq",
    # File descriptor management
    "dup",
    "dup2",
    "dup3",
    "fcntl",
    "ioctl",
    # System info
    "sysinfo",
    "uname",
    "getrandom",
    # Signals between threads
    "tgkill",
    "kill",
    # Threading (Python threads, GC)
    "clone",
    "clone3",
    "wait4",
    "waitid",
    # mmap for anonymous mappings (Python allocator)
    "memfd_create",
]


def available() -> bool:
    """Return True if seccomp can be applied on this system."""
    if sys.platform != "linux":
        return False
    try:
        import seccomp  # noqa: F401

        return True
    except ImportError:
        pass
    try:
        import pyseccomp  # noqa: F401

        return True
    except ImportError:
        return False


# Syscalls needed for network I/O.  Only added when allow_network=True.
_NETWORK_SYSCALLS = [
    "socket",
    "connect",
    "bind",
    "listen",
    "accept",
    "accept4",
    "getsockopt",
    "setsockopt",
    "getsockname",
    "getpeername",
    "shutdown",
    "socketpair",
]


def apply(*, allow_network: bool = False) -> bool:
    """Install an allowlist seccomp filter.

    Parameters
    ----------
    allow_network:
        If True, include network syscalls in the allowlist.

    Returns True if the filter was applied, False if unavailable.
    """
    if sys.platform != "linux":
        return False

    SyscallFilter = None
    ALLOW = None
    KILL_PROCESS = None

    try:
        from seccomp import ALLOW, KILL_PROCESS, SyscallFilter
    except ImportError:
        try:
            from pyseccomp import ALLOW, KILL_PROCESS, SyscallFilter
        except ImportError:
            warnings.warn(
                "Neither seccomp nor pyseccomp installed; "
                "seccomp filtering not applied",
                RuntimeWarning,
                stacklevel=2,
            )
            return False

    try:
        syscalls = list(_ALLOWED_SYSCALLS)
        if allow_network:
            syscalls.extend(_NETWORK_SYSCALLS)

        f = SyscallFilter(defaction=KILL_PROCESS)
        for name in syscalls:
            try:
                f.add_rule(ALLOW, name)
            except OSError:
                # Syscall may not exist on this architecture/kernel version
                pass
        f.load()
        return True
    except OSError as exc:
        warnings.warn(
            f"seccomp filter not applied: {exc}",
            RuntimeWarning,
            stacklevel=2,
        )
        return False
