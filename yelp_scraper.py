"""
Yelp Profile Scraper (Playwright Edition v3 - Fixed)
==========================================================
Fixes:
  - yelp: Bing URL redirect decoding, DuckDuckGo fallback, relaxed verification
  - Yelp: Concurrent Yelp per record, reduced delays, faster processing
  - Both: Better candidate extraction, smarter name matching

Usage:
    pip install playwright beautifulsoup4 lxml
    playwright install chromium
    python yelp_scraper.py
"""

import asyncio
import csv
import re
import sys
import time
import random
import logging
import json
import base64
import os
from urllib.parse import quote_plus, urlparse, unquote, parse_qs
from difflib import SequenceMatcher
from typing import Optional

from playwright.async_api import async_playwright, Page, BrowserContext
from bs4 import BeautifulSoup

# == Configuration ===========================================================
INPUT_CSV = "bussiness_records.csv"
OUTPUT_CSV = "bussiness_records_yelp_output.csv"
PROGRESS_FILE = "progress.jsonl"
TEST_START = 0
TEST_LIMIT = 300

HEADLESS = True
NAVIGATION_TIMEOUT = 25000
MIN_DELAY = 0.5
MAX_DELAY = 1.5
MAX_CANDIDATES_TO_CHECK = 5
RECORD_CONCURRENCY = 4

STRATEGY_DELAY_MIN = 0.1
STRATEGY_DELAY_MAX = 0.25
VERIFY_DELAY_MIN = 0.05
VERIFY_DELAY_MAX = 0.15

LOAD_WAIT_SHORT = 0.8
LOAD_WAIT_MEDIUM = 1.2
LOAD_WAIT_LONG = 1.8
VERIFY_PAGE_WAIT = 1.2

PROGRESS_FLUSH_INTERVAL = 1.0
PROGRESS_BATCH_SIZE = 8
CSV_WRITE_RETRIES = 12
CSV_WRITE_RETRY_DELAY = 0.25

# == Proxy Configuration =====================================================
PROXIES = []

# == User Agents =============================================================
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
]

# == Logging =================================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("yelp_scraper.log", mode="w", encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


# == Utility Functions ========================================================
def extract_domain(url: str) -> str:
    if not url:
        return ""
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    try:
        domain = urlparse(url).netloc.lower().replace("www.", "")
        if ":" in domain:
            domain = domain.split(":")[0]
        return domain
    except Exception:
        return ""


def domains_match(url1: str, url2: str) -> bool:
    d1 = extract_domain(url1)
    d2 = extract_domain(url2)
    if not d1 or not d2:
        return False
    return d1 == d2


def similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    a_clean = re.sub(r'[^a-z0-9\s]', '', a.lower().strip())
    b_clean = re.sub(r'[^a-z0-9\s]', '', b.lower().strip())
    return SequenceMatcher(None, a_clean, b_clean).ratio()


def clean_company_name(name: str) -> str:
    cleaned = re.sub(
        r'\b(Inc\.?|LLC\.?|Corp\.?|Co\.?|Ltd\.?|,\s*Inc\.?|,\s*LLC|\.com)\b',
        '', name, flags=re.IGNORECASE,
    )
    return cleaned.strip().strip(',').strip('"').strip()


def is_valid_bbb_profile(url: str) -> bool:
    return bool(url and "bbb.org" in url and "/profile/" in url)


def is_valid_yelp_profile(url: str) -> bool:
    return bool(url and "yelp.com/biz/" in url)


def clean_yelp_url(url: str) -> str:
    if "?" in url:
        url = url.split("?")[0]
    return url.strip()


def clean_bbb_url(url: str) -> str:
    if "#" in url:
        url = url.split("#")[0]
    return url.strip()


def decode_bing_url(bing_href: str) -> str:
    """Decode Bing's redirect URL to get the actual destination URL."""
    if not bing_href:
        return ""
    if "bing.com/ck/" not in bing_href and "bing.com/aclick" not in bing_href:
        return bing_href
    try:
        parsed = urlparse(bing_href)
        params = parse_qs(parsed.query)
        u_param = params.get("u", [""])[0]
        if u_param:
            if u_param.startswith("a1"):
                u_param = u_param[2:]
            # Pad base64
            padding = 4 - len(u_param) % 4
            if padding != 4:
                u_param += "=" * padding
            decoded = base64.b64decode(u_param).decode("utf-8", errors="replace")
            if decoded.startswith("http"):
                return decoded
    except Exception:
        pass
    return bing_href


def name_in_url(company_name: str, url: str) -> bool:
    """Check if the company name appears in the URL slug."""
    clean = clean_company_name(company_name).lower()
    words = [w for w in re.split(r'\s+', clean) if len(w) > 2]
    url_lower = url.lower()
    if not words:
        return False
    matched = sum(1 for w in words if w in url_lower)
    return matched >= len(words) * 0.6


async def random_delay(min_s=MIN_DELAY, max_s=MAX_DELAY):
    await asyncio.sleep(random.uniform(min_s, max_s))


def has_value(value: str) -> bool:
    return bool(value and value.strip().lower() not in ("", "null", "none"))


def build_record_key(row: dict) -> str:
    return "|".join([
        row.get("name", "").strip().lower(),
        row.get("city", "").strip().lower(),
        row.get("state", "").strip().lower(),
        extract_domain(row.get("website", "").strip()),
    ])


def load_progress(path: str) -> dict[str, dict]:
    state: dict[str, dict] = {}
    if not os.path.exists(path):
        return state

    with open(path, "r", encoding="utf-8") as f:
        for line_no, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue

            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                log.warning(f"[Progress] Ignoring bad JSON line {line_no} in {path}")
                continue

            key = item.get("key")
            if not key:
                continue

            prev = state.setdefault(
                key,
                {"yelp": "", "yelp_done": False},
            )

            yelp = item.get("yelp", "")

            if has_value(yelp):
                prev["yelp"] = yelp
                prev["yelp_done"] = True

            prev["yelp_done"] = prev["yelp_done"] or bool(item.get("yelp_done", False))

    return state


def persist_batch_update(progress_events: list[dict], output_rows: list[dict], fieldnames: list[str]) -> None:
    try:
        with open(PROGRESS_FILE, "a", encoding="utf-8") as pf:
            for event in progress_events:
                pf.write(json.dumps(event, ensure_ascii=False) + "\n")
    except Exception as e:
        log.error(f"[Writer] Could not append to progress file: {e}")
        return

    temp_output = OUTPUT_CSV + ".tmp"
    for attempt in range(1, CSV_WRITE_RETRIES + 1):
        try:
            with open(temp_output, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(output_rows)
            os.replace(temp_output, OUTPUT_CSV)
            return
        except PermissionError:
            if attempt == CSV_WRITE_RETRIES:
                log.warning(
                    f"[Writer] Could not update {OUTPUT_CSV} after {CSV_WRITE_RETRIES} retries; "
                    "file may be open in Excel. Progress file is up to date."
                )
                return
            time.sleep(CSV_WRITE_RETRY_DELAY * attempt)
        except Exception as e:
            log.error(f"[Writer] Could not update output CSV: {e}")
            return


async def progress_writer_worker(queue: asyncio.Queue, output_rows: list[dict], fieldnames: list[str]) -> None:
    pending_events: list[dict] = []
    last_flush = time.monotonic()

    while True:
        timeout = max(0.0, PROGRESS_FLUSH_INTERVAL - (time.monotonic() - last_flush))
        if not pending_events:
            timeout = None

        timed_out = False
        try:
            item = await asyncio.wait_for(queue.get(), timeout=timeout)
        except asyncio.TimeoutError:
            timed_out = True
            item = None

        if timed_out:
            if pending_events:
                batch = pending_events
                pending_events = []
                await asyncio.to_thread(
                    persist_batch_update,
                    batch,
                    output_rows,
                    fieldnames,
                )
                last_flush = time.monotonic()
            continue

        if item is None:
            queue.task_done()
            if pending_events:
                await asyncio.to_thread(
                    persist_batch_update,
                    pending_events,
                    output_rows,
                    fieldnames,
                )
            break

        output_rows[item["index"]] = item["row"]
        pending_events.append(item["progress"])

        if len(pending_events) >= PROGRESS_BATCH_SIZE:
            batch = pending_events
            pending_events = []
            await asyncio.to_thread(
                persist_batch_update,
                batch,
                output_rows,
                fieldnames,
            )
            last_flush = time.monotonic()

        queue.task_done()


# == Stealth helpers ==========================================================
STEALTH_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
window.chrome = { runtime: {} };
const originalQuery = window.navigator.permissions.query;
window.navigator.permissions.query = (parameters) =>
    parameters.name === 'notifications'
        ? Promise.resolve({ state: Notification.permission })
        : originalQuery(parameters);
Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
"""


async def create_stealth_context(playwright_instance, proxy: dict = None):
    ua = random.choice(USER_AGENTS)
    launch_args = {
        "headless": HEADLESS,
        "args": [
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
            "--disable-dev-shm-usage",
        ],
    }
    if proxy:
        launch_args["proxy"] = proxy
    browser = await playwright_instance.chromium.launch(**launch_args)
    context = await browser.new_context(
        user_agent=ua,
        viewport={"width": 1920, "height": 1080},
        locale="en-US",
        timezone_id="America/New_York",
        ignore_https_errors=True,
    )
    await context.add_init_script(STEALTH_SCRIPT)
    return browser, context


async def setup_page(context: BrowserContext) -> Page:
    page = await context.new_page()

    blocked_extensions = re.compile(
        r"\.(png|jpg|jpeg|gif|svg|ico|webp|avif|woff|woff2|ttf|eot|mp4|webm|css)(\?|$)",
        re.IGNORECASE,
    )
    blocked_resource_types = {"image", "media", "font", "stylesheet"}

    async def route_handler(route):
        req = route.request
        if req.resource_type in blocked_resource_types or blocked_extensions.search(req.url):
            await route.abort()
        else:
            await route.continue_()

    await page.route(
        "**/*",
        route_handler,
    )
    return page


# ============================================================================
# BBB Scraper
# ============================================================================
class BBBScraper:

    async def search(self, page: Page, company_name: str, city: str, state: str, website: str) -> Optional[str]:
        log.info(f"   [BBB] Searching for: {company_name}")

        # Strategy 1: DuckDuckGo search (most reliable - direct links)
        result = await self._search_duckduckgo(page, company_name, city, state, website)
        if result:
            return result
        await random_delay(STRATEGY_DELAY_MIN, STRATEGY_DELAY_MAX)

        # Strategy 2: Direct BBB search
        result = await self._search_bbb_direct(page, company_name, city, state, website)
        if result:
            return result
        await random_delay(STRATEGY_DELAY_MIN, STRATEGY_DELAY_MAX)

        # Strategy 3: Bing search (with URL decoding fix)
        result = await self._search_bing(page, company_name, city, state, website)
        if result:
            return result
        await random_delay(STRATEGY_DELAY_MIN, STRATEGY_DELAY_MAX)

        # Strategy 4: Check company website for BBB link
        if website:
            result = await self._check_website(page, website)
            if result:
                return result

        return None

    async def _search_duckduckgo(self, page: Page, company_name: str, city: str, state: str, website: str) -> Optional[str]:
        """Search DuckDuckGo HTML for BBB profile - most reliable for direct links."""
        clean_name = clean_company_name(company_name)
        location = f"{city} {state}".strip()
        query = f"site:bbb.org/us \"{clean_name}\" {location} profile"
        search_url = f"https://html.duckduckgo.com/html/?q={quote_plus(query)}"

        try:
            log.info(f"   [BBB] DuckDuckGo search: {clean_name} {location}")
            await page.goto(search_url, wait_until="domcontentloaded", timeout=NAVIGATION_TIMEOUT)
            await asyncio.sleep(LOAD_WAIT_SHORT)

            content = await page.content()
            soup = BeautifulSoup(content, "html.parser")

            candidates = []
            # DuckDuckGo HTML results use class "result__a" for links
            for link in soup.find_all("a", class_="result__a", href=True):
                href = link["href"]
                # DDG may wrap URLs in a redirect
                if "duckduckgo.com" in href and "uddg=" in href:
                    parsed = urlparse(href)
                    params = parse_qs(parsed.query)
                    actual = params.get("uddg", [""])[0]
                    if actual:
                        href = unquote(actual)
                if is_valid_bbb_profile(href):
                    cleaned = clean_bbb_url(href)
                    if cleaned not in candidates:
                        candidates.append(cleaned)

            # Also check all links on the page
            for link in soup.find_all("a", href=True):
                href = link["href"]
                if "duckduckgo.com" in href and "uddg=" in href:
                    parsed = urlparse(href)
                    params = parse_qs(parsed.query)
                    actual = params.get("uddg", [""])[0]
                    if actual:
                        href = unquote(actual)
                if is_valid_bbb_profile(href):
                    cleaned = clean_bbb_url(href)
                    if cleaned not in candidates:
                        candidates.append(cleaned)

            if candidates:
                log.info(f"   [BBB] Found {len(candidates)} candidates from DuckDuckGo")
                verified = await self._verify_candidates(page, candidates, website, company_name)
                if verified:
                    return verified

        except Exception as e:
            log.warning(f"   [BBB] DuckDuckGo search error: {e}")

        return None

    async def _search_bbb_direct(self, page: Page, company_name: str, city: str, state: str, website: str) -> Optional[str]:
        """Search BBB.org directly."""
        clean_name = clean_company_name(company_name)
        location = f"{city}, {state}".strip(", ")
        search_url = (
            f"https://www.bbb.org/search?find_country=US"
            f"&find_text={quote_plus(clean_name)}"
            f"&find_loc={quote_plus(location)}"
            f"&page=1"
        )

        try:
            log.info(f"   [BBB] Direct search: {clean_name} in {location}")

            api_results = []

            async def capture_api(response):
                url = response.url
                if any(x in url for x in ["/api/", "/gateway/", "search", "/graphql"]):
                    try:
                        body = await response.json()
                        api_results.append(body)
                    except Exception:
                        pass

            page.on("response", capture_api)

            await page.goto(search_url, wait_until="domcontentloaded", timeout=NAVIGATION_TIMEOUT)
            await asyncio.sleep(LOAD_WAIT_LONG)

            # Try multiple selectors for BBB's React app
            for selector in [
                'a[href*="/profile/"]',
                '[data-testid="search-result"] a',
                '.search-results a',
                'a[href*="bbb.org/us/"]',
                '.result-item a',
            ]:
                try:
                    await page.wait_for_selector(selector, timeout=3000)
                    break
                except Exception:
                    continue

            page.remove_listener("response", capture_api)

            content = await page.content()
            candidates = self._extract_bbb_links(content)

            # Extract from API responses
            for api_body in api_results:
                api_candidates = self._extract_from_api(api_body)
                candidates.extend(api_candidates)

            # Also extract from Next.js / React hydration data
            hydration_candidates = self._extract_from_nextjs_data(content)
            candidates.extend(hydration_candidates)

            # Deduplicate
            seen = set()
            unique = []
            for c in candidates:
                normalized = clean_bbb_url(c)
                if normalized not in seen:
                    seen.add(normalized)
                    unique.append(normalized)

            if unique:
                log.info(f"   [BBB] Found {len(unique)} candidates from direct search")
                verified = await self._verify_candidates(page, unique, website, company_name)
                if verified:
                    return verified

        except Exception as e:
            log.warning(f"   [BBB] Direct search error: {e}")

        return None

    async def _search_bing(self, page: Page, company_name: str, city: str, state: str, website: str) -> Optional[str]:
        """Search Bing with URL redirect decoding."""
        clean_name = clean_company_name(company_name)
        location = f"{city} {state}".strip()
        query = f"site:bbb.org \"{clean_name}\" {location}"
        search_url = f"https://www.bing.com/search?q={quote_plus(query)}"

        try:
            log.info(f"   [BBB] Bing search: {clean_name} {location}")
            await page.goto(search_url, wait_until="domcontentloaded", timeout=NAVIGATION_TIMEOUT)
            await asyncio.sleep(LOAD_WAIT_MEDIUM)

            content = await page.content()
            soup = BeautifulSoup(content, "html.parser")

            candidates = []
            for link in soup.find_all("a", href=True):
                href = link["href"]
                # Decode Bing redirect URLs
                decoded = decode_bing_url(href)
                if is_valid_bbb_profile(decoded):
                    cleaned = clean_bbb_url(decoded)
                    if cleaned not in candidates:
                        candidates.append(cleaned)
                elif is_valid_bbb_profile(href):
                    cleaned = clean_bbb_url(href)
                    if cleaned not in candidates:
                        candidates.append(cleaned)

            # Also try extracting URLs from cite elements (Bing shows URL text)
            for cite in soup.find_all("cite"):
                text = cite.get_text(strip=True)
                if "bbb.org" in text and "/profile/" in text:
                    if not text.startswith("http"):
                        text = "https://" + text
                    # Clean the URL text (may have ellipsis)
                    text = text.replace(" ", "").replace("›", "/").replace("...", "")
                    if is_valid_bbb_profile(text):
                        cleaned = clean_bbb_url(text)
                        if cleaned not in candidates:
                            candidates.append(cleaned)

            if candidates:
                log.info(f"   [BBB] Found {len(candidates)} candidates from Bing")
                verified = await self._verify_candidates(page, candidates, website, company_name)
                if verified:
                    return verified

        except Exception as e:
            log.warning(f"   [BBB] Bing search error: {e}")

        return None

    async def _check_website(self, page: Page, website: str) -> Optional[str]:
        if not website:
            return None
        url = website if website.startswith(("http://", "https://")) else f"https://{website}"

        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=15000)
            await asyncio.sleep(LOAD_WAIT_SHORT)
            content = await page.content()

            bbb_pattern = re.compile(r'https?://(?:www\.)?bbb\.org/us/[^"\s<>\']+/profile/[^"\s<>\']+')
            matches = bbb_pattern.findall(content)
            for match in matches:
                cleaned = clean_bbb_url(match)
                log.info(f"   [BBB] Found on company website: {cleaned[:80]}")
                return cleaned

            soup = BeautifulSoup(content, "html.parser")
            for link in soup.find_all("a", href=True):
                href = link["href"]
                if is_valid_bbb_profile(href):
                    cleaned = clean_bbb_url(href)
                    log.info(f"   [BBB] Found on company website: {cleaned[:80]}")
                    return cleaned

        except Exception as e:
            log.debug(f"   [BBB] Website check error: {e}")

        return None

    def _extract_bbb_links(self, html: str) -> list[str]:
        candidates = []
        soup = BeautifulSoup(html, "html.parser")
        for link in soup.find_all("a", href=True):
            href = link["href"]
            if "/profile/" in href:
                full_url = href if href.startswith("http") else f"https://www.bbb.org{href}"
                if full_url not in candidates:
                    candidates.append(full_url)

        pattern = re.compile(r'https?://(?:www\.)?bbb\.org/us/[a-z]{2}/[^"\s<>\']+/profile/[^"\s<>\']+')
        for match in pattern.findall(html):
            cleaned = clean_bbb_url(match)
            if cleaned not in candidates:
                candidates.append(cleaned)

        return candidates

    def _extract_from_api(self, api_body) -> list[str]:
        candidates = []
        try:
            if isinstance(api_body, dict):
                # Recursively search for profile URLs in the JSON
                self._find_profile_urls(api_body, candidates, "bbb")
            elif isinstance(api_body, list):
                for item in api_body:
                    if isinstance(item, dict):
                        self._find_profile_urls(item, candidates, "bbb")
        except Exception:
            pass
        return candidates

    def _find_profile_urls(self, obj, candidates, site_type):
        """Recursively find profile URLs in nested JSON."""
        if isinstance(obj, dict):
            for key, value in obj.items():
                if isinstance(value, str) and "/profile/" in value and "bbb.org" in value:
                    url = value if value.startswith("http") else f"https://www.bbb.org{value}"
                    if url not in candidates:
                        candidates.append(url)
                elif isinstance(value, str) and key.lower() in ("reporturl", "url", "profileurl", "href"):
                    if "/profile/" in value:
                        url = value if value.startswith("http") else f"https://www.bbb.org{value}"
                        if url not in candidates:
                            candidates.append(url)
                elif isinstance(value, (dict, list)):
                    self._find_profile_urls(value, candidates, site_type)
        elif isinstance(obj, list):
            for item in obj:
                self._find_profile_urls(item, candidates, site_type)

    def _extract_from_nextjs_data(self, html: str) -> list[str]:
        """Extract BBB profile URLs from Next.js hydration data."""
        candidates = []
        try:
            # Look for __NEXT_DATA__ script
            pattern = re.compile(r'<script[^>]*id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.DOTALL)
            match = pattern.search(html)
            if match:
                data = json.loads(match.group(1))
                self._find_profile_urls(data, candidates, "bbb")
        except Exception:
            pass

        # Also look for any JSON data blocks
        try:
            json_pattern = re.compile(r'"reportUrl"\s*:\s*"(/us/[^"]+/profile/[^"]+)"')
            for m in json_pattern.finditer(html):
                url = f"https://www.bbb.org{m.group(1)}"
                if url not in candidates:
                    candidates.append(url)
        except Exception:
            pass

        return candidates

    async def _verify_candidates(self, page: Page, candidates: list[str], website: str, company_name: str) -> Optional[str]:
        """Verify candidates - relaxed matching for BBB."""
        # First, check if any candidate URL contains the company name (fast check)
        for candidate_url in candidates[:MAX_CANDIDATES_TO_CHECK]:
            if name_in_url(company_name, candidate_url):
                log.info(f"   [BBB] URL name match (fast): {candidate_url[:70]}")
                # Still verify by visiting if website is available
                if website:
                    try:
                        await page.goto(candidate_url, wait_until="domcontentloaded", timeout=NAVIGATION_TIMEOUT)
                        await asyncio.sleep(VERIFY_PAGE_WAIT)
                        content = await page.content()
                        profile_website = self._extract_website_from_bbb_profile(content)
                        if profile_website and domains_match(profile_website, website):
                            log.info(f"   [BBB] VERIFIED by website match!")
                            return candidate_url
                        # Even without website match, accept if name matches in URL
                        log.info(f"   [BBB] Accepted by URL name match")
                        return candidate_url
                    except Exception:
                        return candidate_url
                else:
                    return candidate_url

        # Full verification for remaining candidates
        for i, candidate_url in enumerate(candidates[:MAX_CANDIDATES_TO_CHECK]):
            try:
                log.info(f"   [BBB] Verifying candidate {i+1}: {candidate_url[:70]}")
                await page.goto(candidate_url, wait_until="domcontentloaded", timeout=NAVIGATION_TIMEOUT)
                await asyncio.sleep(VERIFY_PAGE_WAIT)

                content = await page.content()
                profile_website = self._extract_website_from_bbb_profile(content)

                if profile_website:
                    log.info(f"   [BBB] Profile website: {profile_website}")
                    if domains_match(profile_website, website):
                        log.info(f"   [BBB] VERIFIED - Website domain matches!")
                        return candidate_url
                    else:
                        log.info(f"   [BBB] Domain mismatch: {extract_domain(profile_website)} != {extract_domain(website)}")
                else:
                    # No website on profile - try name matching (relaxed threshold)
                    name_on_page = self._extract_name_from_bbb_profile(content)
                    if name_on_page:
                        score = similarity(clean_company_name(company_name), name_on_page)
                        log.info(f"   [BBB] Name match score: {score:.2f} ({name_on_page})")
                        if score > 0.55:  # Relaxed from 0.7
                            log.info(f"   [BBB] Accepted by name match")
                            return candidate_url

                await random_delay(VERIFY_DELAY_MIN, VERIFY_DELAY_MAX)

            except Exception as e:
                log.debug(f"   [BBB] Verify error: {e}")

        return None

    def _extract_website_from_bbb_profile(self, html: str) -> str:
        soup = BeautifulSoup(html, "html.parser")

        for link in soup.find_all("a", href=True):
            href = link["href"]
            text = link.get_text(strip=True).lower()
            if "bbb.org" in href:
                continue
            if any(kw in text for kw in ["visit website", "website", "visit site", "company website"]):
                if href.startswith(("http://", "https://")):
                    return href

        for elem in soup.find_all(string=re.compile(r"Website|Visit Website|Business Website", re.IGNORECASE)):
            parent = elem.parent
            if parent:
                for link in parent.find_all_next("a", href=True, limit=3):
                    href = link["href"]
                    if href.startswith(("http://", "https://")) and "bbb.org" not in href:
                        return href
                grandparent = parent.parent
                if grandparent:
                    for link in grandparent.find_all("a", href=True):
                        href = link["href"]
                        if href.startswith(("http://", "https://")) and "bbb.org" not in href:
                            return href

        website_patterns = [
            re.compile(r'"websiteUrl"\s*:\s*"(https?://[^"]+)"', re.IGNORECASE),
            re.compile(r'"website"\s*:\s*"(https?://[^"]+)"', re.IGNORECASE),
            re.compile(r'"businessUrl"\s*:\s*"(https?://[^"]+)"', re.IGNORECASE),
        ]
        for pattern in website_patterns:
            match = pattern.search(html)
            if match:
                url = match.group(1)
                if "bbb.org" not in url:
                    return url

        return ""

    def _extract_name_from_bbb_profile(self, html: str) -> str:
        soup = BeautifulSoup(html, "html.parser")
        h1 = soup.find("h1")
        if h1:
            return h1.get_text(strip=True)
        og_title = soup.find("meta", property="og:title")
        if og_title:
            return og_title.get("content", "").replace(" | Better Business Bureau", "")
        return ""


# ============================================================================
# Yelp Scraper
# ============================================================================
class YelpScraper:

    async def search(self, page: Page, company_name: str, city: str, state: str, website: str) -> Optional[str]:
        log.info(f"   [Yelp] Searching for: {company_name}")

        # Strategy 1: Direct Yelp search
        result = await self._search_yelp_direct(page, company_name, city, state, website)
        if result:
            return result
        await random_delay(STRATEGY_DELAY_MIN, STRATEGY_DELAY_MAX)

        # Strategy 2: DuckDuckGo search
        result = await self._search_duckduckgo(page, company_name, city, state, website)
        if result:
            return result
        await random_delay(STRATEGY_DELAY_MIN, STRATEGY_DELAY_MAX)

        # Strategy 3: Bing search (with URL decoding)
        result = await self._search_bing(page, company_name, city, state, website)
        if result:
            return result
        await random_delay(STRATEGY_DELAY_MIN, STRATEGY_DELAY_MAX)

        # Strategy 4: Check company website
        if website:
            result = await self._check_website(page, website)
            if result:
                return result

        return None

    async def _search_yelp_direct(self, page: Page, company_name: str, city: str, state: str, website: str) -> Optional[str]:
        clean_name = clean_company_name(company_name)
        location = f"{city}, {state}".strip(", ")
        search_url = f"https://www.yelp.com/search?find_desc={quote_plus(clean_name)}&find_loc={quote_plus(location)}"

        try:
            log.info(f"   [Yelp] Direct search: {clean_name} in {location}")
            await page.goto(search_url, wait_until="domcontentloaded", timeout=NAVIGATION_TIMEOUT)

            try:
                await page.wait_for_selector('a[href*="/biz/"]', timeout=6000)
            except Exception:
                await asyncio.sleep(LOAD_WAIT_SHORT)

            content = await page.content()
            candidates = self._extract_yelp_links(content)

            if candidates:
                log.info(f"   [Yelp] Found {len(candidates)} candidates from direct search")
                # Prioritize candidates with company name in URL
                prioritized = []
                rest = []
                for c in candidates:
                    if name_in_url(company_name, c):
                        prioritized.append(c)
                    else:
                        rest.append(c)
                ordered = prioritized + rest

                verified = await self._verify_candidates(page, ordered, website, company_name)
                if verified:
                    return verified

        except Exception as e:
            log.warning(f"   [Yelp] Direct search error: {e}")

        return None

    async def _search_duckduckgo(self, page: Page, company_name: str, city: str, state: str, website: str) -> Optional[str]:
        """Search DuckDuckGo HTML for Yelp profile."""
        clean_name = clean_company_name(company_name)
        location = f"{city} {state}".strip()
        query = f"site:yelp.com/biz \"{clean_name}\" {location}"
        search_url = f"https://html.duckduckgo.com/html/?q={quote_plus(query)}"

        try:
            log.info(f"   [Yelp] DuckDuckGo search: {clean_name} {location}")
            await page.goto(search_url, wait_until="domcontentloaded", timeout=NAVIGATION_TIMEOUT)
            await asyncio.sleep(LOAD_WAIT_SHORT)

            content = await page.content()
            soup = BeautifulSoup(content, "html.parser")

            candidates = []
            for link in soup.find_all("a", href=True):
                href = link["href"]
                if "duckduckgo.com" in href and "uddg=" in href:
                    parsed = urlparse(href)
                    params = parse_qs(parsed.query)
                    actual = params.get("uddg", [""])[0]
                    if actual:
                        href = unquote(actual)
                if is_valid_yelp_profile(href):
                    cleaned = clean_yelp_url(href)
                    if any(skip in cleaned for skip in ["/biz_redir", "/biz_photos", "/biz_review"]):
                        continue
                    if cleaned not in candidates:
                        candidates.append(cleaned)

            if candidates:
                log.info(f"   [Yelp] Found {len(candidates)} candidates from DuckDuckGo")
                verified = await self._verify_candidates(page, candidates, website, company_name)
                if verified:
                    return verified

        except Exception as e:
            log.warning(f"   [Yelp] DuckDuckGo search error: {e}")

        return None

    async def _search_bing(self, page: Page, company_name: str, city: str, state: str, website: str) -> Optional[str]:
        clean_name = clean_company_name(company_name)
        location = f"{city} {state}".strip()
        query = f"site:yelp.com/biz \"{clean_name}\" {location}"
        search_url = f"https://www.bing.com/search?q={quote_plus(query)}"

        try:
            log.info(f"   [Yelp] Bing search: {clean_name} {location}")
            await page.goto(search_url, wait_until="domcontentloaded", timeout=NAVIGATION_TIMEOUT)
            await asyncio.sleep(LOAD_WAIT_MEDIUM)

            content = await page.content()
            soup = BeautifulSoup(content, "html.parser")

            candidates = []
            for link in soup.find_all("a", href=True):
                href = link["href"]
                decoded = decode_bing_url(href)
                if is_valid_yelp_profile(decoded):
                    cleaned = clean_yelp_url(decoded)
                    if cleaned not in candidates:
                        candidates.append(cleaned)
                elif is_valid_yelp_profile(href):
                    cleaned = clean_yelp_url(href)
                    if cleaned not in candidates:
                        candidates.append(cleaned)

            # Check cite elements too
            for cite in soup.find_all("cite"):
                text = cite.get_text(strip=True)
                if "yelp.com/biz/" in text:
                    if not text.startswith("http"):
                        text = "https://" + text
                    text = text.replace(" ", "").replace("›", "/")
                    if is_valid_yelp_profile(text):
                        cleaned = clean_yelp_url(text)
                        if cleaned not in candidates:
                            candidates.append(cleaned)

            if candidates:
                log.info(f"   [Yelp] Found {len(candidates)} candidates from Bing")
                verified = await self._verify_candidates(page, candidates, website, company_name)
                if verified:
                    return verified

        except Exception as e:
            log.warning(f"   [Yelp] Bing search error: {e}")

        return None

    async def _check_website(self, page: Page, website: str) -> Optional[str]:
        if not website:
            return None
        url = website if website.startswith(("http://", "https://")) else f"https://{website}"

        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=12000)
            await asyncio.sleep(LOAD_WAIT_SHORT)
            content = await page.content()

            yelp_pattern = re.compile(r'https?://(?:www\.)?yelp\.com/biz/[a-zA-Z0-9_-]+(?:-[a-zA-Z0-9_-]+)*')
            matches = yelp_pattern.findall(content)
            for match in matches:
                cleaned = clean_yelp_url(match)
                log.info(f"   [Yelp] Found on company website: {cleaned[:80]}")
                return cleaned

            soup = BeautifulSoup(content, "html.parser")
            for link in soup.find_all("a", href=True):
                href = link["href"]
                if is_valid_yelp_profile(href):
                    cleaned = clean_yelp_url(href)
                    log.info(f"   [Yelp] Found on company website: {cleaned[:80]}")
                    return cleaned

        except Exception as e:
            log.debug(f"   [Yelp] Website check error: {e}")

        return None

    def _extract_yelp_links(self, html: str) -> list[str]:
        candidates = []
        soup = BeautifulSoup(html, "html.parser")

        for link in soup.find_all("a", href=True):
            href = link["href"]
            if "/biz/" in href:
                if href.startswith("/biz/"):
                    full_url = f"https://www.yelp.com{href}"
                elif "yelp.com/biz/" in href:
                    full_url = href
                else:
                    continue

                cleaned = clean_yelp_url(full_url)
                if any(skip in cleaned for skip in ["/biz_redir", "/biz_photos", "/biz_review", "/biz/yelp-"]):
                    continue
                if cleaned not in candidates:
                    candidates.append(cleaned)

        return candidates



    async def _verify_candidates(self, page: Page, candidates: list[str], website: str, company_name: str) -> Optional[str]:
        for i, candidate_url in enumerate(candidates[:MAX_CANDIDATES_TO_CHECK]):
            try:
                log.info(f"   [Yelp] Verifying candidate {i+1}: {candidate_url[:70]}")
                await page.goto(candidate_url, wait_until="domcontentloaded", timeout=NAVIGATION_TIMEOUT)
                # Wait enough for Yelp to render the business website section
                await asyncio.sleep(VERIFY_PAGE_WAIT)

                content = await page.content()
                profile_website = self._extract_website_from_yelp_profile(content)

                if profile_website:
                    log.info(f"   [Yelp] Profile website: {profile_website}")
                    if domains_match(profile_website, website):
                        log.info(f"   [Yelp] VERIFIED - Website domain matches!")
                        return candidate_url
                    else:
                        log.info(f"   [Yelp] Domain mismatch: {extract_domain(profile_website)} != {extract_domain(website)}")
                else:
                    name_on_page = self._extract_name_from_yelp_profile(content)
                    if name_on_page:
                        score = similarity(clean_company_name(company_name), name_on_page)
                        log.info(f"   [Yelp] Name match score: {score:.2f} ({name_on_page})")
                        if score > 0.6:
                            log.info(f"   [Yelp] Accepted by name match")
                            return candidate_url

                await random_delay(VERIFY_DELAY_MIN, VERIFY_DELAY_MAX)

            except Exception as e:
                log.debug(f"   [Yelp] Verify error: {e}")

        return None

    def _extract_website_from_yelp_profile(self, html: str) -> str:
        soup = BeautifulSoup(html, "html.parser")

        for link in soup.find_all("a", href=True):
            href = link["href"]
            if "yelp.com" in href and "/biz_redir" not in href:
                continue
            if "/biz_redir" in href:
                if "url=" in href:
                    parsed = urlparse(href)
                    params = parse_qs(parsed.query)
                    actual_url = params.get("url", params.get("website_url", [""]))[0]
                    if actual_url:
                        return unquote(actual_url)

        for elem in soup.find_all(string=re.compile(r"business website|website", re.IGNORECASE)):
            parent = elem.parent
            if parent:
                for link in parent.find_all_next("a", href=True, limit=5):
                    href = link["href"]
                    if "/biz_redir" in href and "url=" in href:
                        parsed = urlparse(href)
                        params = parse_qs(parsed.query)
                        actual_url = params.get("url", params.get("website_url", [""]))[0]
                        if actual_url:
                            return unquote(actual_url)
                    elif href.startswith(("http://", "https://")) and "yelp.com" not in href:
                        return href

        for script in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(script.string)
                if isinstance(data, dict):
                    url = data.get("url", "")
                    if url and "yelp.com" not in url:
                        return url
                elif isinstance(data, list):
                    for item in data:
                        if isinstance(item, dict):
                            url = item.get("url", "")
                            if url and "yelp.com" not in url:
                                return url
            except Exception:
                pass

        patterns = [
            re.compile(r'"websiteUrl"\s*:\s*"(https?://[^"]+)"', re.IGNORECASE),
            re.compile(r'"externalUrl"\s*:\s*"(https?://[^"]+)"', re.IGNORECASE),
            re.compile(r'"website"\s*:\s*"(https?://[^"]+)"', re.IGNORECASE),
            re.compile(r'biz_redir\?url=(https?%3A%2F%2F[^&"]+)', re.IGNORECASE),
        ]
        for pattern in patterns:
            match = pattern.search(html)
            if match:
                url = unquote(match.group(1))
                if "yelp.com" not in url:
                    return url

        return ""

    def _extract_name_from_yelp_profile(self, html: str) -> str:
        soup = BeautifulSoup(html, "html.parser")
        h1 = soup.find("h1")
        if h1:
            return h1.get_text(strip=True)
        og_title = soup.find("meta", property="og:title")
        if og_title:
            title = og_title.get("content", "")
            title = re.sub(r'\s*[-|]\s*Yelp.*$', '', title)
            return title.strip()
        return ""


# ============================================================================
# Proxy Rotation
# ============================================================================
def get_proxy_config(proxy_url: str) -> dict:
    if not proxy_url:
        return None
    parsed = urlparse(proxy_url)
    config = {"server": f"{parsed.scheme}://{parsed.hostname}:{parsed.port}"}
    if parsed.username:
        config["username"] = parsed.username
    if parsed.password:
        config["password"] = parsed.password
    return config


# ============================================================================
# Main - with concurrent Yelp processing
# ============================================================================
async def process_record(
    context,
    yelp_scraper,
    row,
    index,
    limit,
    pre_yelp_done: bool = False,
):
    """Process a single record for Yelp, with resume-aware skipping."""
    name = row.get("name", "").strip()
    city = row.get("city", "").strip()
    state = row.get("state", "").strip()
    website = row.get("website", "").strip()

    log.info(f"-" * 60)
    log.info(f"[{index + 1}/{limit}] {name}")
    log.info(f"         Location: {city}, {state}")
    log.info(f"         Website:  {website}")
    log.info(f"         Domain:   {extract_domain(website)}")

    found_yelp = 0

    current_yelp = row.get("yelp", "").strip()

    yelp_exists = has_value(current_yelp)

    yelp_done = pre_yelp_done or yelp_exists

    needs_yelp = not yelp_done

    yelp_attempt_ok = False

    yelp_page = None

    try:
        if needs_yelp:
            yelp_page = await setup_page(context)
            try:
                yelp_url = await yelp_scraper.search(yelp_page, name, city, state, website)
                yelp_attempt_ok = True
                if yelp_url:
                    row["yelp"] = yelp_url
                    found_yelp = 1
                    log.info(f"   >> Yelp FOUND: {yelp_url[:80]}")
                else:
                    log.info(f"   >> Yelp: Not found")
            except Exception as e:
                log.error(f"   >> Yelp error: {e}")
        else:
            if yelp_exists:
                log.info(f"   >> Yelp: Already exists")
            elif pre_yelp_done:
                log.info(f"   >> Yelp: Already processed previously (no match)")

    finally:
        if yelp_page:
            await yelp_page.close()

    if needs_yelp and yelp_attempt_ok:
        yelp_done = True

    return found_yelp, yelp_done


async def main():
    start_time = time.time()

    log.info("=" * 70)
    log.info("Yelp Profile Scraper (v3 - Fast)")
    log.info("=" * 70)

    log.info(f"Reading {INPUT_CSV}...")
    rows = []
    with open(INPUT_CSV, "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames
        for row in reader:
            rows.append(row)

    total = len(rows)
    start = TEST_START
    end = start + TEST_LIMIT if TEST_LIMIT else total

    progress_state = load_progress(PROGRESS_FILE)
    log.info(f"Loaded progress entries: {len(progress_state)} from {PROGRESS_FILE}")

    # Hydrate rows from prior progress so resume works immediately.
    for row in rows[start:end]:
        cached = progress_state.get(build_record_key(row))
        if not cached:
            continue
        if has_value(cached.get("yelp", "")) and not has_value(row.get("yelp", "")):
            row["yelp"] = cached["yelp"]

    log.info(f"Total records: {total}")
    log.info(f"Processing records {start + 1} to {end}")

    has_yelp = sum(1 for r in rows[start:end] if has_value(r.get("yelp", "")))
    log.info(f"   Already have Yelp: {has_yelp}/{end - start}")
    log.info("")

    yelp_scraper = YelpScraper()
    log.info(f"Record concurrency: {RECORD_CONCURRENCY}")

    found_yelp = 0

    persist_queue: asyncio.Queue = asyncio.Queue()
    live_rows = [dict(r) for r in rows]
    writer_task = asyncio.create_task(progress_writer_worker(persist_queue, live_rows, fieldnames))

    try:
        async with async_playwright() as p:
            proxy_config = None
            if PROXIES:
                proxy_config = get_proxy_config(PROXIES[0])
                log.info(f"Using proxy: {PROXIES[0][:40]}...")

            browser, context = await create_stealth_context(p, proxy_config)
            semaphore = asyncio.Semaphore(max(1, RECORD_CONCURRENCY))

            async def process_and_persist(i_rel: int, i_abs: int, row: dict) -> int:
                async with semaphore:
                    key = build_record_key(row)
                    cached = progress_state.get(key, {})

                    fy, yelp_done = await process_record(
                        context,
                        yelp_scraper,
                        row,
                        i_rel,
                        end - start,
                        pre_yelp_done=bool(cached.get("yelp_done", False)),
                    )

                    progress_event = {
                        "key": key,
                        "name": row.get("name", "").strip(),
                        "city": row.get("city", "").strip(),
                        "state": row.get("state", "").strip(),
                        "website": row.get("website", "").strip(),
                        "yelp": row.get("yelp", "").strip(),
                        "yelp_done": yelp_done,
                        "updated_at": time.time(),
                    }

                    progress_state[key] = {
                        "yelp": progress_event["yelp"],
                        "yelp_done": yelp_done,
                    }

                    await persist_queue.put(
                        {
                            "index": i_abs,
                            "row": dict(row),
                            "progress": progress_event,
                        }
                    )

                    return fy

            tasks = [
                asyncio.create_task(process_and_persist(i, i + start, row))
                for i, row in enumerate(rows[start:end])
            ]

            for task in asyncio.as_completed(tasks):
                try:
                    found_yelp += await task
                except Exception as e:
                    log.error(f"Task error while processing record: {e}")

            await context.close()
            await browser.close()
    finally:
        await persist_queue.put(None)
        await writer_task

    elapsed = time.time() - start_time
    log.info("")
    log.info("=" * 70)
    log.info("RESULTS SUMMARY")
    log.info("=" * 70)
    log.info(f"   Records processed:     {end - start}")
    log.info(f"   New Yelp found:        {found_yelp}")
    log.info(f"   Total Yelp now:        {has_yelp + found_yelp}/{end - start}")
    log.info(f"   Time elapsed:          {elapsed:.1f}s")
    log.info(f"   Output file:           {OUTPUT_CSV}")
    log.info(f"   Progress file:         {PROGRESS_FILE}")
    log.info("=" * 70)


if __name__ == "__main__":
    asyncio.run(main())
