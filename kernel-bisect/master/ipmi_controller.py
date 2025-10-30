#!/usr/bin/env python3
"""
IPMI Controller - Power management and recovery via IPMI
Handles power control, serial console access, and boot device configuration
"""

import subprocess
import logging
import time
from typing import Optional, List
from enum import Enum

logger = logging.getLogger(__name__)


class PowerState(Enum):
    """Power states"""
    ON = "on"
    OFF = "off"
    UNKNOWN = "unknown"


class BootDevice(Enum):
    """Boot devices"""
    NONE = "none"
    PXE = "pxe"
    DISK = "disk"
    CDROM = "cdrom"
    BIOS = "bios"


class IPMIController:
    """IPMI controller for remote power management"""

    def __init__(self, ipmi_host: str, ipmi_user: str, ipmi_password: str):
        self.ipmi_host = ipmi_host
        self.ipmi_user = ipmi_user
        self.ipmi_password = ipmi_password

    def _run_ipmi_command(self, args: List[str], timeout: int = 30) -> tuple[int, str, str]:
        """Run ipmitool command"""
        cmd = [
            "ipmitool", "-I", "lanplus",
            "-H", self.ipmi_host,
            "-U", self.ipmi_user,
            "-P", self.ipmi_password
        ] + args

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout
            )
            return result.returncode, result.stdout, result.stderr
        except subprocess.TimeoutExpired:
            logger.error(f"IPMI command timed out after {timeout}s")
            return -1, "", "Timeout"
        except Exception as e:
            logger.error(f"IPMI command failed: {e}")
            return -1, "", str(e)

    def get_power_status(self) -> PowerState:
        """Get current power status"""
        ret, stdout, stderr = self._run_ipmi_command(["power", "status"])

        if ret != 0:
            logger.error(f"Failed to get power status: {stderr}")
            return PowerState.UNKNOWN

        stdout = stdout.lower()
        if "on" in stdout:
            return PowerState.ON
        elif "off" in stdout:
            return PowerState.OFF
        else:
            return PowerState.UNKNOWN

    def power_on(self) -> bool:
        """Power on the system"""
        logger.info("Powering on system via IPMI...")

        ret, stdout, stderr = self._run_ipmi_command(["power", "on"])

        if ret != 0:
            logger.error(f"Power on failed: {stderr}")
            return False

        logger.info("✓ Power on command sent")
        return True

    def power_off(self, force: bool = False) -> bool:
        """Power off the system"""
        logger.info("Powering off system via IPMI...")

        if force:
            ret, stdout, stderr = self._run_ipmi_command(["power", "off"])
        else:
            ret, stdout, stderr = self._run_ipmi_command(["power", "soft"])

        if ret != 0:
            logger.error(f"Power off failed: {stderr}")
            return False

        logger.info("✓ Power off command sent")
        return True

    def power_cycle(self, wait_time: int = 10) -> bool:
        """Power cycle the system"""
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
        """Reset (hard reboot) the system"""
        logger.info("Resetting system via IPMI...")

        ret, stdout, stderr = self._run_ipmi_command(["power", "reset"])

        if ret != 0:
            logger.error(f"Reset failed: {stderr}")
            return False

        logger.info("✓ Reset command sent")
        return True

    def set_boot_device(self, device: BootDevice, persistent: bool = False) -> bool:
        """Set next boot device"""
        logger.info(f"Setting boot device to: {device.value}")

        args = ["chassis", "bootdev", device.value]
        if not persistent:
            args.append("options=efiboot")

        ret, stdout, stderr = self._run_ipmi_command(args)

        if ret != 0:
            logger.error(f"Failed to set boot device: {stderr}")
            return False

        logger.info(f"✓ Boot device set to {device.value}")
        return True

    def get_boot_device(self) -> Optional[str]:
        """Get current boot device setting"""
        ret, stdout, stderr = self._run_ipmi_command(["chassis", "bootparam", "get", "5"])

        if ret != 0:
            logger.error(f"Failed to get boot device: {stderr}")
            return None

        # Parse output
        for line in stdout.split('\n'):
            if "Boot Device Selector" in line:
                return line.split(':')[-1].strip()

        return None

    def get_sensor_data(self) -> Optional[str]:
        """Get sensor data (temperature, fans, etc.)"""
        ret, stdout, stderr = self._run_ipmi_command(["sensor"])

        if ret == 0:
            return stdout
        else:
            logger.error(f"Failed to get sensor data: {stderr}")
            return None

    def get_sel_log(self, lines: int = 20) -> Optional[str]:
        """Get System Event Log (SEL)"""
        ret, stdout, stderr = self._run_ipmi_command(["sel", "list", "last", str(lines)])

        if ret == 0:
            return stdout
        else:
            logger.error(f"Failed to get SEL log: {stderr}")
            return None

    def clear_sel_log(self) -> bool:
        """Clear System Event Log"""
        logger.info("Clearing SEL log...")

        ret, stdout, stderr = self._run_ipmi_command(["sel", "clear"])

        if ret != 0:
            logger.error(f"Failed to clear SEL: {stderr}")
            return False

        logger.info("✓ SEL log cleared")
        return True

    def activate_serial_console(self, duration: int = 30) -> Optional[str]:
        """Activate serial console and capture output"""
        logger.info(f"Activating serial console for {duration}s...")

        try:
            # Deactivate any existing SOL session first
            self._run_ipmi_command(["sol", "deactivate"], timeout=5)
            time.sleep(1)

            # Activate SOL
            ret, stdout, stderr = self._run_ipmi_command(
                ["sol", "activate"],
                timeout=duration
            )

            return stdout

        except Exception as e:
            logger.error(f"Serial console activation failed: {e}")
            return None
        finally:
            # Deactivate SOL
            self._run_ipmi_command(["sol", "deactivate"], timeout=5)

    def force_safe_boot(self, safe_kernel_path: str = "/boot/vmlinuz-production") -> bool:
        """
        Force boot to safe kernel
        This is used when the test kernel fails to boot
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
        """
        Emergency recovery procedure
        Used when slave is completely unresponsive
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


def main():
    """Test IPMI controller"""
    import argparse

    parser = argparse.ArgumentParser(description="IPMI Controller")
    parser.add_argument("ipmi_host", help="IPMI hostname or IP")
    parser.add_argument("--user", required=True, help="IPMI username")
    parser.add_argument("--password", required=True, help="IPMI password")
    parser.add_argument("--action", choices=["status", "on", "off", "reset", "cycle", "sensors", "sel"],
                       default="status", help="Action to perform")

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
