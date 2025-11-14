#!/usr/bin/env python3
"""Beaker Controller - Power management via Beaker system-power command.

Handles power control using the Beaker lab automation system.
Requires bkr client to be installed and user authenticated via Kerberos.
"""

import logging
import subprocess
import time
from typing import TYPE_CHECKING, List, Optional, Tuple

from kbisect.power.base import (
    BootDevice,
    PowerController,
    PowerState,
)

if TYPE_CHECKING:
    from kbisect.remote.ssh import SSHClient


logger = logging.getLogger(__name__)

# Constants
DEFAULT_BEAKER_TIMEOUT = 60
POWER_CYCLE_WAIT_TIME = 10


class BeakerError(Exception):
    """Base exception for Beaker-related errors."""


class BeakerTimeoutError(BeakerError):
    """Exception raised when Beaker command times out."""


class BeakerCommandError(BeakerError):
    """Exception raised when Beaker command fails."""


class BeakerController(PowerController):
    """Beaker controller for remote power management.

    Provides methods to control power state via Beaker's system-power command.
    Assumes bkr client is installed and user is authenticated via Kerberos.

    Attributes:
        hostname: System hostname/FQDN to control
    """

    def __init__(self, hostname: str) -> None:
        """Initialize Beaker controller.

        Args:
            hostname: System hostname or FQDN to control
        """
        self.hostname = hostname

    def _run_beaker_command(
        self, action: str, timeout: int = DEFAULT_BEAKER_TIMEOUT
    ) -> Tuple[int, str, str]:
        """Run bkr system-power command.

        Args:
            action: Power action (on, off, reboot, interrupt)
            timeout: Command timeout in seconds

        Returns:
            Tuple of (return_code, stdout, stderr)

        Raises:
            BeakerTimeoutError: If command times out
            BeakerCommandError: If command fails to execute
        """
        cmd = [
            "bkr",
            "system-power",
            "--action",
            action,
            "--force",
            "--clear-netboot",
            self.hostname,
        ]

        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout, check=False
            )
            return result.returncode, result.stdout, result.stderr

        except subprocess.TimeoutExpired as exc:
            msg = f"Beaker command timed out after {timeout}s"
            logger.error(msg)
            raise BeakerTimeoutError(msg) from exc
        except Exception as exc:
            msg = f"Beaker command failed: {exc}"
            logger.error(msg)
            raise BeakerCommandError(msg) from exc

    def get_power_status(self) -> PowerState:
        """Get current power status of the system.

        Note: Beaker does not support querying power status.

        Returns:
            PowerState.UNKNOWN (always)
        """
        return PowerState.UNKNOWN

    def power_on(self) -> bool:
        """Power on the system.

        Returns:
            True if power on command succeeded, False otherwise
        """
        logger.info(f"Powering on system {self.hostname} via Beaker...")

        try:
            ret, stdout, stderr = self._run_beaker_command("on")
        except BeakerError as exc:
            logger.error(f"Power on failed: {exc}")
            return False

        if ret != 0:
            logger.error(f"Power on failed: {stderr}")
            return False

        logger.info(f"✓ Power on command sent for {self.hostname}")
        return True

    def power_off(self, force: bool = False) -> bool:
        """Power off the system.

        Args:
            force: Ignored for Beaker (--force is always used)

        Returns:
            True if power off command succeeded, False otherwise
        """
        logger.info(f"Powering off system {self.hostname} via Beaker...")

        try:
            ret, stdout, stderr = self._run_beaker_command("off")
        except BeakerError as exc:
            logger.error(f"Power off failed: {exc}")
            return False

        if ret != 0:
            logger.error(f"Power off failed: {stderr}")
            return False

        logger.info(f"✓ Power off command sent for {self.hostname}")
        return True

    def power_cycle(self, wait_time: int = POWER_CYCLE_WAIT_TIME) -> bool:
        """Power cycle the system.

        Args:
            wait_time: Seconds to wait between power off and power on

        Returns:
            True if power cycle succeeded, False otherwise
        """
        logger.info(f"Power cycling system {self.hostname} via Beaker...")

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

        logger.info(f"✓ Power cycle complete for {self.hostname}")
        return True

    def reset(self, ssh_client: Optional["SSHClient"] = None) -> bool:
        """Reset (hard reboot) the system.

        Args:
            ssh_client: Optional SSH client for connectivity verification.
                       If provided, will wait for machine shutdown before returning.

        Returns:
            True if reset command succeeded (and shutdown confirmed if ssh_client provided),
            False otherwise
        """
        logger.info(f"Resetting system {self.hostname} via Beaker...")

        # Pre-reboot connectivity check
        if ssh_client:
            logger.info("Verifying SSH connectivity before reboot...")
            if not ssh_client.is_alive():
                logger.warning("SSH not responsive before reboot - machine may already be down")

        # Send reboot command
        try:
            ret, stdout, stderr = self._run_beaker_command("reboot")
        except BeakerError as exc:
            logger.error(f"Reset failed: {exc}")
            return False

        if ret != 0:
            logger.error(f"Reset failed: {stderr}")
            return False

        logger.info(f"✓ Reset command sent for {self.hostname}")

        # Wait for shutdown if SSH client provided
        if ssh_client:
            logger.info("Waiting for machine to shut down...")
            shutdown_timeout = 120  # Fixed timeout to prevent infinite loop
            shutdown_poll_interval = 2
            start_time = time.time()

            while time.time() - start_time < shutdown_timeout:
                if not ssh_client.is_alive():
                    elapsed = time.time() - start_time
                    logger.info(f"✓ Machine shutdown confirmed after {elapsed:.1f}s")
                    return True
                time.sleep(shutdown_poll_interval)

            # Timeout reached - shutdown not confirmed
            logger.warning(
                f"Shutdown not confirmed within {shutdown_timeout}s - "
                "machine may still be up or reboot pending"
            )
            return False

        return True

    def set_boot_device(self, device: BootDevice, persistent: bool = False) -> bool:
        """Set next boot device.

        Note: Beaker does not support boot device configuration.
        The --clear-netboot flag is always used to ensure disk boot.

        Args:
            device: Boot device to set (ignored)
            persistent: Persistence setting (ignored)

        Returns:
            False (not supported)
        """
        logger.warning(
            f"Boot device configuration not supported by Beaker for {self.hostname}"
        )
        return False

    def get_boot_device(self) -> Optional[str]:
        """Get current boot device setting.

        Note: Beaker does not support querying boot device configuration.

        Returns:
            None (not supported)
        """
        return None

    def health_check(self) -> dict:
        """Perform health check on Beaker controller.

        Validates:
        - bkr command availability
        - Kerberos authentication (via bkr whoami)
        - System accessibility

        Returns:
            Dictionary with health check results
        """
        import shutil

        result = {
            'healthy': False,
            'checks': []
        }

        # Check if bkr is installed
        bkr_path = shutil.which('bkr')
        if not bkr_path:
            result['error'] = "bkr command not found in PATH"
            result['checks'].append({'name': 'bkr', 'passed': False})
            return result

        result['tool_path'] = bkr_path
        result['checks'].append({'name': 'bkr', 'passed': True})

        # Test Kerberos authentication with bkr whoami
        try:
            whoami_result = subprocess.run(
                ['bkr', 'whoami'],
                capture_output=True,
                text=True,
                timeout=10,
                check=False
            )

            if whoami_result.returncode != 0:
                result['error'] = f"Kerberos authentication failed: {whoami_result.stderr.strip()}"
                result['checks'].append({'name': 'kerberos_auth', 'passed': False})
                return result

            result['checks'].append({'name': 'kerberos_auth', 'passed': True})
            result['authenticated_user'] = whoami_result.stdout.strip()

        except subprocess.TimeoutExpired:
            result['error'] = "bkr whoami command timed out"
            result['checks'].append({'name': 'kerberos_auth', 'passed': False})
            return result
        except Exception as e:
            result['error'] = f"Failed to check Kerberos authentication: {str(e)}"
            result['checks'].append({'name': 'kerberos_auth', 'passed': False})
            return result

        # Note: We cannot test power status query as Beaker doesn't support it
        # We consider the controller healthy if bkr is available and user is authenticated
        result['healthy'] = True
        result['power_status'] = 'unknown (Beaker does not support status queries)'

        return result


def main() -> int:
    """Test Beaker controller."""
    import argparse

    parser = argparse.ArgumentParser(description="Beaker Power Controller")
    parser.add_argument("hostname", help="System hostname or FQDN")
    parser.add_argument(
        "--action",
        choices=["status", "on", "off", "reset", "cycle"],
        default="status",
        help="Action to perform",
    )

    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)

    controller = BeakerController(args.hostname)

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

    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
