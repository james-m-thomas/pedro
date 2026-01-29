# Pedro Demo Environment

A turnkey environment that boots a Debian 13 QEMU VM, builds Pedro from source, and runs it in
lockdown mode with demo workloads and live telemetry dashboards. One command to start, works on
macOS (Apple Silicon or Intel) and Linux.

## Quick Start

```bash
brew install qemu cdrtools   # one-time setup
./demo/demo.sh start     # ~5-10 min first run
./demo/demo.sh cli       # CLI dashboard
```

## Prerequisites

| Package | Install | Required for |
|---------|---------|-------------|
| QEMU | `brew install qemu` | VM |
| cdrtools | `brew install cdrtools` | Cloud-init ISO |
| Docker Desktop | [docker.com](https://docker.com/products/docker-desktop) | Web dashboard only |
| SSH key | `ssh-keygen -t ed25519` | VM access |

The script checks for missing dependencies and offers to install them via Homebrew.

**Resource requirements:** 8GB RAM and ~30GB disk for the VM. First run downloads a ~600MB cloud
image and builds Pedro from source (cached for subsequent runs).

## Commands

```
./demo/demo.sh <command>

  start     Download image, provision VM, build Pedro, start everything
  stop      Shut down VM and dashboards
  status    Show VM, Pedro, workloads, and dashboard status
  ssh       SSH into the VM (port 2222)
  cli       CLI dashboard — live exec events with color-coded DENY/ALLOW
  web       Streamlit web dashboard at http://localhost:8501
  logs      Tail Pedro and workload logs from VM journal
  destroy   Stop everything, delete VM disk and telemetry data
```

## What It Does

The demo showcases Pedro's core capabilities:

1. **BPF LSM enforcement**: Pedro runs in `LOCKDOWN` mode, blocking any binary whose SHA256 hash is
   on the blocklist. The demo blocks `/usr/bin/lsmod` (actually `/usr/bin/kmod`).

2. **Process telemetry**: Every `exec` event is captured and written to Parquet files with full
   process metadata — PID, UID, executable path, SHA256 hash, argv, file descriptors, and more.

3. **Blocked execution demo**: A workload generator periodically executes `/usr/bin/lsmod`. Pedro
   SIGKILLs the process before it runs and logs a `DENY` event. Benign commands (`ls`, `date`,
   `uname`, etc.) execute normally as `ALLOW` events.

4. **Live dashboards**: Both a terminal TUI and a Streamlit web app read the Parquet spool and
   display events in real time, with denied executions highlighted in red.

## Architecture

```
macOS Host
├── demo/demo.sh                        Orchestrator
├── QEMU VM (Debian 13, 4 CPU, 8GB RAM)
│   ├── Pedro (lockdown mode)               BPF LSM enforcement
│   ├── Pedrito                              Unprivileged event processor
│   └── Workload generator                   Periodic exec of blocked + benign binaries
│        └── /opt/pedro-data/telemetry/spool/*.exec.msg  (Parquet files)
│
├── rsync loop (every 2s)                   VM → host data sync
│   └── demo/.data/telemetry/spool/         Host-side Parquet mirror
│
├── CLI dashboard (cli.py)                  DuckDB + Textual TUI
└── Streamlit container (app.py)            Web dashboard at :8501
```

### VM Provisioning (two-phase boot)

The VM uses cloud-init for automated provisioning:

1. **First boot**: Installs build dependencies via `scripts/setup.sh`, configures GRUB
   for BPF LSM and IMA, then reboots to activate kernel parameters.

2. **Second boot**: Copies the source tree to a writable directory, runs `scripts/build.sh` to
   compile Pedro, hashes `/usr/bin/lsmod` for the blocklist, writes the startup script, and
   launches Pedro and the workload generator as systemd services.

### Data Transport

An rsync loop syncs `/opt/pedro-data/telemetry/` from the VM to `demo/.data/telemetry/` on the host
every 2 seconds.

### Telemetry Schema

Parquet files use the [Santa-compatible schema](../vendor/rednose/) with these key fields:

| Field | Description |
|-------|-------------|
| `common.event_time` | Timestamp of the exec event |
| `target.id.pid` | PID of the executed process |
| `target.executable.path.path` | Path of the executable |
| `target.executable.hash.value` | SHA256 hash of the binary |
| `argv` | Command-line arguments (BPF blob array) |
| `decision` | `ALLOW` or `DENY` |
| `mode` | `LOCKDOWN` or `MONITOR` |

## Dashboard Details

### CLI Dashboard (`cli`)

Uses DuckDB to query Parquet files and Textual for a full-screen TUI:
- Auto-refreshes every 2 seconds
- Color-coded: red for DENY, green for ALLOW, magenta for LOCKDOWN mode
- Shows timestamp, PID, UID, executable path, args, decision, and mode
- Summary stats: total events, denied count, unique binaries, spool file count

Requires `pip install duckdb textual` (auto-installed on first run).

### Web Dashboard (`web`)

Streamlit app running in Docker with four tabs:
- **Live Feed**: Most recent 200 exec events, denied rows highlighted red
- **Denied**: Filtered view of blocked binaries with SHA256 hashes
- **Binary Stats**: Bar chart of most-executed binaries with allow/deny breakdown
- **Test Binaries**: Run predefined blocked binaries or upload custom binaries to test

Auto-refreshes every 3 seconds. Runs at http://localhost:8501.

## File Layout

```
demo/
├── demo.sh                 Main orchestrator
├── vm/
│   ├── user-data               Cloud-init: users, provisioning scripts, systemd units
│   └── meta-data               Cloud-init: instance-id, hostname
├── dashboard/
│   ├── cli.py                  CLI dashboard (DuckDB + Textual)
│   ├── app.py                  Streamlit web dashboard
│   ├── Dockerfile              Streamlit container image
│   ├── docker-compose.yml      Compose config (mounts .data/ read-only)
│   └── requirements.txt        Python dependencies
├── blocking/
│   └── global.toml             Demo config: lockdown mode with blocklist
├── .cache/                     Downloaded images, VM disk, PIDs (gitignored)
└── .data/                      Parquet telemetry mirror from VM (gitignored)
```

## Troubleshooting

**First boot takes a long time**: The VM downloads and compiles dependencies from source
including Rust and Bazel. This is normal for the first run (~5-10 min, or ~15-20 min with
`--build-all`). Use `./demo/demo.sh logs` to monitor progress.

**Pedro not starting**: Check `./demo/demo.sh ssh` then `sudo journalctl -u pedro-demo -u
pedro-provision --no-pager` inside the VM. Common causes: provisioning still running, or kernel
boot parameters not active yet (needs the post-firstboot reboot).

**No telemetry data on host**: Run `./demo/demo.sh status` to check if the rsync loop is
active. If not, restart with `./demo/demo.sh stop && ./demo/demo.sh start`. You can also
manually sync: `rsync -az -e "ssh -p 2222" pedro@localhost:/opt/pedro-data/telemetry/
demo/.data/telemetry/`.

**VM not booting (aarch64)**: Ensure QEMU was installed with `brew install qemu` which includes
the UEFI firmware (`edk2-aarch64-code.fd`). The script looks for it in standard Homebrew and
Linux paths.

**Subsequent starts are fast**: The VM disk and build artifacts are cached in `demo/.cache/`. Only
the first run downloads the image and builds Pedro. `destroy` deletes the VM disk but keeps the
base image.
