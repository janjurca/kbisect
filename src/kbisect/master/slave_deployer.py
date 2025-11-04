#!/usr/bin/env python3
"""Slave Deployer - Automatic deployment of slave components.

Handles copying scripts, installing services, and initializing the slave machine.
"""

import logging
import os
import shlex
import subprocess
from pathlib import Path
from typing import List, Optional, Tuple


logger = logging.getLogger(__name__)

# Constants
DEFAULT_DEPLOY_PATH = "/root/kernel-bisect/lib"
DEFAULT_STATE_DIR = "/var/lib/kernel-bisect"
DEFAULT_SSH_TIMEOUT = 30


class DeploymentError(Exception):
    """Base exception for deployment-related errors."""


class SSHError(DeploymentError):
    """Exception raised when SSH operations fail."""


class TransferError(DeploymentError):
    """Exception raised when file transfer operations fail."""


class SlaveDeployer:
    """Automatically deploy and configure slave machine.

    Handles deployment of bisection library, initialization of protection
    mechanisms, and verification of deployment status.

    Attributes:
        slave_host: Hostname or IP of slave machine
        slave_user: SSH username for slave access
        deploy_path: Target path on slave for library deployment
        local_lib_path: Path to local library files
    """

    def __init__(
        self,
        slave_host: str,
        slave_user: str = "root",
        deploy_path: str = DEFAULT_DEPLOY_PATH,
        local_lib_path: Optional[str] = None,
    ) -> None:
        """Initialize slave deployer.

        Args:
            slave_host: Slave hostname or IP address
            slave_user: SSH username (defaults to root)
            deploy_path: Deployment path on slave
            local_lib_path: Local library path (auto-detected if None)
        """
        self.slave_host = slave_host
        self.slave_user = slave_user
        self.deploy_path = deploy_path

        # Determine local library path
        if local_lib_path:
            self.local_lib_path = Path(local_lib_path)
        else:
            # Assume we're in master/ directory or kbisect/ root
            script_dir = Path(__file__).parent.parent
            self.local_lib_path = script_dir / "lib"

    def _ssh_command(
        self, command: str, timeout: int = DEFAULT_SSH_TIMEOUT
    ) -> Tuple[int, str, str]:
        """Execute SSH command on slave.

        Args:
            command: Command to execute
            timeout: Command timeout in seconds

        Returns:
            Tuple of (return_code, stdout, stderr)

        Raises:
            SSHError: If SSH command fails to execute
        """
        ssh_cmd = [
            "ssh",
            "-o",
            "StrictHostKeyChecking=no",
            "-o",
            "ConnectTimeout=10",
            f"{self.slave_user}@{self.slave_host}",
            command,
        ]

        try:
            result = subprocess.run(
                ssh_cmd, capture_output=True, text=True, timeout=timeout, check=False
            )
            return result.returncode, result.stdout, result.stderr
        except subprocess.TimeoutExpired as exc:
            msg = f"SSH command timed out: {command}"
            logger.error(msg)
            raise SSHError(msg) from exc
        except Exception as exc:
            msg = f"SSH command failed: {exc}"
            logger.error(msg)
            raise SSHError(msg) from exc

    def _copy_to_slave(self, local_path: str, remote_path: str) -> bool:
        """Copy files to slave using scp.

        Args:
            local_path: Local file or directory path
            remote_path: Remote destination path

        Returns:
            True if copy succeeded, False otherwise

        Raises:
            TransferError: If file transfer fails to execute
        """
        try:
            # Step 1: Create remote directory structure
            remote_dir = os.path.dirname(remote_path)
            ret, _, stderr = self._ssh_command(f"mkdir -p {shlex.quote(remote_dir)}")
            if ret != 0:
                logger.error(f"Failed to create remote directory: {stderr}")
                return False

            # Step 2: Copy file with rsync
            rsync_cmd = [
                "rsync",
                "-az",  # Archive mode (preserves permissions, times, etc.) + compression
                "-e",
                "ssh -o StrictHostKeyChecking=no -o ConnectTimeout=10",
                local_path,
                f"{self.slave_user}@{self.slave_host}:{remote_path}",
            ]

            result = subprocess.run(rsync_cmd, capture_output=True, text=True, timeout=60, check=False)
            if result.returncode == 0:
                return True

            logger.error(f"rsync failed: {result.stderr}")
            return False

        except subprocess.TimeoutExpired:
            msg = "File transfer timed out"
            logger.error(msg)
            raise TransferError(msg)
        except Exception as exc:
            msg = f"File transfer error: {exc}"
            logger.error(msg)
            raise TransferError(msg) from exc

    def check_connectivity(self) -> bool:
        """Check if slave is reachable via SSH.

        Returns:
            True if SSH connection works, False otherwise
        """
        logger.info(f"Checking SSH connectivity to {self.slave_host}...")

        try:
            ret, stdout, stderr = self._ssh_command("echo test", timeout=10)
        except SSHError:
            logger.error("✗ SSH connectivity failed")
            return False

        if ret == 0 and "test" in stdout:
            logger.info("✓ SSH connectivity OK")
            return True

        logger.error(f"✗ SSH connectivity failed: {stderr}")
        return False

    def create_directories(self) -> bool:
        """Create required directories on slave.

        Returns:
            True if all directories created successfully, False otherwise
        """
        logger.info("Creating directories on slave...")

        directories = [
            self.deploy_path,
            DEFAULT_STATE_DIR,
            "/var/log",
        ]

        for directory in directories:
            try:
                ret, _, stderr = self._ssh_command(f"mkdir -p {directory}")
            except SSHError:
                logger.error(f"Failed to create {directory}")
                return False

            if ret != 0:
                logger.error(f"Failed to create {directory}: {stderr}")
                return False

        logger.info("✓ Directories created")
        return True

    def deploy_library(self) -> bool:
        """Deploy bisect library file via rsync.

        Returns:
            True if library deployed successfully, False otherwise
        """
        logger.info(f"Deploying library from {self.local_lib_path} to slave...")

        if not self.local_lib_path.exists():
            logger.error(f"Local library path not found: {self.local_lib_path}")
            return False

        library_file = self.local_lib_path / "bisect-functions.sh"
        if not library_file.exists():
            logger.error(f"Library file not found: {library_file}")
            return False

        # Ensure remote directory exists
        try:
            ret, _, _ = self._ssh_command(f"mkdir -p {self.deploy_path}")
        except SSHError:
            logger.error("Failed to create deploy directory on slave")
            return False

        if ret != 0:
            logger.error("Failed to create deploy directory on slave")
            return False

        # Copy library file
        try:
            if not self._copy_to_slave(
                str(library_file), f"{self.deploy_path}/bisect-functions.sh"
            ):
                logger.error("Failed to copy library file")
                return False
        except TransferError:
            return False

        # Make library executable
        try:
            ret, _, stderr = self._ssh_command(f"chmod +x {self.deploy_path}/bisect-functions.sh")
        except SSHError:
            logger.warning("Failed to chmod library")
            return True  # Not fatal

        if ret != 0:
            logger.warning(f"Failed to chmod library: {stderr}")

        logger.info("✓ Library deployed")
        return True

    def initialize_protection(self) -> bool:
        """Initialize kernel protection on slave.

        Returns:
            True if protection initialized successfully, False otherwise
        """
        logger.info("Initializing kernel protection...")

        # Call init_protection function from library
        init_command = (
            f"source {self.deploy_path}/bisect-functions.sh && init_protection"
        )

        try:
            ret, stdout, stderr = self._ssh_command(init_command, timeout=60)
        except SSHError:
            logger.error("Failed to initialize protection")
            return False

        if ret != 0:
            logger.error(f"Failed to initialize protection: {stderr}")
            return False

        logger.info("✓ Kernel protection initialized")
        logger.debug(f"Protection output: {stdout}")
        return True

    def verify_deployment(self) -> Tuple[bool, List[str]]:
        """Verify deployment is complete and correct.

        Returns:
            Tuple of (all_checks_passed, list_of_check_results)
        """
        logger.info("Verifying deployment...")

        checks = []
        all_passed = True

        # Check 1: Library directory exists
        try:
            ret, _, _ = self._ssh_command(f"test -d {self.deploy_path}")
            if ret == 0:
                checks.append("✓ Library directory exists")
            else:
                checks.append("✗ Library directory missing")
                all_passed = False
        except SSHError:
            checks.append("✗ Library directory check failed")
            all_passed = False

        # Check 2: bisect-functions.sh exists and is executable
        try:
            ret, _, _ = self._ssh_command(f"test -x {self.deploy_path}/bisect-functions.sh")
            if ret == 0:
                checks.append("✓ bisect-functions.sh executable")
            else:
                checks.append("✗ bisect-functions.sh not found")
                all_passed = False
        except SSHError:
            checks.append("✗ bisect-functions.sh check failed")
            all_passed = False

        # Check 3: Protection initialized
        try:
            ret, _, _ = self._ssh_command(
                f"test -f {DEFAULT_STATE_DIR}/protected-kernels.list"
            )
            if ret == 0:
                checks.append("✓ Kernel protection initialized")
            else:
                checks.append("✗ Kernel protection not initialized")
                all_passed = False
        except SSHError:
            checks.append("✗ Kernel protection check failed")
            all_passed = False

        # Check 4: State directory exists
        try:
            ret, _, _ = self._ssh_command(f"test -d {DEFAULT_STATE_DIR}")
            if ret == 0:
                checks.append("✓ State directory exists")
            else:
                checks.append("✗ State directory missing")
                all_passed = False
        except SSHError:
            checks.append("✗ State directory check failed")
            all_passed = False

        for check in checks:
            logger.info(f"  {check}")

        return all_passed, checks

    def deploy_full(self) -> bool:
        """Full deployment workflow.

        Executes all deployment steps in sequence.

        Returns:
            True if deployment successful, False otherwise
        """
        logger.info("=" * 60)
        logger.info("Starting slave deployment")
        logger.info("=" * 60)

        # Step 1: Check connectivity
        if not self.check_connectivity():
            logger.error("Deployment failed: No SSH connectivity")
            return False

        # Step 2: Create directories
        if not self.create_directories():
            logger.error("Deployment failed: Could not create directories")
            return False

        # Step 3: Deploy library
        if not self.deploy_library():
            logger.error("Deployment failed: Could not deploy library")
            return False

        # Step 4: Initialize protection
        if not self.initialize_protection():
            logger.error("Deployment failed: Could not initialize protection")
            return False

        # Step 5: Verify deployment
        success, checks = self.verify_deployment()

        if success:
            logger.info("=" * 60)
            logger.info("✓ Deployment completed successfully!")
            logger.info("=" * 60)
            return True

        logger.error("=" * 60)
        logger.error("✗ Deployment completed with errors")
        logger.error("=" * 60)
        return False

    def is_deployed(self) -> bool:
        """Check if slave is already deployed.

        Returns:
            True if slave appears to be deployed, False otherwise
        """
        # Quick check: do critical components exist?
        critical_checks = [
            f"test -d {self.deploy_path}",
            f"test -x {self.deploy_path}/bisect-functions.sh",
            f"test -f {DEFAULT_STATE_DIR}/protected-kernels.list",
        ]

        for check in critical_checks:
            try:
                ret, _, _ = self._ssh_command(check)
            except SSHError:
                return False

            if ret != 0:
                return False

        return True

    def update_library(self) -> bool:
        """Update only the library file (for updates after initial deployment).

        Returns:
            True if library updated successfully, False otherwise
        """
        logger.info("Updating library file...")

        if not self.deploy_library():
            logger.error("Library update failed")
            return False

        logger.info("✓ Library updated successfully")
        return True


def main() -> int:
    """Test deployer."""
    import argparse

    parser = argparse.ArgumentParser(description="Slave Deployer")
    parser.add_argument("slave_host", help="Slave hostname or IP")
    parser.add_argument("--user", default="root", help="SSH user")
    parser.add_argument(
        "--deploy-path", default=DEFAULT_DEPLOY_PATH, help="Deployment path on slave"
    )
    parser.add_argument("--check-only", action="store_true", help="Only check if deployed")
    parser.add_argument("--update-only", action="store_true", help="Only update library")

    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    deployer = SlaveDeployer(args.slave_host, args.user, args.deploy_path)

    if args.check_only:
        if deployer.is_deployed():
            print("Slave is deployed")
            success, checks = deployer.verify_deployment()
            return 0 if success else 1

        print("Slave is NOT deployed")
        return 1

    if args.update_only:
        return 0 if deployer.update_library() else 1

    # Full deployment
    return 0 if deployer.deploy_full() else 1


if __name__ == "__main__":
    import sys

    sys.exit(main())
