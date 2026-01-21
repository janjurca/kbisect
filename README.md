# kbisect - Automated Kernel Bisection Tool

Automatically find the exact kernel commit that introduced a bug. The tool handles building, rebooting, testing, and failure recovery without manual intervention.

## Table of Contents

- [Installation](#installation)
- [Quick Start](#quick-start)
- [Prerequisites](#prerequisites)
- [Configuration](#configuration)
  - [Quick Start: Minimum Single-Host Configuration](#quick-start-minimum-single-host-configuration)
  - [Multi-Host Configuration](#multi-host-configuration)
  - [Power Control Options](#power-control-options)
  - [Key Optional Settings](#key-optional-settings)
- [Usage](#usage)
  - [Basic Bisection](#basic-bisection-boot-test)
  - [Using a Custom Kernel Config](#using-a-custom-kernel-config)
  - [Running Custom Tests](#running-custom-tests)
  - [Advanced: Multi-Host Bisection](#advanced-multi-host-bisection)
  - [Build-Only Mode](#build-only-mode)
  - [Resume After Interruption](#resume-after-interruption)
- [How It Works](#how-it-works)
- [Common Issues](#common-issues)
- [Additional Commands](#additional-commands)
- [FAQ](#faq)

## Installation

### Using pipx (Recommended)

[pipx](https://pipx.pypa.io/) installs kbisect in an isolated environment, preventing dependency conflicts:

```bash
# Install pipx if you don't have it
python3 -m pip install --user pipx
python3 -m pipx ensurepath

# Install kbisect
pipx install git+https://github.com/janjurca/kbisect.git

# Verify installation
kbisect --help
```

### Using pip (Alternative)

```bash
# Install system dependencies
sudo dnf install python3 python3-pip ipmitool git  # RHEL/Fedora
# OR
sudo apt install python3 python3-pip ipmitool git  # Debian/Ubuntu

# Optional: Install Beaker client (if using Beaker lab automation)
sudo dnf install beaker-client  # RHEL/Fedora

# Install kbisect
pip install git+https://github.com/janjurca/kbisect.git

# Verify installation
kbisect --help
```

### Setup SSH Keys

```bash
# Generate SSH key (if you don't have one)
ssh-keygen -t ed25519

# Copy to test host(s) - enables passwordless SSH
ssh-copy-id root@<test-host-ip>

# For multiple hosts, repeat for each
ssh-copy-id root@<test-host-2-ip>

# Test connection
ssh root@<test-host-ip> 'echo "SSH works"'
```

## Quick Start

Get started with a basic bisection in 5 steps:

```bash
# 1. Create a directory for your bisection
mkdir ~/my-bisection
cd ~/my-bisection

# 2. Generate configuration file
kbisect init-config

# 3. Edit bisect.yaml with your test host details
vim bisect.yaml
```

**Minimal configuration** (edit the generated `bisect.yaml`):

```yaml
hosts:
  - hostname: 192.168.1.100      # YOUR TEST HOST IP
    ssh_user: root
    kernel_path: /root/kernel

    # Optional but recommended: IPMI power control
    power_control_type: ipmi
    ipmi_host: 192.168.1.101     # YOUR IPMI IP
    ipmi_user: admin
    ipmi_password: changeme
```

```bash
# 4. Initialize bisection range
kbisect init v6.1 v6.6

# 5. Start automatic bisection
kbisect start

# Monitor progress (from another terminal)
kbisect status

# When complete, view results
kbisect report
```

The report will show the exact commit that introduced the problem.

## Prerequisites

**Control Machine** (where you run kbisect):
- Python 3.8+
- SSH access to test host(s) (passwordless, as root)
- Power control tools (optional but recommended):
  - `ipmitool` for IPMI-based power control, OR
  - `bkr` (Beaker client) for lab automation systems, OR
  - Neither (falls back to SSH reboot)

**Test Host(s)** (where kernels are built and tested):
- Linux system(s) where kernels will be built and tested
- Kernel source at `/root/kernel` (auto-deployed or manual git clone)
- Power management interface (optional but recommended):
  - IPMI (best for reliability and recovery)
  - Beaker lab system integration
  - SSH access (minimum requirement)
- **Multi-host support**: Configure multiple test hosts for parallel bisection (e.g., network testing with server/client roles)

## Configuration

### Quick Start: Minimum Single-Host Configuration

For a simple single-host setup, configure one test host in the `hosts` array:

```yaml
# Simplest configuration for single host
hosts:
  - hostname: 192.168.1.100      # YOUR TEST HOST IP
    ssh_user: root
    kernel_path: /root/kernel
    test_script: test.sh

    # Optional but recommended: IPMI power control
    power_control_type: ipmi
    ipmi_host: 192.168.1.101     # YOUR IPMI IP
    ipmi_user: admin
    ipmi_password: changeme
```

**Note**: Even for single-host setups, the `hosts` array is used. This provides consistency and easy expansion to multi-host later.

### Multi-Host Configuration

For network testing or scenarios requiring multiple hosts (e.g., server/client pairs), configure multiple hosts with role-specific test scripts:

```yaml
# Multi-host example: Network performance testing
hosts:
  - hostname: server1.example.com
    ssh_user: root
    kernel_path: /root/kernel
    bisect_path: /root/kernel-bisect/lib
    test_script: test-server.sh      # Server role: runs iperf3 server

    power_control_type: ipmi
    ipmi_host: ipmi1.example.com
    ipmi_user: admin
    ipmi_password: secret1

  - hostname: client1.example.com
    ssh_user: root
    kernel_path: /root/kernel
    bisect_path: /root/kernel-bisect/lib
    test_script: test-client.sh      # Client role: runs iperf3 client

    power_control_type: beaker       # Different power control type
```

**How multi-host bisection works:**
- All hosts build kernels **in parallel** (faster iteration)
- All hosts reboot **in parallel** with their configured power controllers
- All hosts run tests **in parallel** with role-specific test scripts
- **Aggregation**: ALL hosts must pass for commit to be marked GOOD; if ANY host fails → BAD

### Power Control Options

Each host can use different power control mechanisms. Choose based on your infrastructure:

#### Option 1: IPMI (Recommended)
Best for reliability, supports hard reset and recovery:

```yaml
hosts:
  - hostname: 192.168.1.100
    power_control_type: ipmi
    ipmi_host: 192.168.1.101         # IPMI interface IP
    ipmi_user: admin
    ipmi_password: changeme
```

**Test IPMI setup:**
```bash
kbisect check                         # Validates IPMI configuration
kbisect ipmi status                   # Check power status
```

#### Option 2: Beaker Lab Automation
For hosts managed by Beaker lab systems:

```yaml
hosts:
  - hostname: system.example.com      # FQDN as registered in Beaker
    power_control_type: beaker
    # No additional config needed - uses Kerberos auth
```

**Prerequisites:**
- `bkr` command installed on control machine
- Active Kerberos ticket: `kinit your-username@REALM`
- Verify: `bkr whoami`

#### Option 3: SSH Fallback
No external power control, uses SSH reboot command:

```yaml
hosts:
  - hostname: 192.168.1.100
    power_control_type: null          # or omit this field
    # Falls back to SSH reboot command
```

**Limitations**: Cannot force power-off or hard reset (reboot only). Best for development or hosts without IPMI/Beaker.

### Key Optional Settings

```yaml
# Automatic kernel repository deployment
# If configured, kbisect will clone/copy the kernel repo to all test hosts
kernel_repo:
  source: https://git.kernel.org/pub/scm/linux/kernel/git/torvalds/linux.git
  branch: master  # Optional: specific branch to checkout

# Global kernel config (applies to all hosts unless overridden)
kernel_config:
  config_file: /path/to/.config  # Path on control machine (auto-transferred)

# Per-host kernel config override (in hosts array)
hosts:
  - hostname: 192.168.1.100
    kernel_config_file: /path/to/host1-config  # Override for this host

# Timeouts (apply to all hosts)
timeouts:
  boot: 300    # Seconds to wait for boot per host
  build: 1800  # Seconds to wait for build per host (parallel builds)
  test: 600    # Seconds to wait for test per host
  ssh_connect: 15  # SSH connection timeout

# Console log collection (per-host, requires conserver or IPMI)
console_logs:
  enabled: true
  collector: "auto"  # Try conserver, fall back to IPMI SOL
```

**Note:** Console log collection is currently only supported for single-host configurations. Multi-host console support is not yet implemented.

**Note:** If `kernel_repo` is not configured, you must manually clone the kernel source to `/root/kernel` on each test host before running init.

## Usage

### Basic Bisection (Boot Test)

Find which commit breaks kernel boot:

```bash
# Initialize bisection
kbisect init v6.1 v6.6

# Start automatic bisection
kbisect start

# Check progress (from another terminal)
kbisect status

# When complete, view results
kbisect report
```

The report will show the **first bad commit** - the exact commit that introduced the problem.

### Using a Custom Kernel Config

**Problem:** Different kernel versions have different config options. You want consistent builds.

**Solution:** Provide a base `.config` file.

**Option 1: Use a specific config file**

```bash
# Save your known-good config from test host to control machine
scp root@<test-host-ip>:/boot/config-$(uname -r) /tmp/my-config

# Configure it in bisect.yaml
cat > bisect.yaml <<EOF
kernel_config:
  config_file: /tmp/my-config  # Path on control machine (auto-transferred to hosts)
EOF

kbisect start
```

**Option 2: Relative path in config file**

```yaml
# bisect.yaml (in your bisection directory)
kernel_config:
  config_file: my-baseline.config  # Relative to config file location
```

**How it works:**
1. Config file is read from **control machine**
2. File is automatically transferred to all test hosts during initialization
3. Base `.config` is copied to kernel source on each test host
4. `make olddefconfig` runs (handles new/removed options automatically)
5. New options get default values (non-interactive - no prompts!)
6. Kernel builds with consistent config across all hosts

### Running Custom Tests

**Default test (no test script specified):**

### Custom Test Script

For specific bugs (network issues, performance regressions, etc.), provide a test script:

```bash
#!/bin/bash
# test-network.sh
# Exit 0 = kernel is GOOD, Exit 1 = kernel is BAD

ping -c 5 8.8.8.8 > /dev/null 2>&1
if [ $? -eq 0 ]; then
    echo "Network works"
    exit 0  # GOOD
else
    echo "Network broken"
    exit 1  # BAD
fi
```

Configure the test in your bisect.yaml:

**Single-host example:**
```yaml
# Enable custom testing mode
test:
  type: custom

# Specify test script for the host
hosts:
  - hostname: 192.168.1.100
    ssh_user: root
    kernel_path: /root/kernel
    test_script: ./test-network.sh  # Custom test script
```

**Multi-host example (different tests per host):**
```yaml
# Enable custom testing mode
test:
  type: custom

# Each host can run a different test script
hosts:
  - hostname: server1.example.com
    ssh_user: root
    kernel_path: /root/kernel
    test_script: ./test-server.sh   # Server-specific test

  - hostname: client1.example.com
    ssh_user: root
    kernel_path: /root/kernel
    test_script: ./test-client.sh   # Client-specific test
```

Then run bisection:

```bash
chmod +x test-network.sh  # Make test script executable
# (or test-server.sh and test-client.sh for multi-host)
kbisect init v6.1 v6.6
kbisect start
```

**Note:** The global `test: type: custom` enables custom testing mode, while per-host `test_script:` specifies which script to run on each host. This allows different hosts to run different test scripts (e.g., server vs. client roles).

**Note:** When using custom tests, kernels that fail to boot are automatically marked as SKIP (can't test functionality if kernel doesn't boot).

### Advanced: Multi-Host Bisection

For bugs that require multiple hosts (network issues, distributed systems, etc.), kbisect supports parallel bisection across multiple test hosts:

**Example: Network Performance Regression**

```yaml
# bisect.yaml
hosts:
  - hostname: server1.example.com
    test_script: test-server.sh      # Server role
    power_control_type: ipmi
    ipmi_host: ipmi1.example.com
    ipmi_user: admin
    ipmi_password: secret1

  - hostname: client1.example.com
    test_script: test-client.sh      # Client role
    power_control_type: ipmi
    ipmi_host: ipmi2.example.com
    ipmi_user: admin
    ipmi_password: secret2
```

**Test Scripts:**

```bash
# test-server.sh (on server host)
#!/bin/bash
# Start iperf3 server in background
iperf3 -s -D
sleep 2
exit 0  # Server always returns GOOD

# test-client.sh (on client host)
#!/bin/bash
# Run iperf3 client against server
THROUGHPUT=$(iperf3 -c server1.example.com -t 10 -J | jq '.end.sum_received.bits_per_second')
THRESHOLD=8000000000  # 8 Gbps

if [ "$THROUGHPUT" -lt "$THRESHOLD" ]; then
    echo "Performance regression: ${THROUGHPUT}bps < ${THRESHOLD}bps"
    exit 1  # BAD
else
    echo "Performance OK: ${THROUGHPUT}bps"
    exit 0  # GOOD
fi
```

**How it works:**

1. **Parallel Build**: All hosts build the kernel simultaneously
2. **Parallel Reboot**: All hosts reboot with their configured power controllers
3. **Parallel Tests**: Each host runs its role-specific test script
4. **Aggregation**:
   - ALL hosts PASS → Commit marked **GOOD**
   - ANY host FAILS → Commit marked **BAD**
   - ANY host SKIPS → Commit marked **SKIP**

**Sample Output:**

```
=== Iteration 5: abc123def ===
Building kernel on 2 hosts...
  [server1.example.com] Build successful: 6.5.0-bisect-abc123d
  [client1.example.com] Build successful: 6.5.0-bisect-abc123d
✓ All hosts built successfully

Rebooting 2 hosts...
  [server1.example.com] Reboot successful
  [client1.example.com] Reboot successful
✓ All hosts rebooted

Running tests on 2 hosts...
  [server1.example.com] Test PASSED
  [client1.example.com] Test FAILED: Performance regression
✗ Failed on: client1.example.com - marking commit BAD
```

### Build-Only Mode

Test kernel compilation without running a full bisection cycle. Useful for:
- Pre-build validation before starting bisection
- Testing custom kernel configs
- CI/CD integration
- Debugging build failures

**Basic Usage:**

```bash
# Build a specific commit on all configured hosts
kbisect build abc123def

# Build with logs saved to database
kbisect build v6.6 --save-logs

# Build supports short or full commit SHAs, tags, or branch names
kbisect build HEAD
kbisect build v6.5.0
kbisect build abc123def456789abc123def456789abc123def45
```

**What it does:**
- ✓ Validates commit exists on all hosts
- ✓ Builds kernel in parallel on all configured hosts
- ✓ Applies configured kernel config (if any)
- ✓ Installs kernel to /boot (runs `make modules_install` and `make install`)
- ✗ Does NOT set one-time boot (no grub-reboot)
- ✗ Does NOT reboot hosts
- ✗ Does NOT run tests
- ✗ Does NOT require `kbisect init` first

**Important:** The kernel is installed to `/boot` but won't be the default boot option until you manually configure it or reboot and select it from the boot menu.

**View Build Logs:**

```bash
# Build with saved logs
kbisect build abc123def --save-logs

# List build logs
kbisect logs list

# View specific build log
kbisect logs show <log-id>

# View logs for a specific session
kbisect logs list --session-id <session-id>
```

**Example: Testing Kernel Config**

```yaml
# bisect.yaml
hosts:
  - hostname: 192.168.1.100

kernel_config:
  config_file: /tmp/debug-config  # Custom config with DEBUG options
```

```bash
# Test if kernel builds with debug config
kbisect build v6.6 --save-logs

# If successful, proceed with full bisection
kbisect init v6.1 v6.6
kbisect start
```

### Resume After Interruption

State is saved in SQLite. Resume anytime with:

```bash
kbisect start
```

## How It Works

### Single-Host Architecture

```
┌──────────────────┐                    ┌──────────────────┐
│ Control Machine  │────────SSH─────────│   Test Host      │
│                  │                    │                  │
│  • Orchestrates  │                    │  • Builds kernel │
│  • Makes         │                    │  • Boots kernel  │
│    decisions     │                    │  • Runs tests    │
│  • Stores state  │                    │                  │
│    in SQLite     │                    │                  │
└────────┬─────────┘                    └──────────────────┘
         │                                       ▲
         │    Power Control (IPMI/Beaker/SSH)   │
         └───────────────────────────────────────┘
```

### Multi-Host Architecture

```
                    ┌─────────────────────┐
                    │  Control Machine    │
                    │  • Orchestrates     │
                    │  • Parallel builds  │
                    │  • Aggregates tests │
                    │  • Stores state     │
                    └──────────┬──────────┘
                               │
         ┌─────────────────────┼─────────────────────┐
         │ SSH                 │ SSH                 │ SSH
         ▼                     ▼                     ▼
  ┌──────────────┐      ┌──────────────┐     ┌──────────────┐
  │  Test Host 1 │      │  Test Host 2 │     │  Test Host N │
  │              │      │              │     │              │
  │ • Build      │      │ • Build      │     │ • Build      │
  │ • Boot       │      │ • Boot       │     │ • Boot       │
  │ • Test       │      │ • Test       │     │ • Test       │
  └──────┬───────┘      └──────┬───────┘     └──────┬───────┘
         ▲                     ▲                     ▲
         │ IPMI                │ Beaker              │ SSH
         └─────────────────────┴─────────────────────┘
```

**Workflow for each commit:**

1. Control machine deploys bash library to all test hosts (first run only)
2. Protects current kernel(s) from deletion on all hosts
3. For each commit in the bisection range:
   - **Phase 1 - Build** (parallel): Builds kernel on all test hosts simultaneously
   - **Phase 2 - Install & Reboot** (parallel): Installs kernel with one-time boot (grub-reboot) and reboots all hosts
   - **Phase 3 - Wait** (parallel): Waits for all hosts to boot (SSH connectivity check)
   - **Phase 4 - Test** (parallel): Runs role-specific tests on all hosts simultaneously
   - **Phase 5 - Aggregate**: Collects results from all hosts and marks commit:
     - ALL pass → GOOD
     - ANY fail → BAD
     - ANY skip → SKIP
4. Reports the exact commit that introduced the bug

**Multi-host benefits:**
- Parallel execution reduces total bisection time
- Test distributed systems and network interactions
- Each host can use different power control mechanisms
- Per-host metadata and logs for debugging

**Recovery from failures:**
- Kernel panics: One-time boot falls back to protected kernel automatically
- Boot timeouts: Power control handles recovery (IPMI reset, Beaker reboot, or SSH)
- Build failures: Automatically marked as skip
- Per-host failure isolation: Other hosts continue if one fails

## Common Issues

### Test host won't boot after kernel install

```bash
# Force power cycle (IPMI)
kbisect ipmi cycle

# Or manually via IPMI console
ipmitool -I lanplus -H <ipmi-ip> -U <user> -P <pass> sol activate

# For Beaker systems
bkr system-power --action reboot --force <hostname>

# For SSH-only (requires host to be responsive)
ssh root@<test-host-ip> 'reboot'
```

### Build fails with "No space left on device"

```bash
# Check disk space on test host
ssh root@<test-host-ip> 'df -h /boot'

# Manual cleanup (keeps only 1 test kernel)
ssh root@<test-host-ip> 'source /root/kernel-bisect/lib/bisect-functions.sh && KEEP_TEST_KERNELS=1 cleanup_old_kernels'
```

### SSH connection fails

```bash
# Verify passwordless SSH works
ssh root@<test-host-ip> 'echo test'  # Should print "test" without password prompt

# If needed, re-copy SSH key
ssh-copy-id root@<test-host-ip>

# For multiple hosts, repeat for each
for host in host1 host2 host3; do
    ssh-copy-id root@$host
done
```

### Build dependencies missing on test host

```bash
# RHEL/Fedora
ssh root@<test-host-ip> 'dnf groupinstall "Development Tools" && dnf install ncurses-devel bc bison flex elfutils-libelf-devel openssl-devel'

# Debian/Ubuntu
ssh root@<test-host-ip> 'apt install build-essential libncurses-dev bc bison flex libelf-dev libssl-dev'

# For multiple hosts, use a loop
for host in host1 host2 host3; do
    ssh root@$host 'dnf groupinstall "Development Tools" && dnf install ncurses-devel bc bison flex elfutils-libelf-devel openssl-devel'
done
```

## Global Options

All kbisect commands support these global options:

```bash
# Use custom config file (default: bisect.yaml)
kbisect -c /path/to/config.yaml <command>

# Enable verbose/debug output
kbisect --verbose <command>
kbisect -v <command>

# Example
kbisect -v -c my-config.yaml start
```

### Additional Commands

**Build-Only Mode:**
```bash
# Build kernel without bisection
kbisect build <commit>              # Build specific commit
kbisect build <commit> --save-logs  # Build and save logs to database

# View build logs
kbisect logs list                    # List all logs
kbisect logs show <log-id>           # View specific log
```

**Power Control (IPMI):**
```bash
# IPMI commands (requires IPMI configured for at least one host)
kbisect ipmi status                  # Check power status
kbisect ipmi on                      # Power on
kbisect ipmi off                     # Power off
kbisect ipmi reset                   # Hard reset
kbisect ipmi cycle                   # Power cycle (off → wait → on)

# Note: For multi-host setups with multiple IPMI hosts,
# the first configured host is used
```

**Configuration Validation:**
```bash
# Validate configuration and check host connectivity
kbisect check                        # Validates:
                                     # - SSH connectivity to all hosts
                                     # - Power controller health (IPMI/Beaker)
                                     # - Kernel source availability
                                     # - Build dependencies
```

**Monitoring:**
```bash
# Monitor host health
kbisect monitor                      # One-time check
kbisect monitor --continuous         # Continuous monitoring
kbisect monitor --interval 5         # Check every 5 seconds
```
## FAQ

**Q: How long does bisection take?**

A: Depends on the commit range. Formula: ~log2(commits) iterations.
- 10 commits: ~4 iterations
- 100 commits: ~7 iterations
- 1000 commits: ~10 iterations
- Each iteration: build time (~30 min) + boot time (~2 min) + test time
- Multi-host: Parallel builds reduce wall-clock time (all hosts build simultaneously)

**Q: Can I bisect across multiple hosts?**

A: Yes! kbisect has native multi-host support. Configure multiple hosts in the `hosts` array with role-specific test scripts. All hosts must pass their tests for a commit to be marked GOOD. If any host fails, the commit is marked BAD. This is perfect for:
- Network performance testing (server + client)
- Distributed system issues
- Hardware-specific bugs requiring multiple systems

**Q: Which power control should I use?**

A: Choose based on your infrastructure:
- **IPMI** (recommended): Most reliable, supports hard reset, power cycling, and recovery. Best for production systems with IPMI/BMC interfaces.
- **Beaker**: For hosts managed in Beaker lab automation systems. Requires Kerberos (or other beaker) authentication.
- **SSH Fallback**: Simplest option, uses SSH reboot command. No hard power control (can't force power-off). Good for development or systems without IPMI/Beaker.

You can mix power control types - each host in a multi-host setup can use a different mechanism.

**Q: Can I test if a commit builds without running bisection?**

A: Yes! Use the build-only mode:
```bash
kbisect build <commit> --save-logs
```

This builds the kernel on all configured hosts without rebooting, testing, or requiring `kbisect init`. Useful for:
- Pre-build validation before starting bisection
- Testing custom kernel configs
- CI/CD integration
- Debugging build failures

**Q: What happens if one host fails in multi-host bisection?**

A: The commit is marked BAD if ANY host fails. The aggregation logic is conservative:
- ALL hosts PASS → Commit marked GOOD
- ANY host FAILS → Commit marked BAD
- ANY host SKIPS (e.g., build failure) → Commit marked SKIP

This ensures that regressions affecting any host are caught.

### Reinitialize bisection

```bash
# Reinitialize bisection range
kbisect start --reinit
```

### Generate config file with custom name

```bash
# Default (creates bisect.yaml)
kbisect init-config

# Custom filename
kbisect init-config -o my-config.yaml

# Overwrite existing without prompt
kbisect init-config --force
```

## Project Structure

```
your-bisection-dir/
├── bisect.yaml       # Configuration
├── bisect.db         # SQLite database (state, logs, metadata)
```

## License

GPLv3 License

---

**Need help?** File an issue at https://github.com/janjurca/kbisect/issues
