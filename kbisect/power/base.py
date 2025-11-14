#!/usr/bin/env python3
"""Abstract base class for power controllers.

Provides interface for remote power management and boot control.
"""

from abc import ABC, abstractmethod
from enum import Enum
from typing import Optional


class PowerState(Enum):
    """Power states for remote systems."""

    ON = "on"
    OFF = "off"
    UNKNOWN = "unknown"


class BootDevice(Enum):
    """Boot devices for system configuration."""

    NONE = "none"
    PXE = "pxe"
    DISK = "disk"
    CDROM = "cdrom"
    BIOS = "bios"


class PowerControlError(Exception):
    """Base exception for power control errors."""


class PowerController(ABC):
    """Abstract base class for power controllers.

    Provides interface for controlling remote system power state and
    boot configuration. Implementations handle specific protocols
    (IPMI, PDU, cloud APIs, etc.).
    """

    @abstractmethod
    def get_power_status(self) -> PowerState:
        """Get current power status of the system.

        Returns:
            PowerState enum value (ON, OFF, or UNKNOWN)
        """

    @abstractmethod
    def power_on(self) -> bool:
        """Power on the system.

        Returns:
            True if successful, False otherwise
        """

    @abstractmethod
    def power_off(self, force: bool = False) -> bool:
        """Power off the system.

        Args:
            force: If True, force immediate power off (hard shutdown)
                   If False, attempt graceful shutdown first

        Returns:
            True if successful, False otherwise
        """

    @abstractmethod
    def power_cycle(self, wait_time: int = 10) -> bool:
        """Power cycle the system (off then on).

        Args:
            wait_time: Seconds to wait between power off and power on

        Returns:
            True if successful, False otherwise
        """

    @abstractmethod
    def reset(self) -> bool:
        """Reset the system (hard reset).

        Returns:
            True if successful, False otherwise
        """

    @abstractmethod
    def set_boot_device(self, device: BootDevice, persistent: bool = False) -> bool:
        """Set boot device for next boot or permanently.

        Args:
            device: Boot device to use
            persistent: If True, make boot device permanent. If False, one-time boot.

        Returns:
            True if successful, False otherwise
        """

    @abstractmethod
    def get_boot_device(self) -> Optional[str]:
        """Get current boot device configuration.

        Returns:
            Boot device string, or None if unable to determine
        """

    @abstractmethod
    def health_check(self) -> dict:
        """Perform health check on power controller.

        Validates that the power controller is properly configured and operational.
        Checks tool availability, credentials, and connectivity.

        Returns:
            Dictionary with health check results:
                - healthy (bool): Overall health status
                - tool_path (str, optional): Path to power control tool
                - power_status (str, optional): Current power status if queryable
                - error (str, optional): Error message if unhealthy
        """

    # Optional methods - implementations can provide these if supported
    def get_sensor_data(self) -> Optional[str]:
        """Get hardware sensor data (temperature, fans, voltage, etc.).

        Optional method. Implementations that support hardware monitoring
        should override this.

        Returns:
            Sensor data as string, or None if not supported
        """
        return None

    def activate_serial_console(self, duration: int = 30) -> Optional[str]:
        """Activate serial console and capture output.

        Optional method. Some power controllers provide serial console access.
        Note: For boot log collection, prefer using ConsoleCollector implementations.

        Args:
            duration: Capture duration in seconds

        Returns:
            Console output as string, or None if not supported
        """
        return None

    def emergency_recovery(self) -> bool:
        """Perform emergency recovery procedures.

        Optional method. Implementations can provide multi-stage recovery
        procedures for unresponsive systems.

        Returns:
            True if recovery successful, False otherwise
        """
        return False
