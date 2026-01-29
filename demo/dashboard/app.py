"""Streamlit dashboard for Pedro telemetry.

Reads parquet spool files and shows live-updating exec event telemetry
with rich search/filtering and test binary execution.
"""

import base64
import glob
import os
import shlex
import subprocess
import tempfile
import time

import duckdb
import pandas as pd
import streamlit as st

SPOOL_DIR = os.environ.get("PEDRO_SPOOL_DIR", "/data/telemetry/spool")
SSH_PORT = os.environ.get("PEDRO_SSH_PORT", "2222")
SSH_USER = os.environ.get("PEDRO_SSH_USER", "pedro")
SSH_HOST = os.environ.get("PEDRO_SSH_HOST", "localhost")
SSH_OPTS = [
    "-o", "StrictHostKeyChecking=no",
    "-o", "UserKnownHostsFile=/dev/null",
    "-o", "LogLevel=ERROR",
    "-p", SSH_PORT,
]

# ── SVG Icons (monochrome, theme-matched) ────────────────────────────────────

SVG_SHIELD = '''<svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24"
  fill="none" stroke="#00bcd4" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
  <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/>
</svg>'''

SVG_LIST = '''<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24"
  fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
  <line x1="8" y1="6" x2="21" y2="6"/><line x1="8" y1="12" x2="21" y2="12"/>
  <line x1="8" y1="18" x2="21" y2="18"/><line x1="3" y1="6" x2="3.01" y2="6"/>
  <line x1="3" y1="12" x2="3.01" y2="12"/><line x1="3" y1="18" x2="3.01" y2="18"/>
</svg>'''

SVG_X_CIRCLE = '''<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24"
  fill="none" stroke="#ef5350" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
  <circle cx="12" cy="12" r="10"/><line x1="15" y1="9" x2="9" y2="15"/>
  <line x1="9" y1="9" x2="15" y2="15"/>
</svg>'''

SVG_BAR_CHART = '''<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24"
  fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
  <line x1="18" y1="20" x2="18" y2="10"/><line x1="12" y1="20" x2="12" y2="4"/>
  <line x1="6" y1="20" x2="6" y2="14"/>
</svg>'''

SVG_FLASK = '''<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24"
  fill="none" stroke="#ff9800" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
  <path d="M9 3h6v5.586l4.707 4.707A1 1 0 0 1 19 14.707V19a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2v-4.293
    a1 1 0 0 1 .293-.707L10 9.586V3z"/>
  <line x1="9" y1="3" x2="15" y2="3"/>
</svg>'''

# Shield as a base64 data URI for page_icon
_SHIELD_FAVICON = (
    "data:image/svg+xml;base64,"
    + base64.b64encode(
        b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" '
        b'stroke="#00bcd4" stroke-width="2"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7'
        b'c0 6 8 10 8 10z"/></svg>'
    ).decode()
)


def svg_icon(svg: str) -> str:
    """Wrap an SVG string for inline display in markdown."""
    return svg.replace("\n", " ").strip()


# ── Page config ──────────────────────────────────────────────────────────────

st.set_page_config(
    page_title="Pedro Telemetry",
    page_icon=_SHIELD_FAVICON,
    layout="wide",
)

# Inject CSS for custom tab styling
st.markdown(
    """<style>
    .svg-title { display: inline-flex; align-items: center; gap: 8px; }
    .svg-title svg { vertical-align: middle; }
    div[data-testid="stMetric"] { padding: 8px 0; }
    </style>""",
    unsafe_allow_html=True,
)


# ── Helpers ──────────────────────────────────────────────────────────────────

def get_parquet_pattern():
    return os.path.join(SPOOL_DIR, "*.exec.msg")


def has_data():
    return len(glob.glob(get_parquet_pattern())) > 0


def query(sql):
    """Run a DuckDB query and return a DataFrame."""
    con = duckdb.connect(":memory:")
    try:
        return con.execute(sql).fetchdf()
    except Exception as e:
        st.error(f"Query error: {e}")
        return pd.DataFrame()
    finally:
        con.close()


def ssh_run(cmd, timeout=30):
    """Run a command on the Pedro VM via SSH. Returns (exit_code, stdout, stderr)."""
    ssh_cmd = ["ssh"] + SSH_OPTS + [f"{SSH_USER}@{SSH_HOST}", cmd]
    try:
        result = subprocess.run(
            ssh_cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return result.returncode, result.stdout, result.stderr
    except subprocess.TimeoutExpired:
        return -1, "", "Command timed out"
    except Exception as e:
        return -1, "", str(e)


def ssh_available():
    """Check if SSH to the VM is available."""
    code, _, _ = ssh_run("true", timeout=5)
    return code == 0


def scp_to_vm(local_path, remote_path):
    """Copy a file to the VM via SCP."""
    scp_cmd = (
        ["scp"] + SSH_OPTS + [local_path, f"{SSH_USER}@{SSH_HOST}:{remote_path}"]
    )
    try:
        result = subprocess.run(scp_cmd, capture_output=True, text=True, timeout=30)
        return result.returncode == 0
    except Exception:
        return False


def build_filter_clauses(search, decision, mode, pid_filter, uid_filter, path_filter):
    """Build SQL WHERE clauses from filter inputs."""
    clauses = []
    if search:
        safe = search.replace("'", "''")
        clauses.append(
            f"(replace(target.executable.path.path, chr(0), '') ILIKE '%{safe}%'"
            f" OR decision ILIKE '%{safe}%'"
            f" OR CAST(target.id.pid AS VARCHAR) ILIKE '%{safe}%')"
        )
    if decision and decision != "ALL":
        clauses.append(f"decision = '{decision}'")
    if mode and mode != "ALL":
        clauses.append(f"mode = '{mode}'")
    if pid_filter:
        clauses.append(f"CAST(target.id.pid AS VARCHAR) LIKE '%{pid_filter}%'")
    if uid_filter:
        clauses.append(f"CAST(target.user.uid AS VARCHAR) = '{uid_filter}'")
    if path_filter:
        safe = path_filter.replace("'", "''")
        clauses.append(
            f"replace(target.executable.path.path, chr(0), '') ILIKE '%{safe}%'"
        )
    return " AND ".join(clauses) if clauses else "TRUE"


def result_badge(exit_code):
    """Render a colored badge for a command's exit code."""
    if exit_code == 137:
        st.error("KILLED (SIGKILL) - Pedro blocked this execution")
    elif exit_code == 0:
        st.success(f"Exited normally (code {exit_code})")
    elif exit_code == -1:
        st.warning("Execution failed (SSH error or timeout)")
    else:
        st.info(f"Exited with code {exit_code}")


# ── Header + Stats ───────────────────────────────────────────────────────────

if not has_data():
    st.markdown(
        f'<h2 class="svg-title">{svg_icon(SVG_SHIELD)} Pedro Telemetry</h2>',
        unsafe_allow_html=True,
    )
    st.warning(
        "No parquet files found. Waiting for Pedro to generate telemetry...\n\n"
        f"Looking in: `{get_parquet_pattern()}`"
    )
    time.sleep(2)
    st.rerun()

pattern = get_parquet_pattern()

hdr, col1, col2, col3, col4 = st.columns([2, 1, 1, 1, 1.5])
hdr.markdown(
    f'<h2 class="svg-title" style="margin:0">{svg_icon(SVG_SHIELD)} Pedro Telemetry</h2>',
    unsafe_allow_html=True,
)
stats = query(f"""
    SELECT
        count(*) AS total,
        count(*) FILTER (WHERE decision = 'DENY') AS denied,
        count(DISTINCT target.executable.path.path) AS unique_bins,
        max(common.event_time) AS latest
    FROM read_parquet('{pattern}')
""")

if not stats.empty:
    col1.metric("Total Events", int(stats["total"].iloc[0]))
    col2.metric("Denied", int(stats["denied"].iloc[0]))
    col3.metric("Unique Binaries", int(stats["unique_bins"].iloc[0]))
    latest = stats["latest"].iloc[0]
    col4.metric("Latest Event", str(latest)[:19] if pd.notna(latest) else "N/A")

# ── Filter bar ───────────────────────────────────────────────────────────────

with st.expander("Search & Filters", expanded=False):
    fc1, fc2, fc3 = st.columns(3)
    with fc1:
        search_text = st.text_input(
            "Search (path, PID, decision)", key="search", placeholder="e.g. lsmod"
        )
        pid_filter = st.text_input("PID", key="pid_filter", placeholder="e.g. 1234")
    with fc2:
        decision_filter = st.selectbox(
            "Decision", ["ALL", "ALLOW", "DENY"], key="decision"
        )
        uid_filter = st.text_input("UID", key="uid_filter", placeholder="e.g. 1000")
    with fc3:
        mode_filter = st.selectbox(
            "Mode", ["ALL", "MONITOR", "LOCKDOWN"], key="mode"
        )
        path_filter = st.text_input(
            "Path filter", key="path_filter", placeholder="e.g. /usr/bin"
        )

where = build_filter_clauses(
    search_text, decision_filter, mode_filter, pid_filter, uid_filter, path_filter
)

# ── Tabs ─────────────────────────────────────────────────────────────────────

tab_live, tab_denied, tab_binaries, tab_test = st.tabs(
    ["Live Feed", "Denied", "Binary Stats", "Test Binaries"]
)

# ── Live Feed ────────────────────────────────────────────────────────────────

with tab_live:
    st.markdown(
        f'{svg_icon(SVG_LIST)} **Recent Exec Events**', unsafe_allow_html=True
    )
    events = query(f"""
        SELECT
            strftime(common.event_time, '%Y-%m-%d %H:%M:%S') AS "Time",
            target.id.pid AS "PID",
            target.user.uid AS "UID",
            replace(target.executable.path.path, chr(0), '') AS "Executable",
            CASE
                WHEN length(argv) > 0
                THEN list_transform(argv[1:4], x -> replace(decode(x), chr(0), ''))
                ELSE []
            END AS "Args",
            decision AS "Decision",
            mode AS "Mode"
        FROM read_parquet('{pattern}')
        WHERE {where}
        ORDER BY common.event_time DESC
        LIMIT 200
    """)

    if not events.empty:
        def highlight_denied(row):
            if row["Decision"] == "DENY":
                return ["background-color: #3d0000; color: #ff6666"] * len(row)
            return [""] * len(row)

        st.dataframe(
            events.style.apply(highlight_denied, axis=1),
            use_container_width=True,
            height=500,
        )
    else:
        st.info("No events matching filters.")

# ── Denied ───────────────────────────────────────────────────────────────────

with tab_denied:
    st.markdown(
        f'{svg_icon(SVG_X_CIRCLE)} **Denied Executions (SIGKILL)**',
        unsafe_allow_html=True,
    )
    # Force decision=DENY but keep other filters
    deny_where = build_filter_clauses(
        search_text, "DENY", mode_filter, pid_filter, uid_filter, path_filter
    )
    denied = query(f"""
        SELECT
            strftime(common.event_time, '%Y-%m-%d %H:%M:%S') AS "Time",
            target.id.pid AS "PID",
            replace(target.executable.path.path, chr(0), '') AS "Executable",
            hex(target.executable.hash.value) AS "SHA256",
            target.user.uid AS "UID"
        FROM read_parquet('{pattern}')
        WHERE {deny_where}
        ORDER BY common.event_time DESC
        LIMIT 100
    """)

    if not denied.empty:
        st.error(f"**{len(denied)}** executions blocked")
        st.dataframe(denied, use_container_width=True, height=400)
    else:
        st.success("No denied executions matching filters.")

# ── Binary Stats ─────────────────────────────────────────────────────────────

with tab_binaries:
    st.markdown(
        f'{svg_icon(SVG_BAR_CHART)} **Most Executed Binaries**',
        unsafe_allow_html=True,
    )
    bins = query(f"""
        SELECT
            replace(target.executable.path.path, chr(0), '') AS "Executable",
            count(*) AS "Count",
            count(*) FILTER (WHERE decision = 'DENY') AS "Denied",
            count(*) FILTER (WHERE decision = 'ALLOW') AS "Allowed"
        FROM read_parquet('{pattern}')
        WHERE {where}
        GROUP BY "Executable"
        ORDER BY "Count" DESC
        LIMIT 25
    """)

    if not bins.empty:
        st.bar_chart(bins, x="Executable", y="Count")
        st.dataframe(bins, use_container_width=True)

# ── Test Binaries ────────────────────────────────────────────────────────────

with tab_test:
    st.markdown(
        f'{svg_icon(SVG_FLASK)} **Test Binary Execution**', unsafe_allow_html=True
    )
    st.caption(
        "Run binaries on the Pedro VM to test blocking rules. "
        "Blocked binaries will be killed by Pedro (exit code 137)."
    )

    # Check VM connectivity once
    if "vm_connected" not in st.session_state:
        st.session_state.vm_connected = ssh_available()

    if not st.session_state.vm_connected:
        st.warning(
            "Cannot reach VM via SSH. Is the demo environment running?\n\n"
            "Start with: `./demo/demo.sh start`"
        )
        if st.button("Retry connection"):
            st.session_state.vm_connected = ssh_available()
            st.rerun()
    else:
        test_s1, test_s2 = st.tabs(
            ["Predefined Tests", "Upload Binary"]
        )

        # ── Predefined Tests ──
        with test_s1:
            st.markdown("**Known blocked binaries:**")
            predefined = [
                ("/usr/bin/lsmod", "Blocked by SHA256 hash"),
            ]

            for binary, desc in predefined:
                c1, c2, c3 = st.columns([3, 4, 2])
                c1.code(binary, language=None)
                c2.caption(desc)
                if c3.button("Run", key=f"run_{binary}"):
                    with st.spinner(f"Executing {binary}..."):
                        code, stdout, stderr = ssh_run(f"{binary} 2>&1 || true")
                    result_badge(code)
                    if stdout:
                        st.code(stdout, language=None)
                    if stderr:
                        st.code(stderr, language=None)

        # ── Upload Binary ──
        with test_s2:
            st.markdown("**Upload a binary to the VM and execute it:**")
            uploaded = st.file_uploader(
                "Choose a binary file",
                type=None,
                key="upload_bin",
            )
            if uploaded is not None:
                st.caption(f"File: {uploaded.name} ({uploaded.size} bytes)")
                if st.button("Upload & Execute", key="upload_exec"):
                    with st.spinner("Uploading and executing..."):
                        remote_path = f"/tmp/pedro-test-{uploaded.name}"
                        with tempfile.NamedTemporaryFile(
                            delete=False, suffix=f"-{uploaded.name}"
                        ) as tmp:
                            tmp.write(uploaded.getvalue())
                            tmp_path = tmp.name

                        try:
                            if scp_to_vm(tmp_path, remote_path):
                                ssh_run(f"chmod +x {shlex.quote(remote_path)}")
                                code, stdout, stderr = ssh_run(
                                    f"{shlex.quote(remote_path)} 2>&1 || true"
                                )
                                result_badge(code)
                                if stdout:
                                    st.code(stdout, language=None)
                                if stderr:
                                    st.code(stderr, language=None)
                                # Clean up remote file
                                ssh_run(f"rm -f {shlex.quote(remote_path)}")
                            else:
                                st.error("Failed to upload file to VM")
                        finally:
                            os.unlink(tmp_path)

# ── Auto-refresh ─────────────────────────────────────────────────────────────

time.sleep(3)
st.rerun()
