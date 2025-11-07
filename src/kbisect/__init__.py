"""Kernel Bisection Tool - Automated kernel regression testing."""

try:
    from kbisect._version import __version__
except ImportError:
    __version__ = "0.0.0+unknown"

__author__ = "Jan Jurca"

from kbisect.core import BisectMaster
from kbisect.core.orchestrator import BisectConfig
from kbisect.persistence import StateManager


__all__ = [
    "BisectConfig",
    "BisectMaster",
    "StateManager",
    "__version__",
]
