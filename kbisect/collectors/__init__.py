"""Console collectors for capturing boot logs during kernel testing."""

from typing import Optional

from kbisect.collectors.base import ConsoleCollector
from kbisect.collectors.conserver import ConserverCollector
from kbisect.collectors.ipmi_sol import IPMISOLCollector


__all__ = [
    "ConserverCollector",
    "ConsoleCollector",
    "IPMISOLCollector",
]


def create_console_collector(
    collector_type: str = "auto",
    hostname: Optional[str] = None,
    ipmi_host: Optional[str] = None,
    ipmi_user: Optional[str] = None,
    ipmi_password: Optional[str] = None,
) -> ConsoleCollector:
    """
    Create appropriate console collector based on type and available tools.

    Args:
        collector_type: Type of collector ("conserver", "ipmi", or "auto")
        hostname: Target machine hostname for conserver
        ipmi_host: IPMI hostname (required for IPMI SOL)
        ipmi_user: IPMI username (required for IPMI SOL)
        ipmi_password: IPMI password (required for IPMI SOL)

    Returns:
        ConsoleCollector instance (ConserverCollector or IPMISOLCollector)

    Raises:
        RuntimeError: If requested collector type is not available
    """
    if collector_type == "conserver":
        # Explicitly requested conserver
        if not hostname:
            raise RuntimeError("Hostname required for Conserver collector")
        return ConserverCollector(hostname)

    elif collector_type == "ipmi":
        # Explicitly requested IPMI SOL
        if not (ipmi_host and ipmi_user is not None and ipmi_password is not None):
            raise RuntimeError(
                "IPMI host, user, and password required for IPMI SOL collector"
            )
        return IPMISOLCollector(
            hostname=hostname or ipmi_host,
            ipmi_host=ipmi_host,  # type: ignore
            ipmi_user=ipmi_user,  # type: ignore
            ipmi_password=ipmi_password,  # type: ignore
        )

    elif collector_type == "auto":
        # Auto-detect: Try conserver first, fall back to IPMI SOL
        if not hostname:
            raise RuntimeError("Hostname required for console collector")

        # Try conserver first
        try:
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
        if not (ipmi_host and ipmi_user is not None and ipmi_password is not None):
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

    else:
        raise RuntimeError(f"Unknown collector type: {collector_type}")
