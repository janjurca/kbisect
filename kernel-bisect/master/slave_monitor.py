#!/usr/bin/env python3
"""
Slave Monitor - Health checking and recovery for slave machine
Monitors network, SSH, and can trigger IPMI recovery if needed
"""

import time
import subprocess
import logging
from typing import Optional
from dataclasses import dataclass
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


@dataclass
class HealthStatus:
    """Health status of slave"""
    is_alive: bool
    ping_responsive: bool
    ssh_responsive: bool
    last_check: str
    uptime: Optional[str] = None
    kernel_version: Optional[str] = None
    error: Optional[str] = None


class SlaveMonitor:
    """Monitor slave machine health"""

    def __init__(self, slave_host: str, slave_user: str = "root"):
        self.slave_host = slave_host
        self.slave_user = slave_user

    def ping(self, timeout: int = 5) -> bool:
        """Check if slave responds to ping"""
        try:
            result = subprocess.run(
                ["ping", "-c", "1", "-W", str(timeout), self.slave_host],
                capture_output=True,
                timeout=timeout + 1
            )
            return result.returncode == 0
        except (subprocess.TimeoutExpired, Exception) as e:
            logger.debug(f"Ping failed: {e}")
            return False

    def ssh_check(self, timeout: int = 10) -> tuple[bool, Optional[str]]:
        """Check if SSH is responsive and get basic info"""
        ssh_command = [
            "ssh",
            "-o", "StrictHostKeyChecking=no",
            "-o", "ConnectTimeout=5",
            "-o", "BatchMode=yes",
            f"{self.slave_user}@{self.slave_host}",
            "echo alive"
        ]

        try:
            result = subprocess.run(
                ssh_command,
                capture_output=True,
                text=True,
                timeout=timeout
            )

            if result.returncode == 0 and "alive" in result.stdout:
                return True, None
            else:
                return False, f"SSH failed: {result.stderr}"

        except subprocess.TimeoutExpired:
            return False, "SSH timeout"
        except Exception as e:
            return False, str(e)

    def get_kernel_version(self) -> Optional[str]:
        """Get current kernel version from slave"""
        ssh_command = [
            "ssh",
            "-o", "StrictHostKeyChecking=no",
            "-o", "ConnectTimeout=5",
            f"{self.slave_user}@{self.slave_host}",
            "uname -r"
        ]

        try:
            result = subprocess.run(
                ssh_command,
                capture_output=True,
                text=True,
                timeout=10
            )

            if result.returncode == 0:
                return result.stdout.strip()

        except Exception as e:
            logger.debug(f"Failed to get kernel version: {e}")

        return None

    def get_uptime(self) -> Optional[str]:
        """Get slave uptime"""
        ssh_command = [
            "ssh",
            "-o", "StrictHostKeyChecking=no",
            "-o", "ConnectTimeout=5",
            f"{self.slave_user}@{self.slave_host}",
            "uptime -p"
        ]

        try:
            result = subprocess.run(
                ssh_command,
                capture_output=True,
                text=True,
                timeout=10
            )

            if result.returncode == 0:
                return result.stdout.strip()

        except Exception:
            pass

        return None

    def check_health(self) -> HealthStatus:
        """Perform comprehensive health check"""
        logger.debug(f"Checking health of {self.slave_host}...")

        ping_ok = self.ping()
        ssh_ok, ssh_error = self.ssh_check()

        is_alive = ping_ok and ssh_ok

        status = HealthStatus(
            is_alive=is_alive,
            ping_responsive=ping_ok,
            ssh_responsive=ssh_ok,
            last_check=datetime.now(timezone.utc).isoformat(),
            error=ssh_error if not ssh_ok else None
        )

        if is_alive:
            status.kernel_version = self.get_kernel_version()
            status.uptime = self.get_uptime()

        return status

    def wait_for_boot(self, timeout: int = 300, check_interval: int = 5) -> bool:
        """Wait for slave to boot up"""
        logger.info(f"Waiting for slave to boot (timeout: {timeout}s)...")

        start_time = time.time()

        while time.time() - start_time < timeout:
            status = self.check_health()

            if status.is_alive:
                elapsed = int(time.time() - start_time)
                logger.info(f"✓ Slave is alive after {elapsed}s")
                logger.info(f"  Kernel: {status.kernel_version}")
                logger.info(f"  Uptime: {status.uptime}")
                return True

            # Log status every 30 seconds
            elapsed = int(time.time() - start_time)
            if elapsed % 30 == 0 and elapsed > 0:
                logger.info(f"Still waiting... ({elapsed}/{timeout}s)")
                logger.debug(f"  Ping: {status.ping_responsive}, SSH: {status.ssh_responsive}")

            time.sleep(check_interval)

        logger.error(f"Slave failed to boot within {timeout}s timeout")
        return False

    def wait_for_shutdown(self, timeout: int = 60) -> bool:
        """Wait for slave to shut down"""
        logger.info("Waiting for slave to shut down...")

        start_time = time.time()

        while time.time() - start_time < timeout:
            if not self.ping():
                elapsed = int(time.time() - start_time)
                logger.info(f"✓ Slave is down after {elapsed}s")
                return True

            time.sleep(2)

        logger.warning("Slave did not shut down within timeout")
        return False

    def monitor_boot(self, boot_timeout: int = 300) -> tuple[bool, Optional[str]]:
        """
        Monitor slave boot process and return success status

        Returns:
            (success, kernel_version)
        """
        # First wait for shutdown to complete (slave should go offline)
        logger.info("Monitoring boot process...")

        # Wait a bit for reboot to initiate
        time.sleep(10)

        # Wait for slave to come back up
        if self.wait_for_boot(timeout=boot_timeout):
            kernel = self.get_kernel_version()
            logger.info(f"Boot successful, kernel: {kernel}")
            return True, kernel
        else:
            logger.error("Boot failed or timed out")
            return False, None


class SerialConsoleMonitor:
    """Monitor serial console for kernel panics and boot issues"""

    def __init__(self, ipmi_host: str, ipmi_user: str, ipmi_password: str):
        self.ipmi_host = ipmi_host
        self.ipmi_user = ipmi_user
        self.ipmi_password = ipmi_password

    def capture_console_log(self, duration: int = 30) -> Optional[str]:
        """Capture serial console output via IPMI SOL (Serial Over LAN)"""
        try:
            # Activate SOL
            cmd = [
                "ipmitool", "-I", "lanplus",
                "-H", self.ipmi_host,
                "-U", self.ipmi_user,
                "-P", self.ipmi_password,
                "sol", "activate"
            ]

            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=duration
            )

            return result.stdout

        except subprocess.TimeoutExpired as e:
            # Timeout is expected for continuous monitoring
            return e.stdout.decode() if e.stdout else None
        except Exception as e:
            logger.error(f"Failed to capture console: {e}")
            return None

    def check_for_panic(self, console_output: str) -> bool:
        """Check if console output contains kernel panic"""
        panic_patterns = [
            "Kernel panic",
            "kernel panic",
            "Oops:",
            "BUG:",
            "general protection fault",
            "unable to handle kernel",
            "Call Trace:",
        ]

        for pattern in panic_patterns:
            if pattern in console_output:
                logger.error(f"Detected kernel issue: {pattern}")
                return True

        return False

    def check_boot_stuck(self, console_output: str) -> bool:
        """Check if boot appears to be stuck"""
        stuck_patterns = [
            "waiting for device",
            "timed out waiting for",
            "Failed to mount",
            "A start job is running"
        ]

        for pattern in stuck_patterns:
            if pattern in console_output:
                logger.warning(f"Boot may be stuck: {pattern}")
                return True

        return False


def main():
    """Test the monitor"""
    import argparse

    parser = argparse.ArgumentParser(description="Slave Monitor")
    parser.add_argument("slave_host", help="Slave hostname or IP")
    parser.add_argument("--user", default="root", help="SSH user")
    parser.add_argument("--wait-boot", action="store_true", help="Wait for boot")
    parser.add_argument("--timeout", type=int, default=300, help="Boot timeout")

    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)

    monitor = SlaveMonitor(args.slave_host, args.user)

    if args.wait_boot:
        success, kernel = monitor.monitor_boot(args.timeout)
        if success:
            print(f"Boot successful: {kernel}")
            return 0
        else:
            print("Boot failed")
            return 1
    else:
        status = monitor.check_health()
        print(f"Alive: {status.is_alive}")
        print(f"Ping: {status.ping_responsive}")
        print(f"SSH: {status.ssh_responsive}")
        if status.kernel_version:
            print(f"Kernel: {status.kernel_version}")
        if status.uptime:
            print(f"Uptime: {status.uptime}")
        if status.error:
            print(f"Error: {status.error}")

        return 0 if status.is_alive else 1


if __name__ == "__main__":
    import sys
    sys.exit(main())
