"""The import path an earlier release pickled ``ModuleRef`` under.

Wrapped mode and everything that lived here with it are gone; this module
exists for one reason. A pickle records the module path of every class it
holds, and a namespace an embedder persisted under an earlier release names
``sandtrap.wrappers.ModuleRef`` -- the class now defined in
``sandtrap.sandbox``. Without this alias ``pickle.loads()`` raises
``ModuleNotFoundError`` before the sandbox can resolve the reference, and
stored state that was valid becomes unreadable on upgrade. Import
``ModuleRef`` from :mod:`sandtrap.sandbox`; nothing new should name this
module.
"""

from .sandbox import ModuleRef

__all__ = ["ModuleRef"]
