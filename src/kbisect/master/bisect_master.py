#!/usr/bin/env python3
"""Master Bisection Controller.

Orchestrates the kernel bisection process across master and slave machines.
"""

import json
import logging
import shlex
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, List, Optional, Tuple


if TYPE_CHECKING:
    from kbisect.master.console_collector import ConsoleCollector
    from kbisect.master.ipmi_controller import IPMIController


logger = logging.getLogger(__name__)

# Constants
DEFAULT_BOOT_TIMEOUT = 300
DEFAULT_TEST_TIMEOUT = 600
DEFAULT_BUILD_TIMEOUT = 1800
DEFAULT_REBOOT_SETTLE_TIME = 10
DEFAULT_POST_BOOT_SETTLE_TIME = 10
COMMIT_HASH_LENGTH = 40
SHORT_COMMIT_LENGTH = 7


class BisectState(Enum):
    """Bisection state."""

    IDLE = "idle"
    BUILDING = "building"
    REBOOTING = "rebooting"
    TESTING = "testing"
    ANALYZING = "analyzing"
    COMPLETE = "complete"
    FAILED = "failed"


class TestResult(Enum):
    """Test result."""

    GOOD = "good"
    BAD = "bad"
    SKIP = "skip"
    UNKNOWN = "unknown"


@dataclass
class BisectConfig:
    """Bisection configuration.

    Attributes:
        slave_host: Slave machine hostname or IP
        slave_user: SSH username for slave
        slave_kernel_path: Path to kernel source on slave
        slave_bisect_path: Path to bisect library on slave
        ipmi_host: IPMI interface hostname or IP (optional)
        ipmi_user: IPMI username (optional)
        ipmi_password: IPMI password (optional)
        boot_timeout: Boot timeout in seconds
        test_timeout: Test timeout in seconds
        build_timeout: Build timeout in seconds
        test_type: Test type (boot or custom)
        test_script: Path to custom test script (optional)
        state_dir: Directory for state/metadata storage
        db_path: Path to SQLite database
        kernel_config_file: Path to kernel config file (optional)
        use_running_config: Use running kernel config as base
        collect_baseline: Collect baseline system metadata
        collect_per_iteration: Collect metadata per iteration
        collect_kernel_config: Collect kernel .config files
        collect_console_logs: Collect console logs during boot
        console_collector_type: Console collector type (conserver, ipmi, auto)
        console_hostname: Override hostname for console connection
        console_fallback_ipmi: Fall back to IPMI SOL if conserver fails
    """

    slave_host: str
    slave_user: str = "root"
    slave_kernel_path: str = "/root/kernel"
    slave_bisect_path: str = "/root/kernel-bisect/lib"
    ipmi_host: Optional[str] = None
    ipmi_user: Optional[str] = None
    ipmi_password: Optional[str] = None
    boot_timeout: int = DEFAULT_BOOT_TIMEOUT
    test_timeout: int = DEFAULT_TEST_TIMEOUT
    build_timeout: int = DEFAULT_BUILD_TIMEOUT
    test_type: str = "boot"
    test_script: Optional[str] = None
    state_dir: str = "."
    db_path: str = "bisect.db"
    kernel_config_file: Optional[str] = None
    use_running_config: bool = False
    collect_baseline: bool = True
    collect_per_iteration: bool = True
    collect_kernel_config: bool = True
    collect_console_logs: bool = False
    console_collector_type: str = "auto"
    console_hostname: Optional[str] = None
    console_fallback_ipmi: bool = True


@dataclass
class BisectIteration:
    """Single bisection iteration.

    Attributes:
        iteration: Iteration number
        commit_sha: Full commit SHA
        commit_short: Short commit SHA
        commit_message: Commit message
        state: Current bisection state
        result: Test result (None until complete)
        start_time: ISO timestamp of iteration start
        end_time: ISO timestamp of iteration end
        duration: Duration in seconds
        error: Error message if iteration failed
    """

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
    """SSH client for slave communication.

    Provides methods to execute commands on slave via SSH and copy files.

    Attributes:
        host: Slave hostname or IP
        user: SSH username
    """

    def __init__(self, host: str, user: str = "root") -> None:
        """Initialize SSH client.

        Args:
            host: Slave hostname or IP
            user: SSH username
        """
        self.host = host
        self.user = user

    def run_command(
        self, command: str, timeout: Optional[int] = None
    ) -> Tuple[int, str, str]:
        """Run command on slave via SSH.

        Args:
            command: Command to execute
            timeout: Command timeout in seconds

        Returns:
            Tuple of (return_code, stdout, stderr)
        """
        ssh_command = [
            "ssh",
            "-o",
            "StrictHostKeyChecking=no",
            "-o",
            "ConnectTimeout=10",
            f"{self.user}@{self.host}",
            command,
        ]

        try:
            result = subprocess.run(
                ssh_command, capture_output=True, text=True, timeout=timeout, check=False
            )
            return result.returncode, result.stdout, result.stderr
        except subprocess.TimeoutExpired:
            logger.error(f"SSH command timed out after {timeout}s")
            return -1, "", "Timeout"
        except Exception as exc:
            logger.error(f"SSH command failed: {exc}")
            return -1, "", str(exc)

    def is_alive(self) -> bool:
        """Check if slave is reachable.

        Returns:
            True if slave responds to SSH, False otherwise
        """
        ret, _, _ = self.run_command("echo alive", timeout=5)
        return ret == 0

    def call_function(
        self,
        function_name: str,
        *args: str,
        library_path: str = "/root/kernel-bisect/lib/bisect-functions.sh",
        timeout: Optional[int] = None,
    ) -> Tuple[int, str, str]:
        """Call a bash function from the bisect library.

        Args:
            function_name: Name of the bash function to call
            *args: Arguments to pass to the function
            library_path: Path to the bisect library on slave
            timeout: Command timeout in seconds

        Returns:
            Tuple of (return_code, stdout, stderr)
        """
        # Properly escape arguments to prevent command injection
        args_str = " ".join(shlex.quote(str(arg)) for arg in args)

        # Source library and call function (quote paths for safety)
        command = f"source {shlex.quote(library_path)} && {function_name} {args_str}"

        return self.run_command(command, timeout=timeout)

    def copy_file(self, local_path: str, remote_path: str) -> bool:
        """Copy file to slave.

        Args:
            local_path: Local file path
            remote_path: Remote destination path

        Returns:
            True if copy succeeded, False otherwise
        """
        scp_command = [
            "scp",
            "-o",
            "StrictHostKeyChecking=no",
            local_path,
            f"{self.user}@{self.host}:{remote_path}",
        ]

        try:
            result = subprocess.run(scp_command, capture_output=True, text=True, check=False)
            return result.returncode == 0
        except Exception as exc:
            logger.error(f"SCP failed: {exc}")
            return False


class BisectMaster:
    """Main bisection controller.

    Orchestrates the kernel bisection process including building kernels,
    rebooting systems, running tests, and managing state.

    Attributes:
        config: Bisection configuration
        good_commit: Known good commit
        bad_commit: Known bad commit
        ssh: SSH client for slave communication
        state_file: Path to state JSON file
        iterations: List of completed iterations
        current_iteration: Currently executing iteration
        iteration_count: Total iteration count
        session_id: Database session ID
        state: State manager instance
    """

    def __init__(self, config: BisectConfig, good_commit: str, bad_commit: str) -> None:
        """Initialize bisection master.

        Args:
            config: Bisection configuration
            good_commit: Known good commit hash
            bad_commit: Known bad commit hash
        """
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
        from kbisect.master.state_manager import StateManager

        self.state = StateManager(db_path=config.db_path)

        # Atomically get existing running session or create new one
        # This prevents race conditions when multiple instances are created
        self.session_id = self.state.get_or_create_session(good_commit, bad_commit)

        # Initialize IPMI controller if configured
        self.ipmi_controller: Optional["IPMIController"] = None  # noqa: UP037
        if config.ipmi_host and config.ipmi_user and config.ipmi_password:
            from kbisect.master.ipmi_controller import IPMIController

            self.ipmi_controller = IPMIController(
                config.ipmi_host, config.ipmi_user, config.ipmi_password
            )

        # Console collector (created per boot cycle)
        self.active_console_collector: Optional["ConsoleCollector"] = None  # noqa: UP037

    def collect_and_store_metadata(
        self, collection_type: str, iteration_id: Optional[int] = None
    ) -> bool:
        """Collect metadata from slave and store in database.

        Args:
            collection_type: Type of metadata to collect
            iteration_id: Optional iteration ID

        Returns:
            True if metadata collected successfully, False otherwise
        """
        logger.debug(f"Collecting {collection_type} metadata...")

        # Call bash function to collect metadata
        ret, stdout, stderr = self.ssh.call_function(
            "collect_metadata", collection_type, timeout=30
        )

        if ret != 0:
            logger.warning(f"Failed to collect {collection_type} metadata: {stderr}")
            return False

        # Parse JSON response
        try:
            metadata_dict = json.loads(stdout)
        except json.JSONDecodeError as exc:
            logger.warning(f"Invalid JSON from metadata collection: {exc}")
            logger.debug(f"Raw output: {stdout}")
            return False

        # Store in database
        self.state.store_metadata(self.session_id, metadata_dict, iteration_id)
        logger.debug(f"✓ Stored {collection_type} metadata")

        return True

    def capture_kernel_config(self, kernel_version: str, iteration_id: int) -> bool:
        """Capture and store kernel config file from slave.

        Args:
            kernel_version: Kernel version string
            iteration_id: Iteration ID for linking config file

        Returns:
            True if config captured successfully, False otherwise
        """
        config_path = f"/boot/config-{kernel_version}"

        # Create local storage directory
        local_config_dir = Path(self.config.state_dir) / "configs"
        local_config_dir.mkdir(parents=True, exist_ok=True)
        local_config_path = local_config_dir / f"config-{kernel_version}"

        # Download config file from slave using scp
        scp_cmd = [
            "scp",
            "-o",
            "StrictHostKeyChecking=no",
            f"{self.config.slave_user}@{self.config.slave_host}:{config_path}",
            str(local_config_path),
        ]

        try:
            result = subprocess.run(scp_cmd, capture_output=True, text=True, timeout=30, check=False)
            if result.returncode != 0:
                logger.warning(f"Failed to download kernel config: {result.stderr}")
                return False
        except subprocess.TimeoutExpired:
            logger.warning("Kernel config download timed out")
            return False
        except Exception as exc:
            logger.warning(f"Error downloading kernel config: {exc}")
            return False

        # Store reference in database
        metadata_list = self.state.get_session_metadata(self.session_id, "iteration")
        if metadata_list:
            # Find metadata for this iteration
            iteration_metadata = [
                m for m in metadata_list if m.get("iteration_id") == iteration_id
            ]
            if iteration_metadata:
                metadata_id = iteration_metadata[0]["metadata_id"]
                self.state.store_metadata_file(
                    metadata_id, "kernel_config", str(local_config_path), compressed=False
                )
                logger.info(f"✓ Captured kernel config: {config_path}")
                return True

        logger.warning("Could not find metadata record to link kernel config")
        return False

    def _start_console_collection(self) -> Optional["ConsoleCollector"]:
        """Start console log collection for boot cycle.

        Returns:
            ConsoleCollector instance if started successfully, None otherwise
        """
        if not self.config.collect_console_logs:
            logger.info("Console log collection skipped (not configured)")
            return None

        from kbisect.master.console_collector import create_console_collector

        # Determine hostname for console connection
        console_hostname = self.config.console_hostname or self.config.slave_host

        # Create collector
        collector = create_console_collector(
            hostname=console_hostname,
            collector_type=self.config.console_collector_type,
            ipmi_controller=self.ipmi_controller,
        )

        if not collector:
            logger.warning("⊘ Console log collection skipped: no collector available")
            return None

        # Try to start collection
        try:
            if collector.start():
                self.active_console_collector = collector
                return collector

            # Primary collector failed, try fallback
            if self.config.console_fallback_ipmi and self.ipmi_controller:
                logger.warning("⚠ Falling back to IPMI SOL for console logs")
                from kbisect.master.console_collector import IPMISOLCollector

                fallback_collector = IPMISOLCollector(console_hostname, self.ipmi_controller)
                if fallback_collector.start():
                    self.active_console_collector = fallback_collector
                    return fallback_collector

            logger.warning("⊘ Console log collection failed (could not start collector)")
            return None

        except Exception as exc:
            logger.warning(f"⊘ Console log collection failed: {exc}")
            return None

    def _stop_console_collection(
        self, collector: Optional["ConsoleCollector"]
    ) -> Optional[str]:
        """Stop console log collection and retrieve output.

        Args:
            collector: Console collector instance to stop

        Returns:
            Collected console output, or None if no output
        """
        if not collector:
            return None

        try:
            output = collector.stop()
            self.active_console_collector = None

            if output:
                lines = output.count("\n")
                size_kb = len(output.encode("utf-8")) / 1024
                logger.debug(f"Console log collected: {lines} lines, {size_kb:.1f} KB")
                return output

            logger.debug("Console log collection produced no output")
            return None

        except Exception as exc:
            logger.error(f"Error stopping console collection: {exc}")
            self.active_console_collector = None
            return None

    def _store_console_log(
        self, iteration_id: int, content: Optional[str], boot_result: str
    ) -> Optional[int]:
        """Store console log in database.

        Args:
            iteration_id: Iteration ID
            content: Console log content
            boot_result: Boot result (success, timeout, panic, etc.)

        Returns:
            Log ID if stored successfully, None otherwise
        """
        if not content:
            return None

        try:
            # Create header with boot result
            header = f"=== Console Log: Boot Result = {boot_result} ===\n"
            header += f"Timestamp: {datetime.utcnow().isoformat()}\n"
            header += f"Size: {len(content.encode('utf-8'))} bytes\n"
            header += "\n" + "=" * 80 + "\n\n"

            full_content = header + content

            # Store in build_logs table with log_type="console"
            log_id = self.state.store_build_log(iteration_id, "console", full_content)

            # Verify log was stored successfully
            log_data = self.state.get_build_log(log_id)
            if log_data and "size_bytes" in log_data:
                size_kb = log_data["size_bytes"] / 1024
                logger.info(f"✓ Console log captured (log_id: {log_id}, {size_kb:.1f} KB)")
            else:
                logger.warning(f"Console log stored with ID {log_id} but verification failed")
            return log_id

        except Exception as exc:
            logger.error(f"Failed to store console log: {exc}")
            return None

    def initialize(self) -> bool:
        """Initialize bisection.

        Returns:
            True if initialization successful, False otherwise
        """
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
        ret, _stdout, stderr = self.ssh.call_function("init_protection")

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

        self.save_state()
        logger.info("=== Initialization Complete ===")
        return True

    def get_next_commit(self) -> Optional[str]:
        """Get next commit to test using git bisect.

        Returns:
            Commit SHA or None if bisection complete
        """
        ret, stdout, stderr = self.ssh.run_command(
            f"cd {self.config.slave_kernel_path} && "
            f"git bisect start {self.bad_commit} {self.good_commit} 2>&1 || "
            f"git rev-parse HEAD"
        )

        if ret != 0:
            logger.error(f"Failed to get next commit: {stderr}")
            return None

        commit = stdout.strip()

        # Validate commit hash: must be 40 characters and hexadecimal
        if not commit or len(commit) != COMMIT_HASH_LENGTH:
            logger.error(f"Invalid commit hash length: {commit}")
            return None

        # Validate hexadecimal format
        try:
            int(commit, 16)
        except ValueError:
            logger.error(f"Invalid commit hash format (not hexadecimal): {commit}")
            return None

        return commit

    def build_kernel(self, commit_sha: str, iteration_id: int) -> Tuple[bool, int, Optional[int]]:
        """Build kernel on slave and store build log.

        Args:
            commit_sha: Commit SHA to build
            iteration_id: Iteration ID for log storage

        Returns:
            Tuple of (success, exit_code, log_id)
        """
        logger.info(f"Building kernel for commit {commit_sha[:SHORT_COMMIT_LENGTH]}...")

        # Determine kernel config source
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
            timeout=self.config.build_timeout,
        )

        # Combine stdout and stderr for full log
        full_log = f"=== Build Kernel: {commit_sha[:SHORT_COMMIT_LENGTH]} ===\n"
        full_log += f"Kernel source: {self.config.slave_kernel_path}\n"
        full_log += f"Config: {kernel_config or 'default'}\n"
        full_log += f"Exit code: {ret}\n"
        full_log += "\n=== STDOUT ===\n"
        full_log += stdout
        full_log += "\n=== STDERR ===\n"
        full_log += stderr

        # Store build log in database
        log_id = self.state.store_build_log(iteration_id, "build", full_log, exit_code=ret)

        # Format size for display
        size_kb = self.state.get_build_log(log_id)["size_bytes"] / 1024

        if ret != 0:
            logger.error(
                f"✗ Kernel build FAILED (log_id: {log_id}, {size_kb:.1f} KB compressed)"
            )
            logger.error(f"  View log: kbisect logs show {log_id}")
            return False, ret, log_id

        logger.info(f"✓ Kernel build complete (log_id: {log_id}, {size_kb:.1f} KB)")
        logger.debug(f"Kernel version: {stdout.strip()}")
        return True, ret, log_id

    def reboot_slave(self, iteration_id: int) -> Tuple[bool, Optional[str]]:
        """Reboot slave machine and return (success, booted_kernel_version).

        Args:
            iteration_id: Iteration ID for console log storage

        Returns:
            Tuple of (success, booted_kernel_version)
        """
        logger.info("Rebooting slave...")

        # Start console log collection BEFORE reboot
        console_collector = self._start_console_collection()

        # Send reboot command
        self.ssh.run_command("reboot", timeout=5)

        # Wait a bit for reboot to start
        time.sleep(DEFAULT_REBOOT_SETTLE_TIME)

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
                time.sleep(DEFAULT_POST_BOOT_SETTLE_TIME)

                # Stop console collection and store log
                console_output = self._stop_console_collection(console_collector)
                self._store_console_log(iteration_id, console_output, "success")

                # Get kernel version that booted
                ret, kernel_ver, _ = self.ssh.call_function("get_kernel_version")
                if ret == 0 and kernel_ver.strip():
                    return (True, kernel_ver.strip())

                logger.warning("Could not determine booted kernel version")
                return (True, None)

            if waited % 30 == 0:
                logger.info(f"Still waiting... ({waited}/{max_wait}s)")

        # Timeout! Stop console collection and try IPMI recovery if configured
        logger.error("Slave failed to reboot within timeout")
        console_output = self._stop_console_collection(console_collector)
        self._store_console_log(iteration_id, console_output, "timeout")

        return self._try_ipmi_recovery()

    def _try_ipmi_recovery(self, max_attempts: int = 3) -> Tuple[bool, Optional[str]]:
        """Try to recover slave using IPMI with retry logic.

        Args:
            max_attempts: Maximum number of recovery attempts

        Returns:
            Tuple of (success, booted_kernel_version)
        """
        if not self.config.ipmi_host:
            logger.warning("IPMI not configured - cannot attempt automatic recovery")
            return (False, None)

        logger.warning(f"Attempting IPMI recovery (up to {max_attempts} attempts)...")

        for attempt in range(1, max_attempts + 1):
            try:
                logger.warning(f"IPMI recovery attempt {attempt}/{max_attempts}...")
                from kbisect.master.ipmi_controller import IPMIController

                ipmi = IPMIController(
                    self.config.ipmi_host, self.config.ipmi_user, self.config.ipmi_password
                )

                # Force power cycle
                ipmi.power_cycle()
                logger.info("IPMI power cycle initiated, waiting for system to boot...")

                # Wait for slave to come back
                time.sleep(DEFAULT_REBOOT_SETTLE_TIME)
                ipmi_wait = 0
                ipmi_max_wait = self.config.boot_timeout

                while ipmi_wait < ipmi_max_wait:
                    time.sleep(5)
                    ipmi_wait += 5

                    if self.ssh.is_alive():
                        logger.info(
                            f"✓ Slave recovered via IPMI after {ipmi_wait}s "
                            f"(attempt {attempt}/{max_attempts})"
                        )
                        time.sleep(DEFAULT_POST_BOOT_SETTLE_TIME)

                        # Get kernel version
                        ret, kernel_ver, _ = self.ssh.call_function("get_kernel_version")
                        if ret == 0 and kernel_ver.strip():
                            logger.info(f"Booted into kernel: {kernel_ver.strip()}")
                            return (True, kernel_ver.strip())

                        return (True, None)

                    if ipmi_wait % 30 == 0:
                        logger.info(
                            f"Still waiting after IPMI... ({ipmi_wait}/{ipmi_max_wait}s)"
                        )

                # This attempt failed
                logger.warning(f"Recovery attempt {attempt} failed - slave not responding")

            except Exception as exc:
                logger.warning(f"Recovery attempt {attempt} failed: {exc}")

            # Wait before next attempt (unless this was the last one)
            if attempt < max_attempts:
                logger.warning("Retrying in 30 seconds...")
                time.sleep(30)

        # All attempts exhausted
        logger.error(f"IPMI recovery failed after {max_attempts} attempts")
        return (False, None)

    def run_tests(self) -> TestResult:
        """Run tests on slave.

        Returns:
            Test result (GOOD, BAD, SKIP, or UNKNOWN)
        """
        logger.info("Running tests on slave...")

        # Call run_test function from library
        if self.config.test_script:
            ret, stdout, stderr = self.ssh.call_function(
                "run_test",
                self.config.test_type,
                self.config.test_script,
                timeout=self.config.test_timeout,
            )
        else:
            ret, stdout, stderr = self.ssh.call_function(
                "run_test", self.config.test_type, timeout=self.config.test_timeout
            )

        logger.debug(f"Test output: {stdout}")

        if ret == 0:
            logger.info("✓ Tests PASSED")
            return TestResult.GOOD

        logger.error("✗ Tests FAILED")
        logger.debug(f"Test error: {stderr}")
        return TestResult.BAD

    def mark_commit(self, commit_sha: str, result: TestResult) -> bool:
        """Mark commit as good or bad in git bisect.

        Args:
            commit_sha: Commit SHA to mark
            result: Test result

        Returns:
            True if marking succeeded, False otherwise
        """
        if result == TestResult.SKIP:
            bisect_cmd = "git bisect skip"
        elif result == TestResult.GOOD:
            bisect_cmd = "git bisect good"
        elif result == TestResult.BAD:
            bisect_cmd = "git bisect bad"
        else:
            logger.error(f"Cannot mark commit with result: {result}")
            return False

        ret, _stdout, stderr = self.ssh.run_command(
            f"cd {self.config.slave_kernel_path} && {bisect_cmd}"
        )

        if ret != 0:
            logger.error(f"Failed to mark commit: {stderr}")
            return False

        logger.info(f"Marked commit {commit_sha[:SHORT_COMMIT_LENGTH]} as {result.value}")
        return True

    def _handle_boot_failure(
        self,
        iteration: BisectIteration,
        iteration_id: int,
        commit_sha: str,
        expected_kernel_ver: Optional[str],
        actual_kernel_ver: Optional[str],
        is_timeout: bool = False,
    ) -> None:
        """Handle boot failure scenario.

        Args:
            iteration: Current iteration object
            iteration_id: Iteration database ID
            commit_sha: Commit SHA being tested
            expected_kernel_ver: Expected kernel version
            actual_kernel_ver: Actual kernel version that booted
            is_timeout: Whether failure was due to timeout
        """
        if is_timeout:
            logger.error("✗ Boot timeout or failure!")
            logger.error(f"  Expected kernel: {expected_kernel_ver}")
            logger.error("  Slave did not respond within timeout")
        else:
            logger.error("✗ Kernel panic detected!")
            logger.error(f"  Expected: {expected_kernel_ver}")
            logger.error(f"  Actual:   {actual_kernel_ver}")
            logger.error("  Test kernel failed to boot, fell back to protected kernel")

        # Determine result based on test type
        if self.config.test_type == "boot" or not self.config.test_script:
            # Boot test mode: non-bootable kernel is BAD
            iteration.result = TestResult.BAD
            iteration.error = (
                "Boot timeout - kernel failed to boot"
                if is_timeout
                else "Kernel panic detected - kernel failed to boot"
            )
            result_str = "bad"
        else:
            # Custom test mode: can't test functionality if kernel doesn't boot
            iteration.result = TestResult.SKIP
            iteration.error = (
                "Boot timeout - cannot test functionality, skipping commit"
                if is_timeout
                else "Kernel panic detected - cannot test functionality, skipping commit"
            )
            result_str = "skip"

        # CRITICAL: Only mark commit if SSH is working
        if not self.ssh.is_alive():
            logger.critical("  ✗ Cannot mark commit - slave is unreachable")
            logger.critical("  Bisection will halt - manual recovery required")
            # Store iteration with error, but don't mark in git bisect yet
            self.state.update_iteration(
                iteration_id, error_message=iteration.error + " (git mark pending - slave down)"
            )
            self.state.update_session(self.session_id, status="halted")
            return

        # SSH is working - safe to mark commit
        if iteration.result == TestResult.BAD:
            logger.error("  Marking as BAD (boot test mode)")
            self.mark_commit(commit_sha, TestResult.BAD)
        else:
            logger.warning(
                "  Marking as SKIP (custom test mode - cannot test if kernel doesn't boot)"
            )
            self.mark_commit(commit_sha, TestResult.SKIP)

        self.state.update_iteration(
            iteration_id, final_result=result_str, error_message=iteration.error
        )

    def run_iteration(self, commit_sha: str) -> BisectIteration:
        """Run single bisection iteration.

        Args:
            commit_sha: Commit SHA to test

        Returns:
            BisectIteration object with results
        """
        self.iteration_count += 1

        # Get commit info
        ret, commit_msg, _ = self.ssh.run_command(
            f"cd {self.config.slave_kernel_path} && git log -1 --oneline {commit_sha}"
        )
        commit_msg = commit_msg.strip() if ret == 0 else "Unknown"

        # Create iteration in database
        iteration_id = self.state.create_iteration(
            self.session_id, self.iteration_count, commit_sha, commit_msg
        )

        iteration = BisectIteration(
            iteration=self.iteration_count,
            commit_sha=commit_sha,
            commit_short=commit_sha[:SHORT_COMMIT_LENGTH],
            commit_message=commit_msg,
            state=BisectState.IDLE,
            start_time=datetime.utcnow().isoformat(),
        )

        self.current_iteration = iteration
        logger.info(f"\n=== Iteration {iteration.iteration}: {iteration.commit_short} ===")
        logger.info(f"Commit: {iteration.commit_message}")

        try:
            # Build kernel
            iteration.state = BisectState.BUILDING
            self.save_state()

            success, _exit_code, _log_id = self.build_kernel(commit_sha, iteration_id)
            if not success:
                iteration.result = TestResult.SKIP
                iteration.error = "Build failed"
                logger.error("Build failed, skipping commit")
                self.mark_commit(commit_sha, TestResult.SKIP)
                self.state.update_iteration(
                    iteration_id, final_result="skip", error_message="Build failed"
                )
                return iteration

            # Get kernel version that was just built
            ret, kernel_version, _ = self.ssh.run_command(
                f"cd {self.config.slave_kernel_path} && make kernelrelease"
            )
            expected_kernel_ver = kernel_version.strip() if ret == 0 and kernel_version.strip() else None
            if expected_kernel_ver:
                logger.info(f"Built kernel version: {expected_kernel_ver}")
            else:
                logger.warning("Could not determine kernel version")

            # Reboot slave
            iteration.state = BisectState.REBOOTING
            self.save_state()

            reboot_success, actual_kernel_ver = self.reboot_slave(iteration_id)

            if not reboot_success:
                # Boot timeout or complete failure
                self._handle_boot_failure(
                    iteration, iteration_id, commit_sha, expected_kernel_ver, None, is_timeout=True
                )

                # Check if system is still down after recovery attempts
                if not self.ssh.is_alive():
                    logger.critical("\n" + "=" * 70)
                    logger.critical("BISECTION HALTED - Slave Unreachable")
                    logger.critical("=" * 70)
                    logger.critical("\nThe slave machine failed to boot and could not be recovered.")
                    logger.critical("All IPMI recovery attempts have been exhausted.")
                    logger.critical("\nSession status: HALTED")
                    logger.critical(f"Session ID: {self.session_id}")
                    logger.critical(f"Failed commit: {commit_sha[:SHORT_COMMIT_LENGTH]}")
                    logger.critical("\nManual intervention required:")
                    logger.critical("  1. Check slave machine physical status")
                    logger.critical("  2. Boot into a stable kernel manually")
                    logger.critical("  3. Verify SSH connectivity")
                    logger.critical("  4. Resume bisection: kbisect start")
                    logger.critical("\nThe git bisect state has NOT been updated yet.")
                    logger.critical("When you resume, this commit will be marked appropriately.")
                    logger.critical("=" * 70 + "\n")
                    sys.exit(1)

                return iteration

            # Verify which kernel actually booted
            if actual_kernel_ver:
                logger.info(f"Booted kernel version: {actual_kernel_ver}")

                if expected_kernel_ver and actual_kernel_ver != expected_kernel_ver:
                    # Kernel panic detected - system fell back to protected kernel
                    self._handle_boot_failure(
                        iteration,
                        iteration_id,
                        commit_sha,
                        expected_kernel_ver,
                        actual_kernel_ver,
                        is_timeout=False,
                    )
                    return iteration

                logger.info("✓ Correct kernel booted successfully")
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
                end_time=datetime.utcnow().isoformat(),
            )

            # Mark in git bisect
            self.mark_commit(commit_sha, test_result)

        except Exception as exc:
            logger.error(f"Iteration failed with exception: {exc}")
            iteration.result = TestResult.SKIP
            iteration.error = str(exc)

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
        """Run complete bisection.

        Returns:
            True if bisection completed successfully, False otherwise
        """
        logger.info("\n=== Starting Bisection ===\n")

        # Safety limit to prevent infinite loops
        MAX_ITERATIONS = 1000
        iteration_count = 0

        while True:
            iteration_count += 1

            # Safety check: prevent infinite loops
            if iteration_count > MAX_ITERATIONS:
                logger.error(
                    f"SAFETY LIMIT REACHED: Exceeded {MAX_ITERATIONS} iterations. "
                    "Bisection may be stuck in an infinite loop. Stopping."
                )
                self.state.update_session(
                    self.session_id,
                    status="failed",
                    end_time=datetime.utcnow().isoformat(),
                )
                return False

            # Get next commit to test
            commit = self.get_next_commit()

            if not commit:
                logger.info("No more commits to test - bisection complete!")
                break

            # Run iteration
            iteration = self.run_iteration(commit)

            logger.info(f"Result: {iteration.result.value}")

            # Check if bisection is done
            _ret, stdout, _ = self.ssh.run_command(
                f"cd {self.config.slave_kernel_path} && git bisect log | tail -1"
            )

            if "is the first bad commit" in stdout:
                logger.info("\n=== Bisection Found First Bad Commit! ===")
                self.generate_report()
                return True

        self.generate_report()
        return True

    def save_state(self) -> None:
        """Save bisection state to file."""
        state = {
            "good_commit": self.good_commit,
            "bad_commit": self.bad_commit,
            "iteration_count": self.iteration_count,
            "current_iteration": (
                asdict(self.current_iteration) if self.current_iteration else None
            ),
            "iterations": [asdict(it) for it in self.iterations],
            "last_update": datetime.utcnow().isoformat(),
        }

        with self.state_file.open("w") as f:
            json.dump(state, f, indent=2)

    def generate_report(self) -> None:
        """Generate bisection report."""
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
            logger.info(
                f"{iteration.iteration:3d}. {iteration.commit_short} | "
                f"{status:7s} | {duration:6s} | {iteration.commit_message[:50]}"
            )

        # Get final result from git bisect
        ret, stdout, _ = self.ssh.run_command(
            f"cd {self.config.slave_kernel_path} && "
            f"git bisect log | grep 'first bad commit' -A 5"
        )

        if ret == 0 and stdout:
            logger.info("\n" + "=" * 60)
            logger.info("FIRST BAD COMMIT:")
            logger.info("=" * 60)
            logger.info(stdout)

        logger.info("=" * 60 + "\n")


def main() -> int:
    """Main entry point."""
    print("Kernel Bisect Master Controller")
    print("Usage: Import this module and use the BisectMaster class")
    return 0


if __name__ == "__main__":
    sys.exit(main())
