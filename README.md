# Automated Kernel Bisection Tool

**Automatically find the exact kernel commit that introduced a bug or performance regression.**

This tool automates the entire kernel bisection process - building kernels, rebooting systems, running tests, and handling failures - so you can go from "something broke between v6.1 and v6.6" to "this specific commit caused the problem" without manual intervention.

## Table of Contents

- [What Does This Tool Do?](#what-does-this-tool-do)
- [Quick Start](#quick-start)
- [How It Works](#how-it-works)
- [Installation](#installation)
- [Usage Guide](#usage-guide)
  - [Basic Bisection](#basic-bisection)
  - [Using a Custom Kernel Config](#using-a-custom-kernel-config)
  - [Running Custom Tests](#running-custom-tests)
  - [Monitoring Progress](#monitoring-progress)
- [Configuration](#configuration)
- [Advanced Features](#advanced-features)
- [Troubleshooting](#troubleshooting)
- [Safety Features](#safety-features)
- [Architecture](#architecture)

---

## What Does This Tool Do?

Given a "good" kernel version (works) and a "bad" kernel version (broken), this tool automatically:

1. ✅ **Deploys** itself to a test machine (slave)
2. ✅ **Protects** your production kernel from deletion
3. ✅ **Builds** kernel commits via git bisect
4. ✅ **Captures** build logs and boot console output
5. ✅ **Reboots** the test machine into new kernels
6. ✅ **Tests** each kernel (boot test or custom scripts)
7. ✅ **Recovers** from kernel panics and boot failures via IPMI (with retry logic)
8. ✅ **Manages** disk space automatically
9. ✅ **Reports** the exact commit that introduced the bug

**No manual intervention required** - it handles reboots, failures, and cleanup automatically.

---

## Quick Start

**Prerequisites:**
- Master machine (Linux, with Python 3.8+)
- Slave/test machine (where kernels will be built and tested)
- SSH access from master to slave (root, passwordless)
- IPMI access to slave (optional but recommended for recovery)
- Conserver access (optional, for console log collection during boot)
- Kernel source on slave: `/root/kernel` (git clone of linux repo)

**5-Minute Setup:**

```bash
# 1. On master: Install system dependencies
# Required: python3, pip, ipmitool (for IPMI), git
# Optional: conserver-client (for console log collection)
sudo dnf install python3 python3-pip ipmitool git conserver-client  # RHEL/Fedora
# or for Debian/Ubuntu:
# sudo apt-get install python3 python3-pip ipmitool git conserver-client

# 2. Install kbisect directly from GitHub
pip install git+https://github.com/janjurca/kbisect.git
# or using pipx (recommended for CLI tools):
# pipx install git+https://github.com/janjurca/kbisect.git
# or user installation (no sudo):
# pip install --user git+https://github.com/janjurca/kbisect.git

# 3. Setup SSH keys (passwordless access to slave)
ssh-keygen -t ed25519
ssh-copy-id root@<slave-ip>

# 4. On slave: Clone kernel source
ssh root@<slave-ip>
git clone https://git.kernel.org/pub/scm/linux/kernel/git/torvalds/linux.git /root/kernel
exit

# 5. Create directory for this bisection case
mkdir ~/bisect-boot-issue
cd ~/bisect-boot-issue

# 6. Create config for this bisection (copy from installed package)
python3 -c "import kbisect; from pathlib import Path; import shutil; src = Path(kbisect.__file__).parent / 'config' / 'bisect.conf.example'; shutil.copy(src, 'bisect.yaml')"
vim bisect.yaml
# Edit: Set slave hostname, IPMI credentials

# 7. Run bisection!
kbisect init v6.1 v6.6    # Replace with your good/bad versions
kbisect start
# Creates: ./bisect.db (database)
# Logs are printed to terminal (stdout/stderr)
```

That's it! The tool will now bisect automatically. Check progress with `kbisect status`.

---

## How It Works

```
┌─────────────────┐         ┌──────────────────┐
│  Master Machine │────SSH──┤  Slave Machine   │
│                 │         │  (Test System)   │
│  - Orchestrates │         │                  │
│  - Makes        │         │  - Bash Library  │
│  - Decisions    │         │  - Builds kernels│
│  - Stores state │         │  - Boots kernels │
│  - Calls        │         │  - Runs tests    │
│    functions    │         │                  │
└────────┬────────┘         └──────────────────┘
         │                           ▲
         │ IPMI (Power Control)      │
         └───────────────────────────┘
```

**Workflow for each iteration:**

1. **Master** tells slave to build kernel for commit X via SSH
2. **Slave** builds kernel, installs it, sets as **one-time boot** (grub-reboot)
3. **Master** stores compressed build log in database
4. **Master** starts console log collection (if configured) - conserver or IPMI SOL
5. **Master** reboots slave
6. **Master** waits for slave to boot (monitors SSH connectivity)
7. **Master** stops console collection and stores boot log in database
8. **Master** verifies correct kernel booted (detects panics via kernel version check)
9. **Master** captures kernel config file for later analysis
10. **Master** collects system metadata (kernel version, modules, etc.)
11. **Master** runs test script on slave (default: boot success test)
12. **Master** marks commit as good/bad/skip in git bisect
13. **Repeat** until exact commit found

**Boot failure handling:**
- If kernel panics or hangs → IPMI recovery if configured → Falls back to protected kernel
- Boot test mode: non-bootable = BAD | Custom test mode: non-bootable = SKIP

**All state stored in SQLite** - survives crashes, can resume later.

---

## Installation

### Master Machine

The master machine runs the `kbisect` CLI tool and orchestrates the bisection.

**1. Install system dependencies:**

```bash
# RHEL/Fedora/Rocky
# Required: python3, pip, ipmitool (for IPMI), git
# Optional: conserver-client (for console log collection)
sudo dnf install python3 python3-pip ipmitool git conserver-client

# Debian/Ubuntu
sudo apt-get install python3 python3-pip ipmitool git conserver-client

# Note: conserver-client is optional but recommended for console log collection
# If not available, IPMI SOL will be used as fallback (requires IPMI configured)

# Verify Python 3.8+
python3 --version
```

**2. Install kbisect:**

Choose the installation method based on your use case:

```bash
# Option A: Install from GitHub (recommended for end users)
pip install git+https://github.com/janjurca/kbisect.git

# Option B: Clone and install in development mode (for contributors)
# This creates a symlink, so code changes are immediately active
git clone https://github.com/janjurca/kbisect.git
cd kbisect
pip install -e .

# Option C: Development installation with dev tools (ruff, mypy, pytest)
git clone https://github.com/janjurca/kbisect.git
cd kbisect
pip install -e ".[dev]"

# Verify installation
kbisect --help
```

**Note:** If you get a "externally managed environment" error, use one of these approaches:
```bash
# Approach 1: Use pipx (recommended for CLI tools)
pipx install git+https://github.com/janjurca/kbisect.git

# Approach 2: Use a virtual environment
python3 -m venv venv
source venv/bin/activate
pip install git+https://github.com/janjurca/kbisect.git

# Approach 3: User installation (no sudo needed)
pip install --user git+https://github.com/janjurca/kbisect.git
```

**3. Setup SSH keys:**

```bash
# Generate SSH key if you don't have one
ssh-keygen -t ed25519 -f ~/.ssh/id_ed25519

# Copy to slave (enables passwordless SSH)
ssh-copy-id root@<slave-ip>

# Test connection
ssh root@<slave-ip> 'echo "SSH works"'
```

**4. Configuration is per-directory:**

Each bisection case gets its own directory with its own config file:

```bash
# Create directory for your bisection case
mkdir ~/my-bisection
cd ~/my-bisection

# Copy and customize config from the installed package
python3 -c "import kbisect; from pathlib import Path; import shutil; src = Path(kbisect.__file__).parent / 'config' / 'bisect.conf.example'; shutil.copy(src, 'bisect.yaml')"

# Or manually copy from cloned repository (if you cloned for development)
# cp ~/projects/kbisect/src/kbisect/config/bisect.conf.example ./bisect.yaml

# Edit the config
vim bisect.yaml
```

**Edit the config file** - minimum required settings:

```yaml
slave:
  hostname: 192.168.1.100        # YOUR SLAVE IP
  ssh_user: root
  kernel_path: /root/kernel

ipmi:
  host: 192.168.1.101            # YOUR IPMI IP (optional)
  username: admin
  password: changeme
```

### Slave Machine

**Only one requirement:** Kernel source must exist at `/root/kernel`

```bash
# On slave machine
ssh root@<slave-ip>

# Clone kernel source
git clone https://git.kernel.org/pub/scm/linux/kernel/git/torvalds/linux.git /root/kernel

# Or clone a specific tree
git clone https://git.kernel.org/pub/scm/linux/kernel/git/stable/linux.git /root/kernel

# That's it! Master will deploy everything else automatically.
```

**Optional:** Install kernel build dependencies:

```bash
# RHEL/Fedora/Rocky
sudo dnf groupinstall "Development Tools"
sudo dnf install ncurses-devel bc bison flex elfutils-libelf-devel openssl-devel

# Debian/Ubuntu
sudo apt-get install build-essential libncurses-dev bc bison flex libelf-dev libssl-dev
```

**Everything else is automated** - when you run `kbisect init`, it will:
- Deploy bash library to slave
- Initialize kernel protection
- Create required directories
- Verify deployment

---

## Usage Guide

### Basic Bisection

**Scenario:** Something broke between kernel v6.1 (good) and v6.6 (bad).

```bash
# Initialize bisection (deploys to slave if needed)
kbisect init v6.1 v6.6

# Start automatic bisection
kbisect start
```

**Execution model:**
- `kbisect start` runs as a **foreground process** (not a daemon)
- Blocks in your terminal until bisection completes or you press Ctrl+C
- All progress saved to SQLite database - you can resume anytime with `kbisect start`
- Real-time output shows build progress, test results, and iteration status

The tool will now:
- Build kernels automatically
- Reboot slave for each test
- Run **boot success test** (checks filesystem writable, SSH daemon running)
- Mark commits as good/bad/skip based on test results
- Continue until it finds the exact commit

> **Note:** Without `--test-script`, the default test only checks if the kernel boots successfully. For specific bugs (network issues, performance, etc.), provide a custom test script.

**Monitor progress:**

```bash
# Check current status (queries database, read-only)
kbisect status

# Shows:
# - Session status (running/completed/halted)
# - Good/bad commits and timestamps
# - Total iterations and last 5 iterations with results
# - First bad commit (if found)
```

**When complete:**

```bash
# Generate detailed report
kbisect report

# Output saved to terminal (or save to file):
kbisect report --output /tmp/bisect-report.txt
```

The report will show the **first bad commit** - the exact commit that introduced the problem.

### Using a Custom Kernel Config

**Problem:** Different kernel versions have different config options. You want consistent builds.

**Solution:** Provide a base `.config` file.

**Option 1: Use a specific config file**

```bash
# Save your known-good config from slave to master
scp root@<slave-ip>:/boot/config-$(uname -r) /tmp/my-config

# Configure it in bisect.yaml
cat > bisect.yaml <<EOF
kernel_config:
  config_file: /tmp/my-config  # Path on master machine (will be transferred to slave)
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
1. Config file is read from **master machine**
2. File is automatically transferred to slave during initialization
3. Base `.config` is copied to kernel source on slave
4. `make olddefconfig` runs (handles new/removed options automatically)
5. New options get default values (non-interactive - no prompts!)
6. Kernel builds with consistent config

### Running Custom Tests

**Default test (no test script specified):**

When you run `kbisect start` without `--test-script`, it performs a **boot success test**:

1. Waits for systemd to finish booting (if systemctl available)
2. **Check 1/2:** Filesystem is writable (`/tmp` access) ✅
3. **Check 2/2:** SSH daemon is running ✅

**Result:** Kernel is marked **GOOD** if at least 1 check passes, **BAD** if both checks fail.

This default test is perfect for finding:
- Boot failures and kernel panics
- Critical system breakage
- Basic boot regressions

**For specific bugs/regressions,** use a custom test script:

```bash
#!/bin/bash
# test-network-bug.sh
# Test if network regression is present

# Your test logic
ping -c 5 8.8.8.8 > /dev/null 2>&1
if [ $? -eq 0 ]; then
    echo "Network works"
    exit 0  # GOOD kernel
else
    echo "Network broken"
    exit 1  # BAD kernel
fi
```

**Usage:**

```bash
chmod +x test-network-bug.sh
kbisect init v6.1 v6.6
kbisect start --test-script /path/to/test-network-bug.sh
```

**Test script requirements:**
- Must be executable
- Exit 0 = kernel is GOOD
- Exit non-zero = kernel is BAD
- Will be copied to slave and executed after boot

**Important: Boot failure handling**

When using a custom test script, boot failures are handled differently:
- **Kernel panics/doesn't boot** → Automatically marked as **SKIP** ⊘
  - Can't test your functionality if kernel doesn't boot
  - Git bisect will pick a different commit to test
- **Kernel boots successfully** → Your test script runs
  - Exit 0 → GOOD ✓
  - Exit non-zero → BAD ✗ (this is the regression you're looking for!)

This ensures bisection finds the commit that **broke your specific functionality**, not just unbootable kernels.

**Performance regression example:**

```bash
#!/bin/bash
# test-io-performance.sh

# Run fio benchmark
IOPS=$(fio --name=test --rw=randread --bs=4k --size=1G --numjobs=1 \
           --ioengine=libaio --direct=1 --time_based --runtime=10 \
           --output-format=json | jq '.jobs[0].read.iops')

# Threshold: expecting at least 50000 IOPS
if [ "$IOPS" -lt 50000 ]; then
    echo "Performance regression: $IOPS IOPS"
    exit 1  # BAD
else
    echo "Performance OK: $IOPS IOPS"
    exit 0  # GOOD
fi
```

### Monitoring Progress

**Note:** `kbisect start` shows real-time output in your terminal. These commands are useful for:
- Checking status before/after bisection
- Monitoring from a different terminal or SSH session
- Verifying slave health before starting

**Check bisection status:**

```bash
# Check status (before starting, after Ctrl+C, or from another terminal)
kbisect status

# This command:
# - Queries the SQLite database (read-only, no slave connection)
# - Shows session info: ID, status (running/completed/halted), commits, timestamps
# - Shows total iteration count and LAST 5 iterations with results
# - Displays first bad commit if found
# - Safe to run anytime, does not modify state

# Example output:
# === Bisection Status ===
#
# Session ID:   1
# Status:       running
# Good commit:  v6.1
# Bad commit:   v6.6
# Started:      2024-01-15 10:23:45
#
# Total iterations: 8
#
# Recent iterations:
#   4. d4e5f6g | good    | 180s  | mm: add new feature X
#   5. a1b2c3d | good    | 175s  | net: improve performance
#   6. g7h8i9j | bad     | 190s  | fs: change buffer handling
#   7. x9y8z7a | skip    | 45s   | driver: update (build failed)
#   8. m5n6o7p | running | N/A   | sched: optimize task handling
```

**Monitor slave health:**

```bash
# One-time health check (useful before starting bisection)
kbisect monitor

# Continuous monitoring from another terminal
kbisect monitor --continuous --interval 5
```

**IPMI power control:**

```bash
# Check power state
kbisect ipmi status

# Manual power cycle (if needed)
kbisect ipmi cycle

# Force power off
kbisect ipmi off
```

**Deployment management:**

```bash
# Verify slave is deployed
kbisect deploy --verify-only

# Update library on slave (if you modified bisect-functions.sh)
kbisect deploy --update-only

# Full redeployment
kbisect deploy
```

**Check database directly:**

```bash
# View all collected metadata (from your bisection directory)
sqlite3 ./bisect.db "SELECT * FROM metadata;"

# View kernel configs captured (stored in state_dir, default: current directory)
ls -l ./configs/

# View a specific config
cat ./configs/config-6.5.0-bisect-a1b2c3d
```

---

## Configuration

**Per-Directory Configuration:** Each bisection case has its own `bisect.yaml` file in its directory.

Default location: `./bisect.yaml` (current working directory)

Override with: `kbisect -c /path/to/config.yaml`

### Minimum Required Config

```yaml
slave:
  hostname: 192.168.1.100        # Required: Your slave IP
  ssh_user: root
  kernel_path: /root/kernel
```

### Full Configuration Example

```yaml
# Slave machine
slave:
  hostname: 192.168.1.100
  ssh_user: root
  kernel_path: /root/kernel
  bisect_path: /root/kernel-bisect/lib

# IPMI for power control (optional but recommended)
ipmi:
  host: 192.168.1.101
  username: admin
  password: changeme              # Use secrets manager in production

# Deployment settings
deployment:
  auto_deploy: true               # Auto-deploy to slave if not set up

# Timeouts (in seconds)
timeouts:
  boot: 300                       # Max time to wait for slave to boot (default: 300s)
  test: 600                       # Max time for test script execution (default: 600s)
  build: 1800                     # Max time for kernel build (default: 1800s / 30 min)

# Disk space management
disk_management:
  boot_min_free_mb: 500           # Minimum free space in /boot before cleanup
  boot_emergency_mb: 100          # Emergency cleanup threshold
  keep_test_kernels: 2            # Number of test kernels to keep

# Kernel configuration
kernel_config:
  # Path to base .config file on master machine (optional)
  # Can be absolute or relative to config file location
  # File will be automatically transferred to slave during initialization
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
  # Override hostname for console connection (default: uses slave hostname)
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

### Security Considerations

**IPMI Passwords:**
```yaml
# Option 1: Plain text (not recommended for production)
ipmi:
  password: changeme

# Option 2: Use environment variable
ipmi:
  password: ${IPMI_PASSWORD}      # Set IPMI_PASSWORD env var

# Option 3: Use secrets management (recommended)
# Integrate with HashiCorp Vault, AWS Secrets Manager, etc.
```

---

## Advanced Features

### Kernel Protection

**Automatic protection** of your production kernel - will never be deleted, even in emergency cleanup.

```bash
# Initialized automatically on first run
# To verify:
kbisect deploy --verify-only

# Check protected kernels via SSH:
ssh root@slave 'source /root/kernel-bisect/lib/bisect-functions.sh && list_kernels'
```

### Metadata Collection

**Automatically captures** for every iteration:
- System info (hostname, OS, arch)
- Hardware info (CPU, RAM)
- Kernel version and loaded modules
- Kernel .config files
- Package versions (rpm -qa / dpkg -l)

**Access metadata:**

```bash
# View in report
kbisect report --format json

# Query database (from your bisection directory)
sqlite3 ./bisect.db << EOF
SELECT collection_type, collection_time FROM metadata;
EOF

# View captured kernel configs
ls ./configs/

# Compare configs between commits
diff ./configs/config-6.5.0-bisect-a1b2c3d \
     ./configs/config-6.5.0-bisect-d4e5f6g
```

### Build Logs and Console Log Collection

**Build logs** are automatically captured and stored in the database with gzip compression:

```bash
# List all build logs
kbisect logs list

# Output:
# Log ID   Iter   Commit    Type     Status     Size       Timestamp
# -----------------------------------------------------------------------
# 1        1      a1b2c3d   build    SUCCESS    45.2 KB    2024-01-15 10:23:45
# 2        1      a1b2c3d   console  SUCCESS    12.1 KB    2024-01-15 10:28:12
# 3        2      d4e5f6g   build    FAILED     67.8 KB    2024-01-15 11:15:30

# View specific build log
kbisect logs show 3

# View all logs for an iteration
kbisect logs iteration 2

# Export log to file
kbisect logs export 3 /tmp/build-log.txt
```

**Console log collection** captures serial console output during boot (requires configuration):

- **Conserver** (default): Uses `console <hostname>` command for console access
  - Requires conserver server configured and accessible from master
  - Authentication via kerberos or conserver config file
  - Non-blocking: runs in background thread during boot

- **IPMI SOL** (fallback): Uses IPMI Serial-Over-LAN for console access
  - Automatically used if conserver fails or unavailable
  - Requires IPMI configured in bisect.yaml

**Enable console log collection:**

```bash
# Option 1: Via CLI flag
kbisect init v6.1 v6.6 --collect-console-logs
kbisect start --collect-console-logs

# Option 2: In config file (applies to that bisection directory)
# bisect.yaml:
console_logs:
  enabled: true
  collector: "auto"  # Try conserver first, fall back to IPMI SOL
  fallback_to_ipmi: true
```

**Console log usage:**

```bash
# List console logs
kbisect logs list --log-type console

# View console log for specific iteration
kbisect logs iteration 3
# Shows both build log and console log if captured

# Export console log
kbisect logs show <log-id>
```

**Use cases for console logs:**
- Debug kernel panics (see exact panic message and stack trace)
- Identify boot hangs (see where boot process stops)
- Analyze early boot issues (before SSH is available)
- Capture firmware/BIOS messages
- Debug bootloader issues

### Disk Space Management

**Automatic cleanup** when /boot gets full:

- Monitors disk space before each build
- Triggers cleanup when below threshold (default: 500MB)
- Keeps N most recent test kernels (default: 2)
- **Never deletes protected/production kernels**
- Triple verification before deletion

**Manual cleanup:**

```bash
# Force cleanup on slave (keep only 1 test kernel)
ssh root@slave 'source /root/kernel-bisect/lib/bisect-functions.sh && KEEP_TEST_KERNELS=1 cleanup_old_kernels'
```

### Boot Failure Recovery

**Automatic recovery** from kernel panics and boot failures:

The tool uses **one-time boot** mechanism to automatically detect and recover from kernel panics:

1. **One-time boot setup**:
   - Test kernels are set to boot **only once** using `grub-reboot`/`grub2-reboot`
   - Protected kernel remains as permanent GRUB default
   - If test kernel panics, system automatically falls back to protected kernel

2. **Kernel panic detection and handling**:
   - After reboot, master checks which kernel actually booted (`uname -r`)
   - Compares actual kernel vs expected test kernel
   - If they don't match → kernel panic detected
   - **Action depends on test mode**:
     - **Boot test mode** (no --test-script): Marks as **BAD** ✗ (we're testing bootability)
     - **Custom test mode** (with --test-script): Marks as **SKIP** ⊘ (can't test functionality if kernel doesn't boot)
   - No need for crash dumps or manual intervention

3. **Boot failure scenarios and IPMI recovery**:
   - **Kernel panics during boot**:
     - GRUB falls back to protected kernel (one-time boot)
     - Master detects wrong kernel booted
     - Boot test → marked **BAD** ✗ (kernel is unbootable)
     - Custom test → marked **SKIP** ⊘ (can't test functionality)

   - **Boot timeout (kernel hangs during boot)**:
     - Slave doesn't respond to SSH within timeout (default 300s)
     - **If IPMI configured**: Master triggers IPMI power cycle for recovery
       - Automatic retry logic: up to 3 recovery attempts with 30s delays
       - Each attempt: power cycle → wait for boot → verify SSH connectivity
       - If any attempt succeeds → marks commit and continues bisection
       - If all attempts fail → session marked as "halted" (see below)
     - Slave reboots and falls back to protected kernel (one-time boot)
     - Boot test → marked **BAD** ✗ (kernel failed to boot)
     - Custom test → marked **SKIP** ⊘ (can't test functionality)
     - **If IPMI not configured**: Manual intervention required

   - **Complete recovery failure (all IPMI retries exhausted)**:
     - If all 3 IPMI recovery attempts fail and slave remains unreachable
     - **Session halted automatically**:
       - Status changed to "halted" in database
       - Git bisect state NOT updated yet (commit remains unmarked)
       - Detailed error message logged with recovery instructions
       - Bisection exits cleanly with exit code 1
     - **Manual recovery required**:
       1. Fix slave machine (power on, boot stable kernel manually)
       2. Verify SSH connectivity
       3. Run `kbisect start` to resume
     - **On resume**:
       - Tool detects halted session
       - Verifies slave is reachable
       - Marks pending commit appropriately (bad or skip)
       - Continues bisection from next commit
     - This ensures git bisect state stays synchronized even after complete failures

**Example recovery flow (boot test mode):**
```
1. Build test kernel: 6.5.0-bisect-abc123
2. Set one-time boot: grub2-reboot "6.5.0-bisect-abc123"
3. Reboot slave
4. Kernel panics during boot
5. GRUB automatically boots protected kernel (6.5.0-production)
6. SSH comes back up
7. Master checks: uname -r = "6.5.0-production" (not 6.5.0-bisect-abc123)
8. Master marks abc123 as BAD: "Kernel panic detected - kernel failed to boot"
9. Continue bisection with next commit
```

**Example recovery flow (custom test mode with --test-script):**
```
1. Build test kernel: 6.5.0-bisect-abc123
2. Set one-time boot: grub2-reboot "6.5.0-bisect-abc123"
3. Reboot slave
4. Kernel panics during boot
5. GRUB automatically boots protected kernel (6.5.0-production)
6. SSH comes back up
7. Master checks: uname -r = "6.5.0-production" (not 6.5.0-bisect-abc123)
8. Master marks abc123 as SKIP: "Cannot test functionality if kernel doesn't boot"
9. Git bisect picks different commit to test
10. Continue bisection until finding commit that boots but fails custom test
```

**Example recovery flow (boot timeout with IPMI recovery):**
```
1. Build test kernel: 6.5.0-bisect-abc123
2. Set one-time boot: grub2-reboot "6.5.0-bisect-abc123"
3. Reboot slave
4. Kernel hangs during boot (stuck, not panic)
5. Master waits for SSH... 30s, 60s, 90s... up to 300s (boot timeout)
6. SSH timeout reached - slave not responding
7. Master logs: "Slave failed to reboot within timeout"
8. Master detects IPMI is configured
9. Master triggers IPMI power cycle: ipmitool power cycle
10. Slave force reboots
11. GRUB boots protected kernel (test kernel was one-time only)
12. SSH comes back up
13. Master checks: uname -r = "6.5.0-production"
14. Boot test mode → marks abc123 as BAD: "Boot timeout - kernel failed to boot"
    Custom test mode → marks abc123 as SKIP: "Boot timeout - cannot test functionality"
15. Continue bisection with next commit
```

**Manual recovery (if needed):**

```bash
# If slave is completely stuck:
kbisect ipmi cycle

# Check IPMI status:
kbisect ipmi status
```

### Resume After Interruption

**State persisted in SQLite** - bisection can resume after:
- Master machine reboot
- Network interruption
- Manual cancellation (Ctrl+C)
- Complete slave failure (session halted)

```bash
# Resume automatically
kbisect start

# Or check status first
kbisect status
# If session is "running" or "halted", just run kbisect start to resume
```

**Halted session recovery:**

If a session is marked as "halted" (slave became completely unreachable after all IPMI recovery attempts):

```bash
# 1. Fix the slave machine
# - Manually power on or reboot the slave
# - Ensure it boots into a stable kernel
# - Verify SSH works: ssh root@<slave-ip>

# 2. Resume bisection
kbisect start

# What happens on resume:
# - Tool detects halted session
# - Shows last failed iteration details
# - Verifies slave is now reachable
# - Marks pending commit (bad or skip based on test mode)
# - Continues with next commit
```

Example output when resuming halted session:
```
======================================================================
RESUMING HALTED BISECTION SESSION
======================================================================

Session ID: 1
Good commit: v6.1
Bad commit: v6.6
Started: 2024-01-15 10:00:00

Last iteration: 5
Failed commit: abc123d
Error: Boot timeout - kernel failed to boot (git mark pending - slave down)

The previous session was halted due to slave being unreachable.
Before resuming, please ensure:
  1. The slave machine is powered on and stable
  2. A stable kernel is booted
  3. SSH connectivity is working

Verifying slave connectivity...
✓ Slave is reachable

Marking pending commit abc123d...
  Boot test mode: marking as BAD
✓ Commit marked as bad
Bisection will continue from next commit.
======================================================================
```

---

## Troubleshooting

### Slave won't boot after kernel install

**Symptoms:** Slave doesn't respond after reboot, SSH timeout.

**Solution:**

```bash
# 1. Force power cycle via IPMI
kbisect ipmi cycle

# 2. If still not responding, power off and use IPMI console
kbisect ipmi off

# 3. Access IPMI console (use your IPMI web interface or):
ipmitool -I lanplus -H <ipmi-ip> -U <user> -P <pass> sol activate

# 4. Power on and select safe kernel from GRUB menu
kbisect ipmi on

# 5. In GRUB, select the protected kernel
# (First entry should be your protected production kernel)
```

### Disk space issues on slave

**Symptoms:** Build fails with "No space left on device"

**Solution:**

```bash
# Check disk space
ssh root@slave 'df -h /boot'

# Emergency cleanup (keeps only 1 test kernel)
ssh root@slave 'source /root/kernel-bisect/lib/bisect-functions.sh && KEEP_TEST_KERNELS=1 cleanup_old_kernels'

# List all kernels
ssh root@slave 'source /root/kernel-bisect/lib/bisect-functions.sh && list_kernels'

# Verify protected kernel is intact
ssh root@slave 'source /root/kernel-bisect/lib/bisect-functions.sh && verify_protection'
```

### Build fails repeatedly

**Symptoms:** All builds fail with compilation errors.

**Check:**

1. **Kernel source is clean:**
   ```bash
   ssh root@slave 'cd /root/kernel && git status'
   ssh root@slave 'cd /root/kernel && git clean -fdx'  # Removes all untracked files
   ```

2. **Build dependencies installed:**
   ```bash
   ssh root@slave 'dnf groupinstall "Development Tools"'
   ssh root@slave 'dnf install ncurses-devel bc bison flex elfutils-libelf-devel openssl-devel'
   ```

3. **Check specific build error:**
   ```bash
   kbisect status  # Shows recent error messages
   ```

### IPMI not responding

**Symptoms:** IPMI commands timeout or fail.

**Check:**

1. **Network connectivity:**
   ```bash
   ping <ipmi-ip>
   ```

2. **IPMI manually:**
   ```bash
   ipmitool -I lanplus -H <ipmi-ip> -U <user> -P <pass> power status
   ```

3. **Credentials in config:**
   ```bash
   cat ./bisect.yaml | grep -A 3 ipmi
   ```

4. **IPMI interface enabled:**
   - Check BIOS settings
   - Ensure IPMI network is configured on slave

### SSH connection fails

**Symptoms:** "SSH connectivity failed" during initialization.

**Check:**

1. **Passwordless SSH:**
   ```bash
   ssh root@<slave-ip> 'echo test'
   # Should print "test" without password prompt
   ```

2. **SSH key copied:**
   ```bash
   ssh-copy-id root@<slave-ip>
   ```

3. **Firewall rules:**
   ```bash
   # On slave, ensure SSH is allowed
   firewall-cmd --list-services  # Should include 'ssh'
   ```

### "Deployment failed" error

**Symptoms:** `kbisect deploy` fails.

**Common causes:**

1. **Library path doesn't exist:**
   ```bash
   # Master needs lib/bisect-functions.sh
   ls /opt/kernel-bisect/kernel-bisect/lib/bisect-functions.sh
   ```

2. **SSH not working:**
   ```bash
   ssh root@<slave-ip> 'mkdir -p /root/kernel-bisect/lib'
   ```

### Console log collection not working

**Symptoms:** Console logs are not being captured, or "Console log collection skipped" message.

**Check:**

1. **Console logs enabled:**
   ```bash
   # Check config file
   cat ./bisect.yaml | grep -A 5 console_logs

   # Or use CLI flag
   kbisect start --collect-console-logs
   ```

2. **Conserver installed and configured:**
   ```bash
   # Test console command manually
   console <slave-hostname>
   # Should connect to slave's console (Ctrl+E c q to exit)

   # If command not found, install conserver-client
   sudo dnf install conserver-client  # RHEL/Fedora
   sudo apt-get install conserver-client  # Debian/Ubuntu
   ```

3. **Conserver authentication:**
   ```bash
   # Check kerberos ticket if using kerberos auth
   klist

   # Or check conserver config file
   cat ~/.consolerc
   ```

4. **IPMI SOL fallback (if conserver fails):**
   ```bash
   # Verify IPMI is configured in bisect.yaml
   cat ./bisect.yaml | grep -A 3 ipmi

   # Test IPMI SOL manually
   ipmitool -I lanplus -H <ipmi-ip> -U <user> -P <pass> sol activate
   # (Ctrl+] then . to exit)
   ```

5. **Check logs for specific errors:**
   ```bash
   # Logs are printed to terminal during kbisect execution
   # To save logs to a file for later review:
   kbisect start 2>&1 | tee bisection-output.log
   # Look for "Console log collection" messages in the output
   ```

**Common issues:**

- **Conserver authentication failure**: Configure kerberos or ~/.consolerc
- **IPMI SOL timeout**: Check IPMI network connectivity
- **Hostname mismatch**: Use `console_hostname` in config to override
- **Console logs optional**: Bisection continues even if console collection fails

---

## Safety Features

### Protected Kernels

**Your production kernel is safe:**

- Locked at first initialization
- Never deleted, even in emergency cleanup
- Verified after every cleanup operation
- Set as GRUB permanent default (fallback kernel for failed test kernels)
- Test kernels use one-time boot - always fall back to protected kernel on panic

**How it works:**

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
- Master machine crashes/reboots
- Network interruptions
- Power failures (slave reboots to safe kernel)
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

**Master-Slave Architecture:**

- **Master**: Python orchestration engine, CLI interface, state management
- **Slave**: Bash library with functions, no autonomous processes
- **Communication**: SSH only (no HTTP, no agents, no daemons)

**Why this design?**

- ✅ Simple: One bash library file on slave
- ✅ Stateless slave: Master controls everything
- ✅ Reliable: SSH is mature and well-tested
- ✅ Secure: Standard SSH authentication and encryption
- ✅ Maintainable: All logic in master Python code

### Data Flow

```
1. Master: kbisect init v6.1 v6.6
   ↓
2. Master: Check if slave deployed
   ↓
3. Master: Deploy lib/bisect-functions.sh to slave
   ↓
4. Master: SSH call: init_protection()
   ↓
5. Master: Create session in SQLite
   ↓
6. Master: Collect baseline metadata
   ↓
7. Loop: For each commit from git bisect
   ├─ Master: SSH call: build_kernel(commit_sha)
   ├─ Slave: Build kernel, install, set one-time boot (grub-reboot)
   ├─ Master: Store build log in SQLite (gzip compressed)
   ├─ Master: Start console log collection (if enabled) - conserver or IPMI SOL
   ├─ Master: Reboot slave
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
│   └── bisect-functions.sh       # Bash library (deployed to slave)
├── master/
│   ├── bisect_master.py          # Main orchestration
│   ├── state_manager.py          # SQLite state management
│   ├── slave_deployer.py         # Automatic deployment
│   ├── slave_monitor.py          # Boot detection
│   ├── ipmi_controller.py        # IPMI power control
│   └── console_collector.py      # Console log collection (conserver/IPMI SOL)
└── README.md

Deployed to slave:
/root/kernel-bisect/lib/bisect-functions.sh
/var/lib/kernel-bisect/protected-kernels.list  # Protected kernel list (on slave)
/var/lib/kernel-bisect/safe-kernel.info         # Safe kernel info (on slave)

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

### Example 2: Network Performance Regression

```bash
# Problem: Network throughput dropped from 10Gbps to 5Gbps

# 1. Create test script
cat > test-network.sh << 'EOF'
#!/bin/bash
# Measure network throughput
THROUGHPUT=$(iperf3 -c 192.168.1.200 -t 10 -J | jq '.end.sum_received.bits_per_second')
THRESHOLD=8000000000  # 8 Gbps

if [ "$THROUGHPUT" -lt "$THRESHOLD" ]; then
    echo "Regression: ${THROUGHPUT}bps"
    exit 1
else
    echo "OK: ${THROUGHPUT}bps"
    exit 0
fi
EOF
chmod +x test-network.sh

# 2. Run bisection with network test
kbisect init v6.1 v6.6
kbisect start --test-script ./test-network.sh

# 3. View results
kbisect report
```

### Example 3: Using Custom Kernel Config

```bash
# Problem: Need to test with specific kernel config (DEBUG options enabled)

# 1. Create custom config on master
scp root@slave:/boot/config-$(uname -r) /tmp/debug.config
vim /tmp/debug.config
# Add: CONFIG_DEBUG_INFO=y
#      CONFIG_DEBUG_KERNEL=y

# 2. Configure bisection to use custom config
cat > bisect.yaml <<EOF
kernel_config:
  config_file: /tmp/debug.config  # Path on master (will be transferred to slave)
EOF

# 3. Run bisection
kbisect start

# 4. All kernels built with DEBUG config
```

---

## FAQ

**Q: How long does bisection take?**

A: Depends on the commit range. Formula: ~log2(commits) iterations.
- 10 commits: ~4 iterations
- 100 commits: ~7 iterations
- 1000 commits: ~10 iterations
- Each iteration: build time (~30 min) + boot time (~2 min) + test time

**Q: Can I bisect between non-tagged commits?**

A: Yes! Use any git ref:
```bash
kbisect init abc123 def456        # commit hashes
kbisect init v6.1 HEAD             # tag to current
kbisect init origin/stable HEAD    # branch to current
```

**Q: What if build fails for some commits?**

A: Build failures are automatically marked as "skip" and git bisect continues.

**Q: Can I run multiple bisections simultaneously?**

A: No - one bisection per master-slave pair. But you can have multiple master-slave pairs.

**Q: What happens if I Ctrl+C during bisection?**

A: State is saved in SQLite. Resume with `kbisect start`.

**Q: Can I bisect without IPMI?**

A: Yes, but recovery from boot timeouts/hangs will require manual intervention. Kernel panics are automatically detected (one-time boot mechanism falls back to protected kernel), but if a kernel hangs during boot without panicking, you'll need to manually power cycle the slave.

**Q: Does this work with custom kernel trees?**

A: Yes - just clone your tree to `/root/kernel` on slave.

**Q: Can I test user-space regressions?**

A: Yes - create a custom test script that tests your specific issue.

---

## Development

### Setting Up Development Environment

```bash
# Clone the repository
git clone <repository-url> kbisect
cd kbisect

# Install in development mode with dev dependencies
pip install -e ".[dev]"

# This installs:
# - kbisect (editable, changes take effect immediately)
# - ruff (linter and formatter)
# - mypy (type checker)
# - pytest (testing framework)
```

### Development Workflow

**Linting and Formatting:**
```bash
# Check code quality
ruff check src/

# Auto-fix issues
ruff check src/ --fix

# Format code
ruff format src/

# Run type checking
mypy src/
```

**Testing:**
```bash
# Run tests (when available)
pytest

# Run with coverage
pytest --cov=kbisect --cov-report=html
```

**Building:**
```bash
# Build distribution packages
pip install build
python -m build

# This creates:
# - dist/kbisect-X.Y.Z-py3-none-any.whl
# - dist/kbisect-X.Y.Z.tar.gz
```

**Versioning:**
The project uses `hatch-vcs` to automatically derive versions from git tags:
```bash
# Create a new version tag
git tag v0.2.0
git push --tags

# Version is automatically updated in builds
python -c "import kbisect; print(kbisect.__version__)"
```

### Project Structure

```
kbisect/
├── pyproject.toml              # Project metadata, dependencies, tool configs
├── README.md                   # This file
├── .gitignore                 # Git ignore patterns
├── src/kbisect/               # Source code (src layout)
│   ├── __init__.py
│   ├── cli.py                 # CLI entry point
│   ├── master/                # Master controller modules
│   │   ├── bisect_master.py   # Main bisection logic
│   │   ├── state_manager.py   # SQLite state management
│   │   ├── slave_monitor.py   # Health monitoring
│   │   ├── ipmi_controller.py # IPMI power control
│   │   ├── slave_deployer.py  # Automatic deployment
│   │   └── console_collector.py # Console log collection
│   ├── lib/                   # Bash library (deployed to slave)
│   │   └── bisect-functions.sh
│   └── config/                # Configuration templates
│       └── bisect.conf.example
└── tests/                     # Test suite (pytest)
```

### Code Style

This project uses modern Python best practices:

- **Type hints**: All functions have type annotations (Python 3.8+ compatible)
- **Docstrings**: Google-style docstrings for all public APIs
- **Formatting**: Ruff (replaces black, flake8, isort)
- **Line length**: 100 characters
- **Imports**: Sorted and organized (stdlib → third-party → local)
- **Constants**: UPPER_CASE module-level constants
- **Exceptions**: Custom exception classes for better error handling

### Contributing

Contributions welcome! Please:

1. **Fork and create a branch**: `git checkout -b feature/your-feature`
2. **Make your changes**: Follow the code style above
3. **Run linters**: `ruff check src/ --fix && ruff format src/`
4. **Run type checker**: `mypy src/`
5. **Test your changes**: Add tests if applicable
6. **Update documentation**: Update README if needed
7. **Commit**: Use clear, descriptive commit messages
8. **Push and create PR**: Describe your changes clearly

**Before submitting:**
```bash
# Ensure code quality
ruff check src/ --fix
ruff format src/
mypy src/

# Run tests
pytest
```

---

## Support

- **Issues**: File at GitHub issues page
- **Documentation**: This README and code comments
- **Questions**: Open a discussion in GitHub

---

## License

[Your License Here]

---

**Happy bisecting!** 🎯

Found a kernel regression? Now you can find the exact commit that caused it.
