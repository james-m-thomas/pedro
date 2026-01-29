#!/bin/bash
# SPDX-License-Identifier: GPL-3.0
#
# Turnkey Pedro demo environment.
#
# Provisions a Debian 13 QEMU VM with Pedro in lockdown mode, demo workloads,
# and a monitoring dashboard. Runs on macOS (Apple Silicon or Intel) or Linux.
#
# Usage: ./demo/demo.sh <command>
#
# Commands:
#   start     Provision VM, build Pedro, start everything
#   stop      Shut down VM and dashboard
#   status    Show component status
#   ssh       SSH into the VM
#   cli       CLI dashboard (requires: pip install duckdb textual)
#   web       Start Streamlit dashboard and open in browser
#   logs      Tail Pedro's stderr from VM
#   down      Stop everything and delete all cached data

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
CACHE_DIR="${SCRIPT_DIR}/.cache"
DATA_DIR="${SCRIPT_DIR}/.data"
VM_DIR="${SCRIPT_DIR}/vm"

SSH_PORT=2222
SSH_OPTS="-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR -o ConnectTimeout=5"
SSH_USER="pedro"
SSH_CMD="ssh ${SSH_OPTS} -p ${SSH_PORT} ${SSH_USER}@localhost"

QEMU_PID_FILE="${CACHE_DIR}/qemu.pid"
RSYNC_PID_FILE="${CACHE_DIR}/rsync.pid"
DEMO_BUILD_ALL=""

# Cloud image URLs
DEBIAN_AMD64_URL="https://cloud.debian.org/images/cloud/trixie/latest/debian-13-genericcloud-amd64.qcow2"
DEBIAN_ARM64_URL="https://cloud.debian.org/images/cloud/trixie/latest/debian-13-genericcloud-arm64.qcow2"
DEBIAN_SHA512SUMS_URL="https://cloud.debian.org/images/cloud/trixie/latest/SHA512SUMS"

# ─── Helpers ───────────────────────────────────────────────────────────────────

log() { printf '\033[1m[pedro-demo]\033[0m %s\n' "$*" >&2; }
err() { printf '\033[1m\033[31m[pedro-demo]\033[0m %s\n' "$*" >&2; }
ok()  { printf '\033[1m\033[32m[pedro-demo]\033[0m %s\n' "$*" >&2; }

detect_arch() {
    case "$(uname -m)" in
        x86_64|amd64)  echo "amd64" ;;
        aarch64|arm64) echo "arm64" ;;
        *) err "Unsupported architecture: $(uname -m)"; exit 1 ;;
    esac
}

detect_os() {
    case "$(uname -s)" in
        Darwin) echo "macos" ;;
        Linux)  echo "linux" ;;
        *) err "Unsupported OS: $(uname -s)"; exit 1 ;;
    esac
}

qemu_binary() {
    local arch="$(detect_arch)"
    if [ "$arch" = "arm64" ]; then
        echo "qemu-system-aarch64"
    else
        echo "qemu-system-x86_64"
    fi
}

find_ssh_pubkey() {
    for key in ~/.ssh/id_ed25519.pub ~/.ssh/id_rsa.pub ~/.ssh/id_ecdsa.pub; do
        if [ -f "$key" ]; then
            cat "$key"
            return 0
        fi
    done

    # No key found — offer to generate one
    log "No SSH public key found."
    printf "  Generate one now (ssh-keygen -t ed25519)? [Y/n] " >&2
    read -r answer
    case "${answer:-y}" in
        [Yy]|"")
            ssh-keygen -t ed25519 -f ~/.ssh/id_ed25519 || return 1
            if [ -f ~/.ssh/id_ed25519.pub ]; then
                cat ~/.ssh/id_ed25519.pub
                return 0
            fi
            return 1
            ;;
        *)
            return 1
            ;;
    esac
}

is_vm_running() {
    [ -f "$QEMU_PID_FILE" ] && kill -0 "$(cat "$QEMU_PID_FILE")" 2>/dev/null
}

_stream_log_until() {
    # Stream VM logs in a fixed-height window until a condition is met.
    # $1 = "serial" (tail serial.log) or "ssh" (journalctl via SSH)
    # $2 = number of display lines
    # $3 = timeout in seconds
    # $4 = check command (shell string, evaluated each tick; return 0 = done)
    local source="$1" lines="$2" timeout="$3" check="$4"
    local w="${COLUMNS:-80}"
    local start elapsed
    start=$(date +%s)

    # Continuously fetch logs into a local file that we tail for display
    local logfile="${CACHE_DIR}/live.log"
    : > "$logfile"
    local fetcher_pid=""

    if [ "$source" = "ssh" ]; then
        # Stream provisioning-related logs from VM (sudo for system journal access).
        # Wrapped in a reconnect loop so that VM reboots (e.g. after first-boot)
        # don't permanently kill the log stream.
        (
            while true; do
                $SSH_CMD "sudo journalctl -f --no-pager -o cat -u pedro-provision -u pedro-demo -u pedro-workloads -u cloud-init 2>/dev/null" >> "$logfile" 2>/dev/null || true
                sleep 5
            done
        ) &
        fetcher_pid=$!
    else
        tail -f "${CACHE_DIR}/serial.log" 2>/dev/null >> "$logfile" &
        fetcher_pid=$!
    fi

    # Reserve screen space: header + lines + footer
    echo ""
    for _ in $(seq 1 $((lines + 2))); do printf '\n'; done

    while true; do
        elapsed=$(( $(date +%s) - start ))

        # Move cursor to top of window
        printf '\033[%dA' "$((lines + 2))"

        # Header
        local hdr
        hdr=$(printf '── VM log (%ds) ' "$elapsed")
        printf '\033[36m%s' "$hdr"
        local pad=$((w - ${#hdr}))
        [ "$pad" -gt 0 ] && printf '%0.s─' $(seq 1 "$pad")
        printf '\033[0m\033[K\n'

        # Last N lines
        local i=0
        while IFS= read -r line; do
            printf '  \033[2m%.*s\033[0m\033[K\n' "$((w - 4))" "$line"
            i=$((i + 1))
        done < <(tail -n "$lines" "$logfile" 2>/dev/null | sed 's/\x1b\[[0-9;]*[a-zA-Z]//g')
        while [ "$i" -lt "$lines" ]; do
            printf '\033[K\n'
            i=$((i + 1))
        done

        # Footer
        printf '\033[36m%0.s─' $(seq 1 "$w")
        printf '\033[0m\033[K\n'

        # Check completion
        if eval "$check" 2>/dev/null; then
            kill "$fetcher_pid" 2>/dev/null; wait "$fetcher_pid" 2>/dev/null
            return 0
        fi

        if [ "$elapsed" -ge "$timeout" ]; then
            kill "$fetcher_pid" 2>/dev/null; wait "$fetcher_pid" 2>/dev/null
            return 1
        fi

        sleep 2
    done
}

wait_for_ssh() {
    local timeout="${1:-600}"
    log "Waiting for VM SSH (timeout: ${timeout}s)..."

    local start elapsed
    start=$(date +%s)
    local spin='⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏'
    local i=0
    local ssh_ok="${CACHE_DIR}/.ssh_ok"
    rm -f "$ssh_ok"

    # Background: probe SSH every 5s, touch sentinel on success
    (
        while [ ! -f "$ssh_ok" ]; do
            if $SSH_CMD "true" 2>/dev/null; then
                touch "$ssh_ok"
                exit 0
            fi
            sleep 5
        done
    ) &
    local probe_pid=$!

    # Foreground: spin until sentinel appears or timeout
    while true; do
        elapsed=$(( $(date +%s) - start ))

        if [ -f "$ssh_ok" ]; then
            printf '\r\033[K'
            rm -f "$ssh_ok"
            wait "$probe_pid" 2>/dev/null
            return 0
        fi

        if [ "$elapsed" -ge "$timeout" ]; then
            printf '\r\033[K'
            kill "$probe_pid" 2>/dev/null; wait "$probe_pid" 2>/dev/null
            rm -f "$ssh_ok"
            return 1
        fi

        printf '\r  \033[36m%s\033[0m  Waiting for SSH... (%ds)' "${spin:i%${#spin}:1}" "$elapsed"
        i=$((i + 1))
        sleep 0.15
    done
}

# ─── Prerequisites ─────────────────────────────────────────────────────────────

check_prerequisites() {
    local missing_pkg=()   # packages installable via package manager
    local missing_other=() # packages requiring manual install
    local os="$(detect_os)"
    local qemu="$(qemu_binary)"

    if ! command -v "$qemu" &>/dev/null; then
        missing_pkg+=("qemu")
    fi

    if ! command -v mkisofs &>/dev/null && ! command -v genisoimage &>/dev/null; then
        missing_pkg+=("cdrtools")
    fi

    if [ "${1:-}" = "--with-docker" ]; then
        if ! command -v docker &>/dev/null; then
            missing_other+=("docker (install Docker Desktop from https://docker.com/products/docker-desktop)")
        fi
    fi

    if [ ${#missing_pkg[@]} -eq 0 ] && [ ${#missing_other[@]} -eq 0 ]; then
        return 0
    fi

    # Report what's missing
    if [ ${#missing_other[@]} -gt 0 ]; then
        err "Missing (manual install required):"
        for m in "${missing_other[@]}"; do
            err "  - $m"
        done
    fi

    # Offer to install what we can
    if [ ${#missing_pkg[@]} -gt 0 ]; then
        if [ "$os" = "linux" ]; then
            _install_prerequisites_apt "${missing_pkg[@]}"
        else
            _install_prerequisites_brew "${missing_pkg[@]}"
        fi
    fi

    if [ ${#missing_other[@]} -gt 0 ]; then
        err "Please install the packages above and rerun."
        exit 1
    fi
}

# Map generic package names to apt packages and install them.
_install_prerequisites_apt() {
    local apt_pkgs=()
    local arch="$(detect_arch)"
    for pkg in "$@"; do
        case "$pkg" in
            qemu)
                if [ "$arch" = "arm64" ]; then
                    apt_pkgs+=("qemu-system-arm")
                else
                    apt_pkgs+=("qemu-system-x86")
                fi
                ;;
            cdrtools) apt_pkgs+=("genisoimage") ;;
            *) apt_pkgs+=("$pkg") ;;
        esac
    done

    if ! command -v apt-get &>/dev/null; then
        err "Missing packages: ${apt_pkgs[*]}"
        err "apt-get not found. Please install manually and rerun."
        exit 1
    fi

    log "Missing packages: ${apt_pkgs[*]}"
    printf "  Install via apt? [Y/n] "
    read -r answer
    case "${answer:-y}" in
        [Yy]|"")
            log "Running: sudo apt-get install -y ${apt_pkgs[*]}"
            sudo apt-get update -qq
            sudo apt-get install -y "${apt_pkgs[@]}"
            ok "Dependencies installed"
            ;;
        *)
            err "Cannot continue without: ${apt_pkgs[*]}"
            exit 1
            ;;
    esac
}

_install_prerequisites_brew() {
    local missing_brew=("$@")
    if ! command -v brew &>/dev/null; then
        err "Missing packages: ${missing_brew[*]}"
        err "Homebrew is not installed. Install from https://brew.sh and rerun."
        exit 1
    fi

    log "Missing packages: ${missing_brew[*]}"
    printf "  Install via Homebrew? [Y/n] "
    read -r answer
    case "${answer:-y}" in
        [Yy]|"")
            log "Running: brew install ${missing_brew[*]}"
            brew install "${missing_brew[@]}"
            ok "Dependencies installed"
            ;;
        *)
            err "Cannot continue without: ${missing_brew[*]}"
            exit 1
            ;;
    esac
}

# ─── Cloud Image ───────────────────────────────────────────────────────────────

download_image() {
    local arch="$(detect_arch)"
    local url
    if [ "$arch" = "arm64" ]; then
        url="$DEBIAN_ARM64_URL"
    else
        url="$DEBIAN_AMD64_URL"
    fi

    local image_name="debian-13-genericcloud-${arch}.qcow2"
    local image_path="${CACHE_DIR}/${image_name}"

    # Fetch expected checksum
    local expected
    expected="$(curl -sfL "$DEBIAN_SHA512SUMS_URL" | grep "$image_name" | awk '{print $1}')"
    if [ -z "$expected" ]; then
        err "Failed to fetch checksum for ${image_name}"
        exit 1
    fi

    if [ -f "$image_path" ]; then
        log "Verifying cached cloud image..."
        local actual
        actual="$(shasum -a 512 "$image_path" | awk '{print $1}')"
        if [ "$actual" = "$expected" ]; then
            log "Using cached cloud image: ${image_name}"
            echo "$image_path"
            return 0
        fi
        err "Cached image is corrupt or incomplete, re-downloading..."
        rm -f "$image_path"
    fi

    log "Downloading Debian 13 cloud image (${arch})..."
    log "  URL: ${url}"
    curl -L --progress-bar -o "$image_path" "$url"

    log "Verifying download..."
    local actual
    actual="$(shasum -a 512 "$image_path" | awk '{print $1}')"
    if [ "$actual" != "$expected" ]; then
        rm -f "$image_path"
        err "Checksum mismatch after download!"
        err "  Expected: ${expected}"
        err "  Got:      ${actual}"
        exit 1
    fi

    ok "Downloaded and verified: ${image_name}"
    echo "$image_path"
}

prepare_disk() {
    local base_image="$1"
    local disk="${CACHE_DIR}/pedro-vm.qcow2"

    if [ -f "$disk" ]; then
        log "Reusing existing VM disk"
        echo "$disk"
        return 0
    fi

    log "Creating VM disk (30GB)..."
    cp "$base_image" "$disk"
    qemu-img resize "$disk" 30G >/dev/null
    echo "$disk"
}

# ─── Cloud-Init ISO ───────────────────────────────────────────────────────────

create_cloud_init_iso() {
    local iso="${CACHE_DIR}/cloud-init.iso"

    # Always regenerate — ISO generation is fast (~1s) and the setup flags
    # or SSH key may have changed between runs.
    rm -f "$iso"

    local tmpdir="$(mktemp -d)"
    trap "rm -rf '$tmpdir'" RETURN

    # Inject SSH key into user-data
    local ssh_key
    if ! ssh_key="$(find_ssh_pubkey)"; then
        err "No SSH public key found. Generate one with: ssh-keygen -t ed25519"
        return 1
    fi

    # Determine setup.sh flags
    local setup_flags=""
    if [ -n "$DEMO_BUILD_ALL" ]; then
        setup_flags="--all"
    fi

    # Use awk instead of sed — SSH keys contain characters that break sed
    awk -v key="$ssh_key" -v flags="$setup_flags" \
        '{gsub(/SSH_PUB_KEY_PLACEHOLDER/, key); gsub(/SETUP_FLAGS_PLACEHOLDER/, flags); print}' \
        "${VM_DIR}/user-data" > "${tmpdir}/user-data"
    cp "${VM_DIR}/meta-data" "${tmpdir}/meta-data"

    log "Creating cloud-init ISO..."
    if command -v mkisofs &>/dev/null; then
        mkisofs -output "$iso" -volid cidata -joliet -rock \
            "${tmpdir}/user-data" "${tmpdir}/meta-data" 2>/dev/null
    else
        genisoimage -output "$iso" -volid cidata -joliet -rock \
            "${tmpdir}/user-data" "${tmpdir}/meta-data" 2>/dev/null
    fi

    ok "Created cloud-init ISO"
    echo "$iso"
}

# ─── QEMU Launch ──────────────────────────────────────────────────────────────

launch_vm() {
    local disk="$1"
    local iso="$2"
    local arch="$(detect_arch)"
    local os="$(detect_os)"
    local qemu="$(qemu_binary)"

    # Ensure data dir exists
    mkdir -p "${DATA_DIR}/telemetry/spool"

    local serial_log="${CACHE_DIR}/serial.log"

    local qemu_args=(
        -m 8G
        -smp 4
        -drive "file=${disk},if=virtio,format=qcow2"
        -drive "file=${iso},format=raw,if=virtio"
        -device virtio-net-pci,netdev=net0
        -netdev "user,id=net0,hostfwd=tcp::${SSH_PORT}-:22"
        -display none
        -serial "file:${serial_log}"
        -pidfile "$QEMU_PID_FILE"
        -daemonize
    )

    # Architecture-specific settings
    if [ "$arch" = "arm64" ]; then
        # Find UEFI firmware
        local bios=""
        for fw in \
            /opt/homebrew/share/qemu/edk2-aarch64-code.fd \
            /usr/share/qemu-efi-aarch64/QEMU_EFI.fd \
            /usr/share/AAVMF/AAVMF_CODE.fd; do
            if [ -f "$fw" ]; then
                bios="$fw"
                break
            fi
        done
        if [ -z "$bios" ]; then
            err "Could not find UEFI firmware for aarch64."
            err "On macOS: brew install qemu (includes edk2)"
            err "On Debian: apt install qemu-efi-aarch64"
            exit 1
        fi

        qemu_args=(-machine virt -cpu host -bios "$bios" "${qemu_args[@]}")

        # Acceleration
        if [ "$os" = "macos" ]; then
            qemu_args=(-accel hvf "${qemu_args[@]}")
        elif [ -e /dev/kvm ]; then
            qemu_args=(-accel kvm "${qemu_args[@]}")
        fi
    else
        # x86_64
        if [ "$os" = "macos" ]; then
            qemu_args=(-accel hvf -cpu host "${qemu_args[@]}")
        elif [ -e /dev/kvm ]; then
            qemu_args=(-accel kvm -cpu host "${qemu_args[@]}")
        else
            qemu_args=(-cpu qemu64 "${qemu_args[@]}")
        fi
    fi

    # Check if something is already bound to the SSH port
    local port_pids
    port_pids="$(lsof -ti tcp:"${SSH_PORT}" 2>/dev/null || true)"
    if [ -n "$port_pids" ]; then
        local port_info
        port_info="$(lsof -i tcp:"${SSH_PORT}" 2>/dev/null | head -5)"
        err "Port ${SSH_PORT} is already in use:"
        echo "$port_info" >&2
        echo "" >&2
        printf '  Kill process %s and continue? [Y/n] ' "$(echo $port_pids)" >&2
        read -r answer
        case "${answer:-y}" in
            [Yy]|"")
                kill $port_pids 2>/dev/null || true
                sleep 1
                if lsof -ti tcp:"${SSH_PORT}" &>/dev/null; then
                    err "Port ${SSH_PORT} is still in use after kill. Aborting."
                    exit 1
                fi
                ok "Port ${SSH_PORT} is now free"
                ;;
            *)
                err "Cannot launch VM without port ${SSH_PORT}. Aborting."
                exit 1
                ;;
        esac
    fi

    log "Launching QEMU VM..."
    "$qemu" "${qemu_args[@]}"
    ok "VM started (PID: $(cat "$QEMU_PID_FILE"))"
}

# ─── Rsync sync ──────────────────────────────────────────────────────────────

start_rsync_loop() {
    log "Starting rsync sync loop..."
    mkdir -p "${DATA_DIR}/telemetry/spool"

    (
        while true; do
            rsync -az -e "ssh ${SSH_OPTS} -p ${SSH_PORT}" \
                "${SSH_USER}@localhost:/opt/pedro-data/telemetry/" \
                "${DATA_DIR}/telemetry/" 2>/dev/null || true
            sleep 2
        done
    ) &
    echo $! > "$RSYNC_PID_FILE"
    log "Rsync loop started (PID: $(cat "$RSYNC_PID_FILE"))"
}

stop_rsync_loop() {
    if [ -f "$RSYNC_PID_FILE" ]; then
        kill "$(cat "$RSYNC_PID_FILE")" 2>/dev/null || true
        rm -f "$RSYNC_PID_FILE"
    fi
}

# ─── Commands ─────────────────────────────────────────────────────────────────

cmd_start() {
    check_prerequisites
    mkdir -p "$CACHE_DIR" "$DATA_DIR"

    if is_vm_running; then
        log "VM is already running"
        _print_status 2>&1
        echo ""
        if ! $SSH_CMD "systemctl is-active pedro-demo --quiet" 2>/dev/null; then
            log "Provisioning still in progress — streaming logs..."
            if ! _stream_log_until "ssh" 10 1800 '$SSH_CMD "test -f /var/lib/pedro-provisioned && systemctl is-active pedro-demo --quiet"'; then
                err "Provisioning timed out. Check: $0 logs"
                exit 1
            fi
            ok "Provisioning complete!"
            start_rsync_loop
        fi
        echo ""
        ok "Pedro demo environment is ready!"
        echo ""
        printf '  \033[1mSSH into VM:\033[0m        %s ssh\n' "$0"
        printf '  \033[1mCLI dashboard:\033[0m      %s cli\n' "$0"
        printf '  \033[1mWeb dashboard:\033[0m      %s web\n' "$0"
        printf '  \033[1mPedro logs:\033[0m         %s logs\n' "$0"
        printf '  \033[1mStop:\033[0m               %s stop\n' "$0"
        echo ""
        return 0
    fi

    # Download and prepare
    local base_image
    base_image="$(download_image)"
    local disk
    disk="$(prepare_disk "$base_image")"
    local iso
    iso="$(create_cloud_init_iso)"

    # Launch
    launch_vm "$disk" "$iso"

    local time_est="~5-10 min"
    if [ -n "$DEMO_BUILD_ALL" ]; then
        time_est="~20 min (with --build-all)"
    fi

    log "Waiting for VM to boot and provision..."
    log "(First run takes ${time_est}: installing packages, building Pedro)"
    log "Monitor progress: $0 logs"

    # Wait for SSH
    if ! wait_for_ssh 90; then
        err "VM failed to come up. Check: $0 logs"
        exit 1
    fi

    ok "VM is accessible via SSH"

    # Wait for provisioning to complete (streams journalctl from VM)
    log "Waiting for provisioning to complete..."
    log "(First run takes ${time_est}: installing packages, building Pedro)"
    if ! _stream_log_until "ssh" 10 1800 '$SSH_CMD "test -f /var/lib/pedro-provisioned && systemctl is-active pedro-demo --quiet"'; then
        err "Provisioning timed out. Check: $0 logs"
        exit 1
    fi

    ok "Provisioning complete!"

    # Start rsync sync
    start_rsync_loop

    echo ""
    ok "Pedro demo environment is ready!"
    echo ""
    printf '  \033[1mSSH into VM:\033[0m        %s ssh\n' "$0"
    printf '  \033[1mCLI dashboard:\033[0m      %s cli\n' "$0"
    printf '  \033[1mWeb dashboard:\033[0m      %s web\n' "$0"
    printf '  \033[1mPedro logs:\033[0m         %s logs\n' "$0"
    printf '  \033[1mStop:\033[0m               %s stop\n' "$0"
    echo ""
}

cmd_stop() {
    # Stop rsync
    stop_rsync_loop

    # Stop docker dashboard
    if command -v docker &>/dev/null; then
        (cd "${SCRIPT_DIR}/dashboard" && docker compose down 2>/dev/null) || true
    fi

    # Shutdown VM gracefully
    if is_vm_running; then
        log "Shutting down VM..."
        $SSH_CMD "sudo poweroff" 2>/dev/null || true
        sleep 3
        # Force kill if still running
        if is_vm_running; then
            kill "$(cat "$QEMU_PID_FILE")" 2>/dev/null || true
            sleep 1
        fi
        rm -f "$QEMU_PID_FILE"
        ok "VM stopped"
    else
        log "VM is not running"
        rm -f "$QEMU_PID_FILE"
    fi

    # Clean up any stale QEMU process still holding our SSH port
    local stale_pids
    stale_pids="$(lsof -ti tcp:"${SSH_PORT}" 2>/dev/null || true)"
    if [ -n "$stale_pids" ]; then
        log "Killing stale process on port ${SSH_PORT} (PID: $(echo $stale_pids))..."
        kill $stale_pids 2>/dev/null || true
    fi
}

_print_status() {
    # VM
    if is_vm_running; then
        ok "VM: running (PID: $(cat "$QEMU_PID_FILE"))"

        # Pedro process (pedro exec's into pedrito)
        if $SSH_CMD "systemctl is-active pedro-demo --quiet" 2>/dev/null; then
            ok "Pedro: running"
        else
            err "Pedro: not running"
        fi

        # Workloads
        if $SSH_CMD "systemctl is-active pedro-workloads --quiet" 2>/dev/null; then
            ok "Workloads: running"
        else
            err "Workloads: not running"
        fi

    else
        log "VM: not running"
    fi

    # Spool data
    local spool="${DATA_DIR}/telemetry/spool"
    if [ -d "$spool" ]; then
        local count
        count=$(find "$spool" -name "*.exec.msg" 2>/dev/null | wc -l | tr -d ' ')
        log "Spool files: ${count}"
    else
        log "Spool: no data yet"
    fi

    # Docker dashboard
    if command -v docker &>/dev/null; then
        if docker ps --format '{{.Names}}' 2>/dev/null | grep -q "dashboard"; then
            ok "Web dashboard: running (http://localhost:8501)"
        else
            log "Web dashboard: not running"
        fi
    fi
}

cmd_status() {
    trap 'printf "\033[?25h\033[J"; exit 0' INT
    printf '\033[?25l'

    local spin='⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏'
    local first=1
    while true; do
        local output
        output="$(_print_status 2>&1)"

        if [ "$first" = 1 ]; then
            printf '\033[H\033[2J'
            printf '  \033[1mPedro Demo Environment Status\033[0m  (Ctrl-C to exit)\n\n'
            printf '%s\n' "$output"
            first=0
        else
            printf '\033[3;1H'
            while IFS= read -r line; do
                printf '%s\033[K\n' "$line"
            done <<< "$output"
            printf '\033[J'
        fi

        # Spinner countdown between refreshes
        local i=0
        while [ "$i" -lt 25 ]; do
            printf '\033[1;1H\033[36m%s\033[0m' "${spin:i%${#spin}:1}"
            sleep 0.2
            i=$((i + 1))
        done
    done
}

cmd_ssh() {
    if ! is_vm_running; then
        err "VM is not running. Start with: $0 start"
        exit 1
    fi
    exec $SSH_CMD "$@"
}

cmd_cli() {
    if ! command -v python3 &>/dev/null; then
        err "python3 not found"
        exit 1
    fi

    # Check deps
    if ! python3 -c "import duckdb, textual" 2>/dev/null; then
        log "Installing dashboard dependencies..."
        pip3 install --quiet duckdb rich textual
    fi

    exec python3 "${SCRIPT_DIR}/dashboard/cli.py" "${DATA_DIR}/telemetry/spool"
}

cmd_web() {
    check_prerequisites --with-docker

    log "Starting Streamlit dashboard..."
    (cd "${SCRIPT_DIR}/dashboard" && docker compose up -d --build)
    ok "Dashboard available at: http://localhost:8501"

    # Try to open in browser
    if command -v open &>/dev/null; then
        sleep 2
        open "http://localhost:8501"
    elif command -v xdg-open &>/dev/null; then
        sleep 2
        xdg-open "http://localhost:8501"
    fi
}

cmd_logs() {
    if ! is_vm_running; then
        err "VM is not running"
        exit 1
    fi
    $SSH_CMD "sudo journalctl -u pedro-demo -u pedro-workloads -u pedro-provision -f --no-pager"
}

cmd_down() {
    local had_vm=false
    if [ -f "${CACHE_DIR}/pedro-vm.qcow2" ] || is_vm_running; then
        had_vm=true
    fi

    log "This will stop the VM and delete the VM disk and telemetry data."
    printf "  Continue? [Y/n] " >&2
    read -r answer
    case "${answer:-y}" in
        [Yy]|"") ;;
        *)
            log "Aborted."
            return 0
            ;;
    esac

    cmd_stop
    rm -f "${CACHE_DIR}/pedro-vm.qcow2" "${CACHE_DIR}/cloud-init.iso"
    rm -f "${CACHE_DIR}/serial.log" "${CACHE_DIR}/qemu.pid" "${CACHE_DIR}/live.log"
    rm -rf "$DATA_DIR"

    if $had_vm; then
        ok "VM removed (base image kept in ${CACHE_DIR})"
    else
        ok "Nothing to remove (base image kept in ${CACHE_DIR})"
    fi
}

# ─── Main ─────────────────────────────────────────────────────────────────────

cmd_help() {
    echo "Usage: $0 <command>"
    echo ""
    echo "Turnkey Pedro demo environment."
    echo ""
    echo "Commands:"
    echo "  start [--build-all]  Provision VM, build Pedro, start everything"
    echo "  stop                 Shut down VM and dashboard"
    echo "  status               Show component status"
    echo "  ssh                  SSH into the VM"
    echo "  cli                  CLI dashboard (DuckDB + Textual TUI)"
    echo "  web                  Start Streamlit web dashboard"
    echo "  logs                 Tail Pedro and workload logs from VM"
    echo "  down                 Stop everything and delete all cached data"
    echo ""
    echo "Flags:"
    echo "  --build-all   Install full dev environment (bloaty, bpftool, moroz, etc.)"
    echo "                Default: build deps only (~5-10 min vs ~20 min)"
    echo ""
    echo "Prerequisites:"
    if [ "$(detect_os)" = "linux" ]; then
        echo "  sudo apt-get install qemu-system-x86 genisoimage"
    else
        echo "  brew install qemu cdrtools"
    fi
    echo "  # For web dashboard: Docker"
    echo ""
    echo "First run takes ~5-10 min (downloading image + building Pedro)."
    echo "Subsequent runs reuse the cached VM disk."
}

case "${1:-help}" in
    start)
        shift
        while [ $# -gt 0 ]; do
            case "$1" in
                --build-all) DEMO_BUILD_ALL=1 ;;
                *) err "Unknown flag for start: $1"; cmd_help; exit 1 ;;
            esac
            shift
        done
        cmd_start
        ;;
    stop)    cmd_stop ;;
    status)  cmd_status ;;
    ssh)     shift; cmd_ssh "$@" ;;
    cli)     cmd_cli ;;
    web)     cmd_web ;;
    logs)    cmd_logs ;;
    down) cmd_down ;;
    help|-h|--help) cmd_help ;;
    *)
        err "Unknown command: $1"
        cmd_help
        exit 1
        ;;
esac
