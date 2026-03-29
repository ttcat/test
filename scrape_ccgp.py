#!/usr/bin/env python3
"""
Scraper for 中國政府採購網 (ccgp.gov.cn) bid announcements.

Task A: 舆情监测 related bids (2023-2025)
Task B: 中科天玑 related bids (all time)

Usage:
    pip install playwright beautifulsoup4 lxml
    playwright install chromium
    python scrape_ccgp.py
"""

import csv
import re
import time
import logging
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode, urljoin

from bs4 import BeautifulSoup

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

SEARCH_URL = "https://search.ccgp.gov.cn/bxsearch"

TASK_A_KEYWORDS = [
    "舆情监测",
    "舆情分析",
    "网络舆情",
    "舆情服务",
    "境外社交媒体",
]
TASK_A_START = "2023:01:01"
TASK_A_END = "2025:12:31"
TASK_A_OUTPUT = "ccgp_yuqing_bids.csv"

TASK_B_KEYWORDS = [
    "中科天玑",
    "中科天玑数据科技",
]
TASK_B_START = ""
TASK_B_END = ""
TASK_B_OUTPUT = "ccgp_golaxy_bids.csv"

CSV_HEADER = ["项目名称", "采购人", "中标供应商", "中标金额(万元)", "公告日期", "URL"]

REQUEST_DELAY = 2.5  # seconds between page loads


# ---------------------------------------------------------------------------
# Browser helpers
# ---------------------------------------------------------------------------

def make_browser(playwright):
    """Launch a headless Chromium browser with stealth-ish settings."""
    browser = playwright.chromium.launch(
        headless=True,
        args=[
            "--no-sandbox",
            "--disable-blink-features=AutomationControlled",
        ],
    )
    context = browser.new_context(
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        locale="zh-CN",
        extra_http_headers={"Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"},
    )
    # Remove webdriver flag
    context.add_init_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
    )
    return browser, context


def build_search_url(keyword, page_index=1, start_time="", end_time=""):
    """Build the search URL for a given keyword and page."""
    params = {
        "searchtype": "1",
        "page_index": str(page_index),
        "bidSort": "0",
        "buyerName": "",
        "projectId": "",
        "pinMu": "0",
        "bidType": "7",           # 中标公告
        "dbselect": "bidx",
        "kw": keyword,
        "start_time": start_time,
        "end_time": end_time,
        "timeType": "6" if start_time else "0",
        "displayZone": "",
        "zoneId": "",
        "pppStatus": "0",
        "agentName": "",
    }
    return f"{SEARCH_URL}?{urlencode(params)}"


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def parse_search_results(page):
    """
    Parse the search result list page.
    Returns list of dicts with keys: title, url, date
    Also returns total_pages (int).
    """
    html = page.content()
    soup = BeautifulSoup(html, "lxml")

    results = []
    search_list = soup.select_one("ul.search_list")
    if not search_list:
        # Try alternative selectors
        search_list = soup.select_one(".vT-srch-result-list-bid") or soup.select_one(".vT-srch-result-list")
    if not search_list:
        log.warning("Could not find search result list on page")
        log.debug("Page HTML (first 2000 chars): %s", html[:2000])
        return results, 0

    items = search_list.select("li")
    for li in items:
        a_tag = li.select_one("a")
        if not a_tag:
            continue
        title = a_tag.get_text(strip=True)
        href = a_tag.get("href", "")
        date_span = li.select_one("span.date") or li.select_one("span")
        date_text = date_span.get_text(strip=True) if date_span else ""
        results.append({
            "title": title,
            "url": href,
            "date": date_text,
        })

    # Parse pagination — look for total page count
    total_pages = 1
    pager = soup.select_one("p.pager") or soup.select_one(".pager")
    if pager:
        # Look for "共 X 页" or page links
        text = pager.get_text()
        m = re.search(r"共\s*(\d+)\s*页", text)
        if m:
            total_pages = int(m.group(1))
        else:
            # Count page links
            page_links = pager.select("a")
            if page_links:
                nums = []
                for a in page_links:
                    t = a.get_text(strip=True)
                    if t.isdigit():
                        nums.append(int(t))
                if nums:
                    total_pages = max(nums)

    # Also try the result count text (e.g. "找到 123 条结果")
    result_info = soup.select_one(".search-result-info") or soup.select_one(".result")
    if result_info:
        log.info("Result info: %s", result_info.get_text(strip=True)[:200])

    return results, total_pages


def parse_detail_page(page):
    """
    Parse a bid detail page to extract structured fields.
    Returns dict with: buyer, supplier, amount, date
    """
    html = page.content()
    soup = BeautifulSoup(html, "lxml")

    info = {
        "buyer": "",
        "supplier": "",
        "amount": "",
        "date": "",
    }

    # The detail page typically has a table or div with key-value pairs
    text = soup.get_text()

    # 采购人 (buyer)
    patterns_buyer = [
        r"采购人[：:]\s*(.+?)(?:\s|$)",
        r"采购单位[：:]\s*(.+?)(?:\s|$)",
        r"采\s*购\s*人.*?[：:]\s*(.+?)(?:\s|$)",
    ]
    for pat in patterns_buyer:
        m = re.search(pat, text)
        if m:
            info["buyer"] = m.group(1).strip()
            break

    # 中标供应商 (supplier)
    patterns_supplier = [
        r"中标[（(]?成交[）)]?供应商[：:]\s*(.+?)(?:\s|$)",
        r"中标供应商[：:]\s*(.+?)(?:\s|$)",
        r"成交供应商[：:]\s*(.+?)(?:\s|$)",
        r"供应商名称[：:]\s*(.+?)(?:\s|$)",
    ]
    for pat in patterns_supplier:
        m = re.search(pat, text)
        if m:
            info["supplier"] = m.group(1).strip()
            break

    # 中标金额 (amount) — usually in 万元
    patterns_amount = [
        r"中标[（(]?成交[）)]?金额[：:]\s*([\d,.]+)\s*[（(]?万元",
        r"中标金额[：:]\s*([\d,.]+)\s*[（(]?万元",
        r"成交金额[：:]\s*([\d,.]+)\s*[（(]?万元",
        r"合同金额[：:]\s*([\d,.]+)\s*[（(]?万元",
        r"中标[（(]?成交[）)]?金额[：:]\s*([\d,.]+)\s*元",
        r"中标金额[：:]\s*([\d,.]+)\s*元",
    ]
    for pat in patterns_amount:
        m = re.search(pat, text)
        if m:
            amount_str = m.group(1).replace(",", "")
            try:
                amount = float(amount_str)
                # If matched "元" (not 万元), convert
                if "万元" not in pat:
                    amount = amount / 10000
                info["amount"] = f"{amount:.4f}"
            except ValueError:
                info["amount"] = amount_str
            break

    # 公告日期
    patterns_date = [
        r"发布日期[：:]\s*([\d-]+)",
        r"公告日期[：:]\s*([\d-]+)",
        r"发布时间[：:]\s*([\d-]+)",
    ]
    for pat in patterns_date:
        m = re.search(pat, text)
        if m:
            info["date"] = m.group(1).strip()
            break

    # Also try extracting from table rows
    for tr in soup.select("tr"):
        cells = tr.select("td, th")
        if len(cells) >= 2:
            label = cells[0].get_text(strip=True)
            value = cells[1].get_text(strip=True)
            if "采购人" in label or "采购单位" in label:
                if not info["buyer"]:
                    info["buyer"] = value
            elif "供应商" in label:
                if not info["supplier"]:
                    info["supplier"] = value
            elif "金额" in label:
                if not info["amount"]:
                    m = re.search(r"([\d,.]+)", value)
                    if m:
                        info["amount"] = m.group(1).replace(",", "")

    return info


# ---------------------------------------------------------------------------
# Scraping logic
# ---------------------------------------------------------------------------

def scrape_keyword(context, keyword, start_time, end_time, max_pages=50):
    """
    Scrape all results for a single keyword.
    Returns list of dicts with all CSV fields.
    """
    all_records = []
    page_index = 1
    page = context.new_page()

    try:
        while page_index <= max_pages:
            url = build_search_url(keyword, page_index, start_time, end_time)
            log.info("Fetching search page %d for keyword '%s'", page_index, keyword)

            try:
                page.goto(url, timeout=30000, wait_until="domcontentloaded")
                page.wait_for_timeout(3000)  # Wait for JS rendering
            except Exception as e:
                log.error("Failed to load search page: %s", e)
                break

            results, total_pages = parse_search_results(page)
            if not results:
                log.info("No results found on page %d, stopping.", page_index)
                break

            log.info("Found %d results on page %d (total pages: %d)", len(results), page_index, total_pages)

            for item in results:
                detail_url = item["url"]
                if not detail_url:
                    continue

                # Ensure absolute URL
                if detail_url.startswith("/"):
                    detail_url = f"https://www.ccgp.gov.cn{detail_url}"
                elif not detail_url.startswith("http"):
                    detail_url = f"https://www.ccgp.gov.cn/{detail_url}"

                log.info("  Fetching detail: %s", item["title"][:60])
                time.sleep(REQUEST_DELAY)

                detail_page = context.new_page()
                try:
                    detail_page.goto(detail_url, timeout=30000, wait_until="domcontentloaded")
                    detail_page.wait_for_timeout(2000)
                    info = parse_detail_page(detail_page)
                except Exception as e:
                    log.warning("  Failed to load detail page %s: %s", detail_url, e)
                    info = {"buyer": "", "supplier": "", "amount": "", "date": ""}
                finally:
                    detail_page.close()

                record = {
                    "项目名称": item["title"],
                    "采购人": info["buyer"],
                    "中标供应商": info["supplier"],
                    "中标金额(万元)": info["amount"],
                    "公告日期": info["date"] or item["date"],
                    "URL": detail_url,
                }
                all_records.append(record)
                log.info("  -> Buyer: %s | Supplier: %s | Amount: %s",
                         record["采购人"][:20], record["中标供应商"][:20], record["中标金额(万元)"])

            if page_index >= total_pages:
                log.info("Reached last page (%d).", total_pages)
                break

            page_index += 1
            time.sleep(REQUEST_DELAY)
    finally:
        page.close()

    return all_records


def write_csv(records, filename):
    """Write records to a CSV file."""
    filepath = Path(__file__).parent / filename
    with open(filepath, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_HEADER)
        writer.writeheader()
        writer.writerows(records)
    log.info("Wrote %d records to %s", len(records), filepath)
    return filepath


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser, context = make_browser(pw)

        try:
            # ---- Task A: 舆情监测 related bids ----
            log.info("=" * 60)
            log.info("TASK A: Scraping 舆情监测 related bids (2023-2025)")
            log.info("=" * 60)

            task_a_records = []
            seen_urls = set()

            for kw in TASK_A_KEYWORDS:
                log.info("-" * 40)
                log.info("Keyword: %s", kw)
                records = scrape_keyword(context, kw, TASK_A_START, TASK_A_END)
                for r in records:
                    if r["URL"] not in seen_urls:
                        seen_urls.add(r["URL"])
                        task_a_records.append(r)
                    else:
                        log.info("  Skipping duplicate: %s", r["项目名称"][:40])

            write_csv(task_a_records, TASK_A_OUTPUT)
            log.info("Task A complete: %d unique records", len(task_a_records))

            # ---- Task B: 中科天玑 bids ----
            log.info("=" * 60)
            log.info("TASK B: Scraping 中科天玑 bids (all time)")
            log.info("=" * 60)

            task_b_records = []
            seen_urls = set()

            for kw in TASK_B_KEYWORDS:
                log.info("-" * 40)
                log.info("Keyword: %s", kw)
                records = scrape_keyword(context, kw, TASK_B_START, TASK_B_END)
                for r in records:
                    if r["URL"] not in seen_urls:
                        seen_urls.add(r["URL"])
                        task_b_records.append(r)
                    else:
                        log.info("  Skipping duplicate: %s", r["项目名称"][:40])

            write_csv(task_b_records, TASK_B_OUTPUT)
            log.info("Task B complete: %d unique records", len(task_b_records))

        finally:
            context.close()
            browser.close()

    log.info("All done!")


if __name__ == "__main__":
    main()
