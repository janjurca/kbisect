#!/usr/bin/env python3
"""Factory for creating power controller instances.

Provides centralized power controller instantiation based on host configuration.
"""

from typing import Optional

from kbisect.config.config import HostConfig
from kbisect.power.base import PowerController


def create_power_controller(
    host_config: HostConfig,
    ssh_connect_timeout: int = 15,
) -> Optional[PowerController]:
    """Create power controller instance based on host configuration.

    Args:
        host_config: Host configuration with power control settings
        ssh_connect_timeout: SSH connection timeout in seconds

    Returns:
        PowerController instance or None if no power control configured

    Raises:
        ValueError: If power control type is invalid or required credentials missing
    """
    power_type = host_config.power_control_type

    # No power control configured
    if power_type is None:
        return None

    # IPMI power control
    if power_type == "ipmi":
        # Validate required credentials
        if not host_config.ipmi_host:
            raise ValueError(f"IPMI configured for {host_config.hostname} but ipmi_host is missing")
        if host_config.ipmi_user is None:
            raise ValueError(f"IPMI configured for {host_config.hostname} but ipmi_user is missing")
        if host_config.ipmi_password is None:
            raise ValueError(f"IPMI configured for {host_config.hostname} but ipmi_password is missing")

        from kbisect.power import IPMIController

        return IPMIController(
            host_config.ipmi_host,
            host_config.ipmi_user,
            host_config.ipmi_password,
            ssh_host=host_config.hostname,
            ssh_connect_timeout=ssh_connect_timeout,
        )

    # Beaker power control
    if power_type == "beaker":
        from kbisect.power import BeakerController

        return BeakerController(host_config.hostname, ssh_connect_timeout)

    # Unknown power control type
    raise ValueError(
        f"Unknown power control type '{power_type}' for {host_config.hostname}. "
        f"Valid types: 'ipmi', 'beaker', or None"
    )
