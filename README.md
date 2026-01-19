# kbisect - Automated Kernel Bisection Tool

Automatically find the exact kernel commit that introduced a bug. The tool handles building, rebooting, testing, and failure recovery without manual intervention.

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

## Installation

### 1. Install on Control Machine

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

### 2. Setup SSH Keys

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


## Configuration

Each bisection case gets its own directory with its own config file.

```bash
# Create directory for your bisection
mkdir ~/my-bisection
cd ~/my-bisection

# Generate config file
kbisect init-config

# Edit the config
vim bisect.yaml
```

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

Configure the test script in your bisect.yaml:

```yaml
test:
  type: custom
  script: ./test-network.sh  # Path to your test script
```

Then run bisection:

```bash
chmod +x test-network.sh
kbisect init v6.1 v6.6
kbisect start
```

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
- ✗ Does NOT install the kernel
- ✗ Does NOT reboot hosts
- ✗ Does NOT run tests
- ✗ Does NOT require `kbisect init` first

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

## Advanced Usage

# Kernel configuration
kernel_config:
  # Path to base .config file on control machine (optional)
  # Can be absolute or relative to config file location
  # File will be automatically transferred to all test hosts during initialization
  # If not specified, kernel defaults are used
  config_file: null

# Kernel protection
protection:
  auto_lock_current_kernel: true    # Lock current kernel at init
  verify_protected_after_cleanup: true  # Verify protection after cleanup

# Test configuration
tests:
  - type: boot                      # Boot success test (always runs)

  - type: custom                    # Custom test script
    path: /root/kernel_build_and_install/reproducers/my-test.sh
    enabled: false

# Cleanup strategy
cleanup:
  mode: aggressive                  # aggressive | conservative
  only_delete_bisect_tagged: true  # Only delete kernels tagged with -bisect-
  verify_before_delete: true       # Triple-check before deletion

# State and Database (per-directory isolation)
# Default: files stored in current working directory
database_path: bisect.db            # SQLite database file (default: ./bisect.db)
state_dir: .                        # Directory for metadata/configs (default: current directory)

# Metadata collection
metadata:
  collect_baseline: true            # Collect system metadata at session start
  collect_per_iteration: true       # Collect kernel metadata per iteration
  collect_kernel_config: true       # Include kernel .config in metadata
  collect_packages: true            # Include rpm -qa / dpkg -l
  compress_large_data: true         # Gzip large metadata files
  # Note: Metadata is stored in state_dir (default: current directory)

# Console log collection (captures boot process output)
console_logs:
  enabled: false                    # Enable console log collection during boot
  collector: "auto"                 # "conserver" | "ipmi" | "auto"
  # Override hostname for console connection (default: uses test host hostname)
  hostname: null
  # Fall back to IPMI SOL if conserver fails (default: true)
  fallback_to_ipmi: true
  # Console logs are stored in database (build_logs table, log_type="console")
  # View with: kbisect logs list --log-type console
  # Requires: 'console' command (conserver) or IPMI configured

# Note: Application logs are printed to terminal (stdout/stderr)
# Use --verbose flag for DEBUG level output: kbisect --verbose start
# Build logs and console logs are stored in the database (see console_logs above)
```

Configure kernel config in your bisect.yaml before running init:

```yaml
# Option 1: Provide config file
kernel_config:
  config_file: /path/to/.config

# Option 2: Use running kernel's config
kernel_config:
  use_running_config: true
```

Then run bisection:

```bash
kbisect init v6.1 v6.6
kbisect start
```

### Multi-Host Configuration

Configure multiple test hosts for parallel bisection:

```yaml
# Example: Network testing with server and client roles
hosts:
  - hostname: server1.example.com
    ssh_user: root
    kernel_path: /root/kernel
    bisect_path: /root/kernel-bisect/lib
    test_script: test-server.sh      # Server-specific test
    power_control_type: ipmi
    ipmi_host: ipmi1.example.com
    ipmi_user: admin
    ipmi_password: secret1

  - hostname: client1.example.com
    ssh_user: root
    kernel_path: /root/kernel
    bisect_path: /root/kernel-bisect/lib
    test_script: test-client.sh      # Client-specific test
    power_control_type: beaker       # Different power control type

  - hostname: client2.example.com
    ssh_user: root
    kernel_path: /root/kernel
    test_script: test-client.sh
    power_control_type: null         # SSH fallback

# All hosts build, reboot, and test in parallel
# All must pass for commit to be marked GOOD
```

### Per-Host Kernel Configurations

Each host can use a different kernel config:

```yaml
# Global config (applies to all hosts unless overridden)
kernel_config:
  config_file: /tmp/baseline-config

hosts:
  - hostname: server1.example.com
    kernel_config_file: /tmp/server-config   # Override for this host

  - hostname: client1.example.com
    kernel_config_file: /tmp/client-config   # Different config for this host

  - hostname: client2.example.com
    # Uses global kernel_config
```

**Use cases:**
- Testing same kernel with different config options
- Hardware-specific config requirements
- Debug builds on some hosts, production builds on others

### Monitor test host health

```bash
# One-time check
kbisect monitor

# Continuous monitoring
kbisect monitor --continuous --interval 5
```

### View build logs

```bash
# List all logs
kbisect logs list

# View specific log
kbisect logs show <log-id>

# View logs for iteration
kbisect logs iteration 3

# Follow log in real-time
kbisect logs tail <log-id>

# Export log to file
kbisect logs export <log-id> /tmp/build.log
```

### View metadata

```bash
# List all metadata
kbisect metadata list

# Show specific metadata
kbisect metadata show <metadata-id>

# Export metadata to file
kbisect metadata export <metadata-id> -o metadata.json

# Export metadata file
kbisect metadata export-file <file-id> -o config.txt
```

### Manual IPMI control

```bash
# Check power status
kbisect ipmi status

# Power cycle
kbisect ipmi cycle

# Power off
kbisect ipmi off
```

### Deployment options

```bash
# At init, current running kernel is protected
# Protected file list: /var/lib/kernel-bisect/protected-kernels.list
# Contains:
#   /boot/vmlinuz-6.5.0-production
#   /boot/initramfs-6.5.0-production.img
#   /lib/modules/6.5.0-production/
```

### Triple Verification Before Deletion

Every cleanup operation checks:

1. ✓ Is file in protected list?
2. ✓ Is this the currently running kernel?
3. ✓ Does filename contain "-bisect-" tag?
4. ✓ Final confirmation before deletion
5. ✓ Verify protected kernels still exist after cleanup

### State Persistence

**SQLite database** stores complete state:

- Session info (good/bad commits, status)
- All iterations (commit, result, duration, errors)
- Metadata (system info, kernel configs)
- Logs (detailed per-iteration logs)

**Location:** `./bisect.db` (in your bisection directory)

**Survives:**
- Control machine crashes/reboots
- Network interruptions
- Power failures (test host reboots to safe kernel)
- Manual cancellation

### Non-Interactive Builds

**Kernel config handling:**

- Uses `make olddefconfig` (not `make oldconfig`)
- New config options get defaults automatically
- **No prompts** - fully automated
- Supports custom base configs for consistency

---

## Architecture

### System Design

**Control-Test Architecture:**

- **Control Machine**: Python orchestration engine, CLI interface, state management
- **Test Host(s)**: Bash library with functions, no autonomous processes
- **Communication**: SSH only (no HTTP, no agents, no daemons)

**Why this design?**

- ✅ Simple: One bash library file on each test host
- ✅ Stateless test hosts: Control machine orchestrates everything
- ✅ Reliable: SSH is mature and well-tested
- ✅ Secure: Standard SSH authentication and encryption
- ✅ Maintainable: All logic in master Python code

### Data Flow

```
1. Master: kbisect init v6.1 v6.6
   ↓
2. Control: Check if test hosts deployed
   ↓
3. Control: Deploy lib/bisect-functions.sh to all test hosts
   ↓
4. Master: SSH call: init_protection()
   ↓
5. Master: Create session in SQLite
   ↓
6. Master: Collect baseline metadata
   ↓
7. Loop: For each commit from git bisect
   ├─ Master: SSH call: build_kernel(commit_sha)
   ├─ Test Host: Build kernel, install, set one-time boot (grub-reboot)
   ├─ Control: Store build log in SQLite (gzip compressed)
   ├─ Control: Start console log collection (if enabled) - conserver or IPMI SOL
   ├─ Control: Reboot test host
   ├─ Master: Wait for SSH (boot detection)
   ├─ Master: Stop console collection, store boot log in SQLite
   ├─ Master: Verify kernel version (detect panics)
   ├─ Master: Download /boot/config-<version>
   ├─ Master: SSH call: collect_metadata("iteration")
   ├─ Master: SSH call: run_test(test_type)
   ├─ Master: Record result in SQLite
   ├─ Master: Mark commit good/bad in git bisect
   └─ Continue until bisection complete
   ↓
8. Master: Generate report with first bad commit
```

### File Structure

```
kernel-bisect/
├── kbisect                       # Main CLI tool
├── config/
│   └── bisect.conf.example       # Configuration template
├── lib/
│   └── bisect-functions.sh       # Bash library (deployed to test hosts)
├── master/
│   ├── bisect_master.py          # Main orchestration
│   ├── state_manager.py          # SQLite state management
│   ├── host_deployer.py          # Automatic deployment
│   ├── host_monitor.py           # Boot detection
│   ├── ipmi_controller.py        # IPMI power control
│   └── console_collector.py      # Console log collection (conserver/IPMI SOL)
└── README.md

Deployed to test host(s):
/root/kernel-bisect/lib/bisect-functions.sh
/var/lib/kernel-bisect/protected-kernels.list  # Protected kernel list (on test host)
/var/lib/kernel-bisect/safe-kernel.info         # Safe kernel info (on test host)

Created in bisection directory (master):
your-bisection-dir/
├── bisect.yaml                   # Configuration file
├── bisect.db                     # SQLite database (contains all metadata and logs)
└── configs/                      # Kernel .config files
    ├── config-6.5.0-bisect-a1b2c3d
    └── config-6.5.0-bisect-d4e5f6g
# Note: Application logs are printed to terminal
# Build logs and console logs are stored in the database
```

### Database Schema

**Tables:**

```sql
-- Bisection sessions
sessions (
  session_id, good_commit, bad_commit,
  start_time, end_time, status, result_commit, config
)

-- Test iterations
iterations (
  iteration_id, session_id, iteration_num, commit_sha, commit_message,
  build_result, boot_result, test_result, final_result,
  start_time, end_time, duration, error_message, kernel_version
)

-- System metadata
metadata (
  metadata_id, session_id, iteration_id,
  collection_time, collection_type, metadata_json, metadata_hash
)

-- Kernel config files
metadata_files (
  file_id, metadata_id, file_type, file_path,
  file_hash, file_size, compressed
)

-- Detailed logs
logs (
  log_id, iteration_id, log_type, timestamp, message
)

-- Build and console logs (compressed)
build_logs (
  log_id, iteration_id, log_type, timestamp,
  log_content (BLOB), compressed, size_bytes, exit_code
)
```

---

## Examples

### Example 1: Boot Regression

```bash
# Problem: Kernel won't boot after upgrading from v6.1 to v6.6

kbisect init v6.1 v6.6
kbisect start

# Wait for completion...
# Result: First bad commit found: commit abc123def456
```

### Example 2: Network Performance Regression (Multi-Host)

```bash
# Problem: Network throughput dropped from 10Gbps to 5Gbps between kernel versions
# Solution: Use multi-host setup with server and client roles

# 1. Create server test script
cat > test-server.sh << 'EOF'
#!/bin/bash
# Start iperf3 server in background
iperf3 -s -D
sleep 2
echo "Server started"
exit 0  # Server always returns GOOD
EOF
chmod +x test-server.sh

# 2. Create client test script
cat > test-client.sh << 'EOF'
#!/bin/bash
# Measure network throughput against server
THROUGHPUT=$(iperf3 -c server1.example.com -t 10 -J | jq '.end.sum_received.bits_per_second')
THRESHOLD=8000000000  # 8 Gbps

if [ "$THROUGHPUT" -lt "$THRESHOLD" ]; then
    echo "Performance regression: ${THROUGHPUT}bps < ${THRESHOLD}bps"
    exit 1  # BAD
else
    echo "Performance OK: ${THROUGHPUT}bps"
    exit 0  # GOOD
fi
EOF
chmod +x test-client.sh

# 3. Configure multi-host bisection
cat > bisect.yaml << 'EOF'
hosts:
  - hostname: server1.example.com      # Server role
    ssh_user: root
    kernel_path: /root/kernel
    test_script: test-server.sh
    power_control_type: ipmi
    ipmi_host: ipmi1.example.com
    ipmi_user: admin
    ipmi_password: secret1

  - hostname: client1.example.com      # Client role
    ssh_user: root
    kernel_path: /root/kernel
    test_script: test-client.sh
    power_control_type: ipmi
    ipmi_host: ipmi2.example.com
    ipmi_user: admin
    ipmi_password: secret2

kernel_repo:
  source: https://git.kernel.org/pub/scm/linux/kernel/git/torvalds/linux.git
EOF

# 4. Run multi-host bisection
kbisect init v6.1 v6.6
kbisect start

# Both hosts will build, reboot, and test in parallel
# If client test fails (performance regression), commit marked BAD
# If both pass, commit marked GOOD

# 5. View results
kbisect report
```

### Example 3: Build-Only Testing

```bash
# Problem: Want to verify a commit builds before running full bisection

# 1. Configure bisect.yaml
cat > bisect.yaml << 'EOF'
hosts:
  - hostname: 192.168.1.100
    ssh_user: root
    kernel_path: /root/kernel

kernel_config:
  config_file: /tmp/debug.config  # Custom config with DEBUG options
EOF

# 2. Test build for specific commit
kbisect build v6.6 --save-logs

# Output:
# ✓ Commit exists on all hosts
# Building kernel on 1 hosts...
# ✓ Build complete on all hosts
# Build logs saved. View with: kbisect logs list --session-id 1

# 3. If build succeeds, proceed with bisection
kbisect init v6.1 v6.6
kbisect start
```

### Example 4: Using Custom Kernel Config

```bash
# Problem: Need to test with specific kernel config (DEBUG options enabled)

# 1. Create custom config on control machine
scp root@<test-host-ip>:/boot/config-$(uname -r) /tmp/debug.config
vim /tmp/debug.config
# Add: CONFIG_DEBUG_INFO=y
#      CONFIG_DEBUG_KERNEL=y

# 2. Configure bisection to use custom config
cat > bisect.yaml <<EOF
hosts:
  - hostname: 192.168.1.100
    ssh_user: root
    kernel_path: /root/kernel

kernel_config:
  config_file: /tmp/debug.config  # Path on control machine (auto-transferred)
EOF

# 3. Run bisection
kbisect init v6.1 v6.6
kbisect start

# 4. All kernels built with DEBUG config across all hosts
```

---

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
- **Beaker**: For hosts managed in Beaker lab automation systems. Requires Kerberos authentication.
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

MIT License

---

**Need help?** File an issue at https://github.com/janjurca/kbisect/issues
