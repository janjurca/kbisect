#!/usr/bin/env python3
"""Master Bisection Controller.

Orchestrates the kernel bisection process across master and slave machines.
"""

import json
import logging
import shlex
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, List, Optional, Tuple


if TYPE_CHECKING:
    from kbisect.collectors import ConsoleCollector
    from kbisect.power.base import PowerController

from kbisect.config.config import BisectConfig, HostConfig
from kbisect.remote import SSHClient


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


class HostManager:
    """Manages a single host in multi-host bisection.

    Encapsulates per-host resources and operations including SSH client,
    power controller, and console collector.

    Attributes:
        config: Host configuration
        host_id: Database host ID
        ssh: SSH client for this host
        power_controller: Power controller for this host (optional)
        console_collector: Console log collector for this host (optional)
    """

    def __init__(
        self,
        host_config: HostConfig,
        host_id: int,
        build_timeout: int = 1800,
        boot_timeout: int = 300,
        test_timeout: int = 600,
        ssh_connect_timeout: int = 15,
    ) -> None:
        """Initialize host manager.

        Args:
            host_config: Host configuration
            host_id: Database host ID
            build_timeout: Build timeout in seconds
            boot_timeout: Boot timeout in seconds
            test_timeout: Test timeout in seconds
            ssh_connect_timeout: SSH connection timeout in seconds
        """
        self.config = host_config
        self.host_id = host_id
        self.build_timeout = build_timeout
        self.boot_timeout = boot_timeout
        self.test_timeout = test_timeout
        self.ssh_connect_timeout = ssh_connect_timeout

        # Create SSH client for this host
        self.ssh = SSHClient(host_config.hostname, host_config.ssh_user, ssh_connect_timeout)

        # Create power controller based on configured type
        self.power_controller: Optional["PowerController"] = None  # noqa: UP037
        if host_config.power_control_type == "ipmi":
            if (
                host_config.ipmi_host
                and host_config.ipmi_user is not None
                and host_config.ipmi_password is not None
            ):
                from kbisect.power import IPMIController

                self.power_controller = IPMIController(
                    host_config.ipmi_host,
                    host_config.ipmi_user,
                    host_config.ipmi_password,
                    ssh_host=host_config.hostname,
                    ssh_connect_timeout=ssh_connect_timeout,
                )
        elif host_config.power_control_type == "beaker":
            from kbisect.power import BeakerController

            self.power_controller = BeakerController(host_config.hostname, ssh_connect_timeout)

        # Console collector (created per boot cycle)
        self.console_collector: Optional["ConsoleCollector"] = None  # noqa: UP037

    def __repr__(self) -> str:
        """String representation."""
        return f"<HostManager(hostname={self.config.hostname}, host_id={self.host_id})>"


class BisectMaster:
    """Main bisection controller.

    Orchestrates the kernel bisection process including building kernels,
    rebooting systems, running tests, and managing state.

    Supports both single-host (legacy) and multi-host bisection modes.

    Attributes:
        config: Bisection configuration
        good_commit: Known good commit
        bad_commit: Known bad commit
        ssh: SSH client for slave communication (single-host mode)
        host_managers: List of HostManager instances (multi-host mode)
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
        self.iterations: List[BisectIteration] = []
        self.current_iteration: Optional[BisectIteration] = None
        self.iteration_count = 0

        # Initialize state manager and create/load session
        from kbisect.persistence import StateManager

        self.state = StateManager(db_path=config.db_path)

        # Atomically get existing running session or create new one
        # This prevents race conditions when multiple instances are created
        self.session_id = self.state.get_or_create_session(good_commit, bad_commit)

        # Initialize host managers for all configured hosts
        self.host_managers: List[HostManager] = []
        logger.info(f"Initializing bisection with {len(config.hosts)} hosts")

        for host_config in config.hosts:
            # Create host record in database
            host_id = self.state.create_host(
                session_id=self.session_id,
                hostname=host_config.hostname,
                ssh_user=host_config.ssh_user,
                kernel_path=host_config.kernel_path,
                bisect_path=host_config.bisect_path,
                test_script=host_config.test_script,
                power_control_type=host_config.power_control_type,
                ipmi_host=host_config.ipmi_host,
                ipmi_user=host_config.ipmi_user,
                ipmi_password=host_config.ipmi_password,
            )

            # Create HostManager for this host
            host_manager = HostManager(
                host_config=host_config,
                host_id=host_id,
                build_timeout=config.build_timeout,
                boot_timeout=config.boot_timeout,
                test_timeout=config.test_timeout,
                ssh_connect_timeout=config.ssh_connect_timeout,
            )
            self.host_managers.append(host_manager)
            logger.info(f"  [{host_config.hostname}] HostManager created (host_id={host_id})")

        # Set ssh and power_controller to first host for convenience in some methods
        self.ssh = self.host_managers[0].ssh
        self.power_controller = self.host_managers[0].power_controller

        # Resolve test script paths for each host and store local paths for transfer
        self._local_test_scripts = {}  # Store mapping of host_id -> local_script_path for transfer
        for host_manager in self.host_managers:
            test_script = host_manager.config.test_script
            if test_script:
                script_path = Path(test_script)
                if script_path.exists():
                    # It's a local file on master - need to transfer it
                    logger.debug(f"Test script is local file on master: {test_script}")
                    bisect_base_dir = Path(host_manager.config.bisect_path).parent
                    remote_script_dir = bisect_base_dir / "test-scripts"
                    remote_script_path = str(remote_script_dir / script_path.name)

                    # Store local path for transfer during initialization
                    self._local_test_scripts[host_manager.host_id] = {
                        "local_path": str(script_path),
                        "remote_path": remote_script_path,
                        "remote_dir": str(remote_script_dir),
                    }

                    # Update config to use remote path
                    host_manager.config.test_script = remote_script_path
                    logger.debug(f"Resolved test script to slave path: {remote_script_path}")
                else:
                    logger.debug(f"Test script assumed to exist on remote host: {test_script}")

        # Resolve kernel config paths for each host and store local paths for transfer
        self._local_kernel_configs = {}  # Store mapping of host_id -> local_config_path for transfer

        # First, validate global config if set
        global_config_path = None
        if self.config.kernel_config_file:
            global_config_path = Path(self.config.kernel_config_file)
            if not global_config_path.exists():
                logger.error(
                    f"Global kernel config file not found on master: {self.config.kernel_config_file}"
                )
                raise FileNotFoundError(
                    f"Global kernel config file not found: {self.config.kernel_config_file}"
                )
            logger.debug(f"Global kernel config validated: {self.config.kernel_config_file}")

        # Process each host's kernel config (per-host or fallback to global)
        for host_manager in self.host_managers:
            kernel_config_file = host_manager.config.kernel_config_file

            # If no per-host config, use global config
            if not kernel_config_file and global_config_path:
                kernel_config_file = str(global_config_path)
                logger.debug(f"Host {host_manager.config.hostname} using global kernel config")

            # Only process if kernel_config_file is set (either per-host or global)
            if kernel_config_file:
                config_path = Path(kernel_config_file)
                if config_path.exists():
                    # It's a local file on master - need to transfer it
                    logger.debug(f"Kernel config is local file on master: {kernel_config_file}")
                    bisect_base_dir = Path(host_manager.config.bisect_path).parent
                    remote_config_dir = bisect_base_dir / "kernel-configs"
                    remote_config_path = str(remote_config_dir / config_path.name)

                    # Store local path for transfer during initialization
                    self._local_kernel_configs[host_manager.host_id] = {
                        "local_path": str(config_path),
                        "remote_path": remote_config_path,
                        "remote_dir": str(remote_config_dir),
                    }

                    # Update config to use remote path
                    host_manager.config.kernel_config_file = remote_config_path
                    logger.debug(f"Resolved kernel config to slave path: {remote_config_path}")
                else:
                    # File doesn't exist on master - this is an error since we expect it to be local
                    logger.error(f"Kernel config file not found on master: {kernel_config_file}")
                    raise FileNotFoundError(f"Kernel config file not found: {kernel_config_file}")

    def create_iteration_metadata_record(self, iteration_id: int) -> Optional[int]:
        """Create a minimal metadata record for an iteration.

        This creates a placeholder metadata record that can be used to link
        files (like kernel configs) before full metadata collection occurs.

        Args:
            iteration_id: Iteration ID for this metadata record

        Returns:
            Metadata ID if created successfully, None otherwise
        """
        # Create minimal metadata record
        minimal_metadata = {
            "collection_type": "iteration",
            "collection_time": datetime.now().isoformat(),
            "note": "Placeholder record - will be updated with full metadata after boot",
        }

        try:
            metadata_id = self.state.store_metadata(
                self.session_id,
                minimal_metadata,
                iteration_id,
                host_id=self.host_managers[0].host_id,
            )
            logger.debug(
                f"Created placeholder metadata record (id: {metadata_id}) for iteration {iteration_id}"
            )
            return metadata_id
        except Exception as exc:
            logger.warning(f"Failed to create placeholder metadata record: {exc}")
            return None

    def collect_and_store_metadata(
        self, collection_type: str, iteration_id: Optional[int] = None
    ) -> bool:
        """Collect metadata from all hosts and store in database.

        If a placeholder metadata record already exists for this iteration,
        it will be updated with the full metadata instead of creating a new record.

        Args:
            collection_type: Type of metadata to collect
            iteration_id: Optional iteration ID

        Returns:
            True if metadata collected successfully from all hosts, False otherwise
        """
        logger.debug(
            f"Collecting {collection_type} metadata from {len(self.host_managers)} host(s)..."
        )

        # Collect metadata from all hosts
        all_host_metadata = {}
        success_count = 0

        for host_manager in self.host_managers:
            hostname = host_manager.config.hostname
            logger.debug(f"  Collecting from {hostname}...")

            # Call bash function to collect metadata
            ret, stdout, stderr = host_manager.ssh.call_function(
                "collect_metadata", collection_type, timeout=host_manager.ssh_connect_timeout
            )

            if ret != 0:
                logger.warning(
                    f"  Failed to collect {collection_type} metadata from {hostname}: {stderr}"
                )
                all_host_metadata[hostname] = {"error": stderr, "status": "failed"}
                continue

            # Parse JSON response
            try:
                metadata_dict = json.loads(stdout)
                all_host_metadata[hostname] = metadata_dict
                success_count += 1
            except json.JSONDecodeError as exc:
                logger.warning(f"  Invalid JSON from {hostname} metadata collection: {exc}")
                logger.debug(f"  Raw output: {stdout}")
                all_host_metadata[hostname] = {"error": str(exc), "status": "parse_failed"}

        if success_count == 0:
            logger.warning(f"Failed to collect {collection_type} metadata from any host")
            return False

        logger.debug(
            f"  ✓ Collected metadata from {success_count}/{len(self.host_managers)} host(s)"
        )

        # Create multihost metadata structure
        multihost_metadata = {
            "collection_type": collection_type,
            "host_count": len(self.host_managers),
            "success_count": success_count,
            "hosts": all_host_metadata,
        }

        # Check if a placeholder metadata record already exists for this iteration
        if iteration_id:
            metadata_list = self.state.get_session_metadata(self.session_id, collection_type)
            if metadata_list:
                # Find metadata for this iteration
                iteration_metadata = [
                    m for m in metadata_list if m.get("iteration_id") == iteration_id
                ]
                if iteration_metadata:
                    # Update existing placeholder record instead of creating new one
                    metadata_id = iteration_metadata[0]["metadata_id"]
                    self.state.update_metadata(metadata_id, multihost_metadata)
                    logger.debug(
                        f"✓ Updated existing {collection_type} metadata record (id: {metadata_id})"
                    )
                    return True

        # No existing record found - create new one
        self.state.store_metadata(self.session_id, multihost_metadata, iteration_id)
        logger.debug(f"✓ Stored {collection_type} metadata for all hosts")

        return True

    def capture_kernel_config(
        self, kernel_version: str, iteration_id: int, host_manager: Optional[HostManager] = None
    ) -> bool:
        """Capture and store kernel config file(s) from host(s) in database.

        Reads the .config file from the kernel build directory (KERNEL_PATH/.config)
        instead of /boot, ensuring the file exists at collection time.

        Args:
            kernel_version: Kernel version string (for logging purposes)
            iteration_id: Iteration ID for linking config file
            host_manager: Optional specific host manager. If None, captures from all hosts.

        Returns:
            True if config captured successfully from all/specified host(s), False otherwise
        """
        # Determine which hosts to capture from
        hosts_to_capture = [host_manager] if host_manager else self.host_managers

        success_count = 0
        all_configs = {}

        for hm in hosts_to_capture:
            hostname = hm.config.hostname
            kernel_path = hm.config.kernel_path
            config_path = f"{kernel_path}/.config"

            # Download config file content from host (to memory, not disk)
            ret, stdout, stderr = hm.ssh.run_command(
                f"cat {config_path}", timeout=hm.ssh_connect_timeout
            )

            if ret != 0:
                logger.warning(f"  [{hostname}] Failed to read kernel config: {stderr}")
                all_configs[hostname] = {"error": stderr, "status": "failed"}
                continue

            if not stdout:
                logger.warning(f"  [{hostname}] Kernel config file is empty: {config_path}")
                all_configs[hostname] = {"error": "Config file empty", "status": "empty"}
                continue

            # Store config content for this host
            all_configs[hostname] = {
                "content": stdout,
                "kernel_version": kernel_version,
                "config_path": config_path,
                "status": "success",
            }
            success_count += 1

        if success_count == 0:
            logger.warning("Failed to capture kernel config from any host")
            return False

        # Store combined config content as metadata record with collection_type='file'
        try:
            # Create multihost config structure
            multihost_config = {
                "file_type": "kernel_config",
                "kernel_version": kernel_version,
                "host_count": len(hosts_to_capture),
                "success_count": success_count,
                "hosts": all_configs,
            }

            # Store as file metadata
            metadata_id = self.state.store_file_metadata(
                session_id=self.session_id,
                iteration_id=iteration_id,
                file_type="kernel_config_multihost",
                file_content=json.dumps(multihost_config, indent=2),
                kernel_version=kernel_version,
            )
            logger.info(
                f"✓ Captured kernel configs from {success_count}/{len(hosts_to_capture)} host(s) (metadata_id: {metadata_id})"
            )
            return True
        except Exception as exc:
            logger.error(f"Failed to store kernel configs in database: {exc}")
            return False

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

                result = subprocess.run(
                    clone_cmd, capture_output=True, text=True, timeout=3600, check=False
                )

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
                    logger.warning(
                        f"Failed to checkout branch {self.config.kernel_repo_branch}: {result.stderr}"
                    )
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

    def _configure_git_safe_directory(self, host_manager: HostManager) -> bool:
        """Configure git safe.directory for a host to prevent dubious ownership errors.

        Args:
            host_manager: Host manager for the target host

        Returns:
            True if configuration successful, False otherwise
        """
        kernel_path = host_manager.config.kernel_path
        hostname = host_manager.config.hostname

        logger.debug(f"Configuring git safe.directory on {hostname}...")
        ret, _stdout, stderr = host_manager.ssh.run_command(
            f"git config --global --add safe.directory {shlex.quote(kernel_path)}",
            timeout=host_manager.ssh_connect_timeout,
        )

        if ret != 0:
            logger.error(f"Failed to configure git safe.directory on {hostname}: {stderr}")
            return False

        logger.debug(f"  ✓ Git safe.directory configured on {hostname}")
        return True

    def _transfer_repo_to_hosts(self, local_repo_path: str) -> bool:
        """Transfer kernel repository from master to all hosts.

        Args:
            local_repo_path: Path to repository on master

        Returns:
            True if transfer succeeded on all hosts, False otherwise
        """
        logger.info(f"Transferring kernel repository to {len(self.host_managers)} hosts...")

        all_success = True
        for i, host_manager in enumerate(self.host_managers, 1):
            hostname = host_manager.config.hostname
            kernel_path = host_manager.config.kernel_path
            ssh_user = host_manager.config.ssh_user

            logger.info(f"\n[{i}/{len(self.host_managers)}] Transferring to {hostname}...")

            # Remove existing kernel path
            ret, _stdout, stderr = host_manager.ssh.run_command(
                f"rm -rf {shlex.quote(kernel_path)}", timeout=host_manager.ssh_connect_timeout
            )
            if ret != 0:
                logger.warning(f"  Failed to remove existing path (may not exist): {stderr}")

            # Create target directory
            ret, _stdout, stderr = host_manager.ssh.run_command(
                f"mkdir -p {shlex.quote(kernel_path)}", timeout=host_manager.ssh_connect_timeout
            )
            if ret != 0:
                logger.error(f"  Failed to create target directory: {stderr}")
                all_success = False
                continue

            # Transfer repository using rsync
            # Exclude .git/index files to prevent corruption from copying incomplete/inconsistent index
            rsync_cmd = [
                "rsync",
                "-avz",
                "--exclude=.git/index",
                "--exclude=.git/index.lock",
                "-e",
                f"ssh -o StrictHostKeyChecking=no -o ConnectTimeout={host_manager.ssh_connect_timeout}",
                f"{local_repo_path}/",
                f"{ssh_user}@{hostname}:{kernel_path}/",
            ]

            try:
                result = subprocess.run(
                    rsync_cmd, capture_output=True, text=True, timeout=3600, check=False
                )

                if result.returncode != 0:
                    logger.error(f"  Repository transfer failed: {result.stderr}")
                    all_success = False
                    continue

                # Verify repository exists
                ret, _stdout, stderr = host_manager.ssh.run_command(
                    f"test -d {shlex.quote(kernel_path)}/.git",
                    timeout=host_manager.ssh_connect_timeout,
                )
                if ret != 0:
                    logger.error("  Verification failed - .git directory not found")
                    all_success = False
                    continue

                # Configure git safe.directory before any git operations
                if not self._configure_git_safe_directory(host_manager):
                    logger.warning(
                        f"  Failed to configure git safe.directory on {host_manager.config.hostname}"
                    )

                # Clean up and regenerate Git index (prevents corruption from partial rsync)
                # Remove any existing index files and let Git recreate them
                logger.debug(f"  Regenerating Git index on {hostname}...")
                ret, _stdout, stderr = host_manager.ssh.run_command(
                    f"cd {shlex.quote(kernel_path)} && "
                    f"rm -f .git/index .git/index.lock && "
                    f"git reset --hard HEAD",
                    timeout=host_manager.ssh_connect_timeout,
                )
                if ret != 0:
                    logger.error(f"  Failed to regenerate Git index: {stderr}")
                    all_success = False
                    continue

                # Verify repository health after transfer and index regeneration
                ret, _stdout, stderr = host_manager.ssh.run_command(
                    f"cd {shlex.quote(kernel_path)} && git status",
                    timeout=host_manager.ssh_connect_timeout,
                )
                if ret != 0:
                    logger.error(f"  Repository health check failed: {stderr}")
                    all_success = False
                    continue

                logger.info("  ✓ Transfer successful, repository verified")

            except subprocess.TimeoutExpired:
                logger.error("  Repository transfer timed out")
                all_success = False
            except Exception as exc:
                logger.error(f"  Error transferring repository: {exc}")
                all_success = False

        # Cleanup temp directory on master
        temp_dir = str(Path(local_repo_path).parent)
        logger.debug(f"Cleaning up temp directory: {temp_dir}")
        subprocess.run(["rm", "-rf", temp_dir], check=False)

        return all_success

    def initialize(self) -> bool:
        """Initialize bisection.

        Returns:
            True if initialization successful, False otherwise
        """
        # Get first host for git bisect initialization (all hosts share same git state)
        first_host = self.host_managers[0]
        kernel_path = first_host.config.kernel_path

        logger.info("=== Initializing Kernel Bisection ===")
        logger.info(f"Good commit: {self.good_commit}")
        logger.info(f"Bad commit: {self.bad_commit}")
        logger.info(f"Hosts: {len(self.host_managers)}")
        for hm in self.host_managers:
            logger.info(f"  - {hm.config.hostname}")

        # Check all hosts are reachable
        logger.info("Checking host connectivity...")
        for hm in self.host_managers:
            if not hm.ssh.is_alive():
                logger.error(f"Host {hm.config.hostname} is not reachable!")
                return False
            logger.info(f"  ✓ {hm.config.hostname} is reachable")

        # Check kernel directories exist before validating commits
        logger.info("Checking kernel directories...")
        all_exist, missing_hosts = self._validate_kernel_directories()

        if not all_exist:
            logger.error("")
            logger.error("=" * 70)
            logger.error("KERNEL DIRECTORY NOT FOUND")
            logger.error("=" * 70)
            logger.error("")
            logger.error("The following hosts are missing kernel directories:")
            for hostname in missing_hosts:
                # Find the kernel_path for this host
                for hm in self.host_managers:
                    if hm.config.hostname == hostname:
                        logger.error(f"  - {hostname}: {hm.config.kernel_path}")
                        break
            logger.error("")

            if self.config.kernel_repo_source:
                logger.info("Auto-deploy is configured - will deploy kernel repository")
                # Continue to deployment section below
            else:
                logger.error("No kernel_repo_source configured in bisect.yaml")
                logger.error("")
                logger.error("Solutions:")
                logger.error("  1. Manually set up kernel repository on hosts")
                logger.error("  2. Configure kernel_repo_source in bisect.yaml for auto-deploy")
                logger.error("")
                logger.error("=" * 70)
                return False
        else:
            logger.info("✓ Kernel directories exist on all hosts")

        # Validate commits before starting bisection
        logger.info("Validating bisect commits...")
        is_valid, error_msg = self._validate_bisect_commits()
        if not is_valid:
            logger.error("")
            logger.error("=" * 70)
            logger.error("COMMIT VALIDATION FAILED")
            logger.error("=" * 70)
            logger.error("")
            logger.error(error_msg)
            logger.error("")
            logger.error("Please check your good and bad commits:")
            logger.error(f"  Good (should be older/working): {self.good_commit}")
            logger.error(f"  Bad (should be newer/broken):   {self.bad_commit}")
            logger.error("")
            logger.error("Hint: You may have swapped the commits. Try:")
            logger.error(f"  kbisect init {self.bad_commit} {self.good_commit}")
            logger.error("=" * 70)
            return False

        logger.info("✓ Commits validated successfully")

        # Prepare and transfer kernel repository if configured
        if self.config.kernel_repo_source:
            logger.info("Kernel repository source configured, preparing...")
            repo_path = self._prepare_kernel_repo()

            if not repo_path:
                logger.error("Failed to prepare kernel repository on master")
                return False

            if not self._transfer_repo_to_hosts(repo_path):
                logger.error("Failed to transfer kernel repository to hosts")
                return False

            logger.info("✓ Kernel repository deployed to all hosts")

        # Initialize git bisect (use first host since all share same git state)
        logger.info("Initializing git bisect...")
        ret, _stdout, stderr = first_host.ssh.run_command(
            f"cd {shlex.quote(kernel_path)} && "
            f"git bisect reset >/dev/null 2>&1; "
            f"git bisect start {shlex.quote(self.bad_commit)} {shlex.quote(self.good_commit)}",
            timeout=first_host.ssh_connect_timeout,
        )

        if ret != 0:
            logger.error(f"Failed to initialize git bisect: {stderr}")
            return False

        logger.info("✓ Git bisect initialized")

        # Initialize protection on all hosts
        logger.info("Initializing kernel protection on all hosts...")
        for hm in self.host_managers:
            ret, _stdout, stderr = hm.ssh.call_function("init_protection")
            if ret != 0:
                logger.error(f"Failed to initialize protection on {hm.config.hostname}: {stderr}")
                return False
            logger.info(f"  ✓ {hm.config.hostname}")

        # Configure git safe.directory on all hosts
        logger.info("Configuring git safe.directory on all hosts...")
        for hm in self.host_managers:
            if not self._configure_git_safe_directory(hm):
                logger.error(f"Failed to configure git safe.directory on {hm.config.hostname}")
                return False
            logger.info(f"  ✓ {hm.config.hostname}")

        # Transfer test scripts if they're local files
        if self._local_test_scripts:
            logger.info("Transferring test scripts to hosts...")
            for hm in self.host_managers:
                if hm.host_id in self._local_test_scripts:
                    script_info = self._local_test_scripts[hm.host_id]
                    local_path = script_info["local_path"]
                    remote_path = script_info["remote_path"]
                    remote_dir = script_info["remote_dir"]

                    # Create remote directory
                    ret, _stdout, stderr = hm.ssh.run_command(
                        f"mkdir -p {shlex.quote(remote_dir)}", timeout=hm.ssh_connect_timeout
                    )
                    if ret != 0:
                        logger.error(
                            f"Failed to create test script directory on {hm.config.hostname}: {stderr}"
                        )
                        return False

                    # Transfer script using SCP via subprocess
                    try:
                        scp_cmd = [
                            "scp",
                            "-o",
                            "StrictHostKeyChecking=no",
                            "-o",
                            f"ConnectTimeout={hm.ssh_connect_timeout}",
                            local_path,
                            f"{hm.config.ssh_user}@{hm.config.hostname}:{remote_path}",
                        ]
                        result = subprocess.run(
                            scp_cmd,
                            capture_output=True,
                            text=True,
                            timeout=hm.ssh_connect_timeout,
                            check=False,
                        )

                        if result.returncode != 0:
                            logger.error(
                                f"Failed to transfer test script to {hm.config.hostname}: {result.stderr}"
                            )
                            return False

                        # Make script executable
                        ret, _stdout, stderr = hm.ssh.run_command(
                            f"chmod +x {shlex.quote(remote_path)}", timeout=hm.ssh_connect_timeout
                        )
                        if ret != 0:
                            logger.error(
                                f"Failed to make test script executable on {hm.config.hostname}: {stderr}"
                            )
                            return False

                        logger.info(
                            f"  ✓ Transferred test script to {hm.config.hostname}: {remote_path}"
                        )

                    except subprocess.TimeoutExpired:
                        logger.error(f"Test script transfer to {hm.config.hostname} timed out")
                        return False
                    except Exception as exc:
                        logger.error(
                            f"Error transferring test script to {hm.config.hostname}: {exc}"
                        )
                        return False

        # Transfer kernel configs if they're local files
        if self._local_kernel_configs:
            logger.info("Transferring kernel configs to hosts...")
            for hm in self.host_managers:
                if hm.host_id in self._local_kernel_configs:
                    config_info = self._local_kernel_configs[hm.host_id]
                    local_path = config_info["local_path"]
                    remote_path = config_info["remote_path"]
                    remote_dir = config_info["remote_dir"]

                    # Create remote directory
                    ret, _stdout, stderr = hm.ssh.run_command(
                        f"mkdir -p {shlex.quote(remote_dir)}", timeout=hm.ssh_connect_timeout
                    )
                    if ret != 0:
                        logger.error(
                            f"Failed to create kernel config directory on {hm.config.hostname}: {stderr}"
                        )
                        return False

                    # Transfer config using SCP via subprocess
                    try:
                        scp_cmd = [
                            "scp",
                            "-o",
                            "StrictHostKeyChecking=no",
                            "-o",
                            f"ConnectTimeout={hm.ssh_connect_timeout}",
                            local_path,
                            f"{hm.config.ssh_user}@{hm.config.hostname}:{remote_path}",
                        ]
                        result = subprocess.run(
                            scp_cmd,
                            capture_output=True,
                            text=True,
                            timeout=hm.ssh_connect_timeout,
                            check=False,
                        )

                        if result.returncode != 0:
                            logger.error(
                                f"Failed to transfer kernel config to {hm.config.hostname}: {result.stderr}"
                            )
                            return False

                        logger.info(
                            f"  ✓ Transferred kernel config to {hm.config.hostname}: {remote_path}"
                        )

                    except subprocess.TimeoutExpired:
                        logger.error(f"Kernel config transfer to {hm.config.hostname} timed out")
                        return False
                    except Exception as exc:
                        logger.error(
                            f"Error transferring kernel config to {hm.config.hostname}: {exc}"
                        )
                        return False

        # Collect baseline metadata if enabled (use first host)
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

        After git bisect is initialized and commits are marked, git automatically
        checks out the next commit to test. This method simply returns the current
        HEAD commit that git bisect has checked out.

        Uses first host since all hosts share the same git bisect state.

        Returns:
            Commit SHA or None if bisection complete
        """
        first_host = self.host_managers[0]
        kernel_path = first_host.config.kernel_path

        ret, stdout, stderr = first_host.ssh.run_command(
            f"cd {shlex.quote(kernel_path)} && git rev-parse HEAD",
            timeout=first_host.ssh_connect_timeout,
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

    def mark_commit(self, commit_sha: str, result: TestResult) -> Tuple[bool, bool]:
        """Mark commit as good or bad in git bisect.

        Uses first host since all hosts share the same git bisect state.

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

        first_host = self.host_managers[0]
        kernel_path = first_host.config.kernel_path

        ret, stdout, stderr = first_host.ssh.run_command(
            f"cd {shlex.quote(kernel_path)} && {bisect_cmd}", timeout=first_host.ssh_connect_timeout
        )

        if ret != 0:
            # Check for specific git bisect error indicating inverted range
            if "merge base" in stderr and "is bad" in stderr:
                logger.error(f"Failed to mark commit: {stderr}")
                logger.error("")
                logger.error("=" * 70)
                logger.error("BISECT RANGE ERROR DETECTED")
                logger.error("=" * 70)
                logger.error("")
                logger.error("Git bisect reports the merge base is bad. This usually means:")
                logger.error("")
                logger.error("1. SWAPPED COMMITS: Your good/bad commits are inverted")
                logger.error("   - Current good: %s", self.good_commit[:12])
                logger.error("   - Current bad:  %s", self.bad_commit[:12])
                logger.error("   Solution: Swap the good and bad commits when reinitializing")
                logger.error("")
                logger.error("2. INVERTED TEST LOGIC: Your test script has reversed logic")
                logger.error("   - Should exit 0 when bug is ABSENT (good)")
                logger.error("   - Should exit non-zero when bug is PRESENT (bad)")
                logger.error("")
                logger.error("3. BUG OUTSIDE RANGE: The regression is before the good commit")
                logger.error("   - The bug may have been introduced earlier in history")
                logger.error("   - Or it may have been fixed (not introduced) in your range")
                logger.error("")
                logger.error("NOTE: This error should have been caught during initialization.")
                logger.error("If you see this, please re-run initialization with correct commits:")
                logger.error("  kbisect init <good> <bad>")
                logger.error("")
                logger.error("=" * 70)
                return (False, False)
            else:
                logger.error(f"Failed to mark commit: {stderr}")
                return (False, False)

        # Check if bisection just completed
        bisection_complete = "first bad commit" in stdout or "first bad commit" in stderr

        logger.info(f"Marked commit {commit_sha[:SHORT_COMMIT_LENGTH]} as {result.value}")

        if bisection_complete:
            logger.info("Git bisect reports: First bad commit found!")

        return (True, bisection_complete)

    def _validate_bisect_commits(self) -> Tuple[bool, str]:
        """Validate that good/bad commits are correct for bisection.

        Assumes kernel directories have already been verified to exist.

        Checks:
        - Both commits exist in the repository
        - Commits are different
        - Good commit is an ancestor of bad commit

        Returns:
            Tuple of (is_valid, error_message). If is_valid is True, error_message is empty.
        """
        first_host = self.host_managers[0]
        kernel_path = first_host.config.kernel_path

        # Step 1: Resolve commits to full SHAs
        logger.debug(f"Validating commits: good={self.good_commit}, bad={self.bad_commit}")

        good_full = None
        bad_full = None

        # Verify and resolve good commit
        ret, stdout, stderr = first_host.ssh.run_command(
            f"cd {shlex.quote(kernel_path)} && git rev-parse --verify {shlex.quote(self.good_commit)}^{{commit}}",
            timeout=first_host.ssh_connect_timeout,
        )

        if ret != 0:
            # Check if it's a directory issue vs commit issue
            if "No such file or directory" in stderr and ("cd:" in stderr or kernel_path in stderr):
                return (
                    False,
                    f"Kernel directory does not exist: {kernel_path}\n"
                    f"This error should have been caught earlier - please report this bug.\n"
                    f"Git error: {stderr.strip()}",
                )
            else:
                return (
                    False,
                    f"Good commit '{self.good_commit}' does not exist in the repository.\n"
                    f"Git error: {stderr.strip()}",
                )

        good_full = stdout.strip()
        logger.debug(f"Good commit resolved to: {good_full}")

        # Verify and resolve bad commit
        ret, stdout, stderr = first_host.ssh.run_command(
            f"cd {shlex.quote(kernel_path)} && git rev-parse --verify {shlex.quote(self.bad_commit)}^{{commit}}",
            timeout=first_host.ssh_connect_timeout,
        )

        if ret != 0:
            # Check if it's a directory issue vs commit issue
            if "No such file or directory" in stderr and ("cd:" in stderr or kernel_path in stderr):
                return (
                    False,
                    f"Kernel directory does not exist: {kernel_path}\n"
                    f"This error should have been caught earlier - please report this bug.\n"
                    f"Git error: {stderr.strip()}",
                )
            else:
                return (
                    False,
                    f"Bad commit '{self.bad_commit}' does not exist in the repository.\n"
                    f"Git error: {stderr.strip()}",
                )

        bad_full = stdout.strip()
        logger.debug(f"Bad commit resolved to: {bad_full}")

        # Step 2: Check if commits are the same
        if good_full == bad_full:
            return (
                False,
                f"Good and bad commits are the same: {good_full}\n"
                "Bisection requires two different commits.",
            )

        # Step 3: Check if good commit is an ancestor of bad commit
        ret, _stdout, stderr = first_host.ssh.run_command(
            f"cd {shlex.quote(kernel_path)} && git merge-base --is-ancestor {shlex.quote(good_full)} {shlex.quote(bad_full)}",
            timeout=first_host.ssh_connect_timeout,
        )

        if ret != 0:
            # Check if the error is because they're on different branches
            # or because the ancestry is reversed
            ret_reverse, _stdout_reverse, _stderr_reverse = first_host.ssh.run_command(
                f"cd {shlex.quote(kernel_path)} && git merge-base --is-ancestor {shlex.quote(bad_full)} {shlex.quote(good_full)}",
                timeout=first_host.ssh_connect_timeout,
            )

            if ret_reverse == 0:
                # Bad is ancestor of good - commits are swapped!
                return (
                    False,
                    f"Commits appear to be SWAPPED!\n"
                    f"The 'bad' commit ({self.bad_commit[:12]}) is actually an ancestor of\n"
                    f"the 'good' commit ({self.good_commit[:12]}).\n\n"
                    f"This means:\n"
                    f"  - Your 'good' commit is the NEWER one (should be the broken version)\n"
                    f"  - Your 'bad' commit is the OLDER one (should be the working version)\n\n"
                    f"You need to SWAP them when calling kbisect init.",
                )
            else:
                # Neither is an ancestor of the other - they're on different branches
                return (
                    False,
                    f"Good commit ({self.good_commit[:12]}) is not an ancestor of bad commit ({self.bad_commit[:12]}).\n"
                    f"This usually means they are on different, unrelated branches.\n\n"
                    f"For bisection to work:\n"
                    f"  - Good commit must be in the history of bad commit\n"
                    f"  - Both commits should be on the same branch lineage\n\n"
                    f"Git error: {stderr.strip()}",
                )

        logger.debug("Commit validation passed - good is ancestor of bad")
        return (True, "")

    def _validate_kernel_directories(self) -> Tuple[bool, List[str]]:
        """Check if kernel directories exist on all hosts.

        Returns:
            Tuple of (all_exist, list_of_missing_hosts)
        """
        missing_hosts = []

        for host_manager in self.host_managers:
            hostname = host_manager.config.hostname
            kernel_path = host_manager.config.kernel_path

            # Check if directory exists and is a git repository
            ret, _stdout, _stderr = host_manager.ssh.run_command(
                f"test -d {shlex.quote(kernel_path)}/.git",
                timeout=host_manager.ssh_connect_timeout,
            )

            if ret != 0:
                logger.debug(f"  ✗ {hostname}: kernel directory not found")
                missing_hosts.append(hostname)
            else:
                logger.debug(f"  ✓ {hostname}: kernel directory exists")

        all_exist = len(missing_hosts) == 0
        return (all_exist, missing_hosts)

    # ===========================================================================
    # Per-Host Helper Methods (for multi-host bisection)
    # ===========================================================================

    def _build_on_host(
        self, host_manager: HostManager, commit_sha: str, iteration_id: int
    ) -> Tuple[bool, int, Optional[int], Optional[str]]:
        """Build kernel on a specific host.

        Args:
            host_manager: HostManager for the target host
            commit_sha: Commit SHA to build
            iteration_id: Iteration ID for log storage

        Returns:
            Tuple of (success, exit_code, log_id, kernel_version)
        """
        hostname = host_manager.config.hostname
        logger.info(
            f"  [{hostname}] Building kernel for commit {commit_sha[:SHORT_COMMIT_LENGTH]}..."
        )

        # Get kernel config path (already resolved to remote path during __init__)
        # Both per-host and global configs are transferred and set in host_manager.config.kernel_config_file
        kernel_config = host_manager.config.kernel_config_file or ""

        # Create initial log entry with header
        log_header = f"=== Build Kernel on {hostname}: {commit_sha[:SHORT_COMMIT_LENGTH]} ===\n"
        log_header += f"Kernel source: {host_manager.config.kernel_path}\n"
        log_header += f"Config: {kernel_config or 'default'}\n\n"
        log_header += "=== BUILD OUTPUT ===\n"

        log_id = self.state.create_build_log(
            iteration_id, "build", log_header, host_id=host_manager.host_id
        )

        # Streaming state
        buffer = []
        buffer_size = 0
        buffer_limit = 10 * 1024
        start_time = time.time()

        def stream_callback(stdout_chunk: str, stderr_chunk: str) -> None:
            """Handle streaming output chunks."""
            nonlocal buffer, buffer_size
            chunk = stdout_chunk + stderr_chunk
            if not chunk:
                return
            buffer.append(chunk)
            buffer_size += len(chunk.encode("utf-8"))
            if buffer_size >= buffer_limit:
                combined_chunk = "".join(buffer)
                try:
                    self.state.append_build_log_chunk(log_id, combined_chunk)
                except Exception as exc:
                    logger.warning(f"[{hostname}] Failed to append log chunk: {exc}")
                buffer.clear()
                buffer_size = 0

        # Call build_kernel function with streaming
        ret, stdout, _stderr = host_manager.ssh.call_function_streaming(
            "build_kernel",
            commit_sha,
            host_manager.config.kernel_path,
            kernel_config,
            timeout=host_manager.build_timeout,
            chunk_callback=stream_callback,
        )

        # Flush remaining buffer
        if buffer:
            combined_chunk = "".join(buffer)
            try:
                self.state.append_build_log_chunk(log_id, combined_chunk)
            except Exception as exc:
                logger.warning(f"[{hostname}] Failed to append final log chunk: {exc}")

        # Extract kernel version from build output
        built_kernel_ver = None
        if ret == 0 and stdout.strip():
            built_kernel_ver = stdout.strip().split("\n")[-1]

        # Add exit code to log
        footer = f"\n\n=== EXIT CODE: {ret} ===\n"
        try:
            self.state.append_build_log_chunk(log_id, footer)
            self.state.finalize_build_log(log_id, ret)
        except Exception as exc:
            logger.warning(f"[{hostname}] Failed to finalize log: {exc}")

        elapsed = int(time.time() - start_time)

        if ret != 0:
            logger.error(f"  [{hostname}] Build FAILED in {elapsed // 60}m {elapsed % 60}s")
            return False, ret, log_id, None

        logger.info(f"  [{hostname}] Build OK in {elapsed // 60}m {elapsed % 60}s")
        return True, ret, log_id, built_kernel_ver

    def _validate_commit_on_all_hosts(self, commit_sha: str) -> Tuple[bool, List[str]]:
        """Validate that a commit exists on all hosts.

        Args:
            commit_sha: Commit SHA to validate

        Returns:
            Tuple of (all_valid, list_of_hosts_missing_commit)
        """
        missing_hosts = []

        for host_manager in self.host_managers:
            hostname = host_manager.config.hostname
            kernel_path = host_manager.config.kernel_path

            # Check if commit exists
            ret, _stdout, stderr = host_manager.ssh.run_command(
                f"cd {shlex.quote(kernel_path)} && git cat-file -t {commit_sha}",
                timeout=host_manager.ssh_connect_timeout,
            )

            if ret != 0:
                logger.warning(f"  [{hostname}] Commit {commit_sha[:7]} not found: {stderr}")
                missing_hosts.append(hostname)
            else:
                logger.debug(f"  [{hostname}] Commit {commit_sha[:7]} exists")

        return (len(missing_hosts) == 0, missing_hosts)

    def _verify_kernel_boot(
        self, host_manager: HostManager, expected_kernel_ver: str, actual_kernel_ver: str
    ) -> Tuple[bool, Optional[str]]:
        """Verify that the expected kernel actually booted.

        Args:
            host_manager: HostManager instance
            expected_kernel_ver: Expected kernel version from build
            actual_kernel_ver: Actual kernel version from uname -r

        Returns:
            Tuple of (verification_passed, error_message)
        """
        if not expected_kernel_ver or not actual_kernel_ver:
            return True, None  # Can't verify without both values

        if expected_kernel_ver == actual_kernel_ver:
            logger.debug(f"[{host_manager.config.hostname}] ✓ Boot verification passed")
            return True, None

        # Kernel mismatch detected
        error_msg = (
            f"Boot verification failed - wrong kernel booted\n"
            f"  Expected: {expected_kernel_ver}\n"
            f"  Actual:   {actual_kernel_ver}\n"
            f"  Likely cause: Test kernel panicked or failed to boot, "
            f"system fell back to protected kernel"
        )

        logger.error(f"[{host_manager.config.hostname}] ✗ {error_msg}")
        return False, error_msg

    def _reboot_host(
        self, host_manager: HostManager, _iteration_id: int, expected_kernel_ver: Optional[str]
    ) -> Tuple[bool, Optional[str], Optional[str]]:
        """Reboot a specific host and verify kernel.

        Args:
            host_manager: HostManager for the target host
            iteration_id: Iteration ID for console log storage
            expected_kernel_ver: Expected kernel version to verify

        Returns:
            Tuple of (success, actual_kernel_version, error_message)
        """
        hostname = host_manager.config.hostname
        logger.info(f"  [{hostname}] Rebooting...")

        # Use power controller if available, otherwise fall back to SSH reboot
        if host_manager.power_controller:
            logger.info(
                f"  [{hostname}] Using {host_manager.config.power_control_type} power control for reboot"
            )
            if not host_manager.power_controller.reset():
                logger.error(f"  [{hostname}] Power controller reset failed")
                return False, None, "Power controller reset failed"
        else:
            logger.info(f"  [{hostname}] Using SSH reboot command")
            # Send reboot command via SSH
            host_manager.ssh.run_command("reboot", timeout=5)

        # Wait for reboot to start
        time.sleep(DEFAULT_REBOOT_SETTLE_TIME)

        # Wait for slave to come back online
        logger.debug(
            f"[{hostname}] Waiting for host to come back online (timeout: {host_manager.boot_timeout}s)..."
        )
        boot_start = time.time()

        while not host_manager.ssh.is_alive():
            if time.time() - boot_start > host_manager.boot_timeout:
                logger.error(f"  [{hostname}] Boot timeout after {host_manager.boot_timeout}s")
                return False, None, f"Boot timeout after {host_manager.boot_timeout}s"
            time.sleep(5)

        # Settle time after boot
        time.sleep(DEFAULT_POST_BOOT_SETTLE_TIME)

        # Verify which kernel booted
        ret, actual_kernel_ver, _ = host_manager.ssh.run_command(
            "uname -r", timeout=host_manager.ssh_connect_timeout
        )
        if ret == 0:
            actual_kernel_ver = actual_kernel_ver.strip()
            logger.info(f"  [{hostname}] Booted kernel: {actual_kernel_ver}")

            # Verify expected kernel booted
            if expected_kernel_ver:
                verified, error_msg = self._verify_kernel_boot(
                    host_manager, expected_kernel_ver, actual_kernel_ver
                )

                if not verified:
                    # Boot verification failed - wrong kernel
                    return False, actual_kernel_ver, error_msg

            return True, actual_kernel_ver, None
        else:
            logger.warning(f"  [{hostname}] Could not determine booted kernel version")
            return True, None, None

    def _test_on_host(self, host_manager: HostManager, iteration_id: int) -> Tuple[TestResult, str]:
        """Run test on a specific host using its configured test script.

        Args:
            host_manager: HostManager for the target host
            iteration_id: Iteration ID for log storage

        Returns:
            Tuple of (test_result, test_output)
        """
        hostname = host_manager.config.hostname
        test_script = host_manager.config.test_script
        logger.info(f"  [{hostname}] Running test: {test_script}...")

        # Create initial log entry with header
        log_header = f"=== Test Execution on {hostname} ===\n"
        log_header += f"Test type: {self.config.test_type}\n"
        log_header += f"Test script: {test_script}\n"
        log_header += f"Timeout: {host_manager.test_timeout}s\n\n"
        log_header += "=== TEST OUTPUT ===\n"

        log_id = self.state.create_build_log(
            iteration_id, "test", log_header, host_id=host_manager.host_id
        )

        # Streaming state
        buffer = []
        buffer_size = 0
        buffer_limit = 10 * 1024
        start_time = time.time()

        def stream_callback(stdout_chunk: str, stderr_chunk: str) -> None:
            """Handle streaming output chunks."""
            nonlocal buffer, buffer_size
            chunk = stdout_chunk + stderr_chunk
            if not chunk:
                return
            buffer.append(chunk)
            buffer_size += len(chunk.encode("utf-8"))
            if buffer_size >= buffer_limit:
                combined_chunk = "".join(buffer)
                try:
                    self.state.append_build_log_chunk(log_id, combined_chunk)
                except Exception as exc:
                    logger.warning(f"[{hostname}] Failed to append log chunk: {exc}")
                buffer.clear()
                buffer_size = 0

        # Call run_test function with streaming
        ret, stdout, stderr = host_manager.ssh.call_function_streaming(
            "run_test",
            self.config.test_type,
            test_script,
            timeout=host_manager.test_timeout,
            chunk_callback=stream_callback,
        )

        # Flush remaining buffer
        if buffer:
            combined_chunk = "".join(buffer)
            try:
                self.state.append_build_log_chunk(log_id, combined_chunk)
            except Exception as exc:
                logger.warning(f"[{hostname}] Failed to append final log chunk: {exc}")

        # Add exit code to log
        footer = f"\n\n=== EXIT CODE: {ret} ===\n"
        try:
            self.state.append_build_log_chunk(log_id, footer)
            self.state.finalize_build_log(log_id, ret)
        except Exception as exc:
            logger.warning(f"[{hostname}] Failed to finalize log: {exc}")

        elapsed = int(time.time() - start_time)
        test_output = stdout + stderr

        if ret == 0:
            logger.info(f"  [{hostname}] Test PASSED in {elapsed}s")
            return (TestResult.GOOD, test_output)

        logger.error(f"  [{hostname}] Test FAILED in {elapsed}s")
        return (TestResult.BAD, test_output)

    # ===========================================================================
    # End of Per-Host Helper Methods
    # ===========================================================================

    # ===========================================================================
    # Phase Extraction Methods
    # ===========================================================================

    def _validate_commit_phase(
        self, commit_sha: str, iteration: "BisectIteration"
    ) -> Tuple[bool, bool]:
        """Phase 0: Validate commit exists on all hosts.

        Args:
            commit_sha: Commit SHA to validate
            iteration: Iteration object to update on failure

        Returns:
            Tuple of (phase_succeeded, bisection_complete)
        """
        logger.debug(f"Validating commit {commit_sha[:7]} exists on all hosts...")
        all_valid, missing_hosts = self._validate_commit_on_all_hosts(commit_sha)

        if not all_valid:
            logger.error(f"Commit {commit_sha[:7]} not found on hosts: {', '.join(missing_hosts)}")
            iteration.result = TestResult.SKIP
            iteration.error = f"Commit not found on hosts: {', '.join(missing_hosts)}"

            # Mark commit as skip in git bisect
            success, bisection_complete = self.mark_commit(commit_sha, TestResult.SKIP)
            if not success:
                logger.error("Failed to mark commit as SKIP in git bisect")

            return (False, bisection_complete)

        logger.debug(f"✓ Commit {commit_sha[:7]} exists on all hosts")
        return (True, False)

    def _build_phase(
        self, commit_sha: str, iteration_id: int, iteration: "BisectIteration"
    ) -> Tuple[bool, dict, bool]:
        """Phase 1: Build kernel on all hosts in parallel.

        Args:
            commit_sha: Commit SHA to build
            iteration_id: Database iteration ID
            iteration: Iteration object to update on failure

        Returns:
            Tuple of (phase_succeeded, build_results dict, bisection_complete)
        """
        iteration.state = BisectState.BUILDING
        logger.info(f"Building kernel on {len(self.host_managers)} hosts...")

        build_results = {}
        # Add 10% buffer to configured timeout for parallel execution overhead
        overall_timeout = self.config.build_timeout * 1.1
        with ThreadPoolExecutor() as executor:
            futures = {
                executor.submit(
                    self._build_on_host, host_manager, commit_sha, iteration_id
                ): host_manager
                for host_manager in self.host_managers
            }

            try:
                for future in as_completed(futures, timeout=overall_timeout):
                    host_manager = futures[future]
                    try:
                        success, exit_code, log_id, kernel_ver = future.result()
                        build_results[host_manager.host_id] = {
                            "success": success,
                            "kernel_ver": kernel_ver,
                            "exit_code": exit_code,
                            "log_id": log_id,
                        }
                    except Exception as e:
                        logger.error(f"  [{host_manager.config.hostname}] Build exception: {e}")
                        build_results[host_manager.host_id] = {"success": False, "error": str(e)}
            except TimeoutError:
                logger.error(f"Build phase timed out after {overall_timeout}s")
                # Mark any hosts without results as timed out
                for host_manager in self.host_managers:
                    if host_manager.host_id not in build_results:
                        logger.error(f"  [{host_manager.config.hostname}] Build timed out")
                        build_results[host_manager.host_id] = {
                            "success": False,
                            "error": f"Build timed out after {overall_timeout}s",
                        }

        # Check if any build failed
        if not all(r.get("success", False) for r in build_results.values()):
            logger.error("One or more hosts failed to build - marking iteration SKIP")
            iteration.result = TestResult.SKIP
            iteration.error = "Build failed on one or more hosts"

            # Prepare bulk results for all hosts
            bulk_results = []
            for host_manager in self.host_managers:
                result_data = build_results.get(host_manager.host_id, {})
                # Ensure error message is populated even if build failed without exception
                error_msg = result_data.get("error")
                if not error_msg and not result_data.get("success"):
                    error_msg = (
                        f"Build failed with exit code {result_data.get('exit_code', 'unknown')}"
                    )

                bulk_results.append(
                    {
                        "iteration_id": iteration_id,
                        "host_id": host_manager.host_id,
                        "build_result": "failure" if not result_data.get("success") else "success",
                        "final_result": "skip",
                        "error_message": error_msg,
                    }
                )

            # Store all results in a single transaction
            self.state.create_iteration_results_bulk(bulk_results)

            success, bisection_complete = self.mark_commit(commit_sha, TestResult.SKIP)
            if not success:
                logger.error("Failed to mark commit as SKIP in git bisect")

            return (False, build_results, bisection_complete)

        logger.info("✓ All hosts built successfully")
        return (True, build_results, False)

    def _reboot_phase(
        self, iteration_id: int, build_results: dict, commit_sha: str, iteration: "BisectIteration"
    ) -> Tuple[bool, dict, bool]:
        """Phase 2: Reboot all hosts in parallel.

        Args:
            iteration_id: Database iteration ID
            build_results: Results from build phase (contains kernel versions)
            commit_sha: Commit SHA being tested
            iteration: Iteration object to update on failure

        Returns:
            Tuple of (phase_succeeded, reboot_results dict, bisection_complete)
        """
        iteration.state = BisectState.REBOOTING
        logger.info(f"Rebooting {len(self.host_managers)} hosts...")

        reboot_results = {}
        # Add 10% buffer to configured timeout for parallel execution overhead
        overall_timeout = self.config.boot_timeout * 1.1
        with ThreadPoolExecutor() as executor:
            futures = {
                executor.submit(
                    self._reboot_host,
                    host_manager,
                    iteration_id,
                    build_results[host_manager.host_id].get(
                        "kernel_ver"
                    ),  # Use .get() to handle missing key
                ): host_manager
                for host_manager in self.host_managers
            }

            try:
                for future in as_completed(futures, timeout=overall_timeout):
                    host_manager = futures[future]
                    try:
                        success, actual_kernel_ver, error_msg = future.result()
                        reboot_results[host_manager.host_id] = {
                            "success": success,
                            "actual_kernel_ver": actual_kernel_ver,
                            "error": error_msg,
                        }
                    except Exception as e:
                        logger.error(f"  [{host_manager.config.hostname}] Reboot exception: {e}")
                        reboot_results[host_manager.host_id] = {"success": False, "error": str(e)}
            except TimeoutError:
                logger.error(f"Reboot phase timed out after {overall_timeout}s")
                # Mark any hosts without results as timed out
                for host_manager in self.host_managers:
                    if host_manager.host_id not in reboot_results:
                        logger.error(f"  [{host_manager.config.hostname}] Reboot timed out")
                        reboot_results[host_manager.host_id] = {
                            "success": False,
                            "error": f"Reboot timed out after {overall_timeout}s",
                        }

        # Check if any reboot failed
        if not all(r.get("success", False) for r in reboot_results.values()):
            logger.error(
                "One or more hosts failed to reboot or boot verification failed - marking iteration SKIP"
            )

            # Aggregate error messages for better diagnostics
            errors = []
            for host_manager in self.host_managers:
                result_data = reboot_results.get(host_manager.host_id, {})
                if not result_data.get("success") and result_data.get("error"):
                    errors.append(f"{host_manager.config.hostname}: {result_data['error']}")

            iteration.result = TestResult.SKIP
            iteration.error = "; ".join(errors) if errors else "Boot failed on one or more hosts"

            # Prepare bulk results for all hosts
            bulk_results = []
            for host_manager in self.host_managers:
                result_data = reboot_results.get(host_manager.host_id, {})
                bulk_results.append(
                    {
                        "iteration_id": iteration_id,
                        "host_id": host_manager.host_id,
                        "build_result": "success",
                        "boot_result": "failure" if not result_data.get("success") else "success",
                        "final_result": "skip",
                        "error_message": result_data.get("error"),
                    }
                )

            # Store all results in a single transaction
            self.state.create_iteration_results_bulk(bulk_results)

            success, bisection_complete = self.mark_commit(commit_sha, TestResult.SKIP)
            if not success:
                logger.error("Failed to mark commit as SKIP in git bisect")

            return (False, reboot_results, bisection_complete)

        logger.info("✓ All hosts rebooted successfully")
        return (True, reboot_results, False)

    def _test_and_aggregate_phase(
        self, iteration_id: int, commit_sha: str, iteration: "BisectIteration"
    ) -> bool:
        """Phase 3 & 4: Run tests on all hosts and aggregate results.

        Args:
            iteration_id: Database iteration ID
            commit_sha: Commit SHA being tested
            iteration: Iteration object to update with results

        Returns:
            bisection_complete flag
        """
        iteration.state = BisectState.TESTING
        logger.info(f"Running tests on {len(self.host_managers)} hosts...")

        test_results = {}
        # Add 10% buffer to configured timeout for parallel execution overhead
        overall_timeout = self.config.test_timeout * 1.1
        with ThreadPoolExecutor() as executor:
            futures = {
                executor.submit(self._test_on_host, host_manager, iteration_id): host_manager
                for host_manager in self.host_managers
            }

            try:
                for future in as_completed(futures, timeout=overall_timeout):
                    host_manager = futures[future]
                    try:
                        test_result, test_output = future.result()
                        test_results[host_manager.host_id] = {
                            "result": test_result,
                            "output": test_output,
                        }
                    except Exception as e:
                        logger.error(f"  [{host_manager.config.hostname}] Test exception: {e}")
                        test_results[host_manager.host_id] = {
                            "result": TestResult.SKIP,
                            "error": str(e),
                        }
            except TimeoutError:
                logger.error(f"Test phase timed out after {overall_timeout}s")
                # Mark any hosts without results as timed out
                for host_manager in self.host_managers:
                    if host_manager.host_id not in test_results:
                        logger.error(f"  [{host_manager.config.hostname}] Test timed out")
                        test_results[host_manager.host_id] = {
                            "result": TestResult.SKIP,
                            "error": f"Test timed out after {overall_timeout}s",
                        }

        # Prepare bulk results for all hosts
        bulk_results = []
        for host_manager in self.host_managers:
            result_data = test_results.get(host_manager.host_id, {})
            test_result = result_data.get("result", TestResult.SKIP)
            bulk_results.append(
                {
                    "iteration_id": iteration_id,
                    "host_id": host_manager.host_id,
                    "build_result": "success",
                    "boot_result": "success",
                    "test_result": test_result.value,
                    "final_result": test_result.value,
                    "test_output": result_data.get("output", ""),
                    "error_message": result_data.get("error"),
                }
            )

        # Store all results in a single transaction
        self.state.create_iteration_results_bulk(bulk_results)

        # Aggregate: ALL pass = GOOD, ANY fail = BAD, ANY skip = SKIP
        all_results = [r["result"] for r in test_results.values()]

        if all(r == TestResult.GOOD for r in all_results):
            final_result = TestResult.GOOD
            logger.info("✓ All hosts passed - marking commit GOOD")
        elif any(r == TestResult.BAD for r in all_results):
            final_result = TestResult.BAD
            # Show which hosts failed
            failed_hosts = [
                host_manager.config.hostname
                for host_manager in self.host_managers
                if test_results[host_manager.host_id]["result"] == TestResult.BAD
            ]
            logger.error(f"✗ Failed on: {', '.join(failed_hosts)} - marking commit BAD")
        else:
            final_result = TestResult.SKIP
            logger.warning("One or more hosts skipped - marking commit SKIP")

        iteration.result = final_result

        # Update iteration in database
        self.state.update_iteration(
            iteration_id,
            final_result=final_result.value,
            end_time=datetime.now(timezone.utc).isoformat(),
        )

        # Mark in git bisect
        success, bisection_complete = self.mark_commit(commit_sha, final_result)
        if not success:
            logger.error(
                "Failed to mark commit in git bisect - bisection state may be inconsistent"
            )
            logger.error("")
            logger.error("STOPPING BISECTION - Please fix the issue and restart")
            logger.error("")
            # Raise exception to stop bisection
            raise RuntimeError(
                "Git bisect failed to mark commit - likely due to incorrect bisect range. "
                "See error messages above for details."
            )

        return bisection_complete

    # ===========================================================================
    # End of Phase Extraction Methods
    # ===========================================================================

    def _run_multihost_iteration(
        self, iteration: BisectIteration, iteration_id: int, commit_sha: str
    ) -> Tuple[BisectIteration, bool]:
        """Run iteration in multi-host mode with parallel execution.

        Args:
            iteration: BisectIteration object
            iteration_id: Database iteration ID
            commit_sha: Commit SHA being tested

        Returns:
            Tuple of (updated iteration, bisection_complete flag)
        """
        bisection_complete = False

        try:
            # Phase 0: Validate commit
            phase_ok, bisection_complete = self._validate_commit_phase(commit_sha, iteration)
            if not phase_ok:
                return (iteration, bisection_complete)

            # Phase 1: Build
            phase_ok, build_results, bisection_complete = self._build_phase(
                commit_sha, iteration_id, iteration
            )
            if not phase_ok:
                return (iteration, bisection_complete)

            # Phase 2: Reboot
            phase_ok, _reboot_results, bisection_complete = self._reboot_phase(
                iteration_id, build_results, commit_sha, iteration
            )
            if not phase_ok:
                return (iteration, bisection_complete)

            # Phase 3 & 4: Test and aggregate
            bisection_complete = self._test_and_aggregate_phase(iteration_id, commit_sha, iteration)

        except Exception as exc:
            logger.error(f"Multi-host iteration failed with exception: {exc}")
            iteration.result = TestResult.SKIP
            iteration.error = str(exc)

        finally:
            iteration.end_time = datetime.now(timezone.utc).isoformat()
            if iteration.start_time and iteration.end_time:
                start = datetime.fromisoformat(iteration.start_time)
                end = datetime.fromisoformat(iteration.end_time)
                iteration.duration = int((end - start).total_seconds())

                # Persist duration to database
                self.state.update_iteration(
                    iteration_id, end_time=iteration.end_time, duration=iteration.duration
                )

            self.iterations.append(iteration)
            self.save_state()

        return (iteration, bisection_complete)

    def run_iteration(self, commit_sha: str) -> Tuple[BisectIteration, bool]:
        """Run single bisection iteration with multi-host execution.

        Args:
            commit_sha: Commit SHA to test

        Returns:
            Tuple of (BisectIteration object, bisection_complete flag)
        """
        self.iteration_count += 1

        # Get commit info (use first host's SSH connection)
        first_host = self.host_managers[0]
        kernel_path = first_host.config.kernel_path
        ret, commit_msg, _ = first_host.ssh.run_command(
            f"cd {kernel_path} && git log -1 --oneline {commit_sha}",
            timeout=first_host.ssh_connect_timeout,
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
            start_time=datetime.now(timezone.utc).isoformat(),
        )

        self.current_iteration = iteration
        logger.info(f"\n=== Iteration {iteration.iteration}: {iteration.commit_short} ===")
        logger.info(f"Commit: {iteration.commit_message}")
        logger.info(f"Testing on {len(self.host_managers)} hosts")

        # Execute multi-host iteration
        return self._run_multihost_iteration(iteration, iteration_id, commit_sha)

    def _extract_first_bad_commit(self) -> Optional[str]:
        """Extract first bad commit SHA from git bisect.

        Uses first host since all hosts share the same git bisect state.

        Returns:
            Commit SHA if found, None otherwise
        """
        first_host = self.host_managers[0]
        kernel_path = first_host.config.kernel_path

        ret, stdout, _ = first_host.ssh.run_command(
            f"cd {shlex.quote(kernel_path)} && "
            "git bisect log | grep 'first bad commit' -A 1 | grep '^commit' | head -1 | awk '{print $2}'",
            timeout=first_host.ssh_connect_timeout,
        )

        if ret == 0 and stdout.strip():
            commit_sha = stdout.strip()
            logger.debug(f"Extracted first bad commit: {commit_sha}")
            return commit_sha

        logger.warning("Could not extract first bad commit from git bisect log")
        return None

    def run(self) -> bool:
        """Run complete bisection.

        Returns:
            True if bisection completed successfully, False otherwise
        """
        logger.info("\n=== Starting Bisection ===\n")

        # Safety limit to prevent infinite loops
        MAX_ITERATIONS = 1000
        iteration_count = 0

        # Track stuck detection (same commit being tested repeatedly)
        previous_commit = None
        same_commit_count = 0
        MAX_SAME_COMMIT = 3  # Abort if stuck on same commit for this many iterations

        while True:
            iteration_count += 1

            # Safety check: prevent infinite loops
            if iteration_count > MAX_ITERATIONS:
                logger.error(
                    f"SAFETY LIMIT REACHED: Exceeded {MAX_ITERATIONS} iterations. Bisection may be stuck in an infinite loop. Stopping."
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

            # Check if we're stuck on the same commit
            if commit == previous_commit:
                same_commit_count += 1
                logger.warning(
                    f"Still on same commit {commit[:8]} (attempt {same_commit_count}/{MAX_SAME_COMMIT})"
                )

                if same_commit_count >= MAX_SAME_COMMIT:
                    logger.error(
                        f"STUCK ON SAME COMMIT: Git bisect has returned the same commit {commit[:8]} "
                        f"for {same_commit_count} consecutive iterations. This indicates git bisect "
                        f"has run out of viable commits to test, or there's a problem with the repository state. "
                        f"Stopping bisection."
                    )
                    self.state.update_session(
                        self.session_id,
                        status="failed",
                        end_time=datetime.utcnow().isoformat(),
                    )
                    return False
            else:
                # Reset counter when we move to a different commit
                same_commit_count = 0
                previous_commit = commit

            # Run iteration
            try:
                iteration, bisection_complete = self.run_iteration(commit)
            except RuntimeError:
                # Git bisect error (e.g., merge base is bad)
                logger.error("")
                logger.error("Bisection cannot continue due to git bisect error")
                self.state.update_session(
                    self.session_id,
                    status="failed",
                    end_time=datetime.utcnow().isoformat(),
                )
                raise

            logger.info(f"Result: {iteration.result.value}")

            # Check if bisection completed
            if bisection_complete:
                logger.info("\n=== Bisection Found First Bad Commit! ===")

                # Extract first bad commit SHA
                first_bad = self._extract_first_bad_commit()
                if first_bad:
                    logger.info(f"First bad commit: {first_bad}")

                # Update session as completed
                self.state.update_session(
                    self.session_id,
                    result_commit=first_bad,
                    status="completed",
                    end_time=datetime.utcnow().isoformat(),
                )

                self.generate_report()
                return True

        # Bisection completed (no more commits to test)
        # Update session status if not already done
        self.state.update_session(
            self.session_id, status="completed", end_time=datetime.utcnow().isoformat()
        )

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
            "current_iteration": (
                self._iteration_to_dict(self.current_iteration) if self.current_iteration else None
            ),
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
        logger.info(f"Hosts tested: {len(self.host_managers)}")
        for hm in self.host_managers:
            logger.info(f"  - {hm.config.hostname}")
        logger.info(f"Total iterations: {len(self.iterations)}")

        logger.info("\nIteration Summary:")
        logger.info("-" * 60)

        for iteration in self.iterations:
            status = iteration.result.value if iteration.result else "unknown"
            duration = f"{iteration.duration}s" if iteration.duration else "N/A"
            logger.info(
                f"{iteration.iteration:3d}. {iteration.commit_short} | {status:7s} | {duration:6s} | {iteration.commit_message[:50]}"
            )

        # Get final result from git bisect (use first host)
        first_host = self.host_managers[0]
        kernel_path = first_host.config.kernel_path

        ret, stdout, _ = first_host.ssh.run_command(
            f"cd {shlex.quote(kernel_path)} && git bisect log | grep 'first bad commit' -A 5",
            timeout=first_host.ssh_connect_timeout,
        )

        if ret == 0 and stdout:
            logger.info("\n" + "=" * 60)
            logger.info("FIRST BAD COMMIT:")
            logger.info("=" * 60)
            logger.info(stdout)

        logger.info("=" * 60 + "\n")

    def build_only(self, commit_sha: str, save_logs: bool = False) -> bool:
        """Build kernel on all hosts without rebooting or testing.

        This is a standalone build operation that doesn't require an active
        bisection session. It builds the specified commit on all configured
        hosts in parallel.

        Args:
            commit_sha: Full or short commit SHA to build
            save_logs: If True, save build logs to database

        Returns:
            True if build succeeded on all hosts, False otherwise
        """
        logger.info("=== Standalone Kernel Build ===")
        logger.info(f"Commit: {commit_sha}")
        logger.info(f"Hosts: {len(self.host_managers)}")

        # Step 1: Pre-flight check - verify kernel_path exists on all hosts
        print("Checking if hosts are initialized...")
        all_initialized = True
        uninitialized_hosts = []

        for host_manager in self.host_managers:
            hostname = host_manager.config.hostname
            kernel_path = host_manager.config.kernel_path

            # Check if directory exists and is a git repository
            ret, _stdout, _stderr = host_manager.ssh.run_command(
                f"test -d {shlex.quote(kernel_path)}/.git && echo 'exists'",
                timeout=host_manager.ssh_connect_timeout,
            )

            if ret != 0:
                all_initialized = False
                uninitialized_hosts.append(hostname)
                logger.debug(f"  ✗ {hostname}: kernel repository not found")
            else:
                logger.debug(f"  ✓ {hostname}: kernel repository exists")

        if not all_initialized:
            print(f"⚠ Kernel source not found on {len(uninitialized_hosts)} host(s)")
            print("Setting up kernel source automatically...\n")

            # Try to auto-initialize hosts
            try:
                success = self._auto_initialize_hosts()
                if not success:
                    print("\n✗ Failed to set up kernel source on hosts")
                    print("\nPlease ensure either:")
                    print("  1. A local kernel repository exists (will be copied to hosts)")
                    print("  2. kernel_repo_source is configured in bisect.yaml (will be cloned)")
                    return False

                print("✓ Kernel source set up successfully\n")
            except Exception as exc:
                logger.error(f"Auto-initialization failed: {exc}")
                print("\n✗ Failed to automatically set up kernel source")
                print("Please run 'kbisect init <good> <bad>' manually")
                return False
        else:
            print("✓ All hosts ready\n")

        # Step 2: Expand short commit SHA to full SHA (if needed)
        commit_full = self._resolve_commit_sha(commit_sha)
        if not commit_full:
            # Error details already logged by _resolve_commit_sha()
            # Just add a summary for the user
            print(f"\n✗ Unable to resolve commit: {commit_sha}")
            print("See error messages above for details")
            return False

        logger.info(f"Resolved commit: {commit_full[:SHORT_COMMIT_LENGTH]}")

        # Step 3: Get commit message for display
        commit_msg = self._get_commit_message(commit_full)
        print(f"Commit: {commit_full[:SHORT_COMMIT_LENGTH]} - {commit_msg}\n")

        # Step 4: Validate commit exists on all hosts
        print("Validating commit on all hosts...")
        all_valid = True
        missing_hosts = []

        for host_manager in self.host_managers:
            hostname = host_manager.config.hostname
            kernel_path = host_manager.config.kernel_path

            ret, _stdout, _stderr = host_manager.ssh.run_command(
                f"cd {shlex.quote(kernel_path)} && git cat-file -t {shlex.quote(commit_full)}",
                timeout=host_manager.ssh_connect_timeout,
            )

            if ret != 0:
                all_valid = False
                missing_hosts.append(hostname)
                logger.error(f"  ✗ {hostname}: commit not found")
            else:
                logger.debug(f"  ✓ {hostname}: commit exists")

        if not all_valid:
            print(f"\n✗ Commit {commit_full[:SHORT_COMMIT_LENGTH]} not found on some hosts:")
            for host in missing_hosts:
                print(f"  - {host}")
            print("\nPossible solutions:")
            print("  1. Ensure all hosts have the latest commits: git fetch origin")
            print("  2. Check that kernel_path in bisect.yaml points to the correct repository")
            print(
                f"  3. Verify the commit exists in your repository: git log --oneline | grep {commit_full[:SHORT_COMMIT_LENGTH]}"
            )
            logger.error(f"Commit validation failed on hosts: {', '.join(missing_hosts)}")
            return False

        print("✓ Commit exists on all hosts\n")

        # Step 5: Create temporary database records if saving logs
        iteration_id = 0
        session_id = None
        if save_logs:
            # Create temporary session for build-only (use dummy commits)
            session_id = self.state.create_session(
                good_commit="build-only", bad_commit=commit_full[:SHORT_COMMIT_LENGTH]
            )
            iteration_id = self.state.create_iteration(session_id, 1, commit_full, commit_msg)
            logger.info(f"Saving logs to database (session_id={session_id})")
        else:
            logger.info("Build logs will not be saved (use --save-logs to enable)")

        # Step 6: Build on all hosts in parallel
        print(f"Building kernel on {len(self.host_managers)} hosts...\n")

        build_results = {}
        overall_timeout = self.config.build_timeout * 1.1

        with ThreadPoolExecutor() as executor:
            futures = {
                executor.submit(
                    self._build_on_host, host_manager, commit_full, iteration_id
                ): host_manager
                for host_manager in self.host_managers
            }

            try:
                for future in as_completed(futures, timeout=overall_timeout):
                    host_manager = futures[future]
                    try:
                        success, exit_code, log_id, kernel_ver = future.result()
                        build_results[host_manager.host_id] = {
                            "hostname": host_manager.config.hostname,
                            "success": success,
                            "kernel_ver": kernel_ver,
                            "exit_code": exit_code,
                            "log_id": log_id,
                        }
                    except Exception as exc:
                        logger.error(f"  [{host_manager.config.hostname}] Build exception: {exc}")
                        build_results[host_manager.host_id] = {
                            "hostname": host_manager.config.hostname,
                            "success": False,
                            "error": str(exc),
                        }
            except TimeoutError:
                logger.error(f"Build phase timed out after {overall_timeout}s")
                # Mark any hosts without results as timed out
                for host_manager in self.host_managers:
                    if host_manager.host_id not in build_results:
                        logger.error(f"  [{host_manager.config.hostname}] Build timed out")
                        build_results[host_manager.host_id] = {
                            "hostname": host_manager.config.hostname,
                            "success": False,
                            "error": f"Build timed out after {overall_timeout}s",
                        }

        # Step 7: Report results
        print("\n=== Build Summary ===")
        all_success = True

        for result in build_results.values():
            hostname = result["hostname"]
            if result.get("success"):
                kernel_ver = result.get("kernel_ver", "unknown")
                print(f"  ✓ {hostname}: {kernel_ver}")
                logger.info(f"  ✓ {hostname}: {kernel_ver}")
            else:
                all_success = False
                error = result.get("error", "Build failed")
                print(f"  ✗ {hostname}: {error}")
                logger.error(f"  ✗ {hostname}: {error}")

        if save_logs and session_id:
            print("\nBuild logs saved. View with:")
            print(f"  kbisect logs list --session-id {session_id}")

        return all_success

    def _auto_initialize_hosts(self) -> bool:
        """Automatically initialize hosts with kernel source and build dependencies.

        Tries to find kernel source locally first, then falls back to
        cloning from configured remote repository. After setting up the kernel
        source, installs build dependencies on all hosts.

        Returns:
            True if initialization succeeded, False otherwise
        """
        logger.info("Auto-initializing hosts with kernel source...")

        # Step 1: Try to prepare kernel repository
        # This will try local first, then fall back to cloning from remote
        repo_path = self._prepare_kernel_repo()
        if not repo_path:
            logger.error("Failed to prepare kernel repository")
            return False

        logger.info(f"Kernel repository prepared at: {repo_path}")

        # Step 2: Transfer repository to all hosts
        print(f"Copying kernel source to {len(self.host_managers)} host(s)...")
        success = self._transfer_repo_to_hosts(repo_path)

        if not success:
            logger.error("Failed to transfer repository to hosts")
            return False

        logger.info("Repository transferred successfully to all hosts")

        # Step 3: Install build dependencies on all hosts
        print("Installing build dependencies on all hosts...")
        all_deps_installed = True

        for host_manager in self.host_managers:
            hostname = host_manager.config.hostname
            logger.info(f"  Installing dependencies on {hostname}...")

            ret, _stdout, stderr = host_manager.ssh.call_function(
                "install_build_deps", timeout=host_manager.ssh_connect_timeout
            )

            if ret != 0:
                logger.warning(f"  Failed to install build dependencies on {hostname}: {stderr}")
                logger.warning("  Kernel builds may fail due to missing dependencies")
                all_deps_installed = False
            else:
                logger.info(f"  ✓ {hostname}: build dependencies installed")

        if not all_deps_installed:
            logger.warning("Build dependencies installation failed on one or more hosts")
            logger.warning("Continuing anyway - builds may fail if dependencies are missing")
        else:
            print("✓ Build dependencies installed on all hosts\n")

        return True

    def _extract_git_error(self, stderr: str) -> str:
        """Extract meaningful error from stderr, filtering SSH warnings.

        Args:
            stderr: Raw stderr output

        Returns:
            Cleaned error message
        """
        lines = stderr.strip().split("\n")

        # Filter out SSH-related lines
        error_lines = []
        for line in lines:
            # Skip SSH warnings and known noise
            if any(
                skip in line
                for skip in [
                    "Permanently added",
                    "Warning:",
                    "ECDSA",
                    "known_hosts",
                ]
            ):
                continue
            error_lines.append(line)

        return "\n".join(error_lines) if error_lines else stderr

    def _resolve_commit_sha(self, commit_sha: str) -> Optional[str]:
        """Resolve short or full commit SHA to full SHA.

        Args:
            commit_sha: Short or full commit SHA

        Returns:
            Full 40-character SHA, or None if resolution failed
        """
        # If already 40 chars and valid hex, return as-is
        if len(commit_sha) == COMMIT_HASH_LENGTH:
            try:
                int(commit_sha, 16)
                return commit_sha
            except ValueError:
                pass

        # Use first host to resolve (all hosts share same repo state)
        first_host = self.host_managers[0]
        kernel_path = first_host.config.kernel_path

        ret, stdout, stderr = first_host.ssh.run_command(
            f"cd {shlex.quote(kernel_path)} && git rev-parse {shlex.quote(commit_sha)}",
            timeout=first_host.ssh_connect_timeout,
        )

        if ret != 0:
            # Parse stderr to extract meaningful error
            error_msg = stderr.strip()

            # Detect common issues and provide helpful hints
            if "No such file or directory" in error_msg and (
                "cd:" in error_msg or kernel_path in error_msg
            ):
                logger.error(f"Kernel directory does not exist: {kernel_path}")
                logger.error(f"Host: {first_host.config.hostname}")
                logger.error(
                    "This shouldn't happen after auto-initialization - please report this issue"
                )
            elif "not a git repository" in error_msg.lower():
                logger.error(f"Not a git repository: {kernel_path}")
                logger.error("Please ensure the kernel_path points to a valid git repository")
            elif "fatal: ambiguous argument" in error_msg.lower():
                logger.error(f"Commit not found in repository: {commit_sha}")
                logger.error("Please ensure the commit exists and the repository is up to date")
            else:
                # Show the actual git error, filtering out SSH noise
                clean_error = self._extract_git_error(error_msg)
                logger.error(f"Failed to resolve commit SHA: {clean_error}")

            return None

        full_sha = stdout.strip()

        # Validate it's 40 chars and hex
        if len(full_sha) != COMMIT_HASH_LENGTH:
            logger.error(f"Invalid commit SHA length: {full_sha}")
            return None

        try:
            int(full_sha, 16)
        except ValueError:
            logger.error(f"Invalid commit SHA format: {full_sha}")
            return None

        return full_sha

    def _get_commit_message(self, commit_sha: str) -> str:
        """Get commit message for display.

        Args:
            commit_sha: Commit SHA

        Returns:
            Commit message (first line)
        """
        first_host = self.host_managers[0]
        kernel_path = first_host.config.kernel_path

        ret, stdout, _stderr = first_host.ssh.run_command(
            f"cd {shlex.quote(kernel_path)} && git log -1 --oneline {shlex.quote(commit_sha)}",
            timeout=first_host.ssh_connect_timeout,
        )

        if ret == 0 and stdout.strip():
            # Remove the commit SHA prefix from oneline output
            parts = stdout.strip().split(maxsplit=1)
            if len(parts) > 1:
                return parts[1]
            return stdout.strip()
        return "Unknown commit"


def main() -> int:
    """Main entry point."""
    print("Kernel Bisect Master Controller")
    print("Usage: Import this module and use the BisectMaster class")
    return 0


if __name__ == "__main__":
    sys.exit(main())
