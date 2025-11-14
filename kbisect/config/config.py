#!/usr/bin/env python3
"""Configuration classes for kernel bisection.

This module contains the main configuration dataclasses used throughout kbisect.
"""

from dataclasses import dataclass
from typing import List, Optional


@dataclass
class HostConfig:
    """Configuration for a single host in multi-host bisection.

    Attributes:
        hostname: Hostname or IP address
        ssh_user: SSH username
        kernel_path: Path to kernel source directory on host
        bisect_path: Path to bisect library on host
        test_script: Path to test script for this host (role-specific)
        kernel_config_file: Path to kernel config file for this host (optional)
        power_control_type: Power control method ("ipmi", "beaker", or None for SSH fallback)
        ipmi_host: Optional IPMI interface hostname or IP
        ipmi_user: Optional IPMI username
        ipmi_password: Optional IPMI password
    """

    hostname: str
    ssh_user: str = "root"
    kernel_path: str = "/root/kernel"
    bisect_path: str = "/root/kernel-bisect/lib"
    test_script: str = "test.sh"
    kernel_config_file: Optional[str] = None
    power_control_type: Optional[str] = "ipmi"
    ipmi_host: Optional[str] = None
    ipmi_user: Optional[str] = None
    ipmi_password: Optional[str] = None


@dataclass
class BisectConfig:
    """Bisection configuration for multi-host kernel bisection.

    Attributes:
        hosts: List of host configurations for multi-host bisection (REQUIRED)
        boot_timeout: Boot timeout in seconds
        test_timeout: Test timeout in seconds
        build_timeout: Build timeout in seconds
        ssh_connect_timeout: SSH connection timeout in seconds
        test_type: Test type (boot or custom)
        state_dir: Directory for state/metadata storage
        db_path: Path to SQLite database
        kernel_config_file: Path to kernel config file (optional)
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

    # Multi-host configuration (REQUIRED)
    hosts: List[HostConfig]

    # Timeouts
    boot_timeout: int = 300
    test_timeout: int = 600
    build_timeout: int = 1800
    ssh_connect_timeout: int = 15

    # Test configuration
    test_type: str = "boot"

    # State and database
    state_dir: str = "."
    db_path: str = "bisect.db"

    # Kernel configuration
    kernel_config_file: Optional[str] = None

    # Metadata collection
    collect_baseline: bool = True
    collect_per_iteration: bool = True
    collect_kernel_config: bool = True

    # Console log collection
    collect_console_logs: bool = False
    console_collector_type: str = "auto"
    console_hostname: Optional[str] = None
    console_fallback_ipmi: bool = True

    # Kernel repository (optional automatic deployment)
    kernel_repo_source: Optional[str] = None
    kernel_repo_branch: Optional[str] = None
