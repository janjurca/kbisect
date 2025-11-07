"""Console collectors for capturing boot logs during kernel testing."""

from typing import Optional

from kbisect.collectors.base import ConsoleCollector
from kbisect.collectors.conserver import ConserverCollector
from kbisect.collectors.ipmi_sol import IPMISOLCollector

__all__ = [
    "ConsoleCollector",
    "ConserverCollector",
    "IPMISOLCollector",
]


def create_console_collector(
    hostname: str,
    ipmi_host: Optional[str] = None,
    ipmi_user: Optional[str] = None,
    ipmi_password: Optional[str] = None,
) -> ConsoleCollector:
    """
    Create appropriate console collector based on available tools.

    Tries conserver first, falls back to IPMI SOL if conserver is not available.

    Args:
        hostname: Target machine hostname for conserver
        ipmi_host: IPMI hostname (required for IPMI SOL fallback)
        ipmi_user: IPMI username (required for IPMI SOL fallback)
        ipmi_password: IPMI password (required for IPMI SOL fallback)

    Returns:
        ConsoleCollector instance (ConserverCollector or IPMISOLCollector)
    """
    # Try conserver first
    try:
        # Test if conserver is available
        import subprocess
        result = subprocess.run(
            ["which", "console"],
            capture_output=True,
            timeout=5,
        )
        if result.returncode == 0:
            return ConserverCollector(hostname)
    except Exception:
        pass

    # Fall back to IPMI SOL
    if not all([ipmi_host, ipmi_user, ipmi_password]):
        raise RuntimeError(
            "Conserver not available and IPMI credentials not provided. "
            "Cannot create console collector."
        )

    return IPMISOLCollector(
        hostname=hostname,
        ipmi_host=ipmi_host,  # type: ignore
        ipmi_user=ipmi_user,  # type: ignore
        ipmi_password=ipmi_password,  # type: ignore
    )
