#!/usr/bin/env python3
"""
Slave Deployer - Automatic deployment of slave components
Handles copying scripts, installing services, and initializing the slave machine
"""

import subprocess
import logging
import os
from pathlib import Path
from typing import Optional, List, Tuple

logger = logging.getLogger(__name__)


class SlaveDeployer:
    """Automatically deploy and configure slave machine"""

    def __init__(self, slave_host: str, slave_user: str = "root",
                 deploy_path: str = "/root/kernel-bisect/lib",
                 local_lib_path: Optional[str] = None):
        self.slave_host = slave_host
        self.slave_user = slave_user
        self.deploy_path = deploy_path

        # Determine local library path
        if local_lib_path:
            self.local_lib_path = Path(local_lib_path)
        else:
            # Assume we're in master/ directory or kernel-bisect/ root
            script_dir = Path(__file__).parent.parent
            self.local_lib_path = script_dir / "lib"

    def _ssh_command(self, command: str, timeout: int = 30) -> Tuple[int, str, str]:
        """Execute SSH command on slave"""
        ssh_cmd = [
            "ssh",
            "-o", "StrictHostKeyChecking=no",
            "-o", "ConnectTimeout=10",
            f"{self.slave_user}@{self.slave_host}",
            command
        ]

        try:
            result = subprocess.run(
                ssh_cmd,
                capture_output=True,
                text=True,
                timeout=timeout
            )
            return result.returncode, result.stdout, result.stderr
        except subprocess.TimeoutExpired:
            logger.error(f"SSH command timed out: {command}")
            return -1, "", "Timeout"
        except Exception as e:
            logger.error(f"SSH command failed: {e}")
            return -1, "", str(e)

    def _rsync_to_slave(self, local_path: str, remote_path: str,
                        options: str = "-avz") -> bool:
        """Rsync files to slave"""
        rsync_cmd = [
            "rsync",
            *options.split(),
            "--rsync-path", "mkdir -p $(dirname {}) && rsync".format(remote_path),
            local_path,
            f"{self.slave_user}@{self.slave_host}:{remote_path}"
        ]

        try:
            result = subprocess.run(rsync_cmd, capture_output=True, text=True)
            if result.returncode == 0:
                return True
            else:
                logger.error(f"Rsync failed: {result.stderr}")
                return False
        except Exception as e:
            logger.error(f"Rsync error: {e}")
            return False

    def check_connectivity(self) -> bool:
        """Check if slave is reachable via SSH"""
        logger.info(f"Checking SSH connectivity to {self.slave_host}...")

        ret, stdout, stderr = self._ssh_command("echo test", timeout=10)

        if ret == 0 and "test" in stdout:
            logger.info("✓ SSH connectivity OK")
            return True
        else:
            logger.error(f"✗ SSH connectivity failed: {stderr}")
            return False

    def create_directories(self) -> bool:
        """Create required directories on slave"""
        logger.info("Creating directories on slave...")

        directories = [
            self.deploy_path,
            "/var/lib/kernel-bisect",
            "/var/log",
        ]

        for directory in directories:
            ret, _, stderr = self._ssh_command(f"mkdir -p {directory}")
            if ret != 0:
                logger.error(f"Failed to create {directory}: {stderr}")
                return False

        logger.info("✓ Directories created")
        return True

    def deploy_library(self) -> bool:
        """Deploy bisect library file via rsync"""
        logger.info(f"Deploying library from {self.local_lib_path} to slave...")

        if not self.local_lib_path.exists():
            logger.error(f"Local library path not found: {self.local_lib_path}")
            return False

        library_file = self.local_lib_path / "bisect-functions.sh"
        if not library_file.exists():
            logger.error(f"Library file not found: {library_file}")
            return False

        # Ensure remote directory exists
        ret, _, _ = self._ssh_command(f"mkdir -p {self.deploy_path}")
        if ret != 0:
            logger.error("Failed to create deploy directory on slave")
            return False

        # Copy library file
        if not self._rsync_to_slave(
            str(library_file),
            f"{self.deploy_path}/bisect-functions.sh"
        ):
            logger.error("Failed to copy library file")
            return False

        # Make library executable
        ret, _, stderr = self._ssh_command(f"chmod +x {self.deploy_path}/bisect-functions.sh")
        if ret != 0:
            logger.warning(f"Failed to chmod library: {stderr}")

        logger.info("✓ Library deployed")
        return True

    def initialize_protection(self) -> bool:
        """Initialize kernel protection on slave"""
        logger.info("Initializing kernel protection...")

        # Call init_protection function from library
        init_command = f"source {self.deploy_path}/bisect-functions.sh && init_protection"

        ret, stdout, stderr = self._ssh_command(init_command, timeout=60)

        if ret != 0:
            logger.error(f"Failed to initialize protection: {stderr}")
            return False

        logger.info("✓ Kernel protection initialized")
        logger.debug(f"Protection output: {stdout}")
        return True

    def verify_deployment(self) -> Tuple[bool, List[str]]:
        """Verify deployment is complete and correct"""
        logger.info("Verifying deployment...")

        checks = []
        all_passed = True

        # Check 1: Library directory exists
        ret, _, _ = self._ssh_command(f"test -d {self.deploy_path}")
        if ret == 0:
            checks.append("✓ Library directory exists")
        else:
            checks.append("✗ Library directory missing")
            all_passed = False

        # Check 2: bisect-functions.sh exists and is executable
        ret, _, _ = self._ssh_command(f"test -x {self.deploy_path}/bisect-functions.sh")
        if ret == 0:
            checks.append("✓ bisect-functions.sh executable")
        else:
            checks.append("✗ bisect-functions.sh not found")
            all_passed = False

        # Check 3: Protection initialized
        ret, _, _ = self._ssh_command("test -f /var/lib/kernel-bisect/protected-kernels.list")
        if ret == 0:
            checks.append("✓ Kernel protection initialized")
        else:
            checks.append("✗ Kernel protection not initialized")
            all_passed = False

        # Check 4: State directory exists
        ret, _, _ = self._ssh_command("test -d /var/lib/kernel-bisect")
        if ret == 0:
            checks.append("✓ State directory exists")
        else:
            checks.append("✗ State directory missing")
            all_passed = False

        for check in checks:
            logger.info(f"  {check}")

        return all_passed, checks

    def deploy_full(self) -> bool:
        """
        Full deployment workflow
        Returns True if successful, False otherwise
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
        else:
            logger.error("=" * 60)
            logger.error("✗ Deployment completed with errors")
            logger.error("=" * 60)
            return False

    def is_deployed(self) -> bool:
        """Check if slave is already deployed"""
        # Quick check: do critical components exist?
        critical_checks = [
            f"test -d {self.deploy_path}",
            f"test -x {self.deploy_path}/bisect-functions.sh",
            "test -f /var/lib/kernel-bisect/protected-kernels.list"
        ]

        for check in critical_checks:
            ret, _, _ = self._ssh_command(check)
            if ret != 0:
                return False

        return True

    def update_library(self) -> bool:
        """Update only the library file (for updates after initial deployment)"""
        logger.info("Updating library file...")

        if not self.deploy_library():
            logger.error("Library update failed")
            return False

        logger.info("✓ Library updated successfully")
        return True


def main():
    """Test deployer"""
    import argparse

    parser = argparse.ArgumentParser(description="Slave Deployer")
    parser.add_argument("slave_host", help="Slave hostname or IP")
    parser.add_argument("--user", default="root", help="SSH user")
    parser.add_argument("--deploy-path", default="/root/kernel-bisect/lib",
                       help="Deployment path on slave")
    parser.add_argument("--check-only", action="store_true",
                       help="Only check if deployed")
    parser.add_argument("--update-only", action="store_true",
                       help="Only update library")

    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO,
                       format='%(asctime)s [%(levelname)s] %(message)s')

    deployer = SlaveDeployer(args.slave_host, args.user, args.deploy_path)

    if args.check_only:
        if deployer.is_deployed():
            print("Slave is deployed")
            success, checks = deployer.verify_deployment()
            return 0 if success else 1
        else:
            print("Slave is NOT deployed")
            return 1

    elif args.update_only:
        if deployer.update_library():
            return 0
        else:
            return 1

    else:
        # Full deployment
        if deployer.deploy_full():
            return 0
        else:
            return 1


if __name__ == "__main__":
    import sys
    sys.exit(main())
