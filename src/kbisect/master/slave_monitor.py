#!/usr/bin/env python3
"""Slave Monitor - Health checking and recovery for slave machine.

Monitors network connectivity, SSH access, and can trigger IPMI recovery if needed.
"""

import logging
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional, Tuple


logger = logging.getLogger(__name__)

# Constants
DEFAULT_PING_TIMEOUT = 5
DEFAULT_SSH_TIMEOUT = 10
DEFAULT_BOOT_TIMEOUT = 300
DEFAULT_CHECK_INTERVAL = 5
STATUS_LOG_INTERVAL = 30
SHUTDOWN_POLL_INTERVAL = 2
SHUTDOWN_TIMEOUT = 60
POST_BOOT_SETTLE_TIME = 10


@dataclass
class HealthStatus:
    """Health status of slave machine.

    Attributes:
        is_alive: Overall health status (True if both ping and SSH work)
        ping_responsive: Whether system responds to ICMP ping
        ssh_responsive: Whether SSH service is accessible
        last_check: ISO timestamp of when check was performed
        uptime: System uptime string (if available)
        kernel_version: Running kernel version (if available)
        error: Error message if health check failed
    """

    is_alive: bool
    ping_responsive: bool
    ssh_responsive: bool
    last_check: str
    uptime: Optional[str] = None
    kernel_version: Optional[str] = None
    error: Optional[str] = None


class SlaveMonitor:
    """Monitor slave machine health and boot status.

    Provides methods to check system health, wait for boot/shutdown,
    and verify kernel versions.

    Attributes:
        slave_host: Hostname or IP address of slave machine
        slave_user: SSH username for slave access
    """

    def __init__(self, slave_host: str, slave_user: str = "root") -> None:
        """Initialize slave monitor.

        Args:
            slave_host: Slave hostname or IP address
            slave_user: SSH username (defaults to root)
        """
        self.slave_host = slave_host
        self.slave_user = slave_user

    def ping(self, timeout: int = DEFAULT_PING_TIMEOUT) -> bool:
        """Check if slave responds to ping.

        Args:
            timeout: Ping timeout in seconds

        Returns:
            True if slave responds to ping, False otherwise
        """
        try:
            result = subprocess.run(
                ["ping", "-c", "1", "-W", str(timeout), self.slave_host],
                capture_output=True,
                timeout=timeout + 1,
                check=False,
            )
            return result.returncode == 0
        except (subprocess.TimeoutExpired, Exception) as exc:
            logger.debug(f"Ping failed: {exc}")
            return False

    def ssh_check(self, timeout: int = DEFAULT_SSH_TIMEOUT) -> Tuple[bool, Optional[str]]:
        """Check if SSH is responsive and accessible.

        Args:
            timeout: SSH connection timeout in seconds

        Returns:
            Tuple of (success, error_message)
        """
        ssh_command = [
            "ssh",
            "-o",
            "StrictHostKeyChecking=no",
            "-o",
            "ConnectTimeout=5",
            "-o",
            "BatchMode=yes",
            f"{self.slave_user}@{self.slave_host}",
            "echo alive",
        ]

        try:
            result = subprocess.run(
                ssh_command, capture_output=True, text=True, timeout=timeout, check=False
            )

            if result.returncode == 0 and "alive" in result.stdout:
                return True, None
            return False, f"SSH failed: {result.stderr}"

        except subprocess.TimeoutExpired:
            return False, "SSH timeout"
        except Exception as exc:
            return False, str(exc)

    def get_kernel_version(self) -> Optional[str]:
        """Get current kernel version from slave.

        Returns:
            Kernel version string or None if unable to retrieve
        """
        ssh_command = [
            "ssh",
            "-o",
            "StrictHostKeyChecking=no",
            "-o",
            "ConnectTimeout=5",
            f"{self.slave_user}@{self.slave_host}",
            "uname -r",
        ]

        try:
            result = subprocess.run(
                ssh_command, capture_output=True, text=True, timeout=10, check=False
            )

            if result.returncode == 0:
                return result.stdout.strip()

        except Exception as exc:
            logger.debug(f"Failed to get kernel version: {exc}")

        return None

    def get_uptime(self) -> Optional[str]:
        """Get slave uptime.

        Returns:
            Uptime string or None if unable to retrieve
        """
        ssh_command = [
            "ssh",
            "-o",
            "StrictHostKeyChecking=no",
            "-o",
            "ConnectTimeout=5",
            f"{self.slave_user}@{self.slave_host}",
            "uptime -p",
        ]

        try:
            result = subprocess.run(
                ssh_command, capture_output=True, text=True, timeout=10, check=False
            )

            if result.returncode == 0:
                return result.stdout.strip()

        except Exception:
            pass

        return None

    def check_health(self) -> HealthStatus:
        """Perform comprehensive health check.

        Returns:
            HealthStatus object with current system state
        """
        logger.debug(f"Checking health of {self.slave_host}...")

        ping_ok = self.ping()
        ssh_ok, ssh_error = self.ssh_check()

        is_alive = ping_ok and ssh_ok

        status = HealthStatus(
            is_alive=is_alive,
            ping_responsive=ping_ok,
            ssh_responsive=ssh_ok,
            last_check=datetime.now(timezone.utc).isoformat(),
            error=ssh_error if not ssh_ok else None,
        )

        if is_alive:
            status.kernel_version = self.get_kernel_version()
            status.uptime = self.get_uptime()

        return status

    def wait_for_boot(
        self, timeout: int = DEFAULT_BOOT_TIMEOUT, check_interval: int = DEFAULT_CHECK_INTERVAL
    ) -> bool:
        """Wait for slave to boot up.

        Args:
            timeout: Maximum time to wait in seconds
            check_interval: How often to check status in seconds

        Returns:
            True if slave booted successfully, False if timeout
        """
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
            if elapsed % STATUS_LOG_INTERVAL == 0 and elapsed > 0:
                logger.info(f"Still waiting... ({elapsed}/{timeout}s)")
                logger.debug(f"  Ping: {status.ping_responsive}, SSH: {status.ssh_responsive}")

            time.sleep(check_interval)

        logger.error(f"Slave failed to boot within {timeout}s timeout")
        return False

    def wait_for_shutdown(self, timeout: int = SHUTDOWN_TIMEOUT) -> bool:
        """Wait for slave to shut down.

        Args:
            timeout: Maximum time to wait in seconds

        Returns:
            True if slave shut down successfully, False if timeout
        """
        logger.info("Waiting for slave to shut down...")

        start_time = time.time()

        while time.time() - start_time < timeout:
            if not self.ping():
                elapsed = int(time.time() - start_time)
                logger.info(f"✓ Slave is down after {elapsed}s")
                return True

            time.sleep(SHUTDOWN_POLL_INTERVAL)

        logger.warning("Slave did not shut down within timeout")
        return False

    def monitor_boot(self, boot_timeout: int = DEFAULT_BOOT_TIMEOUT) -> Tuple[bool, Optional[str]]:
        """Monitor slave boot process and return success status.

        Args:
            boot_timeout: Maximum time to wait for boot in seconds

        Returns:
            Tuple of (success, kernel_version)
        """
        logger.info("Monitoring boot process...")

        # Wait a bit for reboot to initiate
        time.sleep(POST_BOOT_SETTLE_TIME)

        # Wait for slave to come back up
        if self.wait_for_boot(timeout=boot_timeout):
            kernel = self.get_kernel_version()
            logger.info(f"Boot successful, kernel: {kernel}")
            return True, kernel

        logger.error("Boot failed or timed out")
        return False, None


class SerialConsoleMonitor:
    """Monitor serial console for kernel panics and boot issues.

    Uses IPMI Serial Over LAN (SOL) to capture console output and detect
    kernel panics, boot hangs, and other issues.

    Attributes:
        ipmi_host: IPMI interface hostname or IP
        ipmi_user: IPMI username
        ipmi_password: IPMI password
    """

    def __init__(self, ipmi_host: str, ipmi_user: str, ipmi_password: str) -> None:
        """Initialize serial console monitor.

        Args:
            ipmi_host: IPMI interface hostname or IP
            ipmi_user: IPMI username
            ipmi_password: IPMI password
        """
        self.ipmi_host = ipmi_host
        self.ipmi_user = ipmi_user
        self.ipmi_password = ipmi_password

    def capture_console_log(self, duration: int = 30) -> Optional[str]:
        """Capture serial console output via IPMI SOL (Serial Over LAN).

        Args:
            duration: How long to capture output in seconds

        Returns:
            Console output or None if capture failed
        """
        try:
            # Activate SOL
            cmd = [
                "ipmitool",
                "-I",
                "lanplus",
                "-H",
                self.ipmi_host,
                "-U",
                self.ipmi_user,
                "-P",
                self.ipmi_password,
                "sol",
                "activate",
            ]

            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=duration, check=False
            )

            return result.stdout

        except subprocess.TimeoutExpired as exc:
            # Timeout is expected for continuous monitoring
            return exc.stdout.decode() if exc.stdout else None
        except Exception as exc:
            logger.error(f"Failed to capture console: {exc}")
            return None

    def check_for_panic(self, console_output: str) -> bool:
        """Check if console output contains kernel panic.

        Args:
            console_output: Console text to analyze

        Returns:
            True if panic detected, False otherwise
        """
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
        """Check if boot appears to be stuck.

        Args:
            console_output: Console text to analyze

        Returns:
            True if boot appears stuck, False otherwise
        """
        stuck_patterns = [
            "waiting for device",
            "timed out waiting for",
            "Failed to mount",
            "A start job is running",
        ]

        for pattern in stuck_patterns:
            if pattern in console_output:
                logger.warning(f"Boot may be stuck: {pattern}")
                return True

        return False


def main() -> int:
    """Test the monitor."""
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

        print("Boot failed")
        return 1

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
