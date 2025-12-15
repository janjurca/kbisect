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
BOOT_MIN_FREE_MB=${BOOT_MIN_FREE_MB:-200}
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
        if ! grubby --set-default="/boot/vmlinuz-${current_kernel}" 2>&1; then
            echo "ERROR: Failed to set permanent default kernel" >&2
            return 1
        fi

        # Verify it was set
        local default_kernel=$(grubby --default-kernel 2>/dev/null)
        if [ "$default_kernel" != "/boot/vmlinuz-${current_kernel}" ]; then
            echo "ERROR: Default kernel verification failed" >&2
            echo "  Expected: /boot/vmlinuz-${current_kernel}" >&2
            echo "  Actual: $default_kernel" >&2
            return 1
        fi

        echo "✓ Permanent default kernel set: $current_kernel" >&2
    fi

    # Configure GRUB for one-time boot support
    # This is CRITICAL for safe bisection - without GRUB_DEFAULT=saved,
    # grub2-reboot/grub-reboot will not work and system will boot test kernels permanently
    echo "Configuring GRUB for one-time boot support..." >&2

    if [ -f /etc/default/grub ]; then
        # Check if GRUB_DEFAULT is already set to saved
        if ! grep -q '^GRUB_DEFAULT=saved' /etc/default/grub 2>/dev/null; then
            echo "  Setting GRUB_DEFAULT=saved in /etc/default/grub" >&2

            # Backup original
            cp /etc/default/grub /etc/default/grub.bisect-backup 2>/dev/null

            # Remove any existing GRUB_DEFAULT line and add saved
            sed -i '/^GRUB_DEFAULT=/d' /etc/default/grub
            echo 'GRUB_DEFAULT=saved' >> /etc/default/grub

            # Configure GRUB timeout for auto-boot (prevents waiting forever at menu)
            sed -i '/^GRUB_TIMEOUT=/d' /etc/default/grub
            echo 'GRUB_TIMEOUT=5' >> /etc/default/grub

            # Set timeout style to countdown (shows timer, auto-boots)
            sed -i '/^GRUB_TIMEOUT_STYLE=/d' /etc/default/grub
            echo 'GRUB_TIMEOUT_STYLE=countdown' >> /etc/default/grub

            # Regenerate GRUB config to apply changes
            echo "  Regenerating GRUB configuration..." >&2
            if command -v grub2-mkconfig &> /dev/null; then
                grub2-mkconfig -o /boot/grub2/grub.cfg >&2 2>&1
            elif command -v grub-mkconfig &> /dev/null; then
                grub-mkconfig -o /boot/grub/grub.cfg >&2 2>&1
            else
                echo "  Warning: Could not find grub2-mkconfig or grub-mkconfig" >&2
            fi

            echo "✓ GRUB configured for one-time boot (GRUB_DEFAULT=saved)" >&2
        else
            echo "✓ GRUB already configured for one-time boot" >&2
        fi
    else
        echo "Warning: /etc/default/grub not found - GRUB configuration may not persist" >&2
    fi

    chmod 600 "$BISECT_DIR/protected-kernels.list" "$BISECT_DIR/safe-kernel.info"

    echo "Protected kernel: $current_kernel" >&2

    # Install build dependencies
    if ! install_build_deps; then
        echo "Warning: Failed to install all build dependencies" >&2
        echo "Kernel builds may fail. Please install required packages manually." >&2
    fi

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

verify_grub_config() {
    echo "=== GRUB Configuration Check ===" >&2

    # Check GRUB_DEFAULT setting
    if [ -f /etc/default/grub ]; then
        local grub_default=$(grep '^GRUB_DEFAULT=' /etc/default/grub 2>/dev/null | cut -d= -f2)
        echo "GRUB_DEFAULT: ${grub_default:-<not set>}" >&2

        if [ "$grub_default" != "saved" ]; then
            echo "⚠ WARNING: GRUB_DEFAULT is not set to 'saved'" >&2
            echo "  One-time boot will NOT work correctly!" >&2
            echo "  Run init_protection() to fix this." >&2
            return 1
        fi
    else
        echo "✗ /etc/default/grub not found" >&2
        return 1
    fi

    # Check current saved_entry (one-time boot flag)
    if command -v grub2-editenv &> /dev/null; then
        local saved_entry=$(grub2-editenv list 2>/dev/null | grep saved_entry)
        echo "Current saved_entry: ${saved_entry:-<not set>}" >&2
    elif command -v grub-editenv &> /dev/null; then
        local saved_entry=$(grub-editenv list 2>/dev/null | grep saved_entry)
        echo "Current saved_entry: ${saved_entry:-<not set>}" >&2
    fi

    # Check default kernel
    if command -v grubby &> /dev/null; then
        local default_kernel=$(grubby --default-kernel 2>/dev/null)
        echo "Default kernel: ${default_kernel:-<not set>}" >&2
    fi

    # Check protected kernel
    if [ -f "$BISECT_DIR/safe-kernel.info" ]; then
        local protected_kernel=$(grep '^SAFE_KERNEL_VERSION=' "$BISECT_DIR/safe-kernel.info" | cut -d= -f2)
        echo "Protected kernel: ${protected_kernel:-<not found>}" >&2
    fi

    echo "================================" >&2
    return 0
}

# ============================================================================
# DISK SPACE FUNCTIONS
# ============================================================================

check_disk_space() {
    local min_mb="${1:-$BOOT_MIN_FREE_MB}"
    local free_mb=$(df -BM /boot | awk 'NR==2 {gsub(/M/,"",$4); print $4}')
    local total_mb=$(df -BM /boot | awk 'NR==2 {gsub(/M/,"",$2); print $2}')
    local used_mb=$(df -BM /boot | awk 'NR==2 {gsub(/M/,"",$3); print $3}')

    echo "/boot disk space: ${free_mb}MB free / ${total_mb}MB total (${used_mb}MB used)" >&2

    if [ "$free_mb" -gt "$min_mb" ]; then
        echo "✓ Sufficient space (${free_mb}MB > ${min_mb}MB required)" >&2
        return 0
    else
        echo "⚠ Low space: ${free_mb}MB available, ${min_mb}MB required" >&2
        return 1
    fi
}

get_disk_space() {
    df -BM /boot | awk 'NR==2 {gsub(/M/,"",$4); print $4}'
}

verify_onetime_boot() {
    # Check if saved_entry is set (indicates one-time boot is configured)
    local saved_entry=""
    if command -v grub2-editenv &> /dev/null; then
        saved_entry=$(grub2-editenv list 2>/dev/null | grep '^saved_entry=' | cut -d= -f2)
    elif command -v grub-editenv &> /dev/null; then
        saved_entry=$(grub-editenv list 2>/dev/null | grep '^saved_entry=' | cut -d= -f2)
    fi

    if [ -z "$saved_entry" ]; then
        echo "⚠ WARNING: saved_entry is not set!" >&2
        return 1
    fi

    echo "✓ One-time boot entry set: $saved_entry" >&2
    return 0
}

# ============================================================================
# BUILD DEPENDENCIES FUNCTIONS
# ============================================================================

install_build_deps() {
    echo "Checking and installing kernel build dependencies..." >&2

    # Detect distribution
    local distro_id=""
    if [ -f /etc/os-release ]; then
        distro_id=$(grep '^ID=' /etc/os-release | cut -d= -f2 | tr -d '"' | tr '[:upper:]' '[:lower:]')
    fi

    # Determine package manager and packages based on distribution
    case "$distro_id" in
        rhel|centos|fedora|rocky|almalinux)
            echo "Detected RPM-based distribution: $distro_id" >&2

            # Try dnf first (newer RHEL/Fedora), fall back to yum
            local pkg_manager=""
            if command -v dnf &> /dev/null; then
                pkg_manager="dnf"
            elif command -v yum &> /dev/null; then
                pkg_manager="yum"
            else
                echo "Error: No package manager found (dnf/yum)" >&2
                return 1
            fi

            # Install build dependencies
            echo "Installing build dependencies with $pkg_manager..." >&2
            $pkg_manager install -y \
                make \
                gcc \
                flex \
                bison \
                elfutils-libelf-devel \
                openssl \
                openssl-devel \
                bc \
                ncurses-devel \
                perl \
                dwarves \
                >&2 2>&1

            if [ $? -ne 0 ]; then
                echo "Warning: Some packages may have failed to install" >&2
            fi
            ;;

        debian|ubuntu)
            echo "Detected DEB-based distribution: $distro_id" >&2

            # Update package lists first
            echo "Updating package lists..." >&2
            apt-get update -y >&2 2>&1

            # Install build dependencies
            echo "Installing build dependencies with apt..." >&2
            DEBIAN_FRONTEND=noninteractive apt-get install -y \
                build-essential \
                flex \
                bison \
                libelf-dev \
                libssl-dev \
                bc \
                libncurses-dev \
                dwarves \
                >&2 2>&1

            if [ $? -ne 0 ]; then
                echo "Warning: Some packages may have failed to install" >&2
            fi
            ;;

        *)
            echo "Warning: Unknown distribution '$distro_id' - attempting generic installation" >&2
            echo "You may need to manually install: flex, bison, gcc, make, libelf-dev, libssl-dev, bc" >&2
            return 1
            ;;
    esac

    # Verify critical tools are available
    local missing_tools=""
    for tool in flex bison gcc make; do
        if ! command -v $tool &> /dev/null; then
            missing_tools="$missing_tools $tool"
        fi
    done

    if [ -n "$missing_tools" ]; then
        echo "Error: Critical build tools still missing:$missing_tools" >&2
        return 1
    fi

    echo "✓ Build dependencies installed successfully" >&2
    return 0
}

# ============================================================================
# BUILD FUNCTIONS
# ============================================================================

build_kernel() {
    local commit="$1"
    local kernel_path="${2:-$KERNEL_PATH}"
    local kernel_config="${3:-}"

    echo "====================================================================" >&2
    echo "Building kernel for commit: $commit" >&2
    echo "====================================================================" >&2

    # ALWAYS run cleanup BEFORE build to prevent space issues
    # This is critical because make install can fail with no space before cleanup runs
    echo "" >&2
    echo "Pre-build cleanup (keep 1 kernel)..." >&2
    cleanup_old_kernels 1

    echo "" >&2
    echo "Removing all kdump files to save space..." >&2
    cleanup_all_kdump_files

    echo "" >&2
    echo "Checking disk space..." >&2
    if ! check_disk_space; then
        echo "" >&2
        echo "⚠ Still low on space after cleanup!" >&2
        echo "Running EMERGENCY cleanup (keep 0 kernels)..." >&2
        cleanup_old_kernels 0

        echo "" >&2
        echo "Final space check..." >&2
        if ! check_disk_space; then
            echo "" >&2
            echo "✗✗✗ CRITICAL: Cannot free enough space for build!" >&2
            echo "Manual intervention required:" >&2
            echo "  1. SSH to slave and check /boot contents" >&2
            echo "  2. Manually remove old kernels if needed" >&2
            echo "  3. Verify protected kernel is intact" >&2
            return 1
        fi
    fi

    echo "" >&2
    cd "$kernel_path" || return 1

    # Fix any Git index corruption before attempting reset
    # Index can become corrupted from interrupted operations or disk issues
    if ! fix_git_index_corruption "$kernel_path"; then
        echo "✗ Failed to fix Git index corruption" >&2
        echo "Manual intervention required: rm -f $kernel_path/.git/index && cd $kernel_path && git reset" >&2
        return 1
    fi

    # Reset repository to clean state before checkout
    # This removes any modifications from previous builds or file timestamp changes
    echo "Resetting repository to clean state..." >&2
    git reset --hard HEAD >&2 2>&1 || {
        echo "Warning: git reset failed, repository may be dirty" >&2
    }

    # Remove any untracked files and directories
    git clean -fd >&2 2>&1 || {
        echo "Warning: git clean failed, untracked files may remain" >&2
    }

    # Checkout commit
    git checkout "$commit" 2>&1 || return 1

    # Create build label
    local label="bisect-${commit:0:7}"

    # Backup and modify Makefile
    cp Makefile Makefile.bisect-backup
    sed -i "s/^EXTRAVERSION =.*/EXTRAVERSION = -$label/" Makefile

    # Copy base kernel config if specified
    if [ -n "$kernel_config" ]; then
        if [ -f "$kernel_config" ]; then
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

    # Add panic=5 parameter for auto-reboot on kernel panic
    # If test kernel panics, it will automatically reboot after 5 seconds
    # and fall back to protected kernel via one-time boot mechanism
    if command -v grubby &> /dev/null; then
        grubby --update-kernel="$bootfile" --args="panic=5" 2>/dev/null || true
        echo "✓ Added panic=5 parameter for auto-recovery on panic" >&2
    fi

    # Set as next boot (ONE-TIME BOOT)
    # This ensures that if the kernel panics, next reboot automatically falls back
    # to the protected kernel (which remains as permanent default)
    echo "Setting one-time boot for: $kernel_version" >&2

    if command -v grub2-reboot &> /dev/null; then
        # RHEL/Fedora/Rocky - Use BLS entry ID (for RHEL 8+, Fedora 30+)
        # On BLS systems, entry IDs are in format: <machine-id>-<kernel-version>
        local entry_id=$(grubby --info="$bootfile" 2>/dev/null | grep '^id=' | cut -d= -f2 | tr -d '"')

        if [ -z "$entry_id" ]; then
            echo "ERROR: Cannot determine BLS entry ID for kernel" >&2
            echo "  Kernel: $kernel_version" >&2
            echo "  Boot file: $bootfile" >&2
            echo "  This system may not be using BLS" >&2
            return 1
        fi

        echo "BLS entry ID: $entry_id" >&2

        if ! grub2-reboot "$entry_id" 2>&1; then
            echo "ERROR: grub2-reboot failed with BLS entry ID" >&2
            echo "  Tried: grub2-reboot \"$entry_id\"" >&2
            return 1
        fi

        # Verify it was set
        if ! verify_onetime_boot; then
            echo "ERROR: One-time boot verification failed" >&2
            echo "  grub2-reboot command succeeded but saved_entry was not set" >&2
            return 1
        fi

        echo "✓ One-time boot configured (BLS ID: $entry_id)" >&2

    elif command -v grub-reboot &> /dev/null; then
        # Debian/Ubuntu - Use kernel version
        echo "Using grub-reboot for: $kernel_version" >&2

        if ! grub-reboot "$kernel_version" 2>&1; then
            echo "ERROR: grub-reboot failed for kernel $kernel_version" >&2
            return 1
        fi

        # Verify it was set
        if ! verify_onetime_boot; then
            echo "ERROR: One-time boot verification failed" >&2
            return 1
        fi

        echo "✓ One-time boot configured via grub-reboot" >&2

    else
        # No one-time boot mechanism available
        echo "ERROR: No one-time boot mechanism available!" >&2
        echo "  This system requires grub2-reboot (RHEL/Fedora) or grub-reboot (Debian/Ubuntu)" >&2
        echo "  Bisection cannot safely proceed without one-time boot support" >&2
        echo "  Install grub2-tools (RHEL) or grub-common (Debian) package" >&2
        return 1
    fi

    # Restore Makefile
    git restore Makefile
    rm -f Makefile.bisect-backup

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

    echo "=== Kernel Cleanup Starting ===" >&2
    echo "Keep count: $keep_count" >&2
    echo "Current kernel: $current_kernel" >&2

    # Get bisect kernels sorted by modification time (oldest first)
    local bisect_kernels=($(ls -t /boot/vmlinuz-*-bisect-* 2>/dev/null | tac))
    local total=${#bisect_kernels[@]}

    echo "Found $total bisect kernel(s)" >&2

    # List all found kernels for debugging
    if [ "$total" -gt 0 ]; then
        echo "Bisect kernels (oldest → newest):" >&2
        for kernel_file in "${bisect_kernels[@]}"; do
            local version=$(basename "$kernel_file" | sed 's/vmlinuz-//')
            local size=$(du -sh "/boot/vmlinuz-${version}" 2>/dev/null | cut -f1 || echo "?")
            echo "  - $version ($size)" >&2
        done
    fi

    if [ "$total" -le "$keep_count" ]; then
        echo "No cleanup needed ($total <= $keep_count)" >&2
        echo "=== Cleanup Complete: No action taken ===" >&2
        return 0
    fi

    local remove_count=$((total - keep_count))
    echo "Need to remove $remove_count kernel(s) to keep $keep_count" >&2
    echo "" >&2

    local removed=0
    local failed=0
    for kernel_file in "${bisect_kernels[@]}"; do
        [ "$removed" -ge "$remove_count" ] && break

        local version=$(basename "$kernel_file" | sed 's/vmlinuz-//')

        # Triple safety checks
        if is_protected "$kernel_file"; then
            echo "SKIP: $version (protected)" >&2
            continue
        fi

        if [[ "$version" == "$current_kernel" ]]; then
            echo "SKIP: $version (currently running)" >&2
            continue
        fi

        if [[ "$version" != *"-bisect-"* ]]; then
            echo "SKIP: $version (not a bisect kernel)" >&2
            continue
        fi

        echo "Removing: $version" >&2

        # Remove files with error checking
        local removal_failed=false

        if [ -f "/boot/vmlinuz-${version}" ]; then
            if ! rm -f "/boot/vmlinuz-${version}" 2>&1; then
                echo "  ✗ Failed to remove /boot/vmlinuz-${version}" >&2
                removal_failed=true
            elif [ -f "/boot/vmlinuz-${version}" ]; then
                echo "  ✗ /boot/vmlinuz-${version} still exists after removal" >&2
                removal_failed=true
            else
                echo "  ✓ Removed /boot/vmlinuz-${version}" >&2
            fi
        fi

        if [ -f "/boot/initramfs-${version}.img" ]; then
            if ! rm -f "/boot/initramfs-${version}.img" 2>&1; then
                echo "  ✗ Failed to remove /boot/initramfs-${version}.img" >&2
                removal_failed=true
            else
                echo "  ✓ Removed /boot/initramfs-${version}.img" >&2
            fi
        fi

        if [ -f "/boot/initramfs-${version}kdump.img" ]; then
            if ! rm -f "/boot/initramfs-${version}kdump.img" 2>&1; then
                echo "  ✗ Failed to remove /boot/initramfs-${version}kdump.img" >&2
            else
                echo "  ✓ Removed /boot/initramfs-${version}kdump.img" >&2
            fi
        fi

        if [ -f "/boot/System.map-${version}" ]; then
            rm -f "/boot/System.map-${version}" 2>&1 || echo "  ⚠ Failed to remove System.map" >&2
        fi

        if [ -f "/boot/config-${version}" ]; then
            rm -f "/boot/config-${version}" 2>&1 || echo "  ⚠ Failed to remove config" >&2
        fi

        if [ -d "/lib/modules/${version}/" ]; then
            if ! rm -rf "/lib/modules/${version}/" 2>&1; then
                echo "  ✗ Failed to remove /lib/modules/${version}/" >&2
            else
                echo "  ✓ Removed /lib/modules/${version}/" >&2
            fi
        fi

        if [ "$removal_failed" = "true" ]; then
            echo "  ✗ Removal FAILED for $version" >&2
            failed=$((failed + 1))
        else
            echo "  ✓ Successfully removed $version" >&2
            removed=$((removed + 1))
        fi
        echo "" >&2
    done

    # Verify protection still intact
    if ! verify_protection; then
        echo "✗✗✗ CRITICAL: Protection verification failed! ✗✗✗" >&2
        return 1
    fi

    # Show final status
    echo "=== Cleanup Complete ===" >&2
    echo "Successfully removed: $removed kernel(s)" >&2
    [ "$failed" -gt 0 ] && echo "Failed to remove: $failed kernel(s)" >&2

    # Show remaining space
    local free_mb=$(get_disk_space)
    echo "/boot free space: ${free_mb}MB" >&2
    echo "======================" >&2

    [ "$failed" -eq 0 ]
}

cleanup_all_kdump_files() {
    echo "=== Removing All kdump Files ===" >&2
    echo "kdump files are large and not needed for bisection" >&2

    local removed=0
    local failed=0
    local total=0

    # Count kdump files first
    for kdump_file in /boot/initramfs-*kdump.img; do
        [ -f "$kdump_file" ] || continue
        total=$((total + 1))
    done

    if [ "$total" -eq 0 ]; then
        echo "No kdump files found" >&2
        echo "=======================" >&2
        return 0
    fi

    echo "Found $total kdump file(s)" >&2
    echo "" >&2

    # Remove all kdump files (including protected kernel's kdump)
    for kdump_file in /boot/initramfs-*kdump.img; do
        [ -f "$kdump_file" ] || continue

        local filename=$(basename "$kdump_file")
        local size=$(du -sh "$kdump_file" 2>/dev/null | cut -f1 || echo "?")

        if rm -f "$kdump_file" 2>&1; then
            echo "  ✓ Removed $filename ($size)" >&2
            removed=$((removed + 1))
        else
            echo "  ✗ Failed to remove $filename" >&2
            failed=$((failed + 1))
        fi
    done

    echo "" >&2
    echo "=== kdump Cleanup Complete ===" >&2
    echo "Removed: $removed file(s)" >&2
    [ "$failed" -gt 0 ] && echo "Failed: $failed file(s)" >&2

    local free_mb=$(get_disk_space)
    echo "/boot free space: ${free_mb}MB" >&2
    echo "===========================" >&2

    [ "$failed" -eq 0 ]
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

fix_git_index_corruption() {
    local repo_path="${1:-.}"

    echo "Checking for Git index corruption..." >&2

    # Try a simple git status to detect index issues
    if git -C "$repo_path" status >/dev/null 2>&1; then
        echo "✓ Git index is healthy" >&2
        return 0
    fi

    # Check if error is specifically about index file
    local error_msg=$(git -C "$repo_path" status 2>&1)
    if [[ "$error_msg" == *"index file smaller than expected"* ]] || \
       [[ "$error_msg" == *"index file corrupt"* ]] || \
       [[ "$error_msg" == *"bad index"* ]]; then

        echo "⚠ Detected Git index corruption" >&2
        echo "Removing corrupted index file..." >&2

        # Remove corrupted index
        if rm -f "$repo_path/.git/index" 2>&1; then
            echo "✓ Removed corrupted index file" >&2
        else
            echo "✗ Failed to remove index file" >&2
            return 1
        fi

        # Let Git recreate the index with reset
        echo "Recreating Git index..." >&2
        if git -C "$repo_path" reset 2>&1; then
            echo "✓ Git index recreated successfully" >&2
            return 0
        else
            echo "✗ Failed to recreate Git index" >&2
            return 1
        fi
    fi

    # Some other Git error
    echo "✗ Git error (not index corruption): $error_msg" >&2
    return 1
}

get_kernel_version() {
    uname -r
}

get_uptime() {
    cat /proc/uptime | awk '{print int($1)}'
}

validate_kernel_repo() {
    local repo_path="${1:-$KERNEL_PATH}"

    echo "Validating kernel repository: $repo_path" >&2

    # Check if directory exists
    if [ ! -d "$repo_path" ]; then
        echo "ERROR: Kernel path does not exist: $repo_path" >&2
        return 1
    fi

    # Check if it's a git repository
    if [ ! -d "$repo_path/.git" ]; then
        echo "ERROR: Not a git repository: $repo_path" >&2
        return 1
    fi

    # Configure git safe.directory to prevent dubious ownership errors
    git config --global --add safe.directory "$repo_path" 2>/dev/null || true

    # Check if repository is accessible
    if ! git -C "$repo_path" rev-parse HEAD >/dev/null 2>&1; then
        echo "ERROR: Git repository is invalid or corrupted: $repo_path" >&2
        return 1
    fi

    # Get repository info
    local head_commit=$(git -C "$repo_path" rev-parse --short HEAD 2>/dev/null)
    local branch=$(git -C "$repo_path" rev-parse --abbrev-ref HEAD 2>/dev/null)

    echo "✓ Repository valid" >&2
    echo "  Path: $repo_path" >&2
    echo "  Branch: $branch" >&2
    echo "  HEAD: $head_commit" >&2

    return 0
}

# Library initialization
echo "Kernel bisect library loaded ($(date))" >&2
true
