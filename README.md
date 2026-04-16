# Yelp & BBB Scraper Dashboard

A comprehensive, robust data collection system built in Python utilizing **Playwright**, **Streamlit**, and **BeautifulSoup**. The project automates the task of gathering official business profile URLs from **Yelp** and the **Better Business Bureau (BBB)** by running targeted queries, avoiding bot detections, decoding redirect links, and tracking its own processing progress in real time.

## What It Does
1. **Reads Business Data**: Takes an input CSV containing known businesses (Company Name, City, State, Website).
2. **Automated Scraping**: Uses Playwright to programmatically perform headless searches on DuckDuckGo, Bing, BBB, and Yelp without getting easily blocked by captchas or scrape-protections.
3. **Data Verification**: Verifies fetched BBB/Yelp profiles against the original known `website` domain to ensure high accuracy.
4. **Resiliency**: Periodically creates JSONL progress snapshots to save its state. If execution fails or halts, it safely picks up where it left off.
5. **Interactive UI**: Ships with a powerful **Streamlit Dashboard** allowing users to select active input files, update row processing limits, toggle headless execution, retry missed records (Deep Retry mode), and monitor live console execution logs seamlessly.

---

## Prerequisites & Requirements

This system runs on Python 3 and requires several dependencies defined in `requirements.txt`.

Required Python packages:
- `playwright` (Web automation)
- `beautifulsoup4` (HTML parsing)
- `lxml` (Fast XML/HTML parsing engine)
- `streamlit` (Web UI Dashboard)

You will also need **Chromium** binaries installed globally for Playwright.

---

## File Types & Input Contents

### The Input CSV File
The scraper expects to read from a standard CSV file (e.g., `bussiness_records.csv`). You can upload new CSVs directly from the dashboard or put them directly in the project directory.

The system specifically depends on columns that match the following headers (case-sensitive) to effectively run its queries:
- **`name`**: The exact registered business or company name.
- **`city`**: The city where the business operates.
- **`state`**: The state abbreviation or full name.
- **`website`**: The official domain of the business. *(Extremely critical for verification).*

> Note: Any other extra columns in the original CSV will be retained and rewritten safely to the output without data loss.

### Output File
A new CSV titled `{source_file_name}_yelp.csv` will be created with the exact same structure as the input, but containing populated fields for the queried platforms (e.g. `yelp`, `bbb` depending on extensions). 

---

## Commands

### 1. Installation & Environment Setup
Launch your terminal and execute:
```bash
# 1. Install pip requirements
python -m pip install -r requirements.txt

# 2. Install Playwright binaries for the browser
python -m playwright install chromium
```

### 2. Launching the UI Dashboard (Recommended)
This is the easiest way to interact with the project:
```bash
python -m streamlit run ui_app.py
```
*This will open the visual dashboard in your browser (usually `http://localhost:8501`). You can execute all of the scraper logic straight from the buttons here.*

### 3. Alternative: Running in Command Line
If you prefer bypassing the UI, use the wrapper script:

- **Run standard scrape mapping**:
  ```bash
  python run_yelp_with_limit.py --limit 100 --headless true
  ```
  *(Change `100` to the amount of rows you want to process, or remove `--limit` to do all).*

- **Run Deep-Retry Recovery**:  
  To re-run queries on records that previously reported "done" but didn't actually locate a valid Yelp/BBB profile:
  ```bash
  python run_yelp_with_limit.py --deep-retry-not-found --limit 100 --headless true
  ```

---

## Project Structure Overview

```text
/scrapper
│
├── requirements.txt            # Python dependencies lists
├── README.md                   # Project documentation
├── README_UI.md                # Legacy UI docs reference
├── github/                     
│   └── copilot_instructions.md # Specific guidelines for AI IDE contextual grounding
│
├── bussiness_records.csv       # The standard initial input Database
├── "*_yelp.csv"                # Generated Output Data artifacts
├── progress.jsonl              # Real-time backend tracking for pause/resume safety
├── yelp_scraper.log            # Execution log trail (consumed visually by Streamlit)
│
├── yelp_scraper.py             # Core Scraper Engine (Contains anti-bot logic, parsing, searches)
├── run_yelp_with_limit.py      # CLI wrapper to invoke parsing, manage limits & retry states
└── ui_app.py                   # The Streamlit application UI Dashboard code
```
