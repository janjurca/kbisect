#!/usr/bin/env python3
"""
Master Bisection Controller
Orchestrates the kernel bisection process across master and slave machines
"""

import os
import sys
import subprocess
import time
import json
import logging
from pathlib import Path
from typing import Optional, Dict, Any, List
from dataclasses import dataclass, asdict
from datetime import datetime
from enum import Enum

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler('/var/log/kernel-bisect-master.log'),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)


class BisectState(Enum):
    """Bisection state"""
    IDLE = "idle"
    BUILDING = "building"
    REBOOTING = "rebooting"
    TESTING = "testing"
    ANALYZING = "analyzing"
    COMPLETE = "complete"
    FAILED = "failed"


class TestResult(Enum):
    """Test result"""
    GOOD = "good"
    BAD = "bad"
    SKIP = "skip"
    UNKNOWN = "unknown"


@dataclass
class BisectConfig:
    """Bisection configuration"""
    slave_host: str
    slave_user: str = "root"
    slave_kernel_path: str = "/root/kernel"
    slave_bisect_path: str = "/root/kernel-bisect/lib"
    ipmi_host: Optional[str] = None
    ipmi_user: Optional[str] = None
    ipmi_password: Optional[str] = None
    boot_timeout: int = 300  # seconds
    test_timeout: int = 600  # seconds
    build_timeout: int = 1800  # seconds
    test_type: str = "boot"
    test_script: Optional[str] = None
    state_dir: str = "."  # Per-directory: metadata/configs in current directory
    db_path: str = "bisect.db"  # Per-directory: database in current directory
    kernel_config_file: Optional[str] = None
    use_running_config: bool = False
    collect_baseline: bool = True
    collect_per_iteration: bool = True
    collect_kernel_config: bool = True


@dataclass
class BisectIteration:
    """Single bisection iteration"""
    iteration: int
    commit_sha: str
    commit_short: str
    commit_message: str
    state: BisectState
    result: Optional[TestResult] = None
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    duration: Optional[int] = None
    error: Optional[str] = None


class SSHClient:
    """SSH client for slave communication"""

    def __init__(self, host: str, user: str = "root"):
        self.host = host
        self.user = user

    def run_command(self, command: str, timeout: Optional[int] = None) -> tuple[int, str, str]:
        """Run command on slave via SSH"""
        ssh_command = [
            "ssh",
            "-o", "StrictHostKeyChecking=no",
            "-o", "ConnectTimeout=10",
            f"{self.user}@{self.host}",
            command
        ]

        try:
            result = subprocess.run(
                ssh_command,
                capture_output=True,
                text=True,
                timeout=timeout
            )
            return result.returncode, result.stdout, result.stderr
        except subprocess.TimeoutExpired:
            logger.error(f"SSH command timed out after {timeout}s")
            return -1, "", "Timeout"
        except Exception as e:
            logger.error(f"SSH command failed: {e}")
            return -1, "", str(e)

    def is_alive(self) -> bool:
        """Check if slave is reachable"""
        ret, _, _ = self.run_command("echo alive", timeout=5)
        return ret == 0

    def call_function(self, function_name: str, *args,
                     library_path: str = "/root/kernel-bisect/lib/bisect-functions.sh",
                     timeout: Optional[int] = None) -> tuple[int, str, str]:
        """Call a bash function from the bisect library

        Args:
            function_name: Name of the bash function to call
            *args: Arguments to pass to the function
            library_path: Path to the bisect library on slave
            timeout: Command timeout in seconds

        Returns:
            Tuple of (return_code, stdout, stderr)
        """
        # Escape and quote arguments
        args_str = ' '.join(f'"{arg}"' for arg in args)

        # Source library and call function
        command = f'source {library_path} && {function_name} {args_str}'

        return self.run_command(command, timeout=timeout)

    def copy_file(self, local_path: str, remote_path: str) -> bool:
        """Copy file to slave"""
        scp_command = [
            "scp",
            "-o", "StrictHostKeyChecking=no",
            local_path,
            f"{self.user}@{self.host}:{remote_path}"
        ]

        try:
            result = subprocess.run(scp_command, capture_output=True, text=True)
            return result.returncode == 0
        except Exception as e:
            logger.error(f"SCP failed: {e}")
            return False


class BisectMaster:
    """Main bisection controller"""

    def __init__(self, config: BisectConfig, good_commit: str, bad_commit: str):
        self.config = config
        self.good_commit = good_commit
        self.bad_commit = bad_commit
        self.ssh = SSHClient(config.slave_host, config.slave_user)
        self.state_file = Path(config.state_dir) / "bisect-state.json"
        self.iterations: List[BisectIteration] = []
        self.current_iteration: Optional[BisectIteration] = None
        self.iteration_count = 0

        # Create state directory
        Path(config.state_dir).mkdir(parents=True, exist_ok=True)

        # Initialize state manager and create/load session
        from state_manager import StateManager
        self.state = StateManager(db_path=config.db_path)

        # Check for existing running session or create new one
        existing_session = self.state.get_latest_session()
        if existing_session and existing_session.status == "running":
            self.session_id = existing_session.session_id
            logger.info(f"Resuming existing session {self.session_id}")
        else:
            self.session_id = self.state.create_session(good_commit, bad_commit)
            logger.debug(f"Created new session {self.session_id}")

    def collect_and_store_metadata(self, collection_type: str,
                                   iteration_id: Optional[int] = None) -> bool:
        """Collect metadata from slave and store in database"""
        logger.debug(f"Collecting {collection_type} metadata...")

        # Call bash function to collect metadata
        ret, stdout, stderr = self.ssh.call_function(
            "collect_metadata",
            collection_type,
            timeout=30
        )

        if ret != 0:
            logger.warning(f"Failed to collect {collection_type} metadata: {stderr}")
            return False

        # Parse JSON response
        try:
            metadata_dict = json.loads(stdout)
        except json.JSONDecodeError as e:
            logger.warning(f"Invalid JSON from metadata collection: {e}")
            logger.debug(f"Raw output: {stdout}")
            return False

        # Store in database
        self.state.store_metadata(self.session_id, metadata_dict, iteration_id)
        logger.debug(f"✓ Stored {collection_type} metadata")

        return True

    def capture_kernel_config(self, kernel_version: str, iteration_id: int) -> bool:
        """Capture and store kernel config file from slave"""
        config_path = f"/boot/config-{kernel_version}"

        # Create local storage directory
        local_config_dir = Path(self.config.state_dir) / "configs"
        local_config_dir.mkdir(parents=True, exist_ok=True)
        local_config_path = local_config_dir / f"config-{kernel_version}"

        # Download config file from slave using scp
        scp_cmd = [
            "scp",
            "-o", "StrictHostKeyChecking=no",
            f"{self.config.slave_user}@{self.config.slave_host}:{config_path}",
            str(local_config_path)
        ]

        try:
            result = subprocess.run(scp_cmd, capture_output=True, text=True, timeout=30)
            if result.returncode != 0:
                logger.warning(f"Failed to download kernel config: {result.stderr}")
                return False
        except subprocess.TimeoutExpired:
            logger.warning("Kernel config download timed out")
            return False
        except Exception as e:
            logger.warning(f"Error downloading kernel config: {e}")
            return False

        # Store reference in database
        # Get metadata for this iteration to link the config file
        metadata_list = self.state.get_session_metadata(self.session_id, 'iteration')
        if metadata_list:
            # Find metadata for this iteration
            iteration_metadata = [m for m in metadata_list if m.get('iteration_id') == iteration_id]
            if iteration_metadata:
                metadata_id = iteration_metadata[0]['metadata_id']
                self.state.store_metadata_file(
                    metadata_id,
                    "kernel_config",
                    str(local_config_path),
                    compressed=False
                )
                logger.info(f"✓ Captured kernel config: {config_path}")
                return True

        logger.warning("Could not find metadata record to link kernel config")
        return False

    def initialize(self) -> bool:
        """Initialize bisection"""
        logger.info("=== Initializing Kernel Bisection ===")
        logger.info(f"Good commit: {self.good_commit}")
        logger.info(f"Bad commit: {self.bad_commit}")
        logger.info(f"Slave: {self.config.slave_host}")

        # Check slave is reachable
        logger.info("Checking slave connectivity...")
        if not self.ssh.is_alive():
            logger.error("Slave is not reachable!")
            return False

        logger.info("✓ Slave is reachable")

        # Initialize protection on slave
        logger.info("Initializing kernel protection on slave...")
        ret, stdout, stderr = self.ssh.call_function("init_protection")

        if ret != 0:
            logger.error(f"Failed to initialize protection: {stderr}")
            return False

        logger.info("✓ Kernel protection initialized")

        # Collect baseline metadata if enabled
        if self.config.collect_baseline:
            logger.info("Collecting baseline system metadata...")
            if self.collect_and_store_metadata("baseline"):
                logger.info("✓ Baseline metadata collected")
            else:
                logger.warning("Failed to collect baseline metadata (non-fatal)")

        # Start git bisect
        logger.info("Starting git bisect...")
        # Note: This would normally be done on the slave's kernel source
        # For now, we'll manage bisect state ourselves

        self.save_state()
        logger.info("=== Initialization Complete ===")
        return True

    def get_next_commit(self) -> Optional[str]:
        """Get next commit to test using git bisect"""
        # This is a simplified implementation
        # In practice, you'd run git bisect on the kernel source

        # For now, we'll use git bisect commands via SSH on the slave
        ret, stdout, stderr = self.ssh.run_command(
            f"cd {self.config.slave_kernel_path} && "
            f"git bisect start {self.bad_commit} {self.good_commit} 2>&1 || "
            f"git rev-parse HEAD"
        )

        if ret != 0:
            logger.error(f"Failed to get next commit: {stderr}")
            return None

        commit = stdout.strip()
        if not commit or len(commit) != 40:
            logger.error(f"Invalid commit hash: {commit}")
            return None

        return commit

    def build_kernel(self, commit_sha: str) -> bool:
        """Build kernel on slave"""
        logger.info(f"Building kernel for commit {commit_sha[:7]}...")

        # Determine kernel config source (CLI arg > config file > running > none)
        kernel_config = ""
        if self.config.kernel_config_file:
            kernel_config = self.config.kernel_config_file
            logger.debug(f"Using kernel config file: {kernel_config}")
        elif self.config.use_running_config:
            kernel_config = "RUNNING"
            logger.debug("Using running kernel config")

        # Call build_kernel function from library
        ret, stdout, stderr = self.ssh.call_function(
            "build_kernel",
            commit_sha,
            self.config.slave_kernel_path,
            kernel_config,
            timeout=self.config.build_timeout
        )

        if ret != 0:
            logger.error(f"Kernel build failed: {stderr}")
            logger.debug(f"Build output: {stdout}")
            return False

        logger.info("✓ Kernel build complete")
        logger.debug(f"Kernel version: {stdout.strip()}")
        return True

    def reboot_slave(self) -> tuple[bool, Optional[str]]:
        """Reboot slave machine and return (success, booted_kernel_version)"""
        logger.info("Rebooting slave...")

        # Send reboot command
        self.ssh.run_command("reboot", timeout=5)

        # Wait a bit for reboot to start
        time.sleep(10)

        # Wait for slave to come back up
        logger.info("Waiting for slave to reboot...")
        max_wait = self.config.boot_timeout
        waited = 0

        while waited < max_wait:
            time.sleep(5)
            waited += 5

            if self.ssh.is_alive():
                logger.info(f"✓ Slave is back online after {waited}s")
                # Give it a bit more time to fully boot
                time.sleep(10)

                # Get kernel version that booted
                ret, kernel_ver, _ = self.ssh.call_function("get_kernel_version")
                if ret == 0 and kernel_ver.strip():
                    return (True, kernel_ver.strip())
                else:
                    logger.warning("Could not determine booted kernel version")
                    return (True, None)

            if waited % 30 == 0:
                logger.info(f"Still waiting... ({waited}/{max_wait}s)")

        # Timeout! Try IPMI recovery if configured
        logger.error("Slave failed to reboot within timeout")

        if self.config.ipmi_host:
            logger.warning("Attempting IPMI power cycle for recovery...")
            try:
                from ipmi_controller import IPMIController
                ipmi = IPMIController(
                    self.config.ipmi_host,
                    self.config.ipmi_user,
                    self.config.ipmi_password
                )

                # Force power cycle
                ipmi.power_cycle()
                logger.info("IPMI power cycle initiated, waiting for system to boot...")

                # Wait again for slave to come back (should boot protected kernel)
                time.sleep(10)
                ipmi_wait = 0
                ipmi_max_wait = self.config.boot_timeout

                while ipmi_wait < ipmi_max_wait:
                    time.sleep(5)
                    ipmi_wait += 5

                    if self.ssh.is_alive():
                        logger.info(f"✓ Slave recovered via IPMI after {ipmi_wait}s")
                        time.sleep(10)

                        # Get kernel version
                        ret, kernel_ver, _ = self.ssh.call_function("get_kernel_version")
                        if ret == 0 and kernel_ver.strip():
                            logger.info(f"Booted into kernel: {kernel_ver.strip()}")
                            return (True, kernel_ver.strip())
                        else:
                            return (True, None)

                    if ipmi_wait % 30 == 0:
                        logger.info(f"Still waiting after IPMI... ({ipmi_wait}/{ipmi_max_wait}s)")

                logger.error("IPMI recovery failed - slave still not responding")
            except Exception as e:
                logger.error(f"IPMI recovery failed: {e}")
        else:
            logger.warning("IPMI not configured - cannot attempt automatic recovery")

        return (False, None)

    def run_tests(self) -> TestResult:
        """Run tests on slave"""
        logger.info("Running tests on slave...")

        # Call run_test function from library
        if self.config.test_script:
            ret, stdout, stderr = self.ssh.call_function(
                "run_test",
                self.config.test_type,
                self.config.test_script,
                timeout=self.config.test_timeout
            )
        else:
            ret, stdout, stderr = self.ssh.call_function(
                "run_test",
                self.config.test_type,
                timeout=self.config.test_timeout
            )

        logger.debug(f"Test output: {stdout}")

        if ret == 0:
            logger.info("✓ Tests PASSED")
            return TestResult.GOOD
        else:
            logger.error("✗ Tests FAILED")
            logger.debug(f"Test error: {stderr}")
            return TestResult.BAD

    def mark_commit(self, commit_sha: str, result: TestResult) -> bool:
        """Mark commit as good or bad in git bisect"""
        if result == TestResult.SKIP:
            bisect_cmd = "git bisect skip"
        elif result == TestResult.GOOD:
            bisect_cmd = "git bisect good"
        elif result == TestResult.BAD:
            bisect_cmd = "git bisect bad"
        else:
            logger.error(f"Cannot mark commit with result: {result}")
            return False

        ret, stdout, stderr = self.ssh.run_command(
            f"cd {self.config.slave_kernel_path} && {bisect_cmd}"
        )

        if ret != 0:
            logger.error(f"Failed to mark commit: {stderr}")
            return False

        logger.info(f"Marked commit {commit_sha[:7]} as {result.value}")
        return True

    def run_iteration(self, commit_sha: str) -> BisectIteration:
        """Run single bisection iteration"""
        self.iteration_count += 1

        # Get commit info
        ret, commit_msg, _ = self.ssh.run_command(
            f"cd {self.config.slave_kernel_path} && git log -1 --oneline {commit_sha}"
        )
        commit_msg = commit_msg.strip() if ret == 0 else "Unknown"

        # Create iteration in database
        iteration_id = self.state.create_iteration(
            self.session_id,
            self.iteration_count,
            commit_sha,
            commit_msg
        )

        iteration = BisectIteration(
            iteration=self.iteration_count,
            commit_sha=commit_sha,
            commit_short=commit_sha[:7],
            commit_message=commit_msg,
            state=BisectState.IDLE,
            start_time=datetime.utcnow().isoformat()
        )

        self.current_iteration = iteration
        logger.info(f"\n=== Iteration {iteration.iteration}: {iteration.commit_short} ===")
        logger.info(f"Commit: {iteration.commit_message}")

        try:
            # Build kernel
            iteration.state = BisectState.BUILDING
            self.save_state()

            if not self.build_kernel(commit_sha):
                iteration.result = TestResult.SKIP
                iteration.error = "Build failed"
                logger.error("Build failed, skipping commit")
                self.mark_commit(commit_sha, TestResult.SKIP)
                self.state.update_iteration(iteration_id, final_result="skip", error_message="Build failed")
                return iteration

            # Get kernel version that was just built (needed for boot verification)
            ret, kernel_version, _ = self.ssh.run_command(
                f"cd {self.config.slave_kernel_path} && make kernelrelease"
            )
            if ret == 0 and kernel_version.strip():
                expected_kernel_ver = kernel_version.strip()
                logger.info(f"Built kernel version: {expected_kernel_ver}")
            else:
                logger.warning("Could not determine kernel version")
                expected_kernel_ver = None

            # Reboot slave
            iteration.state = BisectState.REBOOTING
            self.save_state()

            reboot_success, actual_kernel_ver = self.reboot_slave()

            if not reboot_success:
                # Boot timeout or complete failure - apply conditional marking
                logger.error("✗ Boot timeout or failure!")
                logger.error(f"  Expected kernel: {expected_kernel_ver}")
                logger.error(f"  Slave did not respond within timeout")

                if self.config.test_type == "boot" or not self.config.test_script:
                    # Boot test mode: timeout is BAD (we're testing bootability)
                    iteration.result = TestResult.BAD
                    iteration.error = "Boot timeout - kernel failed to boot"
                    logger.error(f"  Marking as BAD (boot test mode)")
                    self.mark_commit(commit_sha, TestResult.BAD)
                    self.state.update_iteration(iteration_id, final_result="bad", error_message=iteration.error)
                else:
                    # Custom test mode: can't test functionality if kernel doesn't boot
                    iteration.result = TestResult.SKIP
                    iteration.error = "Boot timeout - cannot test functionality, skipping commit"
                    logger.warning(f"  Marking as SKIP (custom test mode - cannot test if kernel doesn't boot)")
                    self.mark_commit(commit_sha, TestResult.SKIP)
                    self.state.update_iteration(iteration_id, final_result="skip", error_message=iteration.error)

                return iteration

            # Verify which kernel actually booted (critical for detecting kernel panics)
            if actual_kernel_ver:
                logger.info(f"Booted kernel version: {actual_kernel_ver}")

                # Compare expected vs actual kernel
                if expected_kernel_ver and actual_kernel_ver != expected_kernel_ver:
                    # Kernel panic detected - system fell back to protected kernel
                    # Decision: BAD or SKIP depends on test type
                    logger.error(f"✗ Kernel panic detected!")
                    logger.error(f"  Expected: {expected_kernel_ver}")
                    logger.error(f"  Actual:   {actual_kernel_ver}")
                    logger.error(f"  Test kernel failed to boot, fell back to protected kernel")

                    if self.config.test_type == "boot" or not self.config.test_script:
                        # Boot test mode: non-bootable kernel is BAD (this is what we're testing)
                        iteration.result = TestResult.BAD
                        iteration.error = f"Kernel panic detected - kernel failed to boot"
                        logger.error(f"  Marking as BAD (boot test mode)")
                        self.mark_commit(commit_sha, TestResult.BAD)
                        self.state.update_iteration(iteration_id, final_result="bad", error_message=iteration.error)
                    else:
                        # Custom test mode: can't test functionality if kernel doesn't boot
                        iteration.result = TestResult.SKIP
                        iteration.error = f"Kernel panic detected - cannot test functionality, skipping commit"
                        logger.warning(f"  Marking as SKIP (custom test mode - cannot test if kernel doesn't boot)")
                        self.mark_commit(commit_sha, TestResult.SKIP)
                        self.state.update_iteration(iteration_id, final_result="skip", error_message=iteration.error)

                    return iteration
                else:
                    logger.info(f"✓ Correct kernel booted successfully")
            else:
                logger.warning("Could not verify booted kernel version")

            # Capture kernel config now that it exists in /boot
            if self.config.collect_kernel_config and expected_kernel_ver:
                self.capture_kernel_config(expected_kernel_ver, iteration_id)

            # Collect iteration metadata if enabled
            if self.config.collect_per_iteration:
                logger.info("Collecting iteration metadata...")
                if self.collect_and_store_metadata("iteration", iteration_id):
                    logger.debug("✓ Iteration metadata collected")

            # Run tests
            iteration.state = BisectState.TESTING
            self.save_state()

            test_result = self.run_tests()
            iteration.result = test_result

            # Update iteration in database
            self.state.update_iteration(
                iteration_id,
                final_result=test_result.value,
                end_time=datetime.utcnow().isoformat()
            )

            # Mark in git bisect
            self.mark_commit(commit_sha, test_result)

        except Exception as e:
            logger.error(f"Iteration failed with exception: {e}")
            iteration.result = TestResult.SKIP
            iteration.error = str(e)

        finally:
            iteration.end_time = datetime.utcnow().isoformat()
            if iteration.start_time and iteration.end_time:
                start = datetime.fromisoformat(iteration.start_time)
                end = datetime.fromisoformat(iteration.end_time)
                iteration.duration = int((end - start).total_seconds())

            self.iterations.append(iteration)
            self.save_state()

        return iteration

    def run(self) -> bool:
        """Run complete bisection"""
        logger.info("\n=== Starting Bisection ===\n")

        while True:
            # Get next commit to test
            commit = self.get_next_commit()

            if not commit:
                logger.info("No more commits to test - bisection complete!")
                break

            # Check if this is the first bad commit found
            ret, bisect_status, _ = self.ssh.run_command(
                f"cd {self.config.slave_kernel_path} && git bisect log"
            )

            # Run iteration
            iteration = self.run_iteration(commit)

            logger.info(f"Result: {iteration.result.value}")

            # Check if bisection is done
            ret, stdout, _ = self.ssh.run_command(
                f"cd {self.config.slave_kernel_path} && git bisect log | tail -1"
            )

            if "is the first bad commit" in stdout:
                logger.info("\n=== Bisection Found First Bad Commit! ===")
                self.generate_report()
                return True

        self.generate_report()
        return True

    def save_state(self):
        """Save bisection state to file"""
        state = {
            "good_commit": self.good_commit,
            "bad_commit": self.bad_commit,
            "iteration_count": self.iteration_count,
            "current_iteration": asdict(self.current_iteration) if self.current_iteration else None,
            "iterations": [asdict(it) for it in self.iterations],
            "last_update": datetime.utcnow().isoformat()
        }

        with open(self.state_file, 'w') as f:
            json.dump(state, f, indent=2)

    def generate_report(self):
        """Generate bisection report"""
        logger.info("\n" + "=" * 60)
        logger.info("BISECTION REPORT")
        logger.info("=" * 60)

        logger.info(f"\nGood commit: {self.good_commit}")
        logger.info(f"Bad commit:  {self.bad_commit}")
        logger.info(f"Total iterations: {len(self.iterations)}")

        logger.info("\nIteration Summary:")
        logger.info("-" * 60)

        for iteration in self.iterations:
            status = iteration.result.value if iteration.result else "unknown"
            duration = f"{iteration.duration}s" if iteration.duration else "N/A"
            logger.info(f"{iteration.iteration:3d}. {iteration.commit_short} | "
                       f"{status:7s} | {duration:6s} | {iteration.commit_message[:50]}")

        # Get final result from git bisect
        ret, stdout, _ = self.ssh.run_command(
            f"cd {self.config.slave_kernel_path} && git bisect log | grep 'first bad commit' -A 5"
        )

        if ret == 0 and stdout:
            logger.info("\n" + "=" * 60)
            logger.info("FIRST BAD COMMIT:")
            logger.info("=" * 60)
            logger.info(stdout)

        logger.info("=" * 60 + "\n")


def main():
    """Main entry point"""
    # This would normally parse arguments
    # For now, it's a placeholder

    print("Kernel Bisect Master Controller")
    print("Usage: Import this module and use the BisectMaster class")
    return 0


if __name__ == "__main__":
    sys.exit(main())
