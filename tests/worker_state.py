"""A granted module with process state, for tests of what a worker keeps
between executions."""

import os

prepared_in = None


def prepare():
    global prepared_in
    prepared_in = os.getpid()
    return prepared_in


def state():
    return os.getpid(), prepared_in
