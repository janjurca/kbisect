"""System health checker for kbisect dependencies and configuration."""

import logging
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, List, Optional

from ..config.config import BisectConfig
from ..deployment.deployer import SlaveDeployer
from ..remote.ssh import SSHClient


logger = logging.getLogger(__name__)


@dataclass
class CheckResult:
    """Result of a single check operation."""

    category: str
    name: str
    passed: bool
    message: str
    details: Optional[str] = None
    warning: bool = False


class SystemChecker:
    """Validates kbisect system dependencies and configuration."""

    def __init__(self, config: BisectConfig):
        """Initialize system checker with configuration.

        Args:
            config: Loaded bisect configuration
        """
        self.config = config
        self.results: List[CheckResult] = []

    def check_local_tools(self) -> List[CheckResult]:
        """Check availability of required local command-line tools.

        Returns:
            List of check results for local tools
        """
        results = []
        required_tools = ["ssh", "rsync", "git", "ping"]

        for tool in required_tools:
            tool_path = shutil.which(tool)
            if tool_path:
                results.append(
                    CheckResult(
                        category="Local System",
                        name=f"{tool} command",
                        passed=True,
                        message=f"Found at {tool_path}",
                    )
                )
            else:
                results.append(
                    CheckResult(
                        category="Local System",
                        name=f"{tool} command",
                        passed=False,
                        message=f"{tool} not found in PATH",
                    )
                )

        return results

    def check_config_validity(self) -> List[CheckResult]:
        """Validate configuration file structure and required fields.

        Returns:
            List of check results for configuration
        """
        results = []

        # Check hosts configuration
        if not self.config.hosts:
            results.append(
                CheckResult(
                    category="Configuration",
                    name="hosts configuration",
                    passed=False,
                    message="No hosts defined in configuration",
                )
            )
        else:
            results.append(
                CheckResult(
                    category="Configuration",
                    name="hosts configuration",
                    passed=True,
                    message=f"{len(self.config.hosts)} host(s) configured",
                )
            )

        # Check kernel config file if specified
        # Note: kernel config file must be on master machine (it will be transferred to slave)
        if self.config.kernel_config_file:
            config_path = Path(self.config.kernel_config_file)
            if config_path.exists():
                results.append(
                    CheckResult(
                        category="Configuration",
                        name="kernel config file",
                        passed=True,
                        message=f"Found on master at {self.config.kernel_config_file}",
                    )
                )
            else:
                results.append(
                    CheckResult(
                        category="Configuration",
                        name="kernel config file",
                        passed=False,
                        message=f"File not found on master: {self.config.kernel_config_file}",
                    )
                )

        # Check kernel repository if specified
        if self.config.kernel_repo_source:
            source = self.config.kernel_repo_source
            if not source.startswith(("http://", "https://", "git@", "ssh://")):
                # Local path
                repo_path = Path(source)
                if repo_path.exists():
                    results.append(
                        CheckResult(
                            category="Configuration",
                            name="kernel repository",
                            passed=True,
                            message=f"Local repository found at {source}",
                        )
                    )
                else:
                    results.append(
                        CheckResult(
                            category="Configuration",
                            name="kernel repository",
                            passed=True,
                            message=f"Local path not found: {source}",
                            warning=True,
                        )
                    )

        return results

    def check_ssh_connectivity(self, host_config: Any) -> List[CheckResult]:
        """Test SSH connection to a configured host.

        Args:
            host_config: Host configuration object

        Returns:
            List of check results for SSH connectivity
        """
        results = []
        hostname = host_config.hostname

        try:
            ssh = SSHClient(
                host=hostname,
                user=host_config.ssh_user,
                connect_timeout=self.config.ssh_connect_timeout,
            )

            # Try a simple command
            returncode, stdout, _ = ssh.run_command(
                'echo "kbisect-check"', timeout=self.config.ssh_connect_timeout
            )

            if returncode == 0 and stdout.strip() == "kbisect-check":
                results.append(
                    CheckResult(
                        category=f"Host: {hostname}",
                        name="SSH connection",
                        passed=True,
                        message="Connection successful",
                    )
                )
            else:
                results.append(
                    CheckResult(
                        category=f"Host: {hostname}",
                        name="SSH connection",
                        passed=False,
                        message=f"Command execution failed (rc={returncode})",
                    )
                )
        except Exception as e:
            results.append(
                CheckResult(
                    category=f"Host: {hostname}",
                    name="SSH connection",
                    passed=False,
                    message=f"Connection failed: {e!s}",
                )
            )

        return results

    def check_slave_deployment(self, host_config: Any) -> List[CheckResult]:
        """Check slave-side deployment status and tools.

        Args:
            host_config: Host configuration object

        Returns:
            List of check results for slave deployment
        """
        results = []
        hostname = host_config.hostname

        try:
            ssh = SSHClient(
                host=hostname,
                user=host_config.ssh_user,
                connect_timeout=self.config.ssh_connect_timeout,
            )

            deployer = SlaveDeployer(ssh, self.config)

            # Check deployment status
            if deployer.is_deployed():
                try:
                    deployer.verify_deployment()
                    results.append(
                        CheckResult(
                            category=f"Host: {hostname}",
                            name="slave deployment",
                            passed=True,
                            message="Deployment verified successfully",
                        )
                    )
                except Exception as e:
                    results.append(
                        CheckResult(
                            category=f"Host: {hostname}",
                            name="slave deployment",
                            passed=False,
                            message=f"Deployment verification failed: {e!s}",
                        )
                    )
            else:
                results.append(
                    CheckResult(
                        category=f"Host: {hostname}",
                        name="slave deployment",
                        passed=True,
                        message="Not deployed yet",
                        warning=True,
                    )
                )

            # Check remote tools
            remote_tools = ["make", "grubby"]
            for tool in remote_tools:
                returncode, stdout, _ = ssh.run_command(
                    f"which {tool}", timeout=self.config.ssh_connect_timeout
                )
                if returncode == 0:
                    results.append(
                        CheckResult(
                            category=f"Host: {hostname}",
                            name=f"remote {tool}",
                            passed=True,
                            message=f"Found at {stdout.strip()}",
                        )
                    )
                else:
                    results.append(
                        CheckResult(
                            category=f"Host: {hostname}",
                            name=f"remote {tool}",
                            passed=True,
                            message=f"{tool} not found",
                            warning=True,
                        )
                    )

            # Check kernel path
            kernel_path = host_config.kernel_path
            returncode, stdout, _ = ssh.run_command(
                f'test -d {kernel_path} && echo "exists" || echo "missing"',
                timeout=self.config.ssh_connect_timeout,
            )
            if returncode == 0 and stdout.strip() == "exists":
                results.append(
                    CheckResult(
                        category=f"Host: {hostname}",
                        name="kernel path",
                        passed=True,
                        message=f"Directory exists: {kernel_path}",
                    )
                )
            else:
                results.append(
                    CheckResult(
                        category=f"Host: {hostname}",
                        name="kernel path",
                        passed=True,
                        message=f"Directory not found: {kernel_path}",
                        warning=True,
                    )
                )

        except Exception as e:
            results.append(
                CheckResult(
                    category=f"Host: {hostname}",
                    name="slave checks",
                    passed=False,
                    message=f"Unable to perform slave checks: {e!s}",
                )
            )

        return results

    def check_power_controller(self, host_config: Any) -> List[CheckResult]:
        """Check power controller configuration and connectivity.

        Args:
            host_config: Host configuration object

        Returns:
            List of check results for power controller
        """
        results = []
        hostname = host_config.hostname
        power_type = host_config.power_control_type or "null"

        if power_type == "null":
            results.append(
                CheckResult(
                    category=f"Host: {hostname}",
                    name="power control",
                    passed=True,
                    message="No power control configured",
                    warning=True,
                )
            )
            return results

        try:
            # Create power controller instance using factory
            from ..power.factory import create_power_controller

            controller = create_power_controller(host_config)

            # Run health check
            health_result = controller.health_check()

            if health_result["healthy"]:
                details = []
                if "tool_path" in health_result:
                    details.append(f"Tool: {health_result['tool_path']}")
                if "power_status" in health_result:
                    details.append(f"Power status: {health_result['power_status']}")

                results.append(
                    CheckResult(
                        category=f"Host: {hostname}",
                        name=f"{power_type.upper()} power control",
                        passed=True,
                        message="Operational",
                        details=", ".join(details) if details else None,
                    )
                )
            else:
                error_msg = health_result.get("error", "Unknown error")
                results.append(
                    CheckResult(
                        category=f"Host: {hostname}",
                        name=f"{power_type.upper()} power control",
                        passed=False,
                        message=f"Health check failed: {error_msg}",
                    )
                )

        except Exception as e:
            results.append(
                CheckResult(
                    category=f"Host: {hostname}",
                    name=f"{power_type} power control",
                    passed=False,
                    message=f"Failed to initialize: {e!s}",
                )
            )

        return results

    def check_console_collector(self, host_config: Any) -> List[CheckResult]:
        """Check console log collector if configured.

        Args:
            host_config: Host configuration object

        Returns:
            List of check results for console collector
        """
        results = []
        hostname = host_config.hostname

        # Check if console logs are enabled
        if not self.config.collect_console_logs:
            return results

        collector_type = self.config.console_collector_type

        if collector_type in ["conserver", "auto"]:
            # Check for console command
            console_path = shutil.which("console")
            if console_path:
                results.append(
                    CheckResult(
                        category=f"Host: {hostname}",
                        name="conserver collector",
                        passed=True,
                        message=f"console command found at {console_path}",
                    )
                )
            else:
                if collector_type == "conserver":
                    results.append(
                        CheckResult(
                            category=f"Host: {hostname}",
                            name="conserver collector",
                            passed=False,
                            message="console command not found",
                        )
                    )
                else:
                    results.append(
                        CheckResult(
                            category=f"Host: {hostname}",
                            name="conserver collector",
                            passed=True,
                            message="console not found (will fallback to IPMI)",
                            warning=True,
                        )
                    )

        return results

    def run_all_checks(self) -> bool:
        """Run all system checks and collect results.

        Returns:
            True if all checks passed, False otherwise
        """
        self.results = []

        # Local tools check
        logger.info("Checking local tools...")
        self.results.extend(self.check_local_tools())

        # Configuration validation
        logger.info("Validating configuration...")
        self.results.extend(self.check_config_validity())

        # Per-host checks
        for host_config in self.config.hosts:
            hostname = host_config["hostname"]
            logger.info(f"Checking host: {hostname}")

            # SSH connectivity
            ssh_results = self.check_ssh_connectivity(host_config)
            self.results.extend(ssh_results)

            # If SSH failed, skip remaining checks for this host
            if ssh_results and not ssh_results[0].passed:
                self.results.append(
                    CheckResult(
                        category=f"Host: {hostname}",
                        name="remaining checks",
                        passed=True,
                        message="Skipped due to SSH connection failure",
                        warning=True,
                    )
                )
                continue

            # Slave deployment
            self.results.extend(self.check_slave_deployment(host_config))

            # Power controller
            self.results.extend(self.check_power_controller(host_config))

            # Console collector
            self.results.extend(self.check_console_collector(host_config))

        # Return overall success
        return all(r.passed for r in self.results)

    def print_results(self):
        """Print formatted check results to console."""
        if not self.results:
            print("No checks performed.")
            return

        print("\nRunning kbisect system checks...\n")

        # Group results by category
        categories = {}
        for result in self.results:
            if result.category not in categories:
                categories[result.category] = []
            categories[result.category].append(result)

        # Print results by category
        for category, results in categories.items():
            print(f"[{category}]")
            for result in results:
                symbol = ("⚠" if result.warning else "✓") if result.passed else "✗"

                print(f"{symbol} {result.name}: {result.message}")
                if result.details:
                    print(f"  {result.details}")
            print()

        # Print summary
        passed = sum(1 for r in self.results if r.passed and not r.warning)
        failed = sum(1 for r in self.results if not r.passed)
        warnings = sum(1 for r in self.results if r.warning)

        print(f"Summary: {passed} passed, {failed} failed, {warnings} warning(s)")

        if failed > 0:
            print(
                "\n⚠ Some checks failed. Please address the issues above before running bisection."
            )
