"""Kernel Bisection Tool - Automated kernel regression testing."""

try:
    from kbisect._version import __version__
except ImportError:
    __version__ = "0.0.0+unknown"

__author__ = "Jan Jurca"

from kbisect.master.bisect_master import BisectConfig, BisectMaster
from kbisect.master.state_manager import StateManager


__all__ = [
    "BisectConfig",
    "BisectMaster",
    "StateManager",
    "__version__",
]
