# Demo Environment — Claude Code Notes

This directory contains the turnkey Pedro demo harness. It boots a QEMU VM, builds Pedro inside
it, and provides dashboards for live telemetry.

## Directory Structure

- `demo.sh` — Orchestrator script. All user interaction goes through this.
- `vm/user-data` — Cloud-init config. Contains all provisioning logic: firstboot script, secondboot
  script, systemd unit files, and the workload generator. This is the most complex file and the
  source of most provisioning bugs.
- `vm/meta-data` — Trivial cloud-init metadata (instance-id, hostname).
- `dashboard/cli.py` — CLI dashboard. Uses DuckDB + Textual. Reads Parquet from `.data/telemetry/spool/`.
- `dashboard/app.py` — Streamlit web dashboard. Runs in Docker. Same data source.
- `dashboard/Dockerfile`, `dashboard/docker-compose.yml` — Container setup for web dashboard.
- `.cache/` — Downloaded cloud images, VM disk, PID files, serial log. Gitignored.
- `.data/` — Parquet telemetry files synced from VM. Gitignored.
- `blocking/` — Pre-existing demo config (global.toml), not used by the VM harness.

## How Provisioning Works

`vm/user-data` defines a cloud-init config with `write_files` that creates:

1. `/opt/pedro-firstboot.sh` — Runs `scripts/setup.sh`, then reboots for GRUB/BPF changes.
2. `/opt/pedro-secondboot.sh` — Copies source to writable dir, builds Pedro with Bazel, writes the
   start script, starts Pedro and workloads.
3. `/opt/pedro-workloads.sh` — Loop that runs benign commands and periodically executes the blocked
   binary (`/usr/bin/lsmod`).
4. Three systemd units:
   - `pedro-provision.service` — Oneshot that dispatches to firstboot or secondboot.
   - `pedro-demo.service` — `Type=exec` service running Pedro (important: not `Type=forking`,
     because pedro exec's into pedrito rather than forking).
   - `pedro-workloads.service` — Runs the demo workload generator as user `pedro`.

### Key Provisioning Details

- `set -eux` is used in all scripts. This means unset variables are fatal (`-u`).
  `HOME` must be explicitly set (`export HOME="${HOME:-/root}"`) because systemd doesn't set it.
- `setup.sh` installs cargo to `$HOME/.cargo/bin`. PATH must include this before sccache install.
- `setup.sh` creates symlinks in `/usr/local/bin/` pointing to `/root/go/bin/`. User `pedro` can't
  traverse `/root/`, so firstboot replaces these symlinks with copies.
- The git clone is owned by root, so secondboot copies the source to `/home/pedro/pedro-build/` for
  building. Uses `cp -a` not `rsync` (rsync may not be installed at that point).
- `/opt/pedro-data/` must be `chown -R pedro:pedro` because pedrito writes telemetry there as user
  pedro.

## Data Flow

```
VM: pedro → pedrito → /opt/pedro-data/telemetry/spool/*.exec.msg (Parquet)
         ↓ rsync (every 2s)
Host: demo/.data/telemetry/spool/*.exec.msg
         ↓ DuckDB
     cli.py (CLI) or app.py (Streamlit)
```

Parquet files contain exec events with a deeply nested schema. Key query pattern:

```sql
SELECT
    common.event_time,
    target.id.pid,
    replace(target.executable.path.path, chr(0), '') AS path,
    decision,
    mode
FROM read_parquet('demo/.data/telemetry/spool/*.exec.msg')
ORDER BY common.event_time DESC
```

Note: BPF strings have trailing null bytes (`\x00`). Strip them with `replace(..., chr(0), '')`
in SQL or `.rstrip("\x00")` in Python.

## Common Issues When Modifying

- **Pedro service type**: Must be `Type=exec`, not `Type=forking`. Pedro uses `exec()` to become
  pedrito — systemd must track the original PID, not wait for a fork.
- **Systemd ConditionPathExists**: Conditions are cached. If the sentinel file doesn't exist when
  systemd first evaluates the condition, the unit stays failed until `daemon-reload` or reboot.
- **Shell quoting in cloud-init**: `write_files` uses YAML `content: |` blocks. The scripts inside
  are indented with 6 spaces, but `sed -i 's/^      //' ...` strips the indentation at runtime.
  Be careful with nested heredocs (the `pedro-start.sh` uses `cat > ... <<EOF`).
- **rsync**: The orchestrator runs an rsync loop to sync telemetry from the VM. rsync must be
  installed on the VM — it's in the firstboot apt packages.

## Testing Changes

After modifying `vm/user-data`, you need a fresh VM to test provisioning changes:

```bash
./demo/demo.sh destroy   # Removes VM disk, keeps base image
./demo/demo.sh start     # Rebuilds from scratch (~15 min)
```

For live fixes on a running VM, SSH in and edit directly, then update `user-data` to match:

```bash
./demo/demo.sh ssh
sudo vim /opt/pedro-secondboot.sh    # or whichever script
sudo systemctl restart pedro-demo    # test the change
```

Dashboard changes (cli.py, app.py) take effect immediately — no VM rebuild needed. For the web
dashboard, rebuild the container: `cd demo/dashboard && docker compose up -d --build`.
