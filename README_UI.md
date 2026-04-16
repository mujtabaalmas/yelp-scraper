# Yelp Scraper UI Template

This UI runs your existing `yelp_scraper.py` without modifying it.

## What It Does

- Lets you enter a record limit from a local web UI.
- Runs the scraper locally.
- Supports deep retry mode for records that were processed but not found.
- Streams logs in the browser.
- Shows output table preview from `bussiness_records_yelp.csv`.
- Shows progress summary from `progress.jsonl`.

## Setup

```powershell
c:/Users/Mujtaba/Desktop/scrapper/venv/Scripts/python.exe -m pip install -r requirements.txt
c:/Users/Mujtaba/Desktop/scrapper/venv/Scripts/python.exe -m playwright install chromium
```

## Launch UI

```powershell
c:/Users/Mujtaba/Desktop/scrapper/venv/Scripts/python.exe -m streamlit run ui_app.py
```

Then open the local URL shown by Streamlit (usually `http://localhost:8501`).

## Notes

- `run_yelp_with_limit.py` is a wrapper that sets `TEST_LIMIT` at runtime, then calls `yelp_scraper.main()`.
- Deep retry command:

```powershell
c:/Users/Mujtaba/Desktop/scrapper/venv/Scripts/python.exe run_yelp_with_limit.py --deep-retry-not-found --limit 100
```

- In deep retry mode, only records marked `yelp_done=true` with empty Yelp are retried.
- After each run, the wrapper rebuilds `bussiness_records_yelp.csv` from full input + latest progress so records are not lost.
- Your original scraper code remains unchanged.
- If the output CSV is open in Excel, live file updates can be delayed by file locks.
Commands to use:
Normal run:
python.exe run_yelp_with_limit.py --limit 100 --headless true

Deep retry not-found:
python.exe run_yelp_with_limit.py --deep-retry-not-found --limit 100 --headless true