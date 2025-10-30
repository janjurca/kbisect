#!/bin/bash
#
# Kernel Bisection Library Functions
# Single library file sourced by master via SSH
# All essential functions for kernel bisection on slave
#

# Directories
BISECT_DIR="/var/lib/kernel-bisect"
METADATA_DIR="${BISECT_DIR}/metadata"
KERNEL_PATH="${KERNEL_PATH:-/root/kernel}"

# Configuration defaults
BOOT_MIN_FREE_MB=${BOOT_MIN_FREE_MB:-500}
KEEP_TEST_KERNELS=${KEEP_TEST_KERNELS:-2}

# ============================================================================
# PROTECTION FUNCTIONS
# ============================================================================

init_protection() {
    local current_kernel=$(uname -r)

    mkdir -p "$BISECT_DIR"

    echo "Initializing protection for kernel: $current_kernel" >&2

    # Find and lock all files for current kernel
    {
        find /boot -name "*${current_kernel}*" 2>/dev/null
        echo "/lib/modules/${current_kernel}/"
    } > "$BISECT_DIR/protected-kernels.list"

    # Save kernel info
    cat > "$BISECT_DIR/safe-kernel.info" <<EOF
SAFE_KERNEL_VERSION=${current_kernel}
SAFE_KERNEL_IMAGE=/boot/vmlinuz-${current_kernel}
LOCKED_AT=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
EOF

    # Set GRUB permanent default (fallback kernel)
    # This protected kernel remains as the default that GRUB falls back to
    # when test kernels fail to boot (via grub-reboot one-time boot)
    if command -v grubby &> /dev/null; then
        grubby --set-default="/boot/vmlinuz-${current_kernel}" 2>/dev/null
    fi

    chmod 600 "$BISECT_DIR/protected-kernels.list" "$BISECT_DIR/safe-kernel.info"

    echo "Protected kernel: $current_kernel" >&2
    return 0
}

is_protected() {
    local file="$1"

    [ ! -f "$BISECT_DIR/protected-kernels.list" ] && return 1

    # Check exact match
    grep -qxF "$file" "$BISECT_DIR/protected-kernels.list" 2>/dev/null && return 0

    # Check if directory match
    local dir="${file%/}/"
    grep -qxF "$dir" "$BISECT_DIR/protected-kernels.list" 2>/dev/null && return 0

    # Check if inside protected directory
    while IFS= read -r protected_path; do
        [[ "$file" == "$protected_path"* ]] && return 0
    done < "$BISECT_DIR/protected-kernels.list"

    return 1
}

verify_protection() {
    [ ! -f "$BISECT_DIR/protected-kernels.list" ] && return 1

    local missing=0
    while IFS= read -r file; do
        [ ! -e "$file" ] && missing=$((missing + 1))
    done < "$BISECT_DIR/protected-kernels.list"

    [ $missing -eq 0 ]
}

# ============================================================================
# DISK SPACE FUNCTIONS
# ============================================================================

check_disk_space() {
    local min_mb="${1:-$BOOT_MIN_FREE_MB}"
    local free_mb=$(df -BM /boot | awk 'NR==2 {gsub(/M/,"",$4); print $4}')
    [ "$free_mb" -gt "$min_mb" ]
}

get_disk_space() {
    df -BM /boot | awk 'NR==2 {gsub(/M/,"",$4); print $4}'
}

# ============================================================================
# BUILD FUNCTIONS
# ============================================================================

build_kernel() {
    local commit="$1"
    local kernel_path="${2:-$KERNEL_PATH}"
    local kernel_config="${3:-}"

    echo "Building kernel for commit: $commit" >&2

    # Check disk space first
    if ! check_disk_space; then
        echo "Low disk space, cleaning up..." >&2
        cleanup_old_kernels
    fi

    cd "$kernel_path" || return 1

    # Checkout commit
    git checkout "$commit" 2>&1 || return 1

    # Create build label
    local label="bisect-${commit:0:7}"

    # Backup and modify Makefile
    cp Makefile Makefile.bisect-backup
    sed -i "s/^EXTRAVERSION =.*/EXTRAVERSION = -$label/" Makefile

    # Copy base kernel config if specified
    if [ -n "$kernel_config" ]; then
        if [ "$kernel_config" = "RUNNING" ]; then
            local running_config="/boot/config-$(uname -r)"
            if [ -f "$running_config" ]; then
                echo "Using running kernel config: $running_config" >&2
                cp "$running_config" .config
            else
                echo "Warning: Running kernel config not found: $running_config" >&2
            fi
        elif [ -f "$kernel_config" ]; then
            echo "Using kernel config: $kernel_config" >&2
            cp "$kernel_config" .config
        else
            echo "Warning: Kernel config file not found: $kernel_config" >&2
        fi
    fi

    # Build kernel (olddefconfig uses .config as base if it exists, handles new options)
    make olddefconfig >&2 || {
        git restore Makefile
        return 1
    }

    make -j$(nproc) >&2 || {
        git restore Makefile
        return 1
    }

    # Install
    make modules_install >&2 || {
        git restore Makefile
        return 1
    }

    make install >&2 || {
        git restore Makefile
        return 1
    }

    # Update GRUB
    if command -v grub2-mkconfig &> /dev/null; then
        grub2-mkconfig -o /boot/grub2/grub.cfg >&2 2>/dev/null
    elif command -v grub-mkconfig &> /dev/null; then
        grub-mkconfig -o /boot/grub/grub.cfg >&2 2>/dev/null
    fi

    # Get kernel version
    local kernel_version=$(make kernelrelease 2>/dev/null)
    local bootfile="/boot/vmlinuz-${kernel_version}"

    # Set as next boot (ONE-TIME BOOT)
    # This ensures that if the kernel panics, next reboot automatically falls back
    # to the protected kernel (which remains as permanent default)
    local boot_set=false
    if command -v grub2-reboot &> /dev/null; then
        # RHEL/Fedora/Rocky - find boot entry and set one-time boot
        local entry_index=$(grubby --info="$bootfile" 2>/dev/null | grep '^index=' | cut -d= -f2)
        if [ -n "$entry_index" ]; then
            if grub2-reboot "$entry_index" 2>/dev/null; then
                boot_set=true
            else
                echo "ERROR: grub2-reboot failed for entry $entry_index" >&2
                return 1
            fi
        else
            echo "Warning: Could not find boot entry for $bootfile, using grubby fallback" >&2
            if grubby --set-default "$bootfile" 2>/dev/null; then
                boot_set=true
            else
                echo "ERROR: grubby --set-default failed" >&2
                return 1
            fi
        fi
    elif command -v grub-reboot &> /dev/null; then
        # Debian/Ubuntu - use grub-reboot with kernel version
        if grub-reboot "$kernel_version" 2>/dev/null; then
            boot_set=true
        else
            echo "ERROR: grub-reboot failed for kernel $kernel_version" >&2
            return 1
        fi
    elif command -v grubby &> /dev/null; then
        # Fallback: use grubby (WARNING: this sets permanent default, not one-time)
        echo "Warning: grub-reboot not available, using grubby (not one-time boot)" >&2
        if grubby --set-default "$bootfile" 2>/dev/null; then
            boot_set=true
        else
            echo "ERROR: grubby --set-default failed" >&2
            return 1
        fi
    else
        echo "ERROR: No GRUB boot manager found (grub2-reboot, grub-reboot, or grubby)" >&2
        return 1
    fi

    if [ "$boot_set" != "true" ]; then
        echo "ERROR: Failed to set boot kernel" >&2
        return 1
    fi

    # Restore Makefile
    git restore Makefile
    rm -f Makefile.bisect-backup

    # Cleanup after build
    cleanup_old_kernels

    # Output kernel version (for master to capture)
    echo "$kernel_version"
    return 0
}

# ============================================================================
# CLEANUP FUNCTIONS
# ============================================================================

cleanup_old_kernels() {
    local keep_count="${1:-$KEEP_TEST_KERNELS}"
    local current_kernel=$(uname -r)

    echo "Cleaning up old bisect kernels (keeping $keep_count)..." >&2

    # Get bisect kernels sorted by modification time (oldest first)
    local bisect_kernels=($(ls -t /boot/vmlinuz-*-bisect-* 2>/dev/null | tac))
    local total=${#bisect_kernels[@]}

    if [ "$total" -le "$keep_count" ]; then
        echo "No cleanup needed ($total kernels)" >&2
        return 0
    fi

    local remove_count=$((total - keep_count))
    echo "Removing $remove_count old kernel(s)..." >&2

    local removed=0
    for kernel_path in "${bisect_kernels[@]}"; do
        [ "$removed" -ge "$remove_count" ] && break

        local version=$(basename "$kernel_path" | sed 's/vmlinuz-//')

        # Triple safety checks
        if is_protected "$kernel_path"; then
            echo "SKIP: $version (protected)" >&2
            continue
        fi

        if [[ "$version" == "$current_kernel" ]]; then
            echo "SKIP: $version (current)" >&2
            continue
        fi

        if [[ "$version" != *"-bisect-"* ]]; then
            echo "SKIP: $version (not bisect kernel)" >&2
            continue
        fi

        # Remove files
        rm -f "/boot/vmlinuz-${version}" 2>/dev/null
        rm -f "/boot/initramfs-${version}.img" 2>/dev/null
        rm -f "/boot/System.map-${version}" 2>/dev/null
        rm -f "/boot/config-${version}" 2>/dev/null
        rm -rf "/lib/modules/${version}/" 2>/dev/null

        echo "Removed: $version" >&2
        removed=$((removed + 1))
    done

    # Verify protection still intact
    verify_protection || {
        echo "WARNING: Protection verification failed!" >&2
        return 1
    }

    echo "Cleanup complete: removed $removed kernel(s)" >&2
    return 0
}

list_kernels() {
    local current_kernel=$(uname -r)

    echo "Installed Kernels:" >&2
    echo "==================" >&2

    for vmlinuz in /boot/vmlinuz-*; do
        [ -f "$vmlinuz" ] || continue

        local version=$(basename "$vmlinuz" | sed 's/vmlinuz-//')
        local status=""

        is_protected "$vmlinuz" && status="${status}[PROTECTED] "
        [[ "$version" == "$current_kernel" ]] && status="${status}[CURRENT] "
        [[ "$version" == *"-bisect-"* ]] && status="${status}[BISECT] "

        echo "$status$version" >&2
    done

    local free_mb=$(get_disk_space)
    echo "" >&2
    echo "Free space: ${free_mb}MB" >&2
}

# ============================================================================
# METADATA FUNCTIONS
# ============================================================================

collect_metadata() {
    local type="${1:-baseline}"

    case "$type" in
        baseline)
            collect_metadata_baseline
            ;;
        iteration)
            collect_metadata_iteration
            ;;
        quick)
            collect_metadata_quick
            ;;
        *)
            echo "{\"error\": \"Unknown metadata type: $type\"}"
            return 1
            ;;
    esac
}

collect_metadata_baseline() {
    local timestamp=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
    local hostname=$(hostname)
    local kernel=$(uname -r)
    local os=$(grep PRETTY_NAME /etc/os-release 2>/dev/null | cut -d= -f2 | tr -d '"' || echo "unknown")
    local arch=$(uname -m)
    local cpu=$(grep -m1 'model name' /proc/cpuinfo 2>/dev/null | cut -d: -f2 | xargs || echo "unknown")
    local cores=$(nproc)
    local mem_gb=$(free -g | awk 'NR==2 {print $2}')
    local pkg_count=$(rpm -qa 2>/dev/null | wc -l || dpkg -l 2>/dev/null | grep -c '^ii' || echo 0)

    cat <<EOF
{
  "collection_time": "$timestamp",
  "collection_type": "baseline",
  "system": {
    "hostname": "$hostname",
    "os": "$os",
    "architecture": "$arch",
    "kernel": "$kernel"
  },
  "hardware": {
    "cpu_model": "$cpu",
    "cpu_cores": $cores,
    "memory_gb": $mem_gb
  },
  "packages": {
    "count": $pkg_count
  }
}
EOF
}

collect_metadata_iteration() {
    local timestamp=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
    local kernel=$(uname -r)
    local uptime_sec=$(cat /proc/uptime | awk '{print int($1)}')
    local modules=$(lsmod | tail -n +2 | wc -l)

    cat <<EOF
{
  "collection_time": "$timestamp",
  "collection_type": "iteration",
  "kernel_version": "$kernel",
  "uptime_seconds": $uptime_sec,
  "modules_loaded": $modules
}
EOF
}

collect_metadata_quick() {
    cat <<EOF
{
  "collection_time": "$(date -u +"%Y-%m-%dT%H:%M:%SZ")",
  "collection_type": "quick",
  "kernel_version": "$(uname -r)",
  "hostname": "$(hostname)",
  "uptime_seconds": $(cat /proc/uptime | awk '{print int($1)}')
}
EOF
}

# ============================================================================
# TEST FUNCTIONS
# ============================================================================

run_test() {
    local test_type="${1:-boot}"
    local test_arg="${2:-}"

    case "$test_type" in
        boot)
            test_boot_success
            ;;
        custom)
            test_custom_script "$test_arg"
            ;;
        *)
            echo "Unknown test type: $test_type" >&2
            return 1
            ;;
    esac
}

test_boot_success() {
    echo "Running boot success test..." >&2

    # Check system is running
    if command -v systemctl &> /dev/null; then
        systemctl is-system-running --wait 2>/dev/null || true
    fi

    # Basic checks
    local checks_passed=0

    # Check 1: Can write to filesystem
    if touch /tmp/bisect-test-$$ 2>/dev/null && rm -f /tmp/bisect-test-$$; then
        checks_passed=$((checks_passed + 1))
    fi

    # Check 2: SSH daemon running
    if systemctl is-active sshd &>/dev/null || systemctl is-active ssh &>/dev/null; then
        checks_passed=$((checks_passed + 1))
    fi

    echo "Boot test: $checks_passed/2 checks passed" >&2

    [ $checks_passed -ge 1 ]
}

test_custom_script() {
    local script_path="$1"

    if [ ! -f "$script_path" ]; then
        echo "Test script not found: $script_path" >&2
        return 1
    fi

    if [ ! -x "$script_path" ]; then
        chmod +x "$script_path" 2>/dev/null || {
            echo "Test script not executable: $script_path" >&2
            return 1
        }
    fi

    echo "Running custom test: $script_path" >&2
    "$script_path"
}

# ============================================================================
# UTILITY FUNCTIONS
# ============================================================================

get_kernel_version() {
    uname -r
}

get_uptime() {
    cat /proc/uptime | awk '{print int($1)}'
}

# Library initialization
echo "Kernel bisect library loaded ($(date))" >&2
true
