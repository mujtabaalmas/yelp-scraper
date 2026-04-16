"""
Run yelp_scraper.py with runtime overrides without modifying scraper code.

Enhancements:
- Output no-loss rebuild: regenerate full output snapshot from input + progress after run.
- Deep retry mode: reprocess records marked done but still not found in progress file.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import importlib
import json
import os
import tempfile
from pathlib import Path
from typing import Callable


def has_value(value: str | None) -> bool:
    text = (value or "").strip().lower()
    return text not in ("", "null", "none")


def resolve_path(base_dir: Path, value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else base_dir / path


def read_latest_progress(progress_file: Path) -> dict[str, dict]:
    latest: dict[str, dict] = {}
    if not progress_file.exists():
        return latest

    with progress_file.open("r", encoding="utf-8", errors="replace") as f:
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
                latest[key] = item

    return latest


def build_record_key(row: dict, extract_domain: Callable[[str], str]) -> str:
    return "|".join(
        [
            row.get("name", "").strip().lower(),
            row.get("city", "").strip().lower(),
            row.get("state", "").strip().lower(),
            extract_domain(row.get("website", "").strip()),
        ]
    )


def write_rows_csv(path: Path, fieldnames: list[str], rows: list[dict]) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def select_not_found_rows(
    input_csv: Path,
    progress_file: Path,
    extract_domain: Callable[[str], str],
    limit: int | None,
) -> tuple[list[str], list[dict], set[str]]:
    latest = read_latest_progress(progress_file)

    not_found_keys = {
        key
        for key, value in latest.items()
        if bool(value.get("yelp_done", False)) and not has_value(str(value.get("yelp", "")))
    }

    if not not_found_keys:
        return [], [], set()

    selected_rows: list[dict] = []
    with input_csv.open("r", newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames or []

        for row in reader:
            key = build_record_key(row, extract_domain)
            if key in not_found_keys:
                selected_rows.append(row)

    if limit is not None:
        selected_rows = selected_rows[:limit]

    retry_keys = {build_record_key(row, extract_domain) for row in selected_rows}
    return fieldnames, selected_rows, retry_keys


def apply_deep_overrides(scraper_module) -> None:
    """Make deep retries slower but more thorough without editing scraper source."""
    scraper_module.MAX_CANDIDATES_TO_CHECK = max(
        int(getattr(scraper_module, "MAX_CANDIDATES_TO_CHECK", 5)),
        10,
    )
    scraper_module.NAVIGATION_TIMEOUT = max(
        int(getattr(scraper_module, "NAVIGATION_TIMEOUT", 25000)),
        40000,
    )
    scraper_module.LOAD_WAIT_SHORT = max(
        float(getattr(scraper_module, "LOAD_WAIT_SHORT", 0.8)),
        1.2,
    )
    scraper_module.LOAD_WAIT_MEDIUM = max(
        float(getattr(scraper_module, "LOAD_WAIT_MEDIUM", 1.2)),
        1.8,
    )
    scraper_module.LOAD_WAIT_LONG = max(
        float(getattr(scraper_module, "LOAD_WAIT_LONG", 1.8)),
        2.6,
    )
    scraper_module.VERIFY_PAGE_WAIT = max(
        float(getattr(scraper_module, "VERIFY_PAGE_WAIT", 1.2)),
        2.0,
    )


def rebuild_output_from_progress(
    input_csv: Path,
    output_csv: Path,
    progress_file: Path,
    extract_domain: Callable[[str], str],
) -> None:
    """Rebuild full output from full input + latest progress entries."""
    latest = read_latest_progress(progress_file)

    with input_csv.open("r", newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames or []
        rows = list(reader)

    if "yelp" not in fieldnames:
        fieldnames.append("yelp")

    for row in rows:
        key = build_record_key(row, extract_domain)
        cached = latest.get(key)
        if not cached:
            row.setdefault("yelp", "")
            continue

        cached_yelp = str(cached.get("yelp", "")).strip()
        if has_value(cached_yelp):
            row["yelp"] = cached_yelp
        else:
            row.setdefault("yelp", "")

    temp_output = output_csv.with_suffix(output_csv.suffix + ".tmp")
    write_rows_csv(temp_output, fieldnames, rows)
    os.replace(temp_output, output_csv)


def show_config(scraper_module) -> None:
    print(
        json.dumps(
            {
                "INPUT_CSV": getattr(scraper_module, "INPUT_CSV", None),
                "OUTPUT_CSV": getattr(scraper_module, "OUTPUT_CSV", None),
                "PROGRESS_FILE": getattr(scraper_module, "PROGRESS_FILE", None),
                "TEST_START": getattr(scraper_module, "TEST_START", None),
                "TEST_LIMIT": getattr(scraper_module, "TEST_LIMIT", None),
                "HEADLESS": getattr(scraper_module, "HEADLESS", None),
                "RECORD_CONCURRENCY": getattr(scraper_module, "RECORD_CONCURRENCY", None),
            },
            indent=2,
        )
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run Yelp scraper with UI-friendly overrides")
    parser.add_argument("--start", type=int, default=0, help="Starting record index offset (0-indexed)")
    parser.add_argument("--limit", type=int, default=None, help="Number of records to process")
    parser.add_argument(
        "--headless",
        choices=["true", "false"],
        default=None,
        help="Override HEADLESS mode",
    )
    parser.add_argument(
        "--show-config",
        action="store_true",
        help="Print active config and exit",
    )
    parser.add_argument(
        "--deep-retry-not-found",
        action="store_true",
        help="Retry records that are yelp_done=true but still missing Yelp in progress file",
    )
    parser.add_argument("--input", type=str, default=None, help="Override input CSV path")
    parser.add_argument("--output", type=str, default=None, help="Override output CSV path")
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    if args.limit is not None and args.limit < 1:
        raise SystemExit("--limit must be >= 1")

    scraper = importlib.import_module("yelp_scraper")

    base_dir = Path(__file__).resolve().parent
    original_input_csv = resolve_path(base_dir, str(getattr(scraper, "INPUT_CSV", "bussiness_records.csv")))
    original_output_csv = resolve_path(base_dir, str(getattr(scraper, "OUTPUT_CSV", "bussiness_records_yelp.csv")))
    original_progress_file = resolve_path(base_dir, str(getattr(scraper, "PROGRESS_FILE", "progress.jsonl")))

    scraper.TEST_START = args.start

    if args.limit is not None:
        scraper.TEST_LIMIT = args.limit

    if args.headless is not None:
        scraper.HEADLESS = args.headless == "true"

    if args.input is not None:
        scraper.INPUT_CSV = args.input

    if args.output is not None:
        scraper.OUTPUT_CSV = args.output

    if args.show_config:
        show_config(scraper)
        if not args.deep_retry_not_found:
            return

    original_load_progress = None
    temp_input_csv: Path | None = None
    temp_output_csv: Path | None = None

    try:
        if args.deep_retry_not_found:
            fieldnames, selected_rows, retry_keys = select_not_found_rows(
                input_csv=original_input_csv,
                progress_file=original_progress_file,
                extract_domain=scraper.extract_domain,
                limit=args.limit,
            )

            if not selected_rows:
                print("No not-found records found in progress file. Nothing to retry.")
                return

            fd, temp_name = tempfile.mkstemp(prefix="deep_retry_input_", suffix=".csv", dir=str(base_dir))
            os.close(fd)
            temp_input_csv = Path(temp_name)
            temp_output_csv = temp_input_csv.with_name(temp_input_csv.stem + "_output.csv")

            write_rows_csv(temp_input_csv, fieldnames, selected_rows)

            scraper.INPUT_CSV = str(temp_input_csv)
            scraper.OUTPUT_CSV = str(temp_output_csv)
            scraper.PROGRESS_FILE = str(original_progress_file)
            scraper.TEST_START = 0
            scraper.TEST_LIMIT = len(selected_rows)
            apply_deep_overrides(scraper)

            original_load_progress = scraper.load_progress

            def patched_load_progress(path: str):
                state = original_load_progress(path)
                for key in retry_keys:
                    entry = state.get(key)
                    if not entry:
                        continue
                    if not has_value(str(entry.get("yelp", ""))):
                        entry["yelp_done"] = False
                return state

            scraper.load_progress = patched_load_progress

            print(f"Deep retry mode: processing {len(selected_rows)} not-found records")

        asyncio.run(scraper.main())
    finally:
        if original_load_progress is not None:
            scraper.load_progress = original_load_progress

        try:
            rebuild_output_from_progress(
                input_csv=original_input_csv,
                output_csv=original_output_csv,
                progress_file=original_progress_file,
                extract_domain=scraper.extract_domain,
            )
            print("Output snapshot rebuilt from full input + progress (no records lost).")
        except Exception as e:
            print(f"Warning: could not rebuild full output snapshot: {e}")

        if temp_input_csv and temp_input_csv.exists():
            try:
                temp_input_csv.unlink()
            except Exception:
                pass

        if temp_output_csv and temp_output_csv.exists():
            try:
                temp_output_csv.unlink()
            except Exception:
                pass


if __name__ == "__main__":
    main()
