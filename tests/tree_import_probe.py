"""A granted module that notes which process imported it, for the test
that a worker's supervisor never imports the worker's grants."""

import os

imported_in = os.getpid()


def where():
    return os.getpid(), imported_in
