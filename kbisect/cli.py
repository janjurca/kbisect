#!/usr/bin/env python3
"""kbisect - Kernel Bisection CLI Tool.

Main command-line interface for automated kernel bisection.
"""

import argparse
import logging
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Dict

import yaml

from kbisect.core import BisectMaster, SlaveMonitor
from kbisect.core.checker import SystemChecker
from kbisect.core.orchestrator import BisectConfig, HostConfig
from kbisect.deployment import SlaveDeployer
from kbisect.persistence import StateManager
from kbisect.power import IPMIController


# Constants
DEFAULT_CONFIG_PATH = "bisect.yaml"

# Configure logging
logger = logging.getLogger(__name__)


def setup_logging(verbose: bool = False) -> None:
    """Configure logging based on verbosity level.

    Args:
        verbose: If True, enable DEBUG level logging
    """
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(level=level, format="%(asctime)s [%(levelname)s] %(message)s")


def load_config(config_path: str) -> Dict[str, Any]:
    """Load configuration from YAML file.

    Args:
        config_path: Path to YAML configuration file

    Returns:
        Configuration dictionary

    Raises:
        SystemExit: If config file not found
    """
    path = Path(config_path)

    if not path.exists():
        logger.error(f"Config file not found: {config_path}")
        logger.info("Please create a config file. See example at:")
        logger.info("  kernel-bisect/config/bisect.conf.example")
        sys.exit(1)

    with path.open() as f:
        config_dict = yaml.safe_load(f)

    # Resolve relative paths in config relative to config file location
    config_dir = path.parent.resolve()

    # Resolve test script path if it's relative
    if config_dict.get("test", {}).get("script"):
        test_script = config_dict["test"]["script"]
        test_script_path = Path(test_script)

        # Only resolve if it's not already absolute
        if not test_script_path.is_absolute():
            resolved_path = (config_dir / test_script_path).resolve()
            config_dict["test"]["script"] = str(resolved_path)
            logger.debug(f"Resolved test script path: {test_script} -> {resolved_path}")

    # Resolve kernel config file path if it's relative
    if config_dict.get("kernel_config", {}).get("config_file"):
        kernel_config = config_dict["kernel_config"]["config_file"]
        kernel_config_path = Path(kernel_config)

        # Only resolve if it's not already absolute
        if not kernel_config_path.is_absolute():
            resolved_path = (config_dir / kernel_config_path).resolve()
            config_dict["kernel_config"]["config_file"] = str(resolved_path)
            logger.debug(f"Resolved kernel config path: {kernel_config} -> {resolved_path}")

    return config_dict


def create_bisect_config(config_dict: Dict[str, Any], _args: Any) -> BisectConfig:
    """Create BisectConfig from config dict and CLI args.

    Args:
        config_dict: Configuration dictionary from YAML
        args: Parsed command-line arguments

    Returns:
        BisectConfig object

    Raises:
        SystemExit: If hosts configuration is missing or invalid
    """
    # Get kernel config settings from config file only
    kernel_config_file = config_dict.get("kernel_config", {}).get("config_file")

    # Get metadata settings from config
    metadata_config = config_dict.get("metadata", {})

    # Get console log settings from config file
    console_logs_config = config_dict.get("console_logs", {})
    collect_console_logs = console_logs_config.get("enabled", False)
    console_collector_type = console_logs_config.get("collector", "auto")

    # Parse hosts configuration (REQUIRED)
    if "hosts" not in config_dict:
        logger.error("Config file missing 'hosts' section")
        logger.error("Multi-host configuration is required. See example config:")
        logger.error("  kbisect init-config")
        sys.exit(1)

    if not config_dict["hosts"]:
        logger.error("Config file 'hosts' section is empty")
        logger.error("At least one host must be configured")
        sys.exit(1)

    hosts = []
    for host_dict in config_dict["hosts"]:
        if "hostname" not in host_dict:
            logger.error("Each host must have 'hostname' field")
            sys.exit(1)

        host_config = HostConfig(
            hostname=host_dict["hostname"],
            ssh_user=host_dict.get("ssh_user", "root"),
            kernel_path=host_dict.get("kernel_path", "/root/kernel"),
            bisect_path=host_dict.get("bisect_path", "/root/kernel-bisect/lib"),
            test_script=host_dict.get("test_script", "test.sh"),
            kernel_config_file=host_dict.get("kernel_config_file"),
            power_control_type=host_dict.get("power_control_type", "ipmi"),
            ipmi_host=host_dict.get("ipmi_host"),
            ipmi_user=host_dict.get("ipmi_user"),
            ipmi_password=host_dict.get("ipmi_password"),
        )
        hosts.append(host_config)

    logger.info(f"Loaded multi-host configuration with {len(hosts)} hosts")

    return BisectConfig(
        hosts=hosts,
        boot_timeout=config_dict.get("timeouts", {}).get("boot", 300),
        test_timeout=config_dict.get("timeouts", {}).get("test", 600),
        build_timeout=config_dict.get("timeouts", {}).get("build", 1800),
        ssh_connect_timeout=config_dict.get("timeouts", {}).get("ssh_connect", 15),
        test_type=config_dict.get("test", {}).get("type", "boot"),
        state_dir=config_dict.get("state_dir", "."),
        db_path=config_dict.get("database_path", "bisect.db"),
        kernel_config_file=kernel_config_file,
        collect_baseline=metadata_config.get("collect_baseline", True),
        collect_per_iteration=metadata_config.get("collect_per_iteration", True),
        collect_kernel_config=metadata_config.get("collect_kernel_config", True),
        collect_console_logs=collect_console_logs,
        console_collector_type=console_collector_type,
        console_hostname=console_logs_config.get("hostname"),
        console_fallback_ipmi=console_logs_config.get("fallback_to_ipmi", True),
        kernel_repo_source=config_dict.get("kernel_repo", {}).get("source"),
        kernel_repo_branch=config_dict.get("kernel_repo", {}).get("branch"),
    )


def cmd_init(args: argparse.Namespace) -> int:
    """Initialize bisection.

    Args:
        args: Parsed command-line arguments

    Returns:
        Exit code (0 for success, 1 for failure)
    """
    print("=== Kernel Bisection Initialization ===\n")

    # Load config
    config_dict = load_config(args.config)

    # Validate hosts configuration
    if "hosts" not in config_dict or not config_dict["hosts"]:
        print("✗ Config file must have 'hosts' section")
        print("Run: kbisect init-config to create example config")
        return 1

    auto_deploy = config_dict.get("deployment", {}).get("auto_deploy", True)
    ssh_connect_timeout = config_dict.get("timeouts", {}).get("ssh_connect", 15)

    # Check and deploy all hosts if needed
    print(f"Checking setup for {len(config_dict['hosts'])} host(s)...")
    for i, host_dict in enumerate(config_dict["hosts"], 1):
        host_name = host_dict["hostname"]
        host_user = host_dict.get("ssh_user", "root")
        deploy_path = host_dict.get("bisect_path", "/root/kernel-bisect/lib")

        print(f"\n[{i}/{len(config_dict['hosts'])}] Checking host: {host_name}")
        deployer = SlaveDeployer(
            host_name, host_user, deploy_path, connect_timeout=ssh_connect_timeout
        )

        if not deployer.is_deployed():
            if auto_deploy or args.force_deploy:
                print("  Host not configured. Deploying automatically...")
                if not deployer.deploy_full():
                    print(f"\n✗ Deployment failed for {host_name}!")
                    return 1
                print("  ✓ Deployed successfully")
            else:
                print(f"\n✗ Host {host_name} is not deployed and auto_deploy is disabled")
                print("Run: kbisect deploy to deploy manually")
                return 1
        else:
            print("  ✓ Already deployed")

    print("\n✓ All hosts are deployed")

    # Create bisect config
    config = create_bisect_config(config_dict, args)

    # Create bisect master
    bisect = BisectMaster(config, args.good_commit, args.bad_commit)

    # Initialize
    if bisect.initialize():
        print("\n✓ Initialization complete")
        print(f"\nGood commit: {args.good_commit}")
        print(f"Bad commit:  {args.bad_commit}")
        print(f"Hosts:       {len(config.hosts)} configured")
        for host_config in config.hosts:
            print(f"  - {host_config.hostname}")
        print("\nReady to start bisection!")
        print("Run: kbisect start")
        return 0

    print("\n✗ Initialization failed")
    return 1


def _resume_session(session, state: StateManager, config_dict: dict) -> bool:
    """Resume a halted bisection session.

    Args:
        session: The halted session to resume
        state: StateManager instance
        config_dict: Configuration dictionary

    Returns:
        True if resume was successful, False otherwise
    """
    print("=" * 70)
    print("RESUMING HALTED BISECTION SESSION")
    print("=" * 70)
    print(f"\nSession ID: {session.session_id}")
    print(f"Good commit: {session.good_commit}")
    print(f"Bad commit: {session.bad_commit}")
    print(f"Started: {session.start_time}")

    # Get last iteration to show what failed
    iterations = state.get_iterations(session.session_id)
    if iterations:
        last_iteration = iterations[-1]
        print(f"\nLast iteration: {last_iteration.iteration_num}")
        print(f"Failed commit: {last_iteration.commit_sha[:7]}")
        if last_iteration.error_message:
            print(f"Error: {last_iteration.error_message}")

    print("\nThe previous session was halted due to host being unreachable.")
    print("Before resuming, please ensure:")
    print("  1. All host machines are powered on and stable")
    print("  2. Stable kernels are booted on all hosts")
    print("  3. SSH connectivity is working for all hosts")

    # Verify connectivity to all hosts before resuming
    print("\nVerifying host connectivity...")

    if "hosts" not in config_dict or not config_dict["hosts"]:
        print("✗ Config file must have 'hosts' section")
        return False

    from kbisect.remote import SSHClient

    ssh_connect_timeout = config_dict.get("timeouts", {}).get("ssh_connect", 15)
    all_reachable = True
    ssh_clients = []
    for host_dict in config_dict["hosts"]:
        host_name = host_dict["hostname"]
        host_user = host_dict.get("ssh_user", "root")
        ssh = SSHClient(host_name, host_user, ssh_connect_timeout)
        ssh_clients.append((host_name, ssh, host_dict))

        if not ssh.is_alive():
            print(f"  ✗ {host_name} is unreachable!")
            all_reachable = False
        else:
            print(f"  ✓ {host_name} is reachable")

    if not all_reachable:
        print("\n✗ One or more hosts are still unreachable!")
        print("\nPlease fix the host machines and try again.")
        return False

    print("\n✓ All hosts are reachable")
    print("Resuming bisection from halted state...")

    # Check if there's a pending commit to mark
    if iterations:
        last_iteration = iterations[-1]
        if last_iteration.error_message and "(git mark pending" in last_iteration.error_message:
            print(f"Marking pending commit {last_iteration.commit_sha[:7]}...")

            # Determine what to mark based on error message
            if (
                "Boot timeout" in last_iteration.error_message
                or "Kernel panic" in last_iteration.error_message
            ):
                # Determine mark type based on original test type
                test_type = config_dict.get("test", {}).get("type", "boot")
                if test_type == "boot":
                    mark_as = "bad"
                    print("  Boot test mode: marking as BAD")
                else:
                    mark_as = "skip"
                    print(
                        "  Custom test mode: marking as SKIP (cannot test if kernel doesn't boot)"
                    )

                # Mark the commit via SSH (use first host since all share git state)
                _first_host_name, first_ssh, first_host_dict = ssh_clients[0]
                kernel_path = first_host_dict.get("kernel_path", "/root/kernel")
                mark_cmd = f"cd {kernel_path} && git bisect {mark_as}"
                ret, _, stderr = first_ssh.run_command(mark_cmd, timeout=first_ssh.connect_timeout)

                if ret == 0:
                    print(f"✓ Commit marked as {mark_as}")
                    # Update iteration with final result
                    state.update_iteration(
                        last_iteration.iteration_id,
                        final_result=mark_as,
                        error_message=last_iteration.error_message.replace(
                            " (git mark pending - slave down)", ""
                        ),
                    )
                else:
                    print(f"✗ Failed to mark commit: {stderr}")
                    print("  Please mark manually and try again")
                    return False

    print("Bisection will continue from next commit.")
    print("=" * 70 + "\n")

    # Update session status back to running
    state.update_session(session.session_id, status="running")
    return True


def cmd_start(args: argparse.Namespace) -> int:
    """Start bisection.

    Args:
        args: Parsed command-line arguments

    Returns:
        Exit code (0 for success, 1 for failure)
    """
    print("=== Starting Kernel Bisection ===\n")

    # Load config
    config_dict = load_config(args.config)

    # If no commits specified, try to load from state
    state = StateManager()
    session = state.get_latest_session()

    if not session and (not args.good_commit or not args.bad_commit):
        print("Error: No bisection session found and no commits specified")
        print("Usage: kbisect start <good-commit> <bad-commit>")
        print("   or: kbisect init <good-commit> <bad-commit> first")
        return 1

    good = args.good_commit or session.good_commit
    bad = args.bad_commit or session.bad_commit

    # Check for halted session and handle resume
    if session and session.status == "halted" and not _resume_session(session, state, config_dict):
        state.close()
        return 1

    # Create bisect config
    config = create_bisect_config(config_dict, args)

    # Create bisect master
    bisect = BisectMaster(config, good, bad)

    # Initialize if not already done
    if not session or args.reinit:
        print("Initializing bisection...")
        if not bisect.initialize():
            print("✗ Initialization failed")
            return 1

    # Run bisection
    print("Running bisection...\n")
    if bisect.run():
        print("\n✓ Bisection complete!")
        return 0

    print("\n✗ Bisection failed")
    return 1


def cmd_status(_args: argparse.Namespace) -> int:
    """Show bisection status.

    Args:
        _args: Parsed command-line arguments (unused)

    Returns:
        Exit code (0 for success, 1 for failure)
    """
    state = StateManager()
    session = state.get_latest_session()

    if not session:
        print("No active bisection session found")
        return 0

    print("=== Bisection Status ===\n")
    print(f"Session ID:   {session.session_id}")
    print(f"Status:       {session.status}")
    print(f"Good commit:  {session.good_commit}")
    print(f"Bad commit:   {session.bad_commit}")
    print(f"Started:      {session.start_time}")

    if session.end_time:
        print(f"Ended:        {session.end_time}")

    if session.result_commit:
        print(f"\nFirst bad commit: {session.result_commit}")

    # Show iterations
    iterations = state.get_iterations(session.session_id)
    print(f"\nTotal iterations: {len(iterations)}")

    if iterations:
        print("\nRecent iterations:")
        for it in iterations[-5:]:  # Show last 5
            result = it.final_result or "running"
            duration = f"{it.duration}s" if it.duration else "N/A"
            print(
                f"  {it.iteration_num:3d}. {it.commit_sha[:7]} | "
                f"{result:7s} | {duration:6s} | {it.commit_message[:50]}"
            )

    state.close()
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    """Generate bisection report.

    Args:
        args: Parsed command-line arguments

    Returns:
        Exit code (0 for success, 1 for failure)
    """
    state = StateManager()

    session_id = args.session_id
    if not session_id:
        session = state.get_latest_session()
        if session:
            session_id = session.session_id
        else:
            print("No bisection session found")
            return 1

    # Generate report
    report = state.export_report(session_id, format=args.format)

    if args.output:
        output_path = Path(args.output)
        with output_path.open("w") as f:
            f.write(report)
        print(f"Report saved to: {args.output}")
    else:
        print(report)

    state.close()
    return 0


def cmd_monitor(args: argparse.Namespace) -> int:
    """Monitor host health.

    Args:
        args: Parsed command-line arguments

    Returns:
        Exit code (0 for success, 1 for failure)
    """
    config_dict = load_config(args.config)

    if "hosts" not in config_dict or not config_dict["hosts"]:
        print("✗ Config file must have 'hosts' section")
        return 1

    # Create monitors for all hosts
    monitors = []
    ssh_connect_timeout = config_dict.get("timeouts", {}).get("ssh_connect", 15)
    for host_dict in config_dict["hosts"]:
        monitor = SlaveMonitor(
            host_dict["hostname"], host_dict.get("ssh_user", "root"), ssh_connect_timeout
        )
        monitors.append((host_dict["hostname"], monitor))

    print(f"=== Host Monitor ({len(monitors)} hosts) ===\n")

    if args.continuous:
        print(f"Monitoring {len(monitors)} host(s) (Ctrl+C to stop)...\n")
        try:
            while True:
                for hostname, monitor in monitors:
                    status = monitor.check_health()
                    print(
                        f"[{status.last_check}] {hostname}: Alive={status.is_alive} | "
                        f"Kernel={status.kernel_version or 'N/A'}"
                    )
                print()  # Blank line between intervals
                time.sleep(args.interval)
        except KeyboardInterrupt:
            print("\nMonitoring stopped")
    else:
        for hostname, monitor in monitors:
            status = monitor.check_health()
            print(f"Host: {hostname}")
            print(f"  Alive:  {status.is_alive}")
            print(f"  Ping:   {status.ping_responsive}")
            print(f"  SSH:    {status.ssh_responsive}")
            if status.kernel_version:
                print(f"  Kernel: {status.kernel_version}")
            if status.uptime:
                print(f"  Uptime: {status.uptime}")
            if status.error:
                print(f"  Error:  {status.error}")
            print()

    return 0


def cmd_ipmi(args: argparse.Namespace) -> int:
    """IPMI control commands.

    Args:
        args: Parsed command-line arguments

    Returns:
        Exit code (0 for success, 1 for failure)
    """
    config_dict = load_config(args.config)

    if "hosts" not in config_dict or not config_dict["hosts"]:
        print("✗ Config file must have 'hosts' section")
        return 1

    # Collect all hosts with IPMI configured
    ipmi_hosts = []
    for i, host_dict in enumerate(config_dict["hosts"]):
        if host_dict.get("ipmi_host") and "ipmi_user" in host_dict and "ipmi_password" in host_dict:
            ipmi_hosts.append((i, host_dict))

    if not ipmi_hosts:
        print("✗ No hosts have IPMI configured")
        print("Configure IPMI for at least one host in config file")
        return 1

    # If multiple hosts have IPMI, show selection (for now, use first host)
    # TODO: Add --host-index argument to allow selection
    if len(ipmi_hosts) > 1:
        print(f"Note: {len(ipmi_hosts)} hosts have IPMI configured, using first host")
        print("  (Future: Use --host-index to select specific host)\n")

    _host_index, host_dict = ipmi_hosts[0]
    host_name = host_dict["hostname"]

    print(f"=== IPMI Control for {host_name} ===\n")

    controller = IPMIController(
        host_dict["ipmi_host"], host_dict["ipmi_user"], host_dict["ipmi_password"]
    )

    if args.ipmi_command == "status":
        state = controller.get_power_status()
        print(f"Power state: {state.value}")

    elif args.ipmi_command == "on":
        print("Powering on...")
        controller.power_on()
        print("✓ Power on command sent")

    elif args.ipmi_command == "off":
        print("Powering off...")
        controller.power_off()
        print("✓ Power off command sent")

    elif args.ipmi_command == "reset":
        print("Resetting...")
        controller.reset()
        print("✓ Reset command sent")

    elif args.ipmi_command == "cycle":
        print("Power cycling...")
        controller.power_cycle()
        print("✓ Power cycle command sent")

    return 0


def _handle_logs_list(state: StateManager, args: argparse.Namespace) -> int:
    """Handle 'logs list' subcommand."""
    session_id = args.session_id
    log_type = args.log_type

    logs = state.list_build_logs(session_id=session_id, log_type=log_type)

    if not logs:
        print("No build logs found")
        return 0

    print("=== Build Logs ===\n")
    print(
        f"{'Log ID':<8} {'Iter':<6} {'Host':<15} {'Commit':<9} {'Type':<8} {'Status':<10} {'Size':<10} {'Timestamp':<20}"
    )
    print("-" * 100)

    for log in logs:
        size_kb = log["size_bytes"] / 1024 if log["size_bytes"] else 0
        timestamp = log["timestamp"][:19] if log["timestamp"] else "N/A"
        hostname = log["hostname"] if log["hostname"] else "-"
        print(
            f"{log['log_id']:<8} {log['iteration_num']:<6} "
            f"{hostname:<15} {log['commit_sha'][:7]:<9} {log['log_type']:<8} "
            f"{log['status']:<10} {size_kb:>7.1f} KB {timestamp:<20}"
        )
    return 0


def _handle_logs_show(state: StateManager, args: argparse.Namespace) -> int:
    """Handle 'logs show' subcommand."""
    log_data = state.get_build_log(args.log_id)

    if not log_data:
        print(f"Log {args.log_id} not found")
        return 1

    print(f"=== Build Log {args.log_id} ===\n")
    print(f"Iteration:     {log_data['iteration_num']}")
    print(f"Commit:        {log_data['commit_sha'][:7]} - {log_data['commit_message'][:50]}")
    print(f"Type:          {log_data['log_type']}")
    print(f"Exit code:     {log_data['exit_code']}")
    size_kb = log_data["size_bytes"] / 1024 if log_data.get("size_bytes") else 0
    print(f"Size:          {size_kb:.1f} KB (compressed)")
    print(f"Timestamp:     {log_data['timestamp']}")
    print("\n" + "=" * 80 + "\n")
    print(log_data["content"])
    return 0


def _handle_logs_iteration(state: StateManager, args: argparse.Namespace) -> int:
    """Handle 'logs iteration' subcommand."""
    session = state.get_latest_session()
    if not session:
        print("No active bisection session found")
        return 1

    iterations = state.get_iterations(session.session_id)
    target_iteration = None

    for it in iterations:
        if it.iteration_num == args.iteration_num:
            target_iteration = it
            break

    if not target_iteration:
        print(f"Iteration {args.iteration_num} not found")
        return 1

    # Get logs for this iteration
    logs = state.get_iteration_build_logs(target_iteration.iteration_id)

    if not logs:
        print(f"No logs found for iteration {args.iteration_num}")
        return 0

    print(f"=== Logs for Iteration {args.iteration_num} ===\n")
    print(f"Commit: {target_iteration.commit_sha[:7]} - {target_iteration.commit_message}")
    print("\nLogs:")

    for log in logs:
        size_kb = log["size_bytes"] / 1024 if log["size_bytes"] else 0
        exit_status = (
            "RUNNING"
            if log["exit_code"] is None
            else ("SUCCESS" if log["exit_code"] == 0 else "FAILED")
        )
        print(f"  Log ID {log['log_id']}: {log['log_type']} - {exit_status} ({size_kb:.1f} KB)")

    print("\nView log: kbisect logs show <log-id>")
    return 0


def _handle_logs_export(state: StateManager, args: argparse.Namespace) -> int:
    """Handle 'logs export' subcommand."""
    log_data = state.get_build_log(args.log_id)

    if not log_data:
        print(f"Log {args.log_id} not found")
        return 1

    output_path = Path(args.output_file)

    try:
        with output_path.open("w") as f:
            f.write(log_data["content"])
        print(f"Log {args.log_id} exported to: {output_path}")
        file_size = output_path.stat().st_size
        size_kb = file_size / 1024 if file_size else 0
        print(f"Size: {size_kb:.1f} KB (uncompressed)")
        return 0
    except Exception as exc:
        print(f"Failed to export log: {exc}")
        return 1


def _handle_logs_tail(state: StateManager, args: argparse.Namespace) -> int:
    """Handle 'logs tail' subcommand."""
    log_id = args.log_id
    interval = args.interval

    # Get initial log state
    log_data = state.get_build_log(log_id)
    if not log_data:
        print(f"Log {log_id} not found")
        return 1

    # Display header
    print(f"=== Tailing Log {log_id} ===")
    print(f"Type:      {log_data['log_type']}")
    print(f"Iteration: {log_data['iteration_num']}")
    print(f"Commit:    {log_data['commit_sha'][:7]} - {log_data['commit_message'][:50]}")
    if log_data["exit_code"] is not None:
        exit_status = "SUCCESS" if log_data["exit_code"] == 0 else "FAILED"
        print(f"Status:    {exit_status} (already completed)")
    else:
        print("Status:    IN PROGRESS")
    print(f"Interval:  {interval}s")
    print("\nPress Ctrl+C to stop")
    print("=" * 80 + "\n")

    # Display initial content
    print(log_data["content"], end="", flush=True)
    last_length = len(log_data["content"])

    # If already finalized, no need to poll
    if log_data["exit_code"] is not None:
        print(f"\n\n[Log already finalized with exit code: {log_data['exit_code']}]")
        return 0

    # Poll for updates
    try:
        while True:
            time.sleep(interval)

            # Re-fetch log
            log_data = state.get_build_log(log_id)
            if not log_data:
                print("\n\n[Error: Log no longer exists]")
                break

            current_content = log_data["content"]
            current_length = len(current_content)

            # Display new content
            if current_length > last_length:
                new_content = current_content[last_length:]
                print(new_content, end="", flush=True)
                last_length = current_length

            # Check if finalized
            if log_data["exit_code"] is not None:
                exit_status = "SUCCESS" if log_data["exit_code"] == 0 else "FAILED"
                print(f"\n\n[Log finalized: {exit_status} (exit code: {log_data['exit_code']})]")
                break

    except KeyboardInterrupt:
        print("\n\n[Tail stopped by user]")

    return 0


def cmd_logs(args: argparse.Namespace) -> int:
    """Manage build logs.

    Args:
        args: Parsed command-line arguments

    Returns:
        Exit code (0 for success, 1 for failure)
    """
    state = StateManager()

    # Dispatch to appropriate handler
    handlers = {
        "list": _handle_logs_list,
        "show": _handle_logs_show,
        "iteration": _handle_logs_iteration,
        "export": _handle_logs_export,
        "tail": _handle_logs_tail,
    }

    handler = handlers.get(args.logs_command)
    if handler:
        result = handler(state, args)
    else:
        print(f"Unknown logs command: {args.logs_command}")
        result = 1

    state.close()
    return result


def cmd_metadata(args: argparse.Namespace) -> int:
    """Manage metadata.

    Args:
        args: Parsed command-line arguments

    Returns:
        Exit code (0 for success, 1 for failure)
    """
    state = StateManager()

    if args.metadata_command == "list":
        # List all metadata
        session_id = args.session_id
        collection_type = args.type

        # Get session if not specified
        if not session_id:
            session = state.get_latest_session()
            if session:
                session_id = session.session_id
            else:
                print("No bisection session found")
                return 1

        metadata_list = state.get_session_metadata(session_id, collection_type=collection_type)

        if not metadata_list:
            print("No metadata found")
            return 0

        print("=== Metadata ===\n")
        print(
            f"{'ID':<6} {'Session':<8} {'Iteration':<10} {'Host':<15} {'Type':<12} {'Collection Time':<20}"
        )
        print("-" * 85)

        for meta in metadata_list:
            iter_str = str(meta["iteration_id"]) if meta["iteration_id"] else "N/A"
            timestamp = meta["collection_time"][:19] if meta["collection_time"] else "N/A"
            hostname = meta["hostname"] if meta["hostname"] else "N/A"
            print(
                f"{meta['metadata_id']:<6} {meta['session_id']:<8} "
                f"{iter_str:<10} {hostname:<15} {meta['collection_type']:<12} {timestamp:<20}"
            )

    elif args.metadata_command == "show":
        # Show specific metadata
        metadata = state.get_metadata(args.metadata_id)

        if not metadata:
            print(f"Metadata {args.metadata_id} not found")
            return 1

        print(f"=== Metadata {args.metadata_id} ===\n")
        print(f"Session ID:        {metadata['session_id']}")
        print(f"Iteration ID:      {metadata['iteration_id'] or 'N/A'}")
        print(f"Collection Type:   {metadata['collection_type']}")
        print(f"Collection Time:   {metadata['collection_time']}")
        print("\n" + "=" * 80 + "\n")

        # Display metadata content
        import json

        metadata_content = metadata["metadata"]

        # If metadata is a dict, pretty print as JSON
        if isinstance(metadata_content, dict):
            print(json.dumps(metadata_content, indent=2))
        # If metadata is a string, print it directly (preserves newlines)
        elif isinstance(metadata_content, str):
            print(metadata_content)
        else:
            # Fallback for other types
            print(metadata_content)

    elif args.metadata_command == "export-file":
        # Export file content to disk
        metadata_id = args.file_id

        # Get file content from metadata JSON
        content_text = state.get_file_content(metadata_id)

        if content_text is None:
            print(f"File with metadata_id={metadata_id} not found or has no content")
            return 1

        output_path = Path(args.output) if args.output else Path(f"metadata-file-{metadata_id}")

        try:
            with output_path.open("w", encoding="utf-8") as f:
                f.write(content_text)
            print(f"File exported to: {output_path}")
            size_kb = len(content_text.encode("utf-8")) / 1024
            print(f"Size: {size_kb:.1f} KB")
        except Exception as exc:
            print(f"Failed to export file: {exc}")
            return 1

    elif args.metadata_command == "export":
        # Export metadata JSON to file
        metadata = state.get_metadata(args.metadata_id)

        if not metadata:
            print(f"Metadata {args.metadata_id} not found")
            return 1

        output_path = (
            Path(args.output) if args.output else Path(f"metadata-{args.metadata_id}.json")
        )

        try:
            import json

            with output_path.open("w") as f:
                if args.format == "json":
                    json.dump(metadata["metadata"], f, indent=2)
                else:  # yaml
                    import yaml

                    yaml.dump(metadata["metadata"], f, default_flow_style=False)

            print(f"Metadata {args.metadata_id} exported to: {output_path}")
        except Exception as exc:
            print(f"Failed to export metadata: {exc}")
            return 1

    state.close()
    return 0


def cmd_deploy(args: argparse.Namespace) -> int:
    """Deploy components to all hosts.

    Args:
        args: Parsed command-line arguments

    Returns:
        Exit code (0 for success, 1 for failure)
    """
    print("=== Host Deployment ===\n")

    # Load config
    config_dict = load_config(args.config)

    if "hosts" not in config_dict or not config_dict["hosts"]:
        print("✗ Config file must have 'hosts' section")
        return 1

    print(f"Deploying to {len(config_dict['hosts'])} host(s)...\n")

    ssh_connect_timeout = config_dict.get("timeouts", {}).get("ssh_connect", 15)
    all_success = True
    for i, host_dict in enumerate(config_dict["hosts"], 1):
        host_name = host_dict["hostname"]
        host_user = host_dict.get("ssh_user", "root")
        deploy_path = host_dict.get("bisect_path", "/root/kernel-bisect/lib")

        print(f"[{i}/{len(config_dict['hosts'])}] Host: {host_name}")

        deployer = SlaveDeployer(
            host_name, host_user, deploy_path, connect_timeout=ssh_connect_timeout
        )

        if args.verify_only:
            # Just verify deployment
            print("  Verifying deployment...")
            if deployer.is_deployed():
                print("  ✓ Deployed")
                success, _checks = deployer.verify_deployment()
                if not success:
                    all_success = False
            else:
                print("  ✗ NOT deployed")
                all_success = False

        elif args.update_only:
            # Update library only
            print("  Updating library...")
            if deployer.update_library():
                print("  ✓ Library updated")
            else:
                print("  ✗ Library update failed")
                all_success = False

        else:
            # Full deployment
            print("  Deploying...")
            if deployer.deploy_full():
                print("  ✓ Deployment successful")
            else:
                print("  ✗ Deployment failed")
                all_success = False

        print()

    if all_success:
        print("✓ All hosts deployed successfully!")
        return 0
    else:
        print("✗ One or more hosts failed deployment")
        return 1


def cmd_init_config(args: argparse.Namespace) -> int:
    """Generate example configuration file.

    Args:
        args: Parsed command-line arguments

    Returns:
        Exit code (0 for success, 1 for failure)
    """
    # Locate the example config file in the package
    config_dir = Path(__file__).parent / "config"
    source_file = config_dir / "bisect.conf.example"

    if not source_file.exists():
        print(f"Error: Example config file not found at {source_file}")
        print("This might indicate a corrupted installation.")
        return 1

    # Determine output file path
    output_file = Path(args.output) if args.output else Path("bisect.yaml")

    # Check if output file already exists
    if output_file.exists() and not args.force:
        response = input(f"File '{output_file}' already exists. Overwrite? [y/N]: ")
        if response.lower() not in ["y", "yes"]:
            print("Aborted.")
            return 1

    # Copy the example config
    try:
        shutil.copy(source_file, output_file)
        print(f"✓ Example configuration created: {output_file}")
        print("\nNext steps:")
        print(f"  1. Edit {output_file} with your slave configuration")
        print("  2. Run: kbisect init <good-commit> <bad-commit>")
        return 0
    except Exception as exc:
        print(f"Error copying config file: {exc}")
        return 1


def cmd_check(args: argparse.Namespace) -> int:
    """Check system dependencies and configuration.

    Args:
        args: Parsed command-line arguments

    Returns:
        Exit code (0 if all checks passed, 1 if any check failed)
    """
    logger.info("Running system health checks...")

    # Load configuration
    try:
        config_dict = load_config(args.config)
    except SystemExit:
        logger.error("Failed to load configuration file")
        logger.error(f"Please ensure {args.config} exists and is valid YAML")
        return 1

    # Create BisectConfig object
    try:
        config = create_bisect_config(config_dict, args)
    except SystemExit:
        logger.error("Configuration validation failed")
        return 1

    # Create SystemChecker and run all checks
    checker = SystemChecker(config)

    try:
        all_passed = checker.run_all_checks()
        checker.print_results()

        if all_passed:
            logger.info("✓ All checks passed - system is ready for bisection")
            return 0
        else:
            logger.error("✗ Some checks failed - please address issues before running bisection")
            return 1

    except Exception as exc:
        logger.error(f"Error running system checks: {exc}", exc_info=True)
        return 1


def cmd_build(args: argparse.Namespace) -> int:
    """Build kernel for a specific commit without running tests.

    Args:
        args: Parsed command-line arguments

    Returns:
        Exit code (0 for success, 1 for failure)
    """
    print(f"=== Building Kernel: {args.commit[:7]} ===\n")

    # Load config
    config_dict = load_config(args.config)

    # Validate hosts configuration
    if "hosts" not in config_dict or not config_dict["hosts"]:
        print("✗ Config file must have 'hosts' section")
        return 1

    # Create bisect config
    config = create_bisect_config(config_dict, args)

    # Create temporary BisectMaster instance for build operations
    # Use dummy commits since we're not running bisection
    bisect = BisectMaster(config, "dummy", "dummy")

    # Run build-only operation
    success = bisect.build_only(args.commit, save_logs=args.save_logs)

    if success:
        print("\n✓ Build complete on all hosts")
        return 0
    else:
        print("\n✗ Build failed on one or more hosts")
        return 1


def create_parser() -> argparse.ArgumentParser:
    """Create argument parser.

    Returns:
        Configured ArgumentParser
    """
    parser = argparse.ArgumentParser(
        description="Automated Kernel Bisection Tool",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    parser.add_argument(
        "-c", "--config", default=DEFAULT_CONFIG_PATH, help="Configuration file path"
    )

    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose output")

    subparsers = parser.add_subparsers(dest="command", help="Commands")

    # init command
    parser_init = subparsers.add_parser("init", help="Initialize bisection")
    parser_init.add_argument("good_commit", help="Known good commit (OLDER, working version)")
    parser_init.add_argument("bad_commit", help="Known bad commit (NEWER, broken version)")
    parser_init.add_argument(
        "--force-deploy",
        action="store_true",
        help="Force deployment even if auto_deploy is disabled",
    )

    # start command
    parser_start = subparsers.add_parser("start", help="Start bisection")
    parser_start.add_argument(
        "good_commit", nargs="?", help="Known good commit (OLDER, working version)"
    )
    parser_start.add_argument(
        "bad_commit", nargs="?", help="Known bad commit (NEWER, broken version)"
    )
    parser_start.add_argument("--reinit", action="store_true", help="Reinitialize bisection")

    # status command
    subparsers.add_parser("status", help="Show bisection status")

    # report command
    parser_report = subparsers.add_parser("report", help="Generate bisection report")
    parser_report.add_argument("--session-id", type=int, help="Session ID (default: latest)")
    parser_report.add_argument(
        "--format", choices=["text", "json"], default="text", help="Report format"
    )
    parser_report.add_argument("--output", "-o", help="Output file (default: stdout)")

    # monitor command
    parser_monitor = subparsers.add_parser("monitor", help="Monitor slave health")
    parser_monitor.add_argument("--continuous", action="store_true", help="Continuous monitoring")
    parser_monitor.add_argument("--interval", type=int, default=5, help="Check interval in seconds")

    # ipmi command
    parser_ipmi = subparsers.add_parser("ipmi", help="IPMI control")
    parser_ipmi.add_argument(
        "ipmi_command",
        choices=["status", "on", "off", "reset", "cycle"],
        help="IPMI command",
    )

    # deploy command
    parser_deploy = subparsers.add_parser("deploy", help="Deploy slave components")
    parser_deploy.add_argument(
        "--verify-only", action="store_true", help="Only verify deployment, do not deploy"
    )
    parser_deploy.add_argument(
        "--update-only", action="store_true", help="Only update library, do not full deploy"
    )

    # init-config command
    parser_init_config = subparsers.add_parser(
        "init-config", help="Generate example configuration file"
    )
    parser_init_config.add_argument(
        "--output", "-o", help="Output file path (default: bisect.yaml)"
    )
    parser_init_config.add_argument(
        "--force", "-f", action="store_true", help="Overwrite existing file without prompting"
    )

    # check command
    subparsers.add_parser("check", help="Check system dependencies and configuration")

    # logs command
    parser_logs = subparsers.add_parser("logs", help="Manage build logs")
    logs_subparsers = parser_logs.add_subparsers(dest="logs_command", help="Log commands")

    # logs list
    parser_logs_list = logs_subparsers.add_parser("list", help="List all build logs")
    parser_logs_list.add_argument("--session-id", type=int, help="Filter by session ID")
    parser_logs_list.add_argument(
        "--log-type", choices=["build", "boot", "test"], help="Filter by log type"
    )

    # logs show
    parser_logs_show = logs_subparsers.add_parser("show", help="Show specific build log")
    parser_logs_show.add_argument("log_id", type=int, help="Log ID to display")

    # logs iteration
    parser_logs_iteration = logs_subparsers.add_parser(
        "iteration", help="Show logs for specific iteration"
    )
    parser_logs_iteration.add_argument("iteration_num", type=int, help="Iteration number")

    # logs export
    parser_logs_export = logs_subparsers.add_parser("export", help="Export log to file")
    parser_logs_export.add_argument("log_id", type=int, help="Log ID to export")
    parser_logs_export.add_argument("output_file", help="Output file path")

    # logs tail
    parser_logs_tail = logs_subparsers.add_parser("tail", help="Tail (follow) a log in real-time")
    parser_logs_tail.add_argument("log_id", type=int, help="Log ID to tail")
    parser_logs_tail.add_argument(
        "--interval", type=float, default=1.0, help="Polling interval in seconds (default: 1.0)"
    )

    # metadata command
    parser_metadata = subparsers.add_parser("metadata", help="Manage metadata")
    metadata_subparsers = parser_metadata.add_subparsers(
        dest="metadata_command", help="Metadata commands"
    )

    # metadata list
    parser_metadata_list = metadata_subparsers.add_parser("list", help="List all metadata")
    parser_metadata_list.add_argument("--session-id", type=int, help="Filter by session ID")
    parser_metadata_list.add_argument(
        "--type", choices=["baseline", "iteration"], help="Filter by collection type"
    )

    # metadata show
    parser_metadata_show = metadata_subparsers.add_parser(
        "show", help="Show specific metadata details"
    )
    parser_metadata_show.add_argument("metadata_id", type=int, help="Metadata ID to display")

    # metadata export-file
    parser_metadata_export_file = metadata_subparsers.add_parser(
        "export-file", help="Export metadata file (e.g., kernel config) to disk"
    )
    parser_metadata_export_file.add_argument("file_id", type=int, help="File ID to export")
    parser_metadata_export_file.add_argument(
        "--output", "-o", help="Output file path (default: metadata-file-<id>)"
    )

    # metadata export
    parser_metadata_export = metadata_subparsers.add_parser(
        "export", help="Export metadata JSON to file"
    )
    parser_metadata_export.add_argument("metadata_id", type=int, help="Metadata ID to export")
    parser_metadata_export.add_argument(
        "--output", "-o", help="Output file path (default: metadata-<id>.json)"
    )
    parser_metadata_export.add_argument(
        "--format", choices=["json", "yaml"], default="json", help="Output format"
    )

    # build command
    parser_build = subparsers.add_parser(
        "build", help="Build kernel for a specific commit (no reboot, no tests)"
    )
    parser_build.add_argument(
        "commit", help="Commit hash to build (full 40-char SHA or short form)"
    )
    parser_build.add_argument(
        "--save-logs",
        action="store_true",
        help="Save build logs to database (creates temporary session)",
    )

    return parser


def main() -> int:
    """Main entry point.

    Returns:
        Exit code
    """
    parser = create_parser()
    args = parser.parse_args()

    setup_logging(args.verbose)

    # Route to command handlers
    try:
        if args.command == "init":
            return cmd_init(args)
        if args.command == "start":
            return cmd_start(args)
        if args.command == "status":
            return cmd_status(args)
        if args.command == "report":
            return cmd_report(args)
        if args.command == "monitor":
            return cmd_monitor(args)
        if args.command == "ipmi":
            return cmd_ipmi(args)
        if args.command == "deploy":
            return cmd_deploy(args)
        if args.command == "init-config":
            return cmd_init_config(args)
        if args.command == "check":
            return cmd_check(args)
        if args.command == "logs":
            return cmd_logs(args)
        if args.command == "metadata":
            return cmd_metadata(args)
        if args.command == "build":
            return cmd_build(args)

        parser.print_help()
        return 1

    except KeyboardInterrupt:
        print("\n\nInterrupted by user")
        return 130
    except Exception as exc:
        logger.error(f"Fatal error: {exc}", exc_info=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
