#!/usr/bin/env python3
"""Master Bisection Controller.

Orchestrates the kernel bisection process across master and slave machines.
"""

import json
import logging
import select
import shlex
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Generator, List, Optional, Tuple


if TYPE_CHECKING:
    from kbisect.master.console_collector import ConsoleCollector
    from kbisect.master.ipmi_controller import IPMIController


logger = logging.getLogger(__name__)

# Constants
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
        kernel_repo_source: Git URL or local path to kernel repository (optional)
        kernel_repo_branch: Branch or ref to checkout (optional)
    """

    slave_host: str
    slave_user: str = "root"
    slave_kernel_path: str = "/root/kernel"
    slave_bisect_path: str = "/root/kernel-bisect/lib"
    ipmi_host: Optional[str] = None
    ipmi_user: Optional[str] = None
    ipmi_password: Optional[str] = None
    boot_timeout: int = 300
    test_timeout: int = 600
    build_timeout: int = 1800
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
    kernel_repo_source: Optional[str] = None
    kernel_repo_branch: Optional[str] = None


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

    def run_command(self, command: str, timeout: Optional[int] = None) -> Tuple[int, str, str]:
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
            result = subprocess.run(ssh_command, capture_output=True, text=True, timeout=timeout, check=False)
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

    def call_function_streaming(
        self,
        function_name: str,
        *args: str,
        library_path: str = "/root/kernel-bisect/lib/bisect-functions.sh",
        timeout: Optional[int] = None,
        chunk_callback: Optional[Callable[[str, str], None]] = None,
    ) -> Tuple[int, str, str]:
        """Call a bash function and stream output in real-time.

        Args:
            function_name: Name of the bash function to call
            *args: Arguments to pass to the function
            library_path: Path to the bisect library on slave
            timeout: Command timeout in seconds
            chunk_callback: Optional callback function(stdout_chunk, stderr_chunk) called as output arrives

        Returns:
            Tuple of (return_code, stdout, stderr)
        """
        # Properly escape arguments to prevent command injection
        args_str = " ".join(shlex.quote(str(arg)) for arg in args)

        # Source library and call function
        command = f"source {shlex.quote(library_path)} && {function_name} {args_str}"

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
            # Use Popen for streaming
            process = subprocess.Popen(
                ssh_command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,  # Line buffered
            )

            stdout_lines = []
            stderr_lines = []
            start_time = time.time()

            # Read output as it arrives
            while True:
                # Check timeout
                if timeout and (time.time() - start_time) > timeout:
                    process.kill()
                    logger.error(f"SSH command timed out after {timeout}s")
                    return -1, "".join(stdout_lines), "Timeout"

                # Use select to check which pipes have data
                # Note: select() doesn't work on Windows, but kbisect is Linux-focused
                readable, _, _ = select.select([process.stdout, process.stderr], [], [], 0.1)

                for stream in readable:
                    line = stream.readline()
                    if line:
                        if stream == process.stdout:
                            stdout_lines.append(line)
                            if chunk_callback:
                                chunk_callback(line, "")
                        else:
                            stderr_lines.append(line)
                            if chunk_callback:
                                chunk_callback("", line)

                # Check if process has ended
                if process.poll() is not None:
                    # Read any remaining output
                    remaining_stdout = process.stdout.read()
                    remaining_stderr = process.stderr.read()
                    if remaining_stdout:
                        stdout_lines.append(remaining_stdout)
                        if chunk_callback:
                            chunk_callback(remaining_stdout, "")
                    if remaining_stderr:
                        stderr_lines.append(remaining_stderr)
                        if chunk_callback:
                            chunk_callback("", remaining_stderr)
                    break

            return process.returncode, "".join(stdout_lines), "".join(stderr_lines)

        except Exception as exc:
            logger.error(f"SSH streaming command failed: {exc}")
            return -1, "", str(exc)

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
        self.iterations: List[BisectIteration] = []
        self.current_iteration: Optional[BisectIteration] = None
        self.iteration_count = 0

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

            self.ipmi_controller = IPMIController(config.ipmi_host, config.ipmi_user, config.ipmi_password)

        # Console collector (created per boot cycle)
        self.active_console_collector: Optional["ConsoleCollector"] = None  # noqa: UP037

        # Resolve test script path: if it's a local file on master, compute the remote path on slave
        # This ensures the config always uses the slave path for execution, whether first run or resume
        self._original_test_script = self.config.test_script
        if self.config.test_script:
            script_path = Path(self.config.test_script)
            if script_path.exists():
                # It's a local file on master - use remote path for execution
                logger.debug(f"Test script is local file on master: {self.config.test_script}")
                bisect_base_dir = Path(self.config.slave_bisect_path).parent
                remote_script_dir = bisect_base_dir / "test-scripts"
                remote_script_path = str(remote_script_dir / script_path.name)
                self.config.test_script = remote_script_path
                logger.debug(f"Resolved test script to slave path: {remote_script_path}")

    def collect_and_store_metadata(self, collection_type: str, iteration_id: Optional[int] = None) -> bool:
        """Collect metadata from slave and store in database.

        Args:
            collection_type: Type of metadata to collect
            iteration_id: Optional iteration ID

        Returns:
            True if metadata collected successfully, False otherwise
        """
        logger.debug(f"Collecting {collection_type} metadata...")

        # Call bash function to collect metadata
        ret, stdout, stderr = self.ssh.call_function("collect_metadata", collection_type, timeout=30)

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
        """Capture and store kernel config file from slave in database.

        Args:
            kernel_version: Kernel version string
            iteration_id: Iteration ID for linking config file

        Returns:
            True if config captured successfully, False otherwise
        """
        config_path = f"/boot/config-{kernel_version}"

        # Download config file content from slave (to memory, not disk)
        ret, stdout, stderr = self.ssh.run_command(f"cat {shlex.quote(config_path)}", timeout=30)

        if ret != 0:
            logger.warning(f"Failed to read kernel config from slave: {stderr}")
            return False

        if not stdout:
            logger.warning(f"Kernel config file is empty: {config_path}")
            return False

        # Store config content directly in database
        metadata_list = self.state.get_session_metadata(self.session_id, "iteration")
        if metadata_list:
            # Find metadata for this iteration
            iteration_metadata = [m for m in metadata_list if m.get("iteration_id") == iteration_id]
            if iteration_metadata:
                metadata_id = iteration_metadata[0]["metadata_id"]
                # Store content as bytes in database (compressed)
                config_content = stdout.encode("utf-8")
                file_id = self.state.store_metadata_file_content(metadata_id, "kernel_config", config_content, compress=True)
                logger.info(f"✓ Captured kernel config in DB (file_id: {file_id}): {config_path}")
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

    def _stop_console_collection(self, collector: Optional["ConsoleCollector"]) -> Optional[str]:
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

    def _store_console_log(self, iteration_id: int, content: Optional[str], boot_result: str) -> Optional[int]:
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

    def _prepare_kernel_repo(self) -> Optional[str]:
        """Prepare kernel repository on master (clone or copy to temp directory).

        Returns:
            Path to prepared repository on master, or None if preparation failed
        """
        if not self.config.kernel_repo_source:
            logger.debug("No kernel_repo_source configured, skipping repo preparation")
            return None

        logger.info("Preparing kernel repository on master...")
        logger.info(f"Source: {self.config.kernel_repo_source}")

        # Create temp directory for repo
        import tempfile

        temp_dir = tempfile.mkdtemp(prefix="kbisect-repo-")
        repo_path = Path(temp_dir) / "kernel"

        try:
            # Check if source is a local path or remote URL
            source_path = Path(self.config.kernel_repo_source)
            is_local = source_path.exists() and source_path.is_dir()

            if is_local:
                # Copy from local path using Python shutil
                logger.info(f"Copying local repository from {self.config.kernel_repo_source}...")

                try:
                    # Remove destination first if it exists (simulates rsync --delete)
                    if repo_path.exists():
                        shutil.rmtree(repo_path)

                    # Copy entire directory tree, preserving symlinks
                    shutil.copytree(
                        source_path,
                        repo_path,
                        symlinks=True,
                        ignore_dangling_symlinks=True,
                        dirs_exist_ok=False,
                    )

                    logger.info("✓ Local repository copied successfully")

                except (OSError, shutil.Error) as exc:
                    logger.error(f"Failed to copy local repository: {exc}")
                    subprocess.run(["rm", "-rf", temp_dir], check=False)
                    return None
            else:
                # Clone from remote URL
                logger.info(f"Cloning repository from {self.config.kernel_repo_source}...")
                clone_cmd = ["git", "clone", self.config.kernel_repo_source, str(repo_path)]

                result = subprocess.run(clone_cmd, capture_output=True, text=True, timeout=3600, check=False)

                if result.returncode != 0:
                    logger.error(f"Failed to clone repository: {result.stderr}")
                    subprocess.run(["rm", "-rf", temp_dir], check=False)
                    return None

                logger.info("✓ Repository cloned successfully")

            # Checkout specified branch if configured
            if self.config.kernel_repo_branch:
                logger.info(f"Checking out branch: {self.config.kernel_repo_branch}")
                result = subprocess.run(
                    ["git", "-C", str(repo_path), "checkout", self.config.kernel_repo_branch],
                    capture_output=True,
                    text=True,
                    timeout=60,
                    check=False,
                )

                if result.returncode != 0:
                    logger.warning(f"Failed to checkout branch {self.config.kernel_repo_branch}: {result.stderr}")
                else:
                    logger.info(f"✓ Checked out branch {self.config.kernel_repo_branch}")

            return str(repo_path)

        except subprocess.TimeoutExpired:
            logger.error("Repository preparation timed out")
            subprocess.run(["rm", "-rf", temp_dir], check=False)
            return None
        except Exception as exc:
            logger.error(f"Error preparing repository: {exc}")
            subprocess.run(["rm", "-rf", temp_dir], check=False)
            return None

    def _transfer_repo_to_slave(self, local_repo_path: str) -> bool:
        """Transfer kernel repository from master to slave.

        Args:
            local_repo_path: Path to repository on master

        Returns:
            True if transfer succeeded, False otherwise
        """
        logger.info("Transferring kernel repository to slave...")

        # Remove existing kernel path on slave
        logger.info(f"Removing existing path on slave: {self.config.slave_kernel_path}")
        ret, _stdout, stderr = self.ssh.run_command(f"rm -rf {shlex.quote(self.config.slave_kernel_path)}")

        if ret != 0:
            logger.warning(f"Failed to remove existing path (may not exist): {stderr}")

        # Create target directory on slave
        ret, _stdout, stderr = self.ssh.run_command(f"mkdir -p {shlex.quote(self.config.slave_kernel_path)}")

        if ret != 0:
            logger.error(f"Failed to create target directory on slave: {stderr}")
            return False

        # Transfer repository using rsync
        logger.info("Starting repository transfer (this may take several minutes)...")

        # Use rsync with archive mode and compression for faster transfers
        # Note: We copy the contents of local_repo_path into slave_kernel_path
        # by using trailing slash on source which copies contents without creating nested dir
        rsync_cmd = [
            "rsync",
            "-avz",  # Archive mode (preserves permissions, times, symlinks) + verbose + compression
            "-e",
            "ssh -o StrictHostKeyChecking=no -o ConnectTimeout=10",
            f"{local_repo_path}/",  # Trailing slash copies contents without creating parent
            f"{self.config.slave_user}@{self.config.slave_host}:{self.config.slave_kernel_path}/",
        ]

        try:
            logger.info(f"rsync command: {' '.join(shlex.quote(arg) for arg in rsync_cmd)}")
            result = subprocess.run(rsync_cmd, capture_output=True, text=True, timeout=3600, check=False)

            if result.returncode != 0:
                logger.error(f"Repository transfer failed: {result.stderr}")
                return False

            logger.info("✓ Repository transferred successfully")

            # Verify repository exists on slave
            ret, _stdout, stderr = self.ssh.run_command(f"test -d {shlex.quote(self.config.slave_kernel_path)}/.git")

            if ret != 0:
                logger.error("Repository verification failed - .git directory not found on slave")
                return False

            logger.info("✓ Repository verified on slave")

            # Reset repository to clean state (removes any modifications from transfer)
            logger.info("Resetting repository to clean state on slave...")
            ret, _stdout, stderr = self.ssh.run_command(
                f"cd {shlex.quote(self.config.slave_kernel_path)} && git reset --hard HEAD"
            )

            if ret != 0:
                logger.warning(f"Failed to reset repository: {stderr}")
                logger.warning("Repository may have uncommitted changes")
            else:
                logger.info("✓ Repository reset to clean state")

            # Configure git to trust this repository (fixes "dubious ownership" error in Git 2.35.2+)
            # This is needed because the repository is transferred from master and may have different ownership
            logger.info("Configuring git safe.directory...")
            ret, _stdout, stderr = self.ssh.run_command(
                f"git config --global --add safe.directory {shlex.quote(self.config.slave_kernel_path)}"
            )

            if ret != 0:
                logger.warning(f"Failed to configure git safe.directory: {stderr}")
                logger.warning("Git commands may fail due to ownership checks")
            else:
                logger.info("✓ Git safe.directory configured")

            return True

        except subprocess.TimeoutExpired:
            logger.error("Repository transfer timed out")
            return False
        except Exception as exc:
            logger.error(f"Error transferring repository: {exc}")
            return False
        finally:
            # Cleanup temp directory on master
            temp_dir = str(Path(local_repo_path).parent)
            logger.debug(f"Cleaning up temp directory: {temp_dir}")
            subprocess.run(["rm", "-rf", temp_dir], check=False)

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

        # Prepare and transfer kernel repository if configured
        if self.config.kernel_repo_source:
            logger.info("Kernel repository source configured, preparing...")
            repo_path = self._prepare_kernel_repo()

            if not repo_path:
                logger.error("Failed to prepare kernel repository on master")
                return False

            if not self._transfer_repo_to_slave(repo_path):
                logger.error("Failed to transfer kernel repository to slave")
                return False

            logger.info("✓ Kernel repository deployed to slave")

        # Initialize git bisect on slave
        logger.info("Initializing git bisect on slave...")
        ret, _stdout, stderr = self.ssh.run_command(
            f"cd {shlex.quote(self.config.slave_kernel_path)} && "
            f"git bisect reset >/dev/null 2>&1; "
            f"git bisect start {shlex.quote(self.bad_commit)} {shlex.quote(self.good_commit)}"
        )

        if ret != 0:
            logger.error(f"Failed to initialize git bisect: {stderr}")
            return False

        logger.info("✓ Git bisect initialized")

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

        # Transfer test script to slave if configured
        if self._original_test_script:
            script_path = Path(self._original_test_script)

            # Check if it's a local file on master
            if script_path.exists():
                logger.info(f"Transferring test script to slave: {script_path}")

                # Derive test-scripts directory from bisect library path
                bisect_base_dir = Path(self.config.slave_bisect_path).parent
                remote_script_dir = bisect_base_dir / "test-scripts"
                remote_script_path = str(remote_script_dir / script_path.name)

                # Create test-scripts directory on slave if it doesn't exist
                ret, _, stderr = self.ssh.run_command(f"mkdir -p {shlex.quote(str(remote_script_dir))}")
                if ret != 0:
                    logger.warning(f"Failed to create test-scripts directory: {stderr}")
                    # Fall back to /tmp if directory creation fails
                    remote_script_path = f"/tmp/kbisect-test-{script_path.name}"
                    logger.warning(f"Falling back to /tmp location: {remote_script_path}")

                if self.ssh.copy_file(str(script_path), remote_script_path):
                    # Make executable on slave
                    ret, _, stderr = self.ssh.run_command(f"chmod +x {shlex.quote(remote_script_path)}")
                    if ret != 0:
                        logger.warning(f"Failed to make test script executable: {stderr}")

                    logger.info(f"✓ Test script deployed to slave: {remote_script_path}")
                else:
                    logger.error("Failed to transfer test script to slave")
                    return False
            else:
                # Assume it's already an absolute path on slave - verify it exists
                logger.info(f"Verifying test script exists on slave: {self.config.test_script}")
                ret, _, _ = self.ssh.run_command(f"test -f {shlex.quote(self.config.test_script)}")
                if ret != 0:
                    logger.error(f"Test script not found on slave: {self.config.test_script}")
                    return False
                logger.info("✓ Test script verified on slave")

        self.save_state()
        logger.info("=== Initialization Complete ===")
        return True

    def get_next_commit(self) -> Optional[str]:
        """Get next commit to test using git bisect.

        After git bisect is initialized and commits are marked, git automatically
        checks out the next commit to test. This method simply returns the current
        HEAD commit that git bisect has checked out.

        Returns:
            Commit SHA or None if bisection complete
        """
        ret, stdout, stderr = self.ssh.run_command(
            f"cd {shlex.quote(self.config.slave_kernel_path)} && git rev-parse HEAD"
        )

        if ret != 0:
            logger.error(f"Failed to get current commit: {stderr}")
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

    def build_kernel(self, commit_sha: str, iteration_id: int) -> Tuple[bool, int, Optional[int], Optional[str]]:
        """Build kernel on slave and store build log with streaming.

        Args:
            commit_sha: Commit SHA to build
            iteration_id: Iteration ID for log storage

        Returns:
            Tuple of (success, exit_code, log_id, kernel_version)
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

        # Create initial log entry with header
        log_header = f"=== Build Kernel: {commit_sha[:SHORT_COMMIT_LENGTH]} ===\n"
        log_header += f"Kernel source: {self.config.slave_kernel_path}\n"
        log_header += f"Config: {kernel_config or 'default'}\n\n"
        log_header += "=== BUILD OUTPUT ===\n"

        log_id = self.state.create_build_log(iteration_id, "build", log_header)
        logger.debug(f"Created build log {log_id} for streaming")

        # Streaming state
        buffer = []
        buffer_size = 0
        buffer_limit = 10 * 1024  # Flush every 10KB
        total_bytes = 0
        start_time = time.time()
        last_progress_time = start_time
        progress_interval = 20  # Log progress every 20 seconds

        def stream_callback(stdout_chunk: str, stderr_chunk: str) -> None:
            """Handle streaming output chunks."""
            nonlocal buffer, buffer_size, total_bytes, last_progress_time

            chunk = stdout_chunk + stderr_chunk
            if not chunk:
                return

            # Add to buffer
            buffer.append(chunk)
            chunk_bytes = len(chunk.encode("utf-8"))
            buffer_size += chunk_bytes
            total_bytes += chunk_bytes

            # Flush buffer if it's getting large
            if buffer_size >= buffer_limit:
                combined_chunk = "".join(buffer)
                try:
                    self.state.append_build_log_chunk(log_id, combined_chunk)
                except Exception as exc:
                    logger.warning(f"Failed to append log chunk: {exc}")

                buffer.clear()
                buffer_size = 0

            # Log progress periodically
            current_time = time.time()
            if current_time - last_progress_time >= progress_interval:
                elapsed = int(current_time - start_time)
                logger.info(f"  Building... {total_bytes // 1024}KB logged, {elapsed // 60}m {elapsed % 60}s elapsed")
                last_progress_time = current_time

        # Call build_kernel function with streaming
        ret, stdout, stderr = self.ssh.call_function_streaming(
            "build_kernel",
            commit_sha,
            self.config.slave_kernel_path,
            kernel_config,
            timeout=self.config.build_timeout,
            chunk_callback=stream_callback,
        )

        # Flush remaining buffer
        if buffer:
            combined_chunk = "".join(buffer)
            try:
                self.state.append_build_log_chunk(log_id, combined_chunk)
            except Exception as exc:
                logger.warning(f"Failed to append final log chunk: {exc}")

        # Extract kernel version from build output (last line of stdout)
        built_kernel_ver = None
        if ret == 0 and stdout.strip():
            # The build_kernel bash function outputs the kernel version as its last line
            built_kernel_ver = stdout.strip().split('\n')[-1]
            logger.debug(f"Extracted kernel version from build output: {built_kernel_ver}")

        # Add exit code to log
        footer = f"\n\n=== EXIT CODE: {ret} ===\n"
        try:
            self.state.append_build_log_chunk(log_id, footer)
            self.state.finalize_build_log(log_id, ret)
        except Exception as exc:
            logger.warning(f"Failed to finalize log: {exc}")

        # Format size for display
        size_kb = self.state.get_build_log(log_id)["size_bytes"] / 1024
        elapsed = int(time.time() - start_time)

        if ret != 0:
            logger.error(f"✗ Kernel build FAILED in {elapsed // 60}m {elapsed % 60}s (log_id: {log_id}, {size_kb:.1f} KB compressed)")
            logger.error(f"  View log: kbisect logs show {log_id}")
            return False, ret, log_id, None

        logger.info(f"✓ Kernel build complete in {elapsed // 60}m {elapsed % 60}s (log_id: {log_id}, {size_kb:.1f} KB)")
        if built_kernel_ver:
            logger.info(f"  Kernel version: {built_kernel_ver}")
        return True, ret, log_id, built_kernel_ver

    def reboot_slave(self, iteration_id: int) -> Tuple[bool, Optional[str]]:
        """Reboot slave machine and return (success, booted_kernel_version).

        Args:
            iteration_id: Iteration ID for console log storage

        Returns:
            Tuple of (success, booted_kernel_version)
        """
        logger.info("Rebooting slave...")

        # Create console log entry for streaming (if enabled)
        console_log_id: Optional[int] = None
        if self.config.collect_console_logs:
            try:
                log_header = "=== Console Log: Boot Cycle ===\n"
                log_header += f"Timestamp: {datetime.utcnow().isoformat()}\n\n"
                log_header += "=== CONSOLE OUTPUT ===\n"
                console_log_id = self.state.create_build_log(iteration_id, "console", log_header)
                logger.debug(f"Created console log {console_log_id} for streaming")
            except Exception as exc:
                logger.warning(f"Failed to create console log entry: {exc}")

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
        last_flush_time = time.time()
        flush_interval = 5  # Flush console buffer every 5 seconds

        while waited < max_wait:
            time.sleep(5)
            waited += 5

            # Periodically flush console buffer to database
            if console_collector and console_log_id:
                current_time = time.time()
                if current_time - last_flush_time >= flush_interval:
                    try:
                        chunk = console_collector.get_and_clear_buffer()
                        if chunk:
                            self.state.append_build_log_chunk(console_log_id, chunk)
                            stats = console_collector.get_buffer_stats()
                            logger.debug(f"Flushed console buffer: {len(chunk)} bytes ({stats['lines']} lines in current buffer)")
                        last_flush_time = current_time
                    except Exception as exc:
                        logger.debug(f"Failed to flush console buffer: {exc}")

            if self.ssh.is_alive():
                logger.info(f"✓ Slave is back online after {waited}s")
                # Give it a bit more time to fully boot
                time.sleep(DEFAULT_POST_BOOT_SETTLE_TIME)

                # Flush and finalize console log
                if console_collector and console_log_id:
                    # Final flush
                    try:
                        chunk = console_collector.get_and_clear_buffer()
                        if chunk:
                            self.state.append_build_log_chunk(console_log_id, chunk)
                    except Exception as exc:
                        logger.debug(f"Failed final console buffer flush: {exc}")

                    # Stop collector and get any remaining output
                    console_output = self._stop_console_collection(console_collector)
                    if console_output:
                        try:
                            self.state.append_build_log_chunk(console_log_id, console_output)
                        except Exception as exc:
                            logger.debug(f"Failed to append final console output: {exc}")

                    # Add footer and finalize
                    try:
                        footer = f"\n\n=== BOOT RESULT: success ===\n"
                        footer += f"Boot time: {waited}s\n"
                        self.state.append_build_log_chunk(console_log_id, footer)
                        self.state.finalize_build_log(console_log_id, 0)

                        log_data = self.state.get_build_log(console_log_id)
                        if log_data:
                            size_kb = log_data["size_bytes"] / 1024
                            logger.info(f"✓ Console log captured (log_id: {console_log_id}, {size_kb:.1f} KB)")
                    except Exception as exc:
                        logger.warning(f"Failed to finalize console log: {exc}")
                else:
                    # Fallback to old method if streaming not enabled
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

        # Timeout! Flush and finalize console log
        logger.error("Slave failed to reboot within timeout")

        if console_collector and console_log_id:
            # Final flush
            try:
                chunk = console_collector.get_and_clear_buffer()
                if chunk:
                    self.state.append_build_log_chunk(console_log_id, chunk)
            except Exception as exc:
                logger.debug(f"Failed final console buffer flush: {exc}")

            # Stop collector and get remaining output
            console_output = self._stop_console_collection(console_collector)
            if console_output:
                try:
                    self.state.append_build_log_chunk(console_log_id, console_output)
                except Exception as exc:
                    logger.debug(f"Failed to append final console output: {exc}")

            # Add footer and finalize
            try:
                footer = f"\n\n=== BOOT RESULT: timeout ===\n"
                footer += f"Timeout after: {waited}s\n"
                self.state.append_build_log_chunk(console_log_id, footer)
                self.state.finalize_build_log(console_log_id, 1)

                log_data = self.state.get_build_log(console_log_id)
                if log_data:
                    size_kb = log_data["size_bytes"] / 1024
                    logger.info(f"✓ Console log captured (log_id: {console_log_id}, {size_kb:.1f} KB)")
            except Exception as exc:
                logger.warning(f"Failed to finalize console log: {exc}")
        else:
            # Fallback to old method
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

                ipmi = IPMIController(self.config.ipmi_host, self.config.ipmi_user, self.config.ipmi_password)

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
                        logger.info(f"✓ Slave recovered via IPMI after {ipmi_wait}s (attempt {attempt}/{max_attempts})")
                        time.sleep(DEFAULT_POST_BOOT_SETTLE_TIME)

                        # Get kernel version
                        ret, kernel_ver, _ = self.ssh.call_function("get_kernel_version")
                        if ret == 0 and kernel_ver.strip():
                            logger.info(f"Booted into kernel: {kernel_ver.strip()}")
                            return (True, kernel_ver.strip())

                        return (True, None)

                    if ipmi_wait % 30 == 0:
                        logger.info(f"Still waiting after IPMI... ({ipmi_wait}/{ipmi_max_wait}s)")

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

    def run_tests(self, iteration_id: int) -> Tuple[TestResult, Optional[int]]:
        """Run tests on slave and store test log with streaming.

        Args:
            iteration_id: Iteration ID for log storage

        Returns:
            Tuple of (test_result, log_id)
        """
        logger.info("Running tests on slave...")

        # Create initial log entry with header
        log_header = f"=== Test Execution ===\n"
        log_header += f"Test type: {self.config.test_type}\n"
        if self.config.test_script:
            log_header += f"Test script: {self.config.test_script}\n"
        log_header += f"Timeout: {self.config.test_timeout}s\n\n"
        log_header += "=== TEST OUTPUT ===\n"

        log_id = self.state.create_build_log(iteration_id, "test", log_header)
        logger.debug(f"Created test log {log_id} for streaming")

        # Streaming state
        buffer = []
        buffer_size = 0
        buffer_limit = 10 * 1024  # Flush every 10KB
        total_bytes = 0
        start_time = time.time()
        last_progress_time = start_time
        progress_interval = 20  # Log progress every 20 seconds

        def stream_callback(stdout_chunk: str, stderr_chunk: str) -> None:
            """Handle streaming output chunks."""
            nonlocal buffer, buffer_size, total_bytes, last_progress_time

            chunk = stdout_chunk + stderr_chunk
            if not chunk:
                return

            # Add to buffer
            buffer.append(chunk)
            chunk_bytes = len(chunk.encode("utf-8"))
            buffer_size += chunk_bytes
            total_bytes += chunk_bytes

            # Flush buffer if it's getting large
            if buffer_size >= buffer_limit:
                combined_chunk = "".join(buffer)
                try:
                    self.state.append_build_log_chunk(log_id, combined_chunk)
                except Exception as exc:
                    logger.warning(f"Failed to append log chunk: {exc}")

                buffer.clear()
                buffer_size = 0

            # Log progress periodically (mainly useful for long-running custom tests)
            current_time = time.time()
            if current_time - last_progress_time >= progress_interval:
                elapsed = int(current_time - start_time)
                logger.info(f"  Testing... {total_bytes // 1024}KB logged, {elapsed // 60}m {elapsed % 60}s elapsed")
                last_progress_time = current_time

        # Call run_test function with streaming
        if self.config.test_script:
            ret, stdout, stderr = self.ssh.call_function_streaming(
                "run_test",
                self.config.test_type,
                self.config.test_script,
                timeout=self.config.test_timeout,
                chunk_callback=stream_callback,
            )
        else:
            ret, stdout, stderr = self.ssh.call_function_streaming(
                "run_test",
                self.config.test_type,
                timeout=self.config.test_timeout,
                chunk_callback=stream_callback,
            )

        # Flush remaining buffer
        if buffer:
            combined_chunk = "".join(buffer)
            try:
                self.state.append_build_log_chunk(log_id, combined_chunk)
            except Exception as exc:
                logger.warning(f"Failed to append final log chunk: {exc}")

        # Add exit code to log
        footer = f"\n\n=== EXIT CODE: {ret} ===\n"
        try:
            self.state.append_build_log_chunk(log_id, footer)
            self.state.finalize_build_log(log_id, ret)
        except Exception as exc:
            logger.warning(f"Failed to finalize log: {exc}")

        # Format size for display
        size_kb = self.state.get_build_log(log_id)["size_bytes"] / 1024
        elapsed = int(time.time() - start_time)

        logger.debug(f"Test output: {stdout}")

        if ret == 0:
            logger.info(f"✓ Tests PASSED in {elapsed}s (log_id: {log_id}, {size_kb:.1f} KB compressed)")
            return (TestResult.GOOD, log_id)

        logger.error(f"✗ Tests FAILED in {elapsed}s (log_id: {log_id}, {size_kb:.1f} KB compressed)")
        logger.debug(f"Test error: {stderr}")
        return (TestResult.BAD, log_id)

    def mark_commit(self, commit_sha: str, result: TestResult) -> Tuple[bool, bool]:
        """Mark commit as good or bad in git bisect.

        Args:
            commit_sha: Commit SHA to mark
            result: Test result

        Returns:
            Tuple of (success, bisection_complete)
        """
        if result == TestResult.SKIP:
            bisect_cmd = "git bisect skip"
        elif result == TestResult.GOOD:
            bisect_cmd = "git bisect good"
        elif result == TestResult.BAD:
            bisect_cmd = "git bisect bad"
        else:
            logger.error(f"Cannot mark commit with result: {result}")
            return (False, False)

        ret, stdout, stderr = self.ssh.run_command(
            f"cd {shlex.quote(self.config.slave_kernel_path)} && {bisect_cmd}"
        )

        if ret != 0:
            logger.error(f"Failed to mark commit: {stderr}")
            return (False, False)

        # Check if bisection just completed
        bisection_complete = "first bad commit" in stdout or "first bad commit" in stderr

        logger.info(f"Marked commit {commit_sha[:SHORT_COMMIT_LENGTH]} as {result.value}")

        if bisection_complete:
            logger.info("Git bisect reports: First bad commit found!")

        return (True, bisection_complete)

    def _handle_boot_failure(
        self,
        iteration: BisectIteration,
        iteration_id: int,
        commit_sha: str,
        expected_kernel_ver: Optional[str],
        actual_kernel_ver: Optional[str],
        is_timeout: bool = False,
    ) -> Optional[bool]:
        """Handle boot failure scenario.

        Args:
            iteration: Current iteration object
            iteration_id: Iteration database ID
            commit_sha: Commit SHA being tested
            expected_kernel_ver: Expected kernel version
            actual_kernel_ver: Actual kernel version that booted
            is_timeout: Whether failure was due to timeout

        Returns:
            True if bisection completed, False if not, None if slave is down
        """
        if is_timeout:
            logger.error("✗ Boot timeout - slave did not respond!")
            logger.error(f"  Expected kernel: {expected_kernel_ver}")
            logger.error("  Slave did not respond within timeout")
        else:
            logger.error("✗ Boot failure - kernel version mismatch!")
            logger.error(f"  Expected: {expected_kernel_ver}")
            logger.error(f"  Actual:   {actual_kernel_ver}")
            logger.error("  Test kernel failed to boot, system fell back to protected kernel")

        # Determine result based on test type
        if self.config.test_type == "boot" or not self.config.test_script:
            # Boot test mode: non-bootable kernel is BAD
            iteration.result = TestResult.BAD
            iteration.error = "Boot timeout - kernel failed to boot" if is_timeout else "Boot failure - kernel version mismatch (wrong kernel booted)"
            result_str = "bad"
        else:
            # Custom test mode: can't test functionality if kernel doesn't boot
            iteration.result = TestResult.SKIP
            iteration.error = "Boot timeout - cannot test functionality, skipping commit" if is_timeout else "Boot failure - cannot test functionality (wrong kernel booted)"
            result_str = "skip"

        # CRITICAL: Only mark commit if SSH is working
        if not self.ssh.is_alive():
            logger.critical("  ✗ Cannot mark commit - slave is unreachable")
            logger.critical("  Bisection will halt - manual recovery required")
            # Store iteration with error, but don't mark in git bisect yet
            self.state.update_iteration(iteration_id, error_message=iteration.error + " (git mark pending - slave down)")
            self.state.update_session(self.session_id, status="halted")
            return None

        # SSH is working - safe to mark commit
        bisection_complete = False
        if iteration.result == TestResult.BAD:
            logger.error("  Marking as BAD (boot test mode)")
            success, bisection_complete = self.mark_commit(commit_sha, TestResult.BAD)
        else:
            logger.warning("  Marking as SKIP (custom test mode - cannot test if kernel doesn't boot)")
            success, bisection_complete = self.mark_commit(commit_sha, TestResult.SKIP)

        self.state.update_iteration(iteration_id, final_result=result_str, error_message=iteration.error)
        return bisection_complete

    def run_iteration(self, commit_sha: str) -> Tuple[BisectIteration, bool]:
        """Run single bisection iteration.

        Args:
            commit_sha: Commit SHA to test

        Returns:
            Tuple of (BisectIteration object, bisection_complete flag)
        """
        self.iteration_count += 1

        # Get commit info
        ret, commit_msg, _ = self.ssh.run_command(f"cd {self.config.slave_kernel_path} && git log -1 --oneline {commit_sha}")
        commit_msg = commit_msg.strip() if ret == 0 else "Unknown"

        # Create iteration in database
        iteration_id = self.state.create_iteration(self.session_id, self.iteration_count, commit_sha, commit_msg)

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

        bisection_complete = False

        try:
            # Build kernel
            iteration.state = BisectState.BUILDING
            self.save_state()

            success, _exit_code, _log_id, expected_kernel_ver = self.build_kernel(commit_sha, iteration_id)
            if not success:
                iteration.result = TestResult.SKIP
                iteration.error = "Build failed"
                logger.error("Build failed, skipping commit")
                _, bisection_complete = self.mark_commit(commit_sha, TestResult.SKIP)
                self.state.update_iteration(iteration_id, final_result="skip", error_message="Build failed")
                return (iteration, bisection_complete)

            # Kernel version was extracted from build output
            if not expected_kernel_ver:
                logger.warning("Could not determine kernel version from build output")

            # Reboot slave
            iteration.state = BisectState.REBOOTING
            self.save_state()

            reboot_success, actual_kernel_ver = self.reboot_slave(iteration_id)

            if not reboot_success:
                # Boot timeout or complete failure
                maybe_complete = self._handle_boot_failure(iteration, iteration_id, commit_sha, expected_kernel_ver, None, is_timeout=True)

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

                # Slave is up but boot failed - check if bisection completed
                if maybe_complete is not None:
                    bisection_complete = maybe_complete

                return (iteration, bisection_complete)

            # Verify which kernel actually booted
            if actual_kernel_ver:
                logger.info(f"Booted kernel version: {actual_kernel_ver}")

                if expected_kernel_ver and actual_kernel_ver != expected_kernel_ver:
                    # Kernel panic detected - system fell back to protected kernel
                    maybe_complete = self._handle_boot_failure(
                        iteration,
                        iteration_id,
                        commit_sha,
                        expected_kernel_ver,
                        actual_kernel_ver,
                        is_timeout=False,
                    )
                    if maybe_complete is not None:
                        bisection_complete = maybe_complete
                    return (iteration, bisection_complete)

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

            test_result, test_log_id = self.run_tests(iteration_id)
            iteration.result = test_result

            # Update iteration in database
            self.state.update_iteration(
                iteration_id,
                final_result=test_result.value,
                end_time=datetime.utcnow().isoformat(),
            )

            # Mark in git bisect
            _, bisection_complete = self.mark_commit(commit_sha, test_result)

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

        return (iteration, bisection_complete)

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
                logger.error(f"SAFETY LIMIT REACHED: Exceeded {MAX_ITERATIONS} iterations. Bisection may be stuck in an infinite loop. Stopping.")
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
            iteration, bisection_complete = self.run_iteration(commit)

            logger.info(f"Result: {iteration.result.value}")

            # Check if bisection completed
            if bisection_complete:
                logger.info("\n=== Bisection Found First Bad Commit! ===")
                self.generate_report()
                return True

        self.generate_report()
        return True

    def _iteration_to_dict(self, iteration: BisectIteration) -> dict:
        """Convert BisectIteration to JSON-serializable dict.

        Args:
            iteration: BisectIteration object to convert

        Returns:
            Dictionary with enum values converted to strings
        """
        data = asdict(iteration)
        # Convert enums to their string values for JSON serialization
        if isinstance(data.get("state"), BisectState):
            data["state"] = data["state"].value
        if data.get("result") and isinstance(data["result"], TestResult):
            data["result"] = data["result"].value
        return data

    def save_state(self) -> None:
        """Save bisection state to database."""
        state = {
            "good_commit": self.good_commit,
            "bad_commit": self.bad_commit,
            "iteration_count": self.iteration_count,
            "current_iteration": (self._iteration_to_dict(self.current_iteration) if self.current_iteration else None),
            "iterations": [self._iteration_to_dict(it) for it in self.iterations],
            "last_update": datetime.utcnow().isoformat(),
        }

        # Store state in database
        self.state.update_session_state(self.session_id, state)

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
            logger.info(f"{iteration.iteration:3d}. {iteration.commit_short} | {status:7s} | {duration:6s} | {iteration.commit_message[:50]}")

        # Get final result from git bisect
        ret, stdout, _ = self.ssh.run_command(
            f"cd {shlex.quote(self.config.slave_kernel_path)} && git bisect log | grep 'first bad commit' -A 5"
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
