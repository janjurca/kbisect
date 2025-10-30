#!/usr/bin/env python3
"""kbisect - Kernel Bisection CLI Tool.

Main command-line interface for automated kernel bisection.
"""

import argparse
import logging
import sys
from pathlib import Path
from typing import Any, Dict

import yaml

from kbisect.master.bisect_master import BisectConfig, BisectMaster
from kbisect.master.ipmi_controller import IPMIController
from kbisect.master.slave_deployer import SlaveDeployer
from kbisect.master.slave_monitor import SlaveMonitor
from kbisect.master.state_manager import StateManager


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

    with open(path) as f:
        return yaml.safe_load(f)


def create_bisect_config(config_dict: Dict[str, Any], args: Any) -> BisectConfig:
    """Create BisectConfig from config dict and CLI args.

    Args:
        config_dict: Configuration dictionary from YAML
        args: Parsed command-line arguments

    Returns:
        BisectConfig object
    """
    # Get kernel config settings (CLI args override config file)
    kernel_config_file = getattr(args, "kernel_config", None) or config_dict.get(
        "kernel_config", {}
    ).get("config_file")
    use_running_config = getattr(args, "use_running_config", False) or config_dict.get(
        "kernel_config", {}
    ).get("use_running_config", False)

    # Get metadata settings from config
    metadata_config = config_dict.get("metadata", {})

    # Get slave host (CLI arg overrides config)
    slave_host = getattr(args, "slave_host", None) or config_dict["slave"]["hostname"]

    return BisectConfig(
        slave_host=slave_host,
        slave_user=config_dict["slave"].get("ssh_user", "root"),
        slave_kernel_path=config_dict["slave"].get("kernel_path", "/root/kernel"),
        slave_bisect_path=config_dict["slave"].get("bisect_path", "/root/kernel-bisect/lib"),
        ipmi_host=config_dict.get("ipmi", {}).get("host"),
        ipmi_user=config_dict.get("ipmi", {}).get("username"),
        ipmi_password=config_dict.get("ipmi", {}).get("password"),
        boot_timeout=config_dict.get("timeouts", {}).get("boot", 300),
        test_timeout=config_dict.get("timeouts", {}).get("test", 600),
        build_timeout=config_dict.get("timeouts", {}).get("build", 1800),
        test_type=getattr(args, "test_type", None)
        or config_dict.get("tests", [{}])[0].get("type", "boot"),
        test_script=getattr(args, "test_script", None),
        state_dir=config_dict.get("state_dir", "."),
        db_path=config_dict.get("database_path", "bisect.db"),
        kernel_config_file=kernel_config_file,
        use_running_config=use_running_config,
        collect_baseline=metadata_config.get("collect_baseline", True),
        collect_per_iteration=metadata_config.get("collect_per_iteration", True),
        collect_kernel_config=metadata_config.get("collect_kernel_config", True),
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

    # Check and deploy slave if needed
    slave_host = args.slave_host or config_dict["slave"]["hostname"]
    slave_user = config_dict["slave"].get("ssh_user", "root")
    deploy_path = config_dict["slave"].get("bisect_path", "/root/kernel-bisect/lib")
    auto_deploy = config_dict.get("deployment", {}).get("auto_deploy", True)

    deployer = SlaveDeployer(slave_host, slave_user, deploy_path)

    # Check if slave is deployed
    print("Checking slave setup...")
    if not deployer.is_deployed():
        if auto_deploy or args.force_deploy:
            print("Slave not configured. Deploying automatically...\n")
            if not deployer.deploy_full():
                print("\n✗ Deployment failed!")
                return 1
        else:
            print("\n✗ Slave is not deployed and auto_deploy is disabled")
            print("Run: kbisect deploy to deploy manually")
            return 1
    else:
        print("✓ Slave is already deployed\n")

    # Create bisect config
    config = create_bisect_config(config_dict, args)

    # Create bisect master
    bisect = BisectMaster(config, args.good_commit, args.bad_commit)

    # Initialize
    if bisect.initialize():
        print("\n✓ Initialization complete")
        print(f"\nGood commit: {args.good_commit}")
        print(f"Bad commit:  {args.bad_commit}")
        print(f"Slave:       {config.slave_host}")
        print("\nReady to start bisection!")
        print("Run: kbisect start")
        return 0

    print("\n✗ Initialization failed")
    return 1


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


def cmd_status(args: argparse.Namespace) -> int:
    """Show bisection status.

    Args:
        args: Parsed command-line arguments

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
        with open(args.output, "w") as f:
            f.write(report)
        print(f"Report saved to: {args.output}")
    else:
        print(report)

    state.close()
    return 0


def cmd_monitor(args: argparse.Namespace) -> int:
    """Monitor slave health.

    Args:
        args: Parsed command-line arguments

    Returns:
        Exit code (0 for success, 1 for failure)
    """
    config_dict = load_config(args.config)

    monitor = SlaveMonitor(
        config_dict["slave"]["hostname"], config_dict["slave"].get("ssh_user", "root")
    )

    print("=== Slave Monitor ===\n")

    if args.continuous:
        print("Monitoring slave (Ctrl+C to stop)...\n")
        try:
            while True:
                status = monitor.check_health()
                print(
                    f"[{status.last_check}] Alive: {status.is_alive} | "
                    f"Kernel: {status.kernel_version or 'N/A'}"
                )
                import time

                time.sleep(args.interval)
        except KeyboardInterrupt:
            print("\nMonitoring stopped")
    else:
        status = monitor.check_health()
        print(f"Slave: {config_dict['slave']['hostname']}")
        print(f"Alive: {status.is_alive}")
        print(f"Ping:  {status.ping_responsive}")
        print(f"SSH:   {status.ssh_responsive}")
        if status.kernel_version:
            print(f"Kernel: {status.kernel_version}")
        if status.uptime:
            print(f"Uptime: {status.uptime}")
        if status.error:
            print(f"Error: {status.error}")

    return 0


def cmd_ipmi(args: argparse.Namespace) -> int:
    """IPMI control commands.

    Args:
        args: Parsed command-line arguments

    Returns:
        Exit code (0 for success, 1 for failure)
    """
    config_dict = load_config(args.config)

    ipmi_config = config_dict.get("ipmi", {})
    if not ipmi_config.get("host"):
        print("Error: IPMI not configured")
        return 1

    controller = IPMIController(
        ipmi_config["host"], ipmi_config["username"], ipmi_config["password"]
    )

    if args.ipmi_command == "status":
        state = controller.get_power_status()
        print(f"Power state: {state.value}")

    elif args.ipmi_command == "on":
        controller.power_on()

    elif args.ipmi_command == "off":
        controller.power_off()

    elif args.ipmi_command == "reset":
        controller.reset()

    elif args.ipmi_command == "cycle":
        controller.power_cycle()

    return 0


def cmd_deploy(args: argparse.Namespace) -> int:
    """Deploy slave components.

    Args:
        args: Parsed command-line arguments

    Returns:
        Exit code (0 for success, 1 for failure)
    """
    print("=== Slave Deployment ===\n")

    # Load config
    config_dict = load_config(args.config)

    slave_host = args.slave_host or config_dict["slave"]["hostname"]
    slave_user = config_dict["slave"].get("ssh_user", "root")
    deploy_path = config_dict["slave"].get("bisect_path", "/root/kernel-bisect/lib")

    deployer = SlaveDeployer(slave_host, slave_user, deploy_path)

    if args.verify_only:
        # Just verify deployment
        print(f"Verifying deployment on {slave_host}...")
        if deployer.is_deployed():
            print("\n✓ Slave is deployed")
            success, checks = deployer.verify_deployment()
            return 0 if success else 1

        print("\n✗ Slave is NOT deployed")
        return 1

    if args.update_only:
        # Update library only
        print(f"Updating library on {slave_host}...")
        if deployer.update_library():
            print("\n✓ Library updated successfully")
            return 0

        print("\n✗ Library update failed")
        return 1

    # Full deployment
    print(f"Deploying to {slave_host}...")
    if deployer.deploy_full():
        print("\n✓ Deployment successful!")
        print(f"\nSlave {slave_host} is now ready for bisection")
        return 0

    print("\n✗ Deployment failed!")
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
    parser_init.add_argument("good_commit", help="Known good commit")
    parser_init.add_argument("bad_commit", help="Known bad commit")
    parser_init.add_argument("--slave-host", help="Slave hostname (override config)")
    parser_init.add_argument("--test-type", choices=["boot", "custom"], help="Test type")
    parser_init.add_argument(
        "--force-deploy",
        action="store_true",
        help="Force deployment even if auto_deploy is disabled",
    )
    parser_init.add_argument("--kernel-config", help="Path to kernel .config file to use as base")
    parser_init.add_argument(
        "--use-running-config", action="store_true", help="Use running kernel config as base"
    )

    # start command
    parser_start = subparsers.add_parser("start", help="Start bisection")
    parser_start.add_argument("good_commit", nargs="?", help="Known good commit")
    parser_start.add_argument("bad_commit", nargs="?", help="Known bad commit")
    parser_start.add_argument("--test-type", choices=["boot", "custom"], help="Test type")
    parser_start.add_argument("--test-script", help="Custom test script path")
    parser_start.add_argument("--reinit", action="store_true", help="Reinitialize bisection")
    parser_start.add_argument("--kernel-config", help="Path to kernel .config file to use as base")
    parser_start.add_argument(
        "--use-running-config", action="store_true", help="Use running kernel config as base"
    )

    # status command
    parser_status = subparsers.add_parser("status", help="Show bisection status")

    # report command
    parser_report = subparsers.add_parser("report", help="Generate bisection report")
    parser_report.add_argument("--session-id", type=int, help="Session ID (default: latest)")
    parser_report.add_argument(
        "--format", choices=["text", "json"], default="text", help="Report format"
    )
    parser_report.add_argument("--output", "-o", help="Output file (default: stdout)")

    # monitor command
    parser_monitor = subparsers.add_parser("monitor", help="Monitor slave health")
    parser_monitor.add_argument(
        "--continuous", action="store_true", help="Continuous monitoring"
    )
    parser_monitor.add_argument(
        "--interval", type=int, default=5, help="Check interval in seconds"
    )

    # ipmi command
    parser_ipmi = subparsers.add_parser("ipmi", help="IPMI control")
    parser_ipmi.add_argument(
        "ipmi_command",
        choices=["status", "on", "off", "reset", "cycle"],
        help="IPMI command",
    )

    # deploy command
    parser_deploy = subparsers.add_parser("deploy", help="Deploy slave components")
    parser_deploy.add_argument("--slave-host", help="Slave hostname (override config)")
    parser_deploy.add_argument(
        "--verify-only", action="store_true", help="Only verify deployment, do not deploy"
    )
    parser_deploy.add_argument(
        "--update-only", action="store_true", help="Only update library, do not full deploy"
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
