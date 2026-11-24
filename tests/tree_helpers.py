"""A granted module that starts processes from inside a worker, for tests
of ``supervise_tree``."""

import os
import subprocess
import sys


def spawn(seconds, *, ignore_term=False, detach=False):
    """Start a helper that sleeps for ``seconds``, and return its pid once
    it is ready. ``ignore_term`` has it outlast SIGTERM; ``detach`` has it
    double-fork into a session of its own, so its parent exits at once."""
    lines = ["import os, signal, time"]
    if ignore_term:
        lines.append("signal.signal(signal.SIGTERM, signal.SIG_IGN)")
    if detach:
        lines += ["if os.fork():", "    os._exit(0)", "os.setsid()"]
    lines += ["print(os.getpid(), flush=True)", f"time.sleep({seconds})"]
    proc = subprocess.Popen(
        [sys.executable, "-c", "\n".join(lines)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    pid = int(proc.stdout.readline())
    proc.stdout.close()
    return pid


def me():
    return os.getpid()


def kill_supervisor():
    """Kill this worker's parent, its supervisor, then stay busy."""
    import signal
    import time

    os.kill(os.getppid(), signal.SIGKILL)
    while True:
        time.sleep(0.05)
