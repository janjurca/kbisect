# kbisect - Automated Kernel Bisection Tool

Automatically find the exact kernel commit that introduced a bug. The tool handles building, rebooting, testing, and failure recovery without manual intervention.

## Prerequisites

**Master Machine:**
- Python 3.8+
- SSH access to slave (passwordless, as root)
- `ipmitool` (optional, for power control and recovery)

**Slave Machine:**
- Linux system where kernels will be built and tested
- Kernel source at `/root/kernel` (git clone)
- IPMI access (optional but recommended)

## Installation

### 1. Install on Master

```bash
# Install system dependencies
sudo dnf install python3 python3-pip ipmitool git  # RHEL/Fedora
# OR
sudo apt install python3 python3-pip ipmitool git  # Debian/Ubuntu

# Install kbisect
pip install git+https://github.com/janjurca/kbisect.git

# Verify installation
kbisect --help
```

### 2. Setup SSH Keys

```bash
# Generate SSH key (if you don't have one)
ssh-keygen -t ed25519

# Copy to slave (enables passwordless SSH)
ssh-copy-id root@<slave-ip>

# Test connection
ssh root@<slave-ip> 'echo "SSH works"'
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

### Minimum Required Settings

```yaml
slave:
  hostname: 192.168.1.100        # YOUR SLAVE IP
  ssh_user: root
  kernel_path: /root/kernel

ipmi:                            # Optional but recommended
  host: 192.168.1.101            # YOUR IPMI IP
  username: admin
  password: changeme
```

### Key Optional Settings

```yaml
# Automatic kernel repository deployment
# If configured, kbisect will clone/copy the kernel repo to slave
kernel_repo:
  source: https://git.kernel.org/pub/scm/linux/kernel/git/torvalds/linux.git
  branch: master  # Optional: specific branch to checkout

# Use custom kernel config
kernel_config:
  config_file: /path/to/.config  # OR
  use_running_config: true       # Use slave's current kernel config

# Timeouts
timeouts:
  boot: 300    # Seconds to wait for boot
  build: 1800  # Seconds to wait for build
  test: 600    # Seconds to wait for test

# Console log collection (requires conserver or IPMI)
console_logs:
  enabled: true
  collector: "auto"  # Try conserver, fall back to IPMI SOL
```

**Note:** If `kernel_repo` is not configured, you must manually clone the kernel source to `/root/kernel` on the slave before running init.

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

### Resume After Interruption

State is saved in SQLite. Resume anytime with:

```bash
kbisect start
```

## How It Works

```
┌──────────────────┐                    ┌──────────────────┐
│  Master Machine  │────────SSH─────────│  Slave Machine   │
│                  │                    │                  │
│  • Orchestrates  │                    │  • Builds kernel │
│  • Makes         │                    │  • Boots kernel  │
│    decisions     │                    │  • Runs tests    │
│  • Stores state  │                    │                  │
│    in SQLite     │                    │                  │
└────────┬─────────┘                    └──────────────────┘
         │                                       ▲
         │         IPMI (Power Control)          │
         └───────────────────────────────────────┘
```

**Workflow for each commit:**

1. Master deploys bash library to slave (first run only)
2. Protects your current kernel from deletion
3. For each commit in the bisection range:
   - Builds kernel on slave
   - Installs kernel with one-time boot (grub-reboot)
   - Reboots slave
   - Waits for boot (SSH connectivity)
   - Runs test (default: boot success test)
   - Marks commit as good/bad/skip
4. Reports the exact commit that introduced the bug

**Recovery from failures:**
- Kernel panics: One-time boot falls back to protected kernel
- Boot timeouts: IPMI power cycle (if configured)
- Build failures: Automatically marked as skip

## Common Issues

### Slave won't boot after kernel install

```bash
# Force power cycle
kbisect ipmi cycle

# Or manually via IPMI console
ipmitool -I lanplus -H <ipmi-ip> -U <user> -P <pass> sol activate
```

### Build fails with "No space left on device"

```bash
# Check disk space
ssh root@<slave-ip> 'df -h /boot'

# Manual cleanup (keeps only 1 test kernel)
ssh root@<slave-ip> 'source /root/kernel-bisect/lib/bisect-functions.sh && KEEP_TEST_KERNELS=1 cleanup_old_kernels'
```

### SSH connection fails

```bash
# Verify passwordless SSH works
ssh root@<slave-ip> 'echo test'  # Should print "test" without password prompt

# If needed, re-copy SSH key
ssh-copy-id root@<slave-ip>
```

### Build dependencies missing on slave

```bash
# RHEL/Fedora
ssh root@<slave-ip> 'dnf groupinstall "Development Tools" && dnf install ncurses-devel bc bison flex elfutils-libelf-devel openssl-devel'

# Debian/Ubuntu
ssh root@<slave-ip> 'apt install build-essential libncurses-dev bc bison flex libelf-dev libssl-dev'
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

## Advanced Usage

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

### Monitor slave health

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

# Update library only (don't full redeploy)
kbisect deploy --update-only

# Force deployment during init
kbisect init v6.1 v6.6 --force-deploy
```

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
