from __future__ import annotations

import csv
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import streamlit as st

BASE_DIR = Path(__file__).resolve().parent
RUNNER_PATH = BASE_DIR / "run_yelp_with_limit.py"
PROGRESS_PATH = BASE_DIR / "progress.jsonl"
LOG_PATH = BASE_DIR / "yelp_scraper.log"


st.set_page_config(
    page_title="Yelp Scraper Dashboard",
    page_icon="🕸️",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown(
    """
<style>
/* Remove the hardcoded light-mode background to fix dark mode */
.block-container {
    padding-top: 2rem;
    padding-bottom: 2rem;
}
/* Style metric cards to look like a dashboard */
[data-testid="stMetric"] {
    background-color: var(--secondary-background-color);
    border-radius: 0.5rem;
    padding: 1rem;
    box-shadow: 0 1px 3px rgba(0,0,0,0.12), 0 1px 2px rgba(0,0,0,0.24);
}
/* Better code block / log display */
[data-testid="stCodeBlock"] {
    border-radius: 0.5rem;
    border: 1px solid var(--secondary-background-color);
}
/* Adjust dataframe container */
[data-testid="stDataFrame"] {
    border-radius: 8px;
    border: 1px solid var(--secondary-background-color);
}
</style>
""",
    unsafe_allow_html=True,
)

st.title("📊 Yelp Scraper Dashboard")
st.markdown("Monitor and control your scraping tasks. Adjust limits, run the scraper, and analyze results seamlessly.")


def has_value(value: str | None) -> bool:
    text = (value or "").strip().lower()
    return text not in ("", "null", "none")


def read_output_rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open("r", newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def count_input_rows(path: Path) -> int:
    if not path.exists():
        return 0
    with path.open("r", newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        return sum(1 for _ in reader)


def with_display_index(rows: list[dict]) -> list[dict]:
    indexed_rows: list[dict] = []
    for i, row in enumerate(rows, start=1):
        indexed_rows.append({"index": i, **row})
    return indexed_rows


def read_progress_snapshot(path: Path) -> dict[str, dict]:
    latest_by_key: dict[str, dict] = {}
    if not path.exists():
        return latest_by_key

    with path.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            key = item.get("key")
            if key:
                latest_by_key[key] = item

    return latest_by_key


def read_progress_summary(path: Path) -> tuple[int, int, int, int]:
    latest_by_key = read_progress_snapshot(path)
    done_count = sum(1 for v in latest_by_key.values() if bool(v.get("yelp_done", False)))
    not_found_count = sum(
        1
        for v in latest_by_key.values()
        if bool(v.get("yelp_done", False)) and not has_value(str(v.get("yelp", "")))
    )
    found_count = sum(
        1
        for v in latest_by_key.values()
        if bool(v.get("yelp_done", False)) and has_value(str(v.get("yelp", "")))
    )
    return len(latest_by_key), done_count, not_found_count, found_count


def run_scraper(limit: int, start_offset: int, headless: bool, deep_retry_not_found: bool = False, input_path: Path | None = None, output_path: Path | None = None) -> tuple[int, list[str], str]:
    cmd = [
        sys.executable,
        str(RUNNER_PATH),
        "--start",
        str(start_offset),
        "--limit",
        str(limit),
        "--headless",
        "true" if headless else "false",
    ]

    if input_path:
        cmd.extend(["--input", str(input_path)])
    if output_path:
        cmd.extend(["--output", str(output_path)])

    if deep_retry_not_found:
        cmd.append("--deep-retry-not-found")

    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"

    process = subprocess.Popen(
        cmd,
        cwd=str(BASE_DIR),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        env=env,
    )

    output_lines: list[str] = []
    mode_label = "Deep retry mode" if deep_retry_not_found else "Normal mode"

    with st.status(f"⚙️ **{mode_label}**: Scraper is running...", expanded=True) as status:
        st.markdown("**Live Execution Logs**")
        live_log = st.empty()

        assert process.stdout is not None
        while True:
            line = process.stdout.readline()
            if line:
                clean = line.rstrip("\n")
                output_lines.append(clean)
                live_log.code("\n".join(output_lines[-100:]), language="shell")

            if process.poll() is not None:
                remainder = process.stdout.read()
                if remainder:
                    output_lines.extend(remainder.splitlines())
                    live_log.code("\n".join(output_lines[-100:]), language="shell")
                break

        rc = process.returncode if process.returncode is not None else 1
        if rc == 0:
            status.update(label="✅ Scraper completed successfully.", state="complete", expanded=False)
        else:
            status.update(label=f"❌ Scraper failed with exit code {rc}.", state="error", expanded=True)

    return rc, output_lines, " ".join(cmd)


with st.sidebar:
    run_settings_container = st.container()
    deep_retry_container = st.container()
    file_mgmt_container = st.container()
    refresh_container = st.container()

with file_mgmt_container:
    st.divider()
    st.header("📂 File Management")
    uploaded_file = st.file_uploader("Upload new input CSV", type=["csv"])
    if uploaded_file is not None:
        if st.button("Save Uploaded File", type="primary", use_container_width=True):
            # Find next available new_{number}.csv
            existing_new_files = list(BASE_DIR.glob("new_*.csv"))
            max_num = 0
            for f in existing_new_files:
                match = re.match(r"new_(\d+)\.csv", f.name)
                if match:
                    num = int(match.group(1))
                    if num > max_num:
                        max_num = num
            next_num = max_num + 1
            new_file_name = f"new_{next_num}.csv"
            new_file_path = BASE_DIR / new_file_name
            new_file_path.write_bytes(uploaded_file.getvalue())
            st.success(f"Saved successfully as **{new_file_name}**")
            # Clear file uploader after save (Workaround by triggering rerun if needed, 
            # but user can easily refresh or switch file below)

    st.divider()

    # Discover valid input CSV files (excluding outputs that end in `_yelp.csv`)
    all_csvs = [f.name for f in BASE_DIR.glob("*.csv") if not f.name.endswith("_yelp.csv")]
    if not all_csvs:
        all_csvs = ["bussiness_records.csv"]

    # Let user pick which file to run and view
    selected_input = st.selectbox("🎯 Select Active Input File", all_csvs, index=0)
    INPUT_PATH = BASE_DIR / selected_input
    
    # Derive output path
    base_name = INPUT_PATH.stem
    OUTPUT_PATH = BASE_DIR / f"{base_name}_yelp.csv"

with run_settings_container:
    st.header("⚙️ Run Settings")
    total_input_rows = max(1, count_input_rows(INPUT_PATH))
    
    col_start, col_end = st.columns(2)
    start_row = col_start.number_input(
        "Start row",
        min_value=1,
        max_value=total_input_rows,
        value=1,
        step=1,
        help="Row number to start from."
    )
    end_row = col_end.number_input(
        "End row",
        min_value=start_row,
        max_value=total_input_rows,
        value=total_input_rows,
        step=1,
        help="Row number to end at."
    )
    st.caption(f"CSV row limit: {total_input_rows}")
    
    headless = st.toggle("💻 Run browser headless", value=True, help="Run without opening the browser window.")
    run_now = st.button("🚀 Run Scraper", type="primary", use_container_width=True)

with deep_retry_container:
    st.divider()
    st.header("🔄 Deep Retry")
    st.info("Reprocess only records marked done but still missing Yelp in progress.")
    deep_default = min(total_input_rows, 100)
    deep_limit = st.number_input(
        "Not-found retry limit",
        min_value=1,
        max_value=total_input_rows,
        value=deep_default,
        step=1,
    )
    deep_retry_now = st.button("🛠️ Retry Not Found (Deep)", use_container_width=True)

with refresh_container:
    st.divider()
    refresh = st.button("🔄 Refresh Data", use_container_width=True)

if run_now:
    start_offset = int(start_row) - 1
    limit_count = int(end_row) - start_offset
    rc, logs, cmd_text = run_scraper(limit_count, start_offset, bool(headless), deep_retry_not_found=False, input_path=INPUT_PATH, output_path=OUTPUT_PATH)
    st.session_state["last_run_exit_code"] = rc
    st.session_state["last_run_logs"] = logs
    st.session_state["last_run_cmd"] = cmd_text
elif deep_retry_now:
    rc, logs, cmd_text = run_scraper(int(deep_limit), 0, bool(headless), deep_retry_not_found=True, input_path=INPUT_PATH, output_path=OUTPUT_PATH)
    st.session_state["last_run_exit_code"] = rc
    st.session_state["last_run_logs"] = logs
    st.session_state["last_run_cmd"] = cmd_text

# Fetch Data
with st.spinner("Loading dashboard data..."):
    rows = read_output_rows(OUTPUT_PATH)
    indexed_rows = with_display_index(rows)
    tracked, done, not_found, found = read_progress_summary(PROGRESS_PATH)

st.subheader(f"📈 Progress Overview for `{INPUT_PATH.name}`")
col1, col2, col3, col4, col5 = st.columns(5)
col1.metric("Output Rows", len(rows), help=f"Total rows written to the final output CSV {OUTPUT_PATH.name}.")
col2.metric("Tracked Records", tracked, help="Total unique records tracked in progress.")
col3.metric("Progress Done", done, help="Records marked as completely processed.")
col4.metric("Yelp Found", found, help="Records processed and Yelp URL was found successfully.", delta_color="normal")
col5.metric("Done but Not Found", not_found, help="Records processed but Yelp URL was not found.", delta_color="inverse")

if "last_run_cmd" in st.session_state:
    st.caption(f"**Last command:** `{st.session_state['last_run_cmd']}`")

st.divider()

# Tab layout for Results and Logs
tab1, tab2 = st.tabs(["🗂️ Results & Data", "📜 System Logs"])

with tab1:
    st.subheader(f"Result File: {OUTPUT_PATH.name}")
    st.caption(f"Showing output generated from **{INPUT_PATH.name}**")

    if OUTPUT_PATH.exists():
        csv_bytes = OUTPUT_PATH.read_bytes()
        st.download_button(
            "⬇️ Download Output CSV",
            data=csv_bytes,
            file_name=OUTPUT_PATH.name,
            mime="text/csv",
            type="primary",
            use_container_width=False,
        )
    else:
        st.info(f"No output file generated yet for `{INPUT_PATH.name}`.")

    if rows:
        st.markdown("### Data Preview")
        show_full_table = st.toggle("🔍 Show full CSV table", value=False)
        if show_full_table:
            st.dataframe(indexed_rows, use_container_width=True, height=600)
        else:
            preview_count = st.slider("Rows to preview", min_value=10, max_value=500, value=50, step=10)
            recent_rows = indexed_rows[-preview_count:]
            st.dataframe(recent_rows, use_container_width=True, height=400)
    else:
        st.info(f"No data available in `{OUTPUT_PATH.name}`. Run the scraper to generate results.")

with tab2:
    st.subheader("Recent Execution Logs")
    if "last_run_logs" in st.session_state:
        st.code("\n".join(st.session_state["last_run_logs"][-500:]), language="shell")
    elif LOG_PATH.exists() and (refresh or True):
        # Read the tail of the log file using basic python
        try:
            tail_lines = LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines()[-500:]
            if tail_lines:
                st.code("\n".join(tail_lines), language="shell")
            else:
                st.info("Log file is empty.")
        except Exception as e:
            st.error(f"Error reading log file: {e}")
    else:
        st.info("No logs available yet.")
