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

The default test checks if the kernel boots successfully (filesystem writable, SSH available).

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

### Use specific kernel config

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
# Verify deployment without making changes
kbisect deploy --verify-only

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
└── configs/          # Kernel .config files (one per iteration)
```

## License

MIT License

---

**Need help?** File an issue at https://github.com/janjurca/kbisect/issues
