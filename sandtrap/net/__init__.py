"""Network interception for sandtrap sandbox."""

from .context import allow_network, deny_network, network_allowed
from .patch import install

__all__ = ["allow_network", "deny_network", "install", "network_allowed"]
