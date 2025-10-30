#!/usr/bin/env python3
"""IPMI Controller - Power management and recovery via IPMI.

Handles power control, serial console access, and boot device configuration.
"""

import logging
import os
import subprocess
import tempfile
import time
from enum import Enum
from typing import List, Optional, Tuple


logger = logging.getLogger(__name__)

# Constants
DEFAULT_IPMI_TIMEOUT = 30
POWER_CYCLE_WAIT_TIME = 10
SOL_DEACTIVATE_TIMEOUT = 5


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


class IPMIError(Exception):
    """Base exception for IPMI-related errors."""


class IPMITimeoutError(IPMIError):
    """Exception raised when IPMI command times out."""


class IPMICommandError(IPMIError):
    """Exception raised when IPMI command fails."""


class IPMIController:
    """IPMI controller for remote power management.

    Provides methods to control power state, boot devices, and access
    serial console via IPMI (Intelligent Platform Management Interface).

    Attributes:
        ipmi_host: Hostname or IP address of IPMI interface
        ipmi_user: IPMI username for authentication
        ipmi_password: IPMI password for authentication
    """

    def __init__(self, ipmi_host: str, ipmi_user: str, ipmi_password: str) -> None:
        """Initialize IPMI controller.

        Args:
            ipmi_host: IPMI interface hostname or IP address
            ipmi_user: IPMI username
            ipmi_password: IPMI password
        """
        self.ipmi_host = ipmi_host
        self.ipmi_user = ipmi_user
        self.ipmi_password = ipmi_password

    def _run_ipmi_command(
        self, args: List[str], timeout: int = DEFAULT_IPMI_TIMEOUT
    ) -> Tuple[int, str, str]:
        """Run ipmitool command.

        Args:
            args: Command arguments to pass to ipmitool
            timeout: Command timeout in seconds

        Returns:
            Tuple of (return_code, stdout, stderr)

        Raises:
            IPMITimeoutError: If command times out
            IPMICommandError: If command fails to execute
        """
        # Use temporary file for password to avoid exposing it in process list
        password_file = None
        try:
            # Create secure temporary file for password
            fd, password_file = tempfile.mkstemp(prefix="ipmi_", suffix=".tmp", text=True)
            try:
                os.write(fd, self.ipmi_password.encode("utf-8"))
            finally:
                os.close(fd)

            # Set restrictive permissions (only owner can read)
            os.chmod(password_file, 0o600)

            cmd = [
                "ipmitool",
                "-I",
                "lanplus",
                "-H",
                self.ipmi_host,
                "-U",
                self.ipmi_user,
                "-f",
                password_file,
            ] + args

            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout, check=False
            )
            return result.returncode, result.stdout, result.stderr

        except subprocess.TimeoutExpired as exc:
            msg = f"IPMI command timed out after {timeout}s"
            logger.error(msg)
            raise IPMITimeoutError(msg) from exc
        except Exception as exc:
            msg = f"IPMI command failed: {exc}"
            logger.error(msg)
            raise IPMICommandError(msg) from exc
        finally:
            # Clean up password file - CRITICAL for security
            if password_file and os.path.exists(password_file):
                # Try multiple times with increasing force
                deleted = False
                for attempt in range(3):
                    try:
                        # Ensure file is writable before deletion
                        os.chmod(password_file, 0o600)
                        os.unlink(password_file)
                        deleted = True
                        break
                    except OSError as e:
                        if attempt < 2:
                            time.sleep(0.1)  # Brief delay before retry
                        else:
                            # SECURITY WARNING: Password file could not be deleted
                            logger.error(
                                f"SECURITY: Failed to delete IPMI password file {password_file} "
                                f"after {attempt + 1} attempts: {e}. Manual cleanup required!"
                            )

                if not deleted:
                    # Last resort: try to zero out the file content
                    try:
                        with open(password_file, 'w') as f:
                            f.write('')
                        logger.warning(f"Zeroed out password file {password_file} but could not delete it")
                    except Exception as zero_exc:
                        logger.error(f"Failed to zero out password file: {zero_exc}")

    def get_power_status(self) -> PowerState:
        """Get current power status of the system.

        Returns:
            Current power state (ON, OFF, or UNKNOWN)
        """
        try:
            ret, stdout, stderr = self._run_ipmi_command(["power", "status"])
        except IPMIError:
            return PowerState.UNKNOWN

        if ret != 0:
            logger.error(f"Failed to get power status: {stderr}")
            return PowerState.UNKNOWN

        stdout_lower = stdout.lower()
        if "on" in stdout_lower:
            return PowerState.ON
        if "off" in stdout_lower:
            return PowerState.OFF
        return PowerState.UNKNOWN

    def power_on(self) -> bool:
        """Power on the system.

        Returns:
            True if power on command succeeded, False otherwise
        """
        logger.info("Powering on system via IPMI...")

        try:
            ret, stdout, stderr = self._run_ipmi_command(["power", "on"])
        except IPMIError:
            return False

        if ret != 0:
            logger.error(f"Power on failed: {stderr}")
            return False

        logger.info("✓ Power on command sent")
        return True

    def power_off(self, force: bool = False) -> bool:
        """Power off the system.

        Args:
            force: If True, force immediate power off. If False, graceful shutdown.

        Returns:
            True if power off command succeeded, False otherwise
        """
        logger.info("Powering off system via IPMI...")

        try:
            if force:
                ret, stdout, stderr = self._run_ipmi_command(["power", "off"])
            else:
                ret, stdout, stderr = self._run_ipmi_command(["power", "soft"])
        except IPMIError:
            return False

        if ret != 0:
            logger.error(f"Power off failed: {stderr}")
            return False

        logger.info("✓ Power off command sent")
        return True

    def power_cycle(self, wait_time: int = POWER_CYCLE_WAIT_TIME) -> bool:
        """Power cycle the system.

        Args:
            wait_time: Seconds to wait between power off and power on

        Returns:
            True if power cycle succeeded, False otherwise
        """
        logger.info("Power cycling system via IPMI...")

        # Power off
        if not self.power_off(force=True):
            logger.error("Failed to power off")
            return False

        # Wait for system to fully power down
        logger.info(f"Waiting {wait_time}s for system to power down...")
        time.sleep(wait_time)

        # Power on
        if not self.power_on():
            logger.error("Failed to power on")
            return False

        logger.info("✓ Power cycle complete")
        return True

    def reset(self) -> bool:
        """Reset (hard reboot) the system.

        Returns:
            True if reset command succeeded, False otherwise
        """
        logger.info("Resetting system via IPMI...")

        try:
            ret, stdout, stderr = self._run_ipmi_command(["power", "reset"])
        except IPMIError:
            return False

        if ret != 0:
            logger.error(f"Reset failed: {stderr}")
            return False

        logger.info("✓ Reset command sent")
        return True

    def set_boot_device(self, device: BootDevice, persistent: bool = False) -> bool:
        """Set next boot device.

        Args:
            device: Boot device to set
            persistent: If True, setting persists across reboots

        Returns:
            True if boot device was set successfully, False otherwise
        """
        logger.info(f"Setting boot device to: {device.value}")

        args = ["chassis", "bootdev", device.value]
        if not persistent:
            args.append("options=efiboot")

        try:
            ret, stdout, stderr = self._run_ipmi_command(args)
        except IPMIError:
            return False

        if ret != 0:
            logger.error(f"Failed to set boot device: {stderr}")
            return False

        logger.info(f"✓ Boot device set to {device.value}")
        return True

    def get_boot_device(self) -> Optional[str]:
        """Get current boot device setting.

        Returns:
            Boot device name or None if unable to determine
        """
        try:
            ret, stdout, stderr = self._run_ipmi_command(
                ["chassis", "bootparam", "get", "5"]
            )
        except IPMIError:
            return None

        if ret != 0:
            logger.error(f"Failed to get boot device: {stderr}")
            return None

        # Parse output
        for line in stdout.split("\n"):
            if "Boot Device Selector" in line:
                return line.split(":")[-1].strip()

        return None

    def get_sensor_data(self) -> Optional[str]:
        """Get sensor data (temperature, fans, etc.).

        Returns:
            Sensor data as string or None if unable to retrieve
        """
        try:
            ret, stdout, stderr = self._run_ipmi_command(["sensor"])
        except IPMIError:
            return None

        if ret == 0:
            return stdout

        logger.error(f"Failed to get sensor data: {stderr}")
        return None

    def get_sel_log(self, lines: int = 20) -> Optional[str]:
        """Get System Event Log (SEL).

        Args:
            lines: Number of recent log entries to retrieve

        Returns:
            SEL log content or None if unable to retrieve
        """
        try:
            ret, stdout, stderr = self._run_ipmi_command(
                ["sel", "list", "last", str(lines)]
            )
        except IPMIError:
            return None

        if ret == 0:
            return stdout

        logger.error(f"Failed to get SEL log: {stderr}")
        return None

    def clear_sel_log(self) -> bool:
        """Clear System Event Log.

        Returns:
            True if log was cleared successfully, False otherwise
        """
        logger.info("Clearing SEL log...")

        try:
            ret, stdout, stderr = self._run_ipmi_command(["sel", "clear"])
        except IPMIError:
            return False

        if ret != 0:
            logger.error(f"Failed to clear SEL: {stderr}")
            return False

        logger.info("✓ SEL log cleared")
        return True

    def activate_serial_console(self, duration: int = 30) -> Optional[str]:
        """Activate serial console and capture output.

        Args:
            duration: How long to capture console output in seconds

        Returns:
            Console output or None if activation failed
        """
        logger.info(f"Activating serial console for {duration}s...")

        try:
            # Deactivate any existing SOL session first
            self._run_ipmi_command(["sol", "deactivate"], timeout=SOL_DEACTIVATE_TIMEOUT)
            time.sleep(1)

            # Activate SOL
            ret, stdout, stderr = self._run_ipmi_command(
                ["sol", "activate"], timeout=duration
            )

            return stdout

        except IPMIError as exc:
            logger.error(f"Serial console activation failed: {exc}")
            return None
        finally:
            # Deactivate SOL
            try:
                self._run_ipmi_command(["sol", "deactivate"], timeout=SOL_DEACTIVATE_TIMEOUT)
            except IPMIError:
                pass  # Best effort cleanup

    def force_safe_boot(self, safe_kernel_path: str = "/boot/vmlinuz-production") -> bool:
        """Force boot to safe kernel.

        This is used when the test kernel fails to boot.

        Args:
            safe_kernel_path: Path to safe kernel on target system

        Returns:
            True if safe boot was initiated successfully, False otherwise
        """
        logger.info("Forcing boot to safe kernel...")

        # Set boot device to disk
        if not self.set_boot_device(BootDevice.DISK):
            logger.error("Failed to set boot device")
            return False

        # Power cycle to boot
        if not self.power_cycle():
            logger.error("Failed to power cycle")
            return False

        logger.info("✓ Forced boot to safe kernel initiated")
        return True

    def emergency_recovery(self) -> bool:
        """Emergency recovery procedure.

        Used when slave is completely unresponsive. Tries multiple recovery
        methods in order of increasing severity.

        Returns:
            True if recovery succeeded, False otherwise
        """
        logger.warning("=== Starting Emergency Recovery ===")

        # Try to get current state
        power_state = self.get_power_status()
        logger.info(f"Current power state: {power_state.value}")

        # Try reset first (less disruptive)
        logger.info("Attempting reset...")
        if self.reset():
            time.sleep(5)
            return True

        # If reset fails, try power cycle
        logger.warning("Reset failed, attempting power cycle...")
        if self.power_cycle():
            return True

        # If power cycle fails, try force power off then on
        logger.error("Power cycle failed, attempting force power off/on...")
        if self.power_off(force=True):
            time.sleep(10)
            if self.power_on():
                return True

        logger.error("=== Emergency Recovery Failed ===")
        return False


def main() -> int:
    """Test IPMI controller."""
    import argparse

    parser = argparse.ArgumentParser(description="IPMI Controller")
    parser.add_argument("ipmi_host", help="IPMI hostname or IP")
    parser.add_argument("--user", required=True, help="IPMI username")
    parser.add_argument("--password", required=True, help="IPMI password")
    parser.add_argument(
        "--action",
        choices=["status", "on", "off", "reset", "cycle", "sensors", "sel"],
        default="status",
        help="Action to perform",
    )

    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)

    controller = IPMIController(args.ipmi_host, args.user, args.password)

    if args.action == "status":
        state = controller.get_power_status()
        print(f"Power state: {state.value}")

    elif args.action == "on":
        controller.power_on()

    elif args.action == "off":
        controller.power_off()

    elif args.action == "reset":
        controller.reset()

    elif args.action == "cycle":
        controller.power_cycle()

    elif args.action == "sensors":
        data = controller.get_sensor_data()
        if data:
            print(data)

    elif args.action == "sel":
        log = controller.get_sel_log()
        if log:
            print(log)

    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
