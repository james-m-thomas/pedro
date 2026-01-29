#!/usr/bin/env python3
"""CLI dashboard for Pedro telemetry.

Interactive terminal UI built on Textual with live event table,
search/filtering, test binary execution, and a log viewer.

Usage:
    python3 cli.py [SPOOL_DIR]
    # Default spool dir: ./demo/.data/telemetry/spool/

Keybindings:
    Enter     Show full event detail
    Escape    Close detail / clear search
    /         Focus search bar
    d         Toggle DENY-only filter
    a         Toggle ALLOW-only filter
    t         Open command input
    p         Run predefined blocked binary (lsmod)
    l         Toggle log viewer pane
    r         Force refresh
    q         Quit
"""

import glob
import os
import re
import subprocess
import sys

try:
    import duckdb
except ImportError:
    print("Missing dependency: duckdb", file=sys.stderr)
    print("  pip install duckdb", file=sys.stderr)
    sys.exit(1)

try:
    from textual.app import App, ComposeResult
    from textual.binding import Binding
    from textual.containers import Horizontal
    from textual.reactive import reactive
    from textual.screen import Screen
    from textual.timer import Timer
    from textual.widgets import (
        DataTable,
        Footer,
        Header,
        Input,
        Label,
        RichLog,
        Static,
    )
except ImportError:
    print("Missing dependency: textual", file=sys.stderr)
    print("  pip install textual", file=sys.stderr)
    sys.exit(1)

from rich.text import Text

# SSH configuration (matches demo.sh)
SSH_PORT = os.environ.get("PEDRO_SSH_PORT", "2222")
SSH_USER = os.environ.get("PEDRO_SSH_USER", "pedro")
SSH_HOST = os.environ.get("PEDRO_SSH_HOST", "localhost")
SSH_OPTS = [
    "-o", "StrictHostKeyChecking=no",
    "-o", "UserKnownHostsFile=/dev/null",
    "-o", "LogLevel=ERROR",
    "-p", SSH_PORT,
]

BLOCKED_BINARIES = [
    ("/usr/bin/lsmod", "Blocked by SHA256 hash"),
]


def find_spool_dir():
    """Find the parquet spool directory."""
    if len(sys.argv) > 1:
        return sys.argv[1]
    candidates = [
        "demo/.data/telemetry/spool",
        "../demo/.data/telemetry/spool",
        os.path.join(os.path.dirname(__file__), "..", ".data", "telemetry", "spool"),
    ]
    for c in candidates:
        if os.path.isdir(c):
            return c
    return "demo/.data/telemetry/spool"


def ssh_run(cmd, timeout=15):
    """Run a command on the VM via SSH."""
    ssh_cmd = ["ssh"] + SSH_OPTS + [f"{SSH_USER}@{SSH_HOST}", cmd]
    try:
        result = subprocess.run(
            ssh_cmd, capture_output=True, text=True, timeout=timeout
        )
        return result.returncode, result.stdout, result.stderr
    except subprocess.TimeoutExpired:
        return -1, "", "Timed out"
    except Exception as e:
        return -1, "", str(e)


class StatsBar(Static):
    """Horizontal bar showing summary statistics."""

    stats = reactive({"total": 0, "denied": 0, "unique_bins": 0, "files": 0})

    def render(self):
        s = self.stats
        text = Text()
        text.append("  Events: ", style="dim")
        text.append(str(s["total"]), style="bold white")
        text.append("    Denied: ", style="dim")
        text.append(
            str(s["denied"]),
            style="bold red" if s["denied"] > 0 else "dim",
        )
        text.append("    Unique Bins: ", style="dim")
        text.append(str(s["unique_bins"]), style="bold white")
        text.append("    Spool Files: ", style="dim")
        text.append(str(s["files"]), style="bold white")

        filter_parts = []
        app = self.app
        if app.search_text:
            filter_parts.append(f"search={app.search_text}")
        if app.decision_filter != "ALL":
            filter_parts.append(f"decision={app.decision_filter}")
        if filter_parts:
            text.append("    Filters: ", style="dim")
            text.append(", ".join(filter_parts), style="yellow")

        return text


class CommandOutput(RichLog):
    """Scrollable pane for command output and log streaming."""
    pass


class EventDetailScreen(Screen):
    """Full-screen detail view for a single exec event."""

    BINDINGS = [
        Binding("escape", "go_back", "Back"),
        Binding("q", "go_back", "Back"),
        Binding("slash", "focus_search", "Search", key_display="/"),
        Binding("n", "next_match", "Next", show=False),
        Binding("N", "prev_match", "Prev", show=False),
    ]

    CSS = """
    #detail-search {
        height: 3;
        padding: 0 1;
    }
    #detail-log {
        height: 1fr;
        padding: 0 1;
    }
    """

    def __init__(self, lines: list):
        super().__init__()
        self._lines = lines
        self._match_lines: list[int] = []
        self._match_cursor: int = 0

    def compose(self) -> ComposeResult:
        yield Header()
        yield Input(placeholder="Search...", id="detail-search")
        yield RichLog(id="detail-log", wrap=True)
        yield Footer()

    def on_mount(self) -> None:
        log = self.query_one("#detail-log", RichLog)
        for line in self._lines:
            log.write(line)

    def action_go_back(self) -> None:
        search = self.query_one("#detail-search", Input)
        if self.focused is search:
            self.query_one("#detail-log", RichLog).focus()
        else:
            self.app.pop_screen()

    def action_focus_search(self) -> None:
        self.query_one("#detail-search", Input).focus()

    def action_next_match(self) -> None:
        if self._match_lines:
            self._match_cursor = (self._match_cursor + 1) % len(self._match_lines)
            self._rerender()

    def action_prev_match(self) -> None:
        if self._match_lines:
            self._match_cursor = (self._match_cursor - 1) % len(self._match_lines)
            self._rerender()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "detail-search":
            self._highlight(event.value)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "detail-search":
            self.query_one("#detail-log", RichLog).focus()

    def _highlight(self, query: str) -> None:
        self._match_lines = []
        self._match_cursor = 0
        if query:
            pattern = f"(?i){re.escape(query)}"
            for i, line in enumerate(self._lines):
                if isinstance(line, Text) and re.search(pattern, line.plain):
                    self._match_lines.append(i)
        self._rerender()

    def _rerender(self) -> None:
        log = self.query_one("#detail-log", RichLog)
        query = self.query_one("#detail-search", Input).value
        log.clear()
        current_line = (
            self._match_lines[self._match_cursor]
            if self._match_lines
            else -1
        )
        for i, line in enumerate(self._lines):
            if query and isinstance(line, Text):
                highlighted = line.copy()
                style = (
                    "bold white on dark_blue"
                    if i == current_line
                    else "bold black on yellow"
                )
                highlighted.highlight_regex(
                    f"(?i){re.escape(query)}", style=style,
                )
                log.write(highlighted)
            else:
                log.write(line)
        if current_line >= 0:
            log.scroll_to(
                y=max(0, current_line - log.size.height // 2),
                animate=False,
            )


class PedroDashboard(App):
    """Pedro telemetry TUI dashboard."""

    TITLE = "Pedro Telemetry"
    CSS = """
    Screen {
        layout: vertical;
    }
    #stats-bar {
        height: 1;
        background: $surface;
        padding: 0 1;
    }
    #search-bar {
        height: 3;
        padding: 0 1;
    }
    #search-input {
        width: 1fr;
    }
    #filter-label {
        width: auto;
        padding: 1 1 0 0;
        color: $text-muted;
    }
    #events-table {
        height: 1fr;
    }
    #bottom-pane {
        height: 12;
        border-top: solid $primary;
        display: none;
    }
    #bottom-pane.visible {
        display: block;
    }
    #cmd-input {
        dock: bottom;
        height: 3;
        padding: 0 1;
        display: none;
    }
    #cmd-input.visible {
        display: block;
    }
    DataTable {
        height: 1fr;
    }
    """

    BINDINGS = [
        Binding("slash", "focus_search", "Search", key_display="/"),
        Binding("d", "toggle_deny", "DENY only"),
        Binding("a", "toggle_allow", "ALLOW only"),
        Binding("t", "open_cmd", "Run command"),
        Binding("p", "run_predefined", "Run blocked"),
        Binding("l", "toggle_logs", "Logs"),
        Binding("r", "refresh", "Refresh"),
        Binding("q", "quit", "Quit"),
        Binding("escape", "escape", "Back", show=False),
    ]

    search_text = reactive("")
    decision_filter = reactive("ALL")

    def __init__(self, spool_dir: str):
        super().__init__()
        self.spool_dir = spool_dir
        self.con = duckdb.connect(":memory:")
        self._refresh_timer: Timer | None = None
        self._log_visible = False
        self._row_event_ids: list[str] = []

    def compose(self) -> ComposeResult:
        yield Header()
        yield StatsBar(id="stats-bar")
        with Horizontal(id="search-bar"):
            yield Label("Filter: ", id="filter-label")
            yield Input(
                placeholder="Type to search (path, PID, decision)...",
                id="search-input",
            )
        yield DataTable(id="events-table")
        yield CommandOutput(id="bottom-pane", wrap=True, highlight=True, markup=True)
        yield Input(
            placeholder="Enter command to run on VM...",
            id="cmd-input",
        )
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#events-table", DataTable)
        table.cursor_type = "row"
        self._add_table_columns()
        self._do_refresh()
        self._refresh_timer = self.set_interval(2, self._do_refresh)
        table.focus()

    def _col_widths(self):
        """Calculate column widths to fill terminal width."""
        w = self.size.width if self.size.width > 0 else 120
        time_w, pid_w, uid_w, dec_w, mode_w = 10, 8, 6, 10, 10
        fixed = time_w + pid_w + uid_w + dec_w + mode_w
        remaining = max(w - fixed - 18, 20)  # 18 for column padding/gutters
        path_w = min(max(int(remaining * 0.35), 15), 45)
        args_w = max(remaining - path_w, 8)
        return time_w, pid_w, uid_w, path_w, args_w, dec_w, mode_w

    def _add_table_columns(self):
        table = self.query_one("#events-table", DataTable)
        table.clear(columns=True)
        tw, pw, uw, pathw, aw, dw, mw = self._col_widths()
        table.add_column("Time", width=tw, key="time")
        table.add_column("PID", width=pw, key="pid")
        table.add_column("UID", width=uw, key="uid")
        table.add_column("Path", width=pathw, key="path")
        table.add_column("Args", width=aw, key="args")
        table.add_column("Decision", width=dw, key="decision")
        table.add_column("Mode", width=mw, key="mode")

    def on_resize(self, event) -> None:
        if isinstance(self.screen, EventDetailScreen):
            return
        self._add_table_columns()
        self._do_refresh()

    def _get_pattern(self):
        return os.path.join(self.spool_dir, "*.exec.msg")

    def _do_refresh(self) -> None:
        if isinstance(self.screen, EventDetailScreen):
            return
        pattern = self._get_pattern()
        files = glob.glob(pattern)
        if not files:
            stats_bar = self.query_one("#stats-bar", StatsBar)
            stats_bar.stats = {"total": 0, "denied": 0, "unique_bins": 0, "files": 0}
            return

        # Query stats
        try:
            row = self.con.execute(f"""
                SELECT
                    count(*) AS total,
                    count(*) FILTER (WHERE decision = 'DENY') AS denied,
                    count(DISTINCT target.executable.path.path) AS unique_bins
                FROM read_parquet('{pattern}')
            """).fetchone()
            stats = {
                "total": row[0],
                "denied": row[1],
                "unique_bins": row[2],
                "files": len(files),
            }
        except Exception:
            stats = {"total": 0, "denied": 0, "unique_bins": 0, "files": len(files)}

        stats_bar = self.query_one("#stats-bar", StatsBar)
        stats_bar.stats = stats

        # Build WHERE clause
        clauses = []
        if self.search_text:
            safe = self.search_text.replace("'", "''")
            clauses.append(
                f"(replace(target.executable.path.path, chr(0), '') ILIKE '%{safe}%'"
                f" OR decision ILIKE '%{safe}%'"
                f" OR CAST(target.id.pid AS VARCHAR) ILIKE '%{safe}%')"
            )
        if self.decision_filter != "ALL":
            clauses.append(f"decision = '{self.decision_filter}'")
        where = " AND ".join(clauses) if clauses else "TRUE"

        # Query events
        try:
            rows = self.con.execute(f"""
                SELECT
                    strftime(common.event_time, '%H:%M:%S') AS time,
                    target.id.pid AS pid,
                    target.user.uid AS uid,
                    target.executable.path.path AS path,
                    CASE
                        WHEN length(argv) > 0
                        THEN list_transform(argv[1:3], x -> decode(x))
                        ELSE []
                    END AS args,
                    decision,
                    mode,
                    common.event_id AS event_id
                FROM read_parquet('{pattern}')
                WHERE {where}
                ORDER BY common.event_time DESC
                LIMIT 100
            """).fetchall()
        except Exception:
            rows = []

        # Update table, preserving cursor position
        table = self.query_one("#events-table", DataTable)
        prev_cursor = table.cursor_row
        table.clear()
        self._row_event_ids = []
        _, _, _, path_max, args_max, _, _ = self._col_widths()

        for row in rows:
            t, pid, path, args_list, decision, mode, uid, event_id = (
                row[0], row[1], row[3], row[4], row[5], row[6], row[2], row[7]
            )
            self._row_event_ids.append(str(event_id))

            # Format args
            args_str = ""
            if args_list:
                args_str = " ".join(str(a).rstrip("\x00") for a in args_list[:3])
                if len(args_str) > args_max:
                    args_str = args_str[:args_max - 3] + "..."

            # Strip nulls and truncate path
            path_str = str(path or "").rstrip("\x00")
            if len(path_str) > path_max:
                path_str = "..." + path_str[-(path_max - 3):]

            # Style decision
            if decision == "DENY":
                dec_text = Text(decision, style="bold red")
                path_text = Text(path_str, style="red")
            elif decision == "ALLOW":
                dec_text = Text(decision, style="green")
                path_text = Text(path_str)
            else:
                dec_text = Text(decision or "?", style="yellow")
                path_text = Text(path_str, style="yellow")

            # Style mode
            if mode == "LOCKDOWN":
                mode_text = Text(mode, style="bold magenta")
            else:
                mode_text = Text(mode or "?", style="dim")

            table.add_row(
                str(t or ""),
                str(pid or ""),
                str(uid or ""),
                path_text,
                args_str,
                dec_text,
                mode_text,
            )

        if rows:
            table.move_cursor(row=min(prev_cursor, len(rows) - 1))

    # ── Event detail drill-down ──────────────────────────────────────────────

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        idx = event.cursor_row
        if 0 <= idx < len(self._row_event_ids):
            self._show_event_detail(self._row_event_ids[idx])

    def _show_event_detail(self, event_id: str) -> None:
        pattern = self._get_pattern()
        try:
            result = self.con.execute(f"""
                SELECT * FROM read_parquet('{pattern}')
                WHERE common.event_id = {event_id}
                LIMIT 1
            """)
            cols = [desc[0] for desc in result.description]
            row_data = result.fetchone()
        except Exception as e:
            self.notify(f"Query error: {e}", severity="error")
            return

        if not row_data:
            self.notify("Event not found", severity="warning")
            return

        data = dict(zip(cols, row_data))
        lines = self._format_event_detail(data)
        self.push_screen(EventDetailScreen(lines))

    @staticmethod
    def _safe_get(d, *keys):
        """Navigate nested dicts, returning None if any key is missing."""
        for k in keys:
            if not isinstance(d, dict):
                return None
            d = d.get(k)
        return d

    def _format_event_detail(self, data: dict) -> list:
        _g = self._safe_get
        lines = []

        def section(title):
            lines.append(Text())
            bar = "\u2500" * max(1, 64 - len(title) - 4)
            lines.append(Text(f"\u2500\u2500 {title} {bar}", style="bold cyan"))

        def field(label, value, style="white"):
            if value is None:
                return
            s = str(value).rstrip("\x00")
            if not s:
                return
            lines.append(
                Text(f"  {label:<16} ", style="dim") + Text(s, style=style)
            )

        common = data.get("common") or {}
        target = data.get("target") or {}
        instigator = data.get("instigator")
        script = data.get("script")
        cwd_data = data.get("cwd") or {}
        decision = data.get("decision")
        reason = data.get("reason")
        mode = data.get("mode")
        argv = data.get("argv") or []
        envp = data.get("envp") or []
        fdt = data.get("fdt") or []
        fdt_trunc = data.get("fdt_truncated", False)

        # ── Event summary
        section("Event")
        dec_style = "bold red" if decision == "DENY" else "bold green"
        mode_style = "bold magenta" if mode == "LOCKDOWN" else "dim"
        field("Decision", decision, dec_style)
        field("Mode", mode, mode_style)
        field("Reason", reason)
        et = common.get("event_time")
        field("Event Time", str(et)[:23] if et else None)
        pt = common.get("processed_time")
        field("Processed", str(pt)[:23] if pt else None)
        field("Event ID", common.get("event_id"))

        # ── Target process
        section("Target Process")
        field("PID", _g(target, "id", "pid"))
        field("NS PID", target.get("linux_local_ns_pid"))
        field("Parent PID", _g(target, "parent_id", "pid"))
        field("Process Cookie", _g(target, "id", "process_cookie"))

        uid = _g(target, "user", "uid")
        uname = _g(target, "user", "name")
        if uid is not None:
            field("User", f"{uid}" + (f" ({uname})" if uname else ""))
        gid = _g(target, "group", "gid")
        gname = _g(target, "group", "name")
        if gid is not None:
            field("Group", f"{gid}" + (f" ({gname})" if gname else ""))
        field("Session ID", target.get("session_id"))

        exe_path = _g(target, "executable", "path", "path")
        if exe_path:
            field("Executable", str(exe_path).rstrip("\x00"))
        field("Inode", _g(target, "executable", "stat", "ino"))

        hash_val = _g(target, "executable", "hash", "value")
        hash_algo = _g(target, "executable", "hash", "algorithm")
        if hash_val:
            hex_h = (
                hash_val.hex()
                if isinstance(hash_val, (bytes, bytearray))
                else str(hash_val)
            )
            field(
                "Hash",
                f"{hash_algo}: {hex_h}" if hash_algo else hex_h,
                "yellow",
            )

        start = target.get("start_time")
        field("Started", str(start)[:23] if start else None)
        tty = _g(target, "tty", "path")
        field("TTY", str(tty).rstrip("\x00") if tty else None)
        cwd_path = _g(cwd_data, "path")
        field("CWD", str(cwd_path).rstrip("\x00") if cwd_path else None)

        # ── Arguments
        if argv:
            decoded_args = []
            for arg in argv:
                if arg:
                    try:
                        s = bytes(arg).decode("utf-8", errors="replace").rstrip("\x00")
                        if s:
                            decoded_args.append(s)
                    except Exception:
                        pass
            if decoded_args:
                section("Arguments")
                for i, a in enumerate(decoded_args):
                    lines.append(Text(f"  [{i}] ", style="dim") + Text(a))

        # ── Environment
        if envp:
            decoded_env = []
            for entry in envp:
                if entry:
                    try:
                        s = bytes(entry).decode("utf-8", errors="replace").rstrip("\x00")
                        if s and "=" in s and len(s) > 2 and s[0].isprintable():
                            decoded_env.append(s)
                    except Exception:
                        pass
            if decoded_env:
                section("Environment")
                for e in decoded_env:
                    lines.append(Text(f"  {e}", style="dim white"))

        # ── Instigator
        if isinstance(instigator, dict) and _g(instigator, "id", "pid") is not None:
            section("Instigator")
            field("PID", _g(instigator, "id", "pid"))
            field("User", _g(instigator, "user", "uid"))
            ins_path = _g(instigator, "executable_path", "path")
            field("Executable", str(ins_path).rstrip("\x00") if ins_path else None)

        # ── Script
        if isinstance(script, dict):
            sc_path = _g(script, "path", "path")
            if sc_path:
                section("Script")
                field("Path", str(sc_path).rstrip("\x00"))
                sc_hash = _g(script, "hash", "value")
                if sc_hash and isinstance(sc_hash, (bytes, bytearray)):
                    field("Hash", sc_hash.hex(), "yellow")

        # ── File descriptors
        if isinstance(fdt, list):
            valid_fds = [
                e for e in fdt
                if isinstance(e, dict) and e.get("fd") is not None
            ]
            if valid_fds:
                section("File Descriptors")
                for entry in valid_fds[:30]:
                    fd = entry.get("fd", "?")
                    ft = entry.get("file_type", "?")
                    lines.append(
                        Text(f"  fd={fd:<4} ", style="dim")
                        + Text(str(ft or ""), style="dim white")
                    )
                if len(valid_fds) > 30:
                    lines.append(
                        Text(f"  ... and {len(valid_fds) - 30} more", style="dim")
                    )
                if fdt_trunc:
                    lines.append(Text("  (truncated)", style="dim yellow"))

        # ── Common
        section("Common")
        field("Boot UUID", common.get("boot_uuid"))
        field("Machine ID", common.get("machine_id"))
        field("Agent", common.get("agent"))

        lines.append(Text())
        return lines

    # ── Input handling ───────────────────────────────────────────────────────

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "search-input":
            self.search_text = event.value
            self._do_refresh()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "cmd-input":
            cmd = event.value.strip()
            if cmd:
                self._run_vm_command(cmd)
            event.input.value = ""
            event.input.remove_class("visible")
            self.query_one("#events-table", DataTable).focus()
        elif event.input.id == "search-input":
            self.query_one("#events-table", DataTable).focus()

    # ── Actions ──────────────────────────────────────────────────────────────

    def action_focus_search(self) -> None:
        self.query_one("#search-input", Input).focus()

    def action_toggle_deny(self) -> None:
        if self.decision_filter == "DENY":
            self.decision_filter = "ALL"
        else:
            self.decision_filter = "DENY"
        self._do_refresh()

    def action_toggle_allow(self) -> None:
        if self.decision_filter == "ALLOW":
            self.decision_filter = "ALL"
        else:
            self.decision_filter = "ALLOW"
        self._do_refresh()

    def action_open_cmd(self) -> None:
        cmd_input = self.query_one("#cmd-input", Input)
        cmd_input.add_class("visible")
        cmd_input.focus()

    def action_run_predefined(self) -> None:
        if BLOCKED_BINARIES:
            binary, _ = BLOCKED_BINARIES[0]
            self._run_vm_command(binary)

    def action_toggle_logs(self) -> None:
        pane = self.query_one("#bottom-pane", CommandOutput)
        self._log_visible = not self._log_visible
        if self._log_visible:
            pane.add_class("visible")
            pane.write(Text("Log viewer active. Press 'l' to hide.", style="dim"))
            # Fetch recent Pedro logs
            self._fetch_logs()
        else:
            pane.remove_class("visible")

    def action_refresh(self) -> None:
        self._do_refresh()

    def action_escape(self) -> None:
        cmd_input = self.query_one("#cmd-input", Input)
        if cmd_input.has_class("visible"):
            cmd_input.remove_class("visible")
        # Always return focus to table; keep search text intact
        self.query_one("#events-table", DataTable).focus()

    def on_key(self, event) -> None:
        if isinstance(self.screen, EventDetailScreen):
            return
        # Down arrow from search bar → focus the table (keep filter active)
        if event.key == "down":
            search = self.query_one("#search-input", Input)
            if self.focused is search:
                self.query_one("#events-table", DataTable).focus()

    # ── VM command execution ─────────────────────────────────────────────────

    def _run_vm_command(self, cmd: str) -> None:
        pane = self.query_one("#bottom-pane", CommandOutput)
        pane.add_class("visible")
        self._log_visible = True

        pane.write(Text(f"$ {cmd}", style="bold cyan"))

        code, stdout, stderr = ssh_run(cmd)

        if code == 137:
            pane.write(Text("KILLED (SIGKILL) - Pedro blocked this execution", style="bold red"))
        elif code == 0:
            pane.write(Text(f"Exited normally (code {code})", style="green"))
        elif code == -1:
            pane.write(Text(f"Failed: {stderr}", style="yellow"))
        else:
            pane.write(Text(f"Exited with code {code}", style="dim"))

        output = (stdout + stderr).strip()
        if output:
            for line in output.split("\n")[:20]:
                pane.write(Text(f"  {line}", style="dim"))

        pane.write("")

    def _fetch_logs(self) -> None:
        """Fetch recent Pedro logs from the VM."""
        pane = self.query_one("#bottom-pane", CommandOutput)
        code, stdout, stderr = ssh_run(
            "sudo journalctl -u pedro-demo -u pedro-workloads --no-pager -n 20",
            timeout=10,
        )
        if code == 0 and stdout:
            for line in stdout.strip().split("\n"):
                pane.write(Text(line, style="dim"))
        elif stderr:
            pane.write(Text(f"Log fetch error: {stderr}", style="yellow"))


def main():
    spool_dir = find_spool_dir()
    app = PedroDashboard(spool_dir)
    app.run()


if __name__ == "__main__":
    main()
