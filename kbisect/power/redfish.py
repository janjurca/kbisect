#!/usr/bin/env python3
"""Redfish Controller - Power management via Redfish REST API.

Handles power control and boot device configuration for BMCs that support
the DMTF Redfish standard (common on OpenBMC systems where IPMI lanplus
is not available).
"""

import json
import logging
import ssl
import time
import base64
from typing import Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from kbisect.power.base import (
    BootDevice,
    PowerController,
    PowerState,
)
from kbisect.remote.ssh import SSHClient


logger = logging.getLogger(__name__)

# Constants
DEFAULT_REDFISH_TIMEOUT = 30
POWER_CYCLE_WAIT_TIME = 10

# Redfish reset action types
RESET_TYPE_ON = "On"
RESET_TYPE_FORCE_OFF = "ForceOff"
RESET_TYPE_GRACEFUL_SHUTDOWN = "GracefulShutdown"
RESET_TYPE_FORCE_RESTART = "ForceRestart"

# Map BootDevice enum to Redfish boot source override targets
BOOT_DEVICE_MAP = {
    BootDevice.PXE: "Pxe",
    BootDevice.DISK: "Hdd",
    BootDevice.CDROM: "Cd",
    BootDevice.BIOS: "BiosSetup",
    BootDevice.NONE: "None",
}


class RedfishError(Exception):
    """Base exception for Redfish-related errors."""


class RedfishTimeoutError(RedfishError):
    """Exception raised when Redfish request times out."""


class RedfishCommandError(RedfishError):
    """Exception raised when Redfish request fails."""


class RedfishController(PowerController):
    """Redfish controller for remote power management.

    Provides methods to control power state and boot devices via the
    Redfish REST API. Uses only Python standard library (urllib) to
    avoid external dependencies.

    Attributes:
        host: Hostname or IP address of BMC with Redfish service
        user: Redfish/BMC username for authentication
        password: Redfish/BMC password for authentication
        system_id: Redfish system identifier (default: "system" for OpenBMC)
    """

    def __init__(
        self,
        host: str,
        user: str,
        password: str,
        ssh_host: Optional[str] = None,
        ssh_connect_timeout: int = 15,
        system_id: str = "system",
    ) -> None:
        """Initialize Redfish controller.

        Args:
            host: BMC hostname or IP address
            user: BMC username
            password: BMC password
            ssh_host: SSH hostname for reboot verification (defaults to host if not provided)
            ssh_connect_timeout: SSH connection timeout in seconds
            system_id: Redfish system identifier (default: "system")
        """
        self.host = host
        self.user = user
        self.password = password
        self.ssh_host = ssh_host or host
        self.ssh_connect_timeout = ssh_connect_timeout
        self.system_id = system_id
        self._base_url = f"https://{host}/redfish/v1"
        self._system_url = f"{self._base_url}/Systems/{system_id}"

    def _make_request(
        self,
        url: str,
        method: str = "GET",
        data: Optional[dict] = None,
        timeout: int = DEFAULT_REDFISH_TIMEOUT,
    ) -> Tuple[int, dict]:
        """Make an authenticated Redfish API request.

        Args:
            url: Full URL for the request
            method: HTTP method (GET, POST, PATCH)
            data: JSON payload for POST/PATCH requests
            timeout: Request timeout in seconds

        Returns:
            Tuple of (http_status_code, response_body_as_dict)

        Raises:
            RedfishTimeoutError: If request times out
            RedfishCommandError: If request fails
        """
        # Create SSL context that doesn't verify certificates (BMCs use self-signed)
        ssl_ctx = ssl.create_default_context()
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode = ssl.CERT_NONE

        # Basic auth header
        credentials = base64.b64encode(f"{self.user}:{self.password}".encode()).decode()
        headers = {
            "Authorization": f"Basic {credentials}",
            "Content-Type": "application/json",
        }

        body = json.dumps(data).encode() if data else None
        req = Request(url, data=body, headers=headers, method=method)

        try:
            with urlopen(req, timeout=timeout, context=ssl_ctx) as response:
                response_body = response.read().decode()
                if response_body:
                    return response.status, json.loads(response_body)
                return response.status, {}
        except HTTPError as exc:
            response_body = exc.read().decode() if exc.fp else ""
            try:
                error_data = json.loads(response_body) if response_body else {}
            except json.JSONDecodeError:
                error_data = {"raw": response_body}
            logger.error(f"Redfish HTTP {exc.code}: {error_data}")
            raise RedfishCommandError(
                f"Redfish request failed with HTTP {exc.code}: {error_data}"
            ) from exc
        except TimeoutError as exc:
            msg = f"Redfish request timed out after {timeout}s: {url}"
            logger.error(msg)
            raise RedfishTimeoutError(msg) from exc
        except URLError as exc:
            msg = f"Redfish connection failed: {exc.reason}"
            logger.error(msg)
            raise RedfishCommandError(msg) from exc

    def _reset_action(self, reset_type: str) -> bool:
        """Send a ComputerSystem.Reset action.

        Args:
            reset_type: Redfish ResetType value

        Returns:
            True if action was accepted, False otherwise
        """
        url = f"{self._system_url}/Actions/ComputerSystem.Reset"
        try:
            status, _ = self._make_request(url, method="POST", data={"ResetType": reset_type})
            return status in (200, 204)
        except RedfishError:
            return False

    def get_power_status(self) -> PowerState:
        """Get current power status of the system."""
        try:
            status, data = self._make_request(self._system_url)
        except RedfishError:
            return PowerState.UNKNOWN

        if status != 200:
            return PowerState.UNKNOWN

        power_state = data.get("PowerState", "").lower()
        if power_state == "on":
            return PowerState.ON
        if power_state == "off":
            return PowerState.OFF
        return PowerState.UNKNOWN

    def power_on(self) -> bool:
        """Power on the system."""
        logger.info("Powering on system via Redfish...")
        if self._reset_action(RESET_TYPE_ON):
            logger.info("Power on command sent")
            return True
        logger.error("Power on failed")
        return False

    def power_off(self, force: bool = False) -> bool:
        """Power off the system."""
        logger.info("Powering off system via Redfish...")
        reset_type = RESET_TYPE_FORCE_OFF if force else RESET_TYPE_GRACEFUL_SHUTDOWN
        if self._reset_action(reset_type):
            logger.info("Power off command sent")
            return True
        logger.error("Power off failed")
        return False

    def power_cycle(self, wait_time: int = POWER_CYCLE_WAIT_TIME) -> bool:
        """Power cycle the system (off then on)."""
        logger.info("Power cycling system via Redfish...")

        # Ensure system boots from disk, not PXE
        self.set_boot_device(BootDevice.DISK)

        if not self.power_off(force=True):
            logger.error("Failed to power off")
            return False

        logger.info(f"Waiting {wait_time}s for system to power down...")
        time.sleep(wait_time)

        if not self.power_on():
            logger.error("Failed to power on")
            return False

        logger.info("Power cycle complete")
        return True

    def reset(self) -> bool:
        """Reset (hard reboot) the system."""
        logger.info("Resetting system via Redfish...")

        # Ensure system boots from disk, not PXE
        logger.info("Setting boot device to disk before reset...")
        self.set_boot_device(BootDevice.DISK)

        ssh_client = SSHClient(self.ssh_host, user="root", connect_timeout=self.ssh_connect_timeout)

        logger.info("Verifying SSH connectivity before reboot...")
        if not ssh_client.is_alive():
            logger.warning("SSH not responsive before reboot - machine may already be down")

        if not self._reset_action(RESET_TYPE_FORCE_RESTART):
            logger.error("Reset command failed")
            return False

        logger.info("Reset command sent")

        # Wait for shutdown
        logger.info("Waiting for machine to shut down...")
        shutdown_timeout = 120
        shutdown_poll_interval = 2
        start_time = time.time()

        while time.time() - start_time < shutdown_timeout:
            if not ssh_client.is_alive():
                elapsed = time.time() - start_time
                logger.info(f"Machine shutdown confirmed after {elapsed:.1f}s")
                return True
            time.sleep(shutdown_poll_interval)

        logger.warning(
            f"Shutdown not confirmed within {shutdown_timeout}s - machine may still be up"
        )
        return False

    def set_boot_device(self, device: BootDevice, persistent: bool = False) -> bool:
        """Set boot device for next boot or permanently."""
        logger.info(f"Setting boot device to: {device.value}")

        redfish_target = BOOT_DEVICE_MAP.get(device)
        if redfish_target is None:
            logger.error(f"Unsupported boot device: {device.value}")
            return False

        boot_override = "Continuous" if persistent else "Once"
        patch_data = {
            "Boot": {
                "BootSourceOverrideTarget": redfish_target,
                "BootSourceOverrideEnabled": boot_override,
            }
        }

        try:
            status, _ = self._make_request(self._system_url, method="PATCH", data=patch_data)
        except RedfishError:
            return False

        if status in (200, 204):
            logger.info(f"Boot device set to {device.value}")
            return True

        logger.error(f"Failed to set boot device (HTTP {status})")
        return False

    def get_boot_device(self) -> Optional[str]:
        """Get current boot device configuration."""
        try:
            status, data = self._make_request(self._system_url)
        except RedfishError:
            return None

        if status != 200:
            return None

        boot = data.get("Boot", {})
        return boot.get("BootSourceOverrideTarget")

    def health_check(self) -> dict:
        """Perform health check on Redfish controller."""
        result = {"healthy": False, "checks": []}

        try:
            status, data = self._make_request(self._base_url)
            if status != 200:
                result["error"] = f"Redfish service root returned HTTP {status}"
                result["checks"].append({"name": "connectivity", "passed": False})
                return result

            result["checks"].append({"name": "connectivity", "passed": True})
        except RedfishError as e:
            result["error"] = f"Failed to connect to Redfish service: {e!s}"
            result["checks"].append({"name": "connectivity", "passed": False})
            return result

        # Check power status to verify system endpoint works
        try:
            power_state = self.get_power_status()
            if power_state == PowerState.UNKNOWN:
                result["error"] = "Could not determine power status"
                result["checks"].append({"name": "system_endpoint", "passed": False})
                return result

            result["power_status"] = power_state.value
            result["checks"].append({"name": "system_endpoint", "passed": True})
            result["checks"].append({"name": "authentication", "passed": True})
            result["healthy"] = True
        except Exception as e:
            result["error"] = f"Failed to query system status: {e!s}"
            result["checks"].append({"name": "system_endpoint", "passed": False})

        return result

    def emergency_recovery(self) -> bool:
        """Emergency recovery procedure."""
        logger.warning("=== Starting Emergency Recovery (Redfish) ===")

        power_state = self.get_power_status()
        logger.info(f"Current power state: {power_state.value}")

        logger.info("Attempting reset...")
        if self.reset():
            time.sleep(5)
            return True

        logger.warning("Reset failed, attempting power cycle...")
        if self.power_cycle():
            return True

        logger.error("Power cycle failed, attempting force power off/on...")
        if self.power_off(force=True):
            time.sleep(10)
            if self.power_on():
                return True

        logger.error("=== Emergency Recovery Failed ===")
        return False


def main() -> int:
    """Test Redfish controller."""
    import argparse

    parser = argparse.ArgumentParser(description="Redfish Controller")
    parser.add_argument("bmc_host", help="BMC hostname or IP")
    parser.add_argument("--user", required=True, help="BMC username")
    parser.add_argument("--password", required=True, help="BMC password")
    parser.add_argument("--system-id", default="system", help="Redfish system ID")
    parser.add_argument(
        "--action",
        choices=["status", "on", "off", "reset", "cycle", "health"],
        default="status",
        help="Action to perform",
    )

    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)

    controller = RedfishController(
        args.bmc_host, args.user, args.password, system_id=args.system_id
    )

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
    elif args.action == "health":
        result = controller.health_check()
        print(json.dumps(result, indent=2))

    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
