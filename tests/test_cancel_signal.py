"""A process worker's cancel is set from a signal handler, on the thread
that also clears it at the start of every execution."""

import signal
import subprocess
import sys

import pytest

# The main thread clears the flag as fast as it can while SIGUSR1 sets it
# from a handler; a flag with a lock in both deadlocks the moment a signal
# lands inside clear().
SCRIPT = """
import os, signal, threading, time
from sandtrap.gates import CancelFlag

flag = CancelFlag()
signal.signal(signal.SIGUSR1, lambda *_: flag.set())
pid = os.getpid()
done = threading.Event()

def fire():
    while not done.is_set():
        os.kill(pid, signal.SIGUSR1)
        time.sleep(0.002)

threading.Thread(target=fire, daemon=True).start()
for _ in range(1_000_000):
    flag.clear()
done.set()
"""


@pytest.mark.skipif(not hasattr(signal, "SIGUSR1"), reason="no SIGUSR1 here")
def test_a_cancel_signalled_during_clear_cannot_deadlock():
    subprocess.run([sys.executable, "-c", SCRIPT], timeout=60, check=True)
