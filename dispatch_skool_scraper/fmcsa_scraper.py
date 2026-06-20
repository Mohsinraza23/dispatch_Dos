"""
fmcsa_scraper.py
────────────────
Dispatch DOS — FMCSA Company Snapshot Scraper (Production, 2026)

Target  : https://safer.fmcsa.dot.gov/query.asp
Primary : requests (GET) + BeautifulSoup — fast, no browser
Fallback: Playwright Chromium (headless) — triggered if HTTP 403 / blocked page

Verified HTML structure (from williamhaley/dot-safer-fmcsa-api + fiacre/FMCSA-scraper):
  • GET params : searchType=ANY, query_type=queryCarrierSnapshot,
                 query_param=USDOT|MC_MX, query_string=<value>
  • Labels in <th>, values in same-row <td>   →  find(string=label) → parent <tr> → find("td")
  • Lists      : <table summary="Cargo Carried|Operation Classification|Carrier Operation">
  • Not-found  : "no records matching" / "the record matching" in page text

Install:
    pip install requests beautifulsoup4 lxml playwright
    playwright install chromium
    pip install playwright-stealth   # optional but recommended

Quick start:
    from fmcsa_scraper import scrape_carrier
    result = scrape_carrier("1597181", "USDOT")
    result = scrape_carrier("MC193369", "MC")
"""

from __future__ import annotations

# ── stdlib ────────────────────────────────────────────────────────────────────
import re
import sys
import json
import time
import random
import asyncio
import logging
import argparse
from datetime import datetime
from typing import Any

# ── third-party ───────────────────────────────────────────────────────────────
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from bs4 import BeautifulSoup, NavigableString, Tag

# ── optional: Playwright (fallback browser) ───────────────────────────────────
_PLAYWRIGHT_OK = False
try:
    from playwright.async_api import (
        async_playwright,
        TimeoutError as _PWTimeout,
    )
    _PLAYWRIGHT_OK = True
except ImportError:
    pass

# ── optional: playwright-stealth (stronger fingerprint evasion) ───────────────
_STEALTH_OK = False
try:
    from playwright_stealth import stealth_async          # type: ignore
    _STEALTH_OK = True
except ImportError:
    pass


# ─────────────────────────────────────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────────────────────────────────────

log = logging.getLogger("fmcsa_scraper")
if not log.handlers:
    _h = logging.StreamHandler(sys.stdout)
    _h.setFormatter(logging.Formatter(
        "%(asctime)s  %(levelname)-7s  %(message)s", datefmt="%H:%M:%S"
    ))
    log.addHandler(_h)
    log.setLevel(logging.INFO)


# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

FMCSA_URL     = "https://safer.fmcsa.dot.gov/query.asp"
FMCSA_API_URL = "https://mobile.fmcsa.dot.gov/qc/services/carriers"

# Verified GET parameters (same params appear in live FMCSA search result URLs)
_QUERY_BASE: dict[str, str] = {
    "searchType":  "ANY",                # capital T — confirmed from reference impl
    "query_type":  "queryCarrierSnapshot",
}

# Public search_type → FMCSA's internal query_param value
SEARCH_TYPE_MAP: dict[str, str] = {
    "USDOT": "USDOT",
    "MC":    "MC_MX",    # FMCSA internal name for MC/MX searches
    "NAME":  "NAME",
}

# Default delay range between requests (seconds) — configurable per call
DEFAULT_DELAY_MIN = 12.0
DEFAULT_DELAY_MAX = 25.0

# ── Field labels exactly as printed on FMCSA result page ─────────────────────
# Pattern: <th>Legal Name:</th><td>[value]</td> in same <tr>
_FIELD_LABELS: dict[str, str] = {
    "legal_name":               "Legal Name:",
    "dba_name":                 "DBA Name:",
    "physical_address":         "Physical Address:",
    "phone":                    "Phone:",
    "mailing_address":          "Mailing Address:",
    "usdot_number":             "USDOT Number:",
    "state_carrier_id":         "State Carrier ID Number:",
    "mc_mx_raw":                "MC/MX/FF Number(s):",
    "duns_number":              "DUNS Number:",
    "entity_type":              "Entity Type:",
    "operating_authority_status": "Operating Authority Status:",
    "safety_rating":            "Safety Rating:",
    "safety_rating_date":       "Rating Date:",
    "review_date":              "Review Date:",
    "oos_date":                 "Out of Service Date:",
    "power_units":              "Power Units:",
    "drivers":                  "Drivers:",
    "mcs150_date":              "MCS-150 Form Date:",
    "mcs150_mileage":           "MCS-150 Mileage (Year):",
}

# Named-table summary attributes for checkbox-list sections
_TABLE_OP_CLASS  = "Operation Classification"
_TABLE_CARGO     = "Cargo Carried"
_TABLE_CARRIER_OP = "Carrier Operation"

# ── Page-state detection strings ──────────────────────────────────────────────
_NOT_FOUND = [
    "no records matching",
    "record not found",
    "the record matching",        # "The record matching X could not be found"
    "could not be found",
    "0 records found",
    "no information available",
]
_BLOCKED = [
    "captcha",
    "access denied",
    "403 forbidden",
    "unusual traffic",
    "robot check",
    "please enable javascript",
    "automated access",
]

# ── Stealth user-agent pool ────────────────────────────────────────────────────
_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36 Edg/123.0.0.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4_1) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.4.1 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) "
    "Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
]


# ─────────────────────────────────────────────────────────────────────────────
# Internal text utilities
# ─────────────────────────────────────────────────────────────────────────────

def _clean(text: str | None) -> str:
    """Collapse whitespace and strip. Returns '' for None/falsy."""
    if not text:
        return ""
    return re.sub(r"[\s\r\n]+", " ", str(text)).strip()


def _td_text(td: Tag) -> str:
    """
    Collect all NavigableString descendants from a <td>.
    This correctly handles labels wrapped in <b>, <font>, <a>, etc.
    Proven technique from williamhaley/dot-safer-fmcsa-api.
    """
    pieces: list[str] = []
    for child in td.descendants:
        if not isinstance(child, NavigableString):
            continue
        fragment = str(child).replace("\r\n", " ").replace("\n", " ").strip()
        if fragment:
            pieces.append(fragment)
    return _clean(" ".join(pieces))


# ─────────────────────────────────────────────────────────────────────────────
# HTML parsing — single field
# ─────────────────────────────────────────────────────────────────────────────

def _find_field(root: BeautifulSoup | Tag, label: str) -> str:
    """
    Find a field value by its exact label text.

    FMCSA page structure (verified):
        <tr>
            <th>Legal Name:</th>        ← NavigableString we search for
            <td>ACME TRUCKING INC</td>  ← first <td> in same row = value
        </tr>

    Falls back to case-insensitive partial match if exact fails.
    """
    # 1. Exact match
    node = root.find(string=label)

    # 2. Case-insensitive partial match — search ONLY inside <th> elements
    # to avoid false positives from <td> cells that happen to contain label text
    # (e.g. FMCSA form dropdowns that say "USDOT Number").
    if node is None:
        bare = label.rstrip(":").strip().lower()
        for th in root.find_all("th"):
            if bare in th.get_text().lower():
                node = next(iter(th.strings), None)
                break

    if node is None:
        return ""

    tr = node.find_parent("tr")
    if not tr:
        return ""

    td = tr.find("td")
    return _td_text(td) if td else ""


def _extract_all_fields(soup: BeautifulSoup) -> dict[str, str]:
    """
    Extract all single-value fields using the label→td pattern.
    Narrows search to the main entity table (anchored on 'Entity' text)
    to avoid picking up false positives from nav bars or footers.
    """
    # Narrow to entity table if possible
    anchor = soup.find(string=re.compile(r"\bEntity\b", re.IGNORECASE))
    search_root: BeautifulSoup | Tag = soup
    if anchor:
        parent_table = anchor.find_parent("table")
        if parent_table:
            search_root = parent_table

    fields: dict[str, str] = {}
    for key, label in _FIELD_LABELS.items():
        val = _find_field(search_root, label)
        # If narrowed search missed it, retry on full soup
        if not val and search_root is not soup:
            val = _find_field(soup, label)
        fields[key] = val

    return fields


# ─────────────────────────────────────────────────────────────────────────────
# HTML parsing — list tables
# ─────────────────────────────────────────────────────────────────────────────

def _parse_named_table(soup: BeautifulSoup, summary: str) -> list[str]:
    """
    Extract items from <table summary="..."> sections.
    FMCSA uses these for Cargo Carried, Operation Classification, Carrier Operation.
    Each row has a checkmark indicator cell + label cell; we grab all label-like text.
    """
    table = soup.find("table", attrs={"summary": summary})
    if not table:
        return []

    seen: set[str] = set()
    items: list[str] = []
    for cell in table.find_all("td"):
        text = _clean(cell.get_text())
        # Keep only meaningful labels (> 2 chars, not a lone digit or symbol)
        if len(text) > 2 and not re.fullmatch(r"[\d\W]+", text) and text not in seen:
            seen.add(text)
            items.append(text)
    return items


# ─────────────────────────────────────────────────────────────────────────────
# HTML parsing — inspection / crash statistics
# ─────────────────────────────────────────────────────────────────────────────

def _parse_inspection_stats(soup: BeautifulSoup) -> dict[str, str]:
    """
    Extract US inspection statistics and crash data.

    Expected inspection table layout:
                     Vehicle  Driver  Hazmat  IEP
        Inspections    150      120      5      0
        Out of Service  25       10      1      0
        Out of Service % 16.7%  8.3%  20.0%  0.0%
        Nat'l Average %  20.1% 10.2%   4.1%  N/A

    Crash table (separate):
                  US   Canada
        Fatal      2      0
        Injury     5      0
        Tow       10      0
        Total     17      0
    """
    stats: dict[str, str] = {}
    col_map: dict[int, str] = {}   # column-index → header name

    # ── Inspection table ──────────────────────────────────────────────────────
    for table in soup.find_all("table"):
        txt = table.get_text(" ", strip=True)
        if "Inspections" not in txt or "Vehicle" not in txt:
            continue

        for row in table.find_all("tr"):
            cells = row.find_all(["th", "td"])
            if not cells:
                continue
            vals = [_clean(c.get_text()) for c in cells]
            first = vals[0].lower()

            # Detect header row by presence of column keywords
            if not col_map and any(h in vals for h in ("Vehicle", "Driver", "Hazmat")):
                for idx, v in enumerate(vals):
                    if v in ("Vehicle", "Driver", "Hazmat", "IEP"):
                        col_map[idx] = v.lower()
                continue

            if not col_map:
                continue

            # Data rows — keyed by row label
            def _store(suffix: str) -> None:
                for idx, col in col_map.items():
                    if idx < len(vals):
                        raw = vals[idx].rstrip("%").strip()
                        if raw and raw not in ("N/A", "-", ""):
                            stats[f"{col}_{suffix}"] = raw

            if "inspection" in first and "out" not in first:
                _store("inspections")
            elif "out of service" in first and "%" not in first:
                _store("oos_count")
            elif "out of service" in first and "%" in first:
                _store("oos_pct")
            elif "nat" in first and "avg" in first:
                _store("nat_avg_pct")

        if col_map:
            break    # found and parsed the inspection table

    # ── Crash table ───────────────────────────────────────────────────────────
    for table in soup.find_all("table"):
        txt = table.get_text(" ", strip=True)
        if "Fatal" not in txt or "Injury" not in txt:
            continue
        for row in table.find_all("tr"):
            cells = row.find_all(["th", "td"])
            if len(cells) < 2:
                continue
            label = _clean(cells[0].get_text()).lower()
            val   = _clean(cells[1].get_text())
            if "fatal"  in label:  stats["crash_fatal"]  = val
            elif "injury" in label: stats["crash_injury"] = val
            elif "tow"    in label: stats["crash_tow"]    = val
            elif "total"  in label: stats["crash_total"]  = val
        break

    return stats


# ─────────────────────────────────────────────────────────────────────────────
# Status helpers
# ─────────────────────────────────────────────────────────────────────────────

def _detect_page_problem(text_lower: str) -> str | None:
    """Return 'not_found', 'blocked', or None."""
    if any(s in text_lower for s in _NOT_FOUND):
        return "not_found"
    if any(s in text_lower for s in _BLOCKED):
        return "blocked"
    return None


def _derive_carrier_status(fields: dict[str, str], page_text_lower: str) -> str:
    """
    Derive ACTIVE / INACTIVE / OUT_OF_SERVICE from scraped fields.
    Priority: OOS date present → OUT_OF_SERVICE
              auth status says inactive/revoked → INACTIVE
              auth status / page says active → ACTIVE
              default → INACTIVE (conservative)
    """
    oos = fields.get("oos_date", "").strip().lower()
    _date_pat = re.compile(r'\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4}')
    if oos and oos not in ("", "none", "n/a", "-") and _date_pat.search(oos):
        return "OUT_OF_SERVICE"

    auth = (fields.get("operating_authority_status", "") + " " +
            fields.get("entity_type", "")).lower()
    if any(s in auth for s in ("inactive", "revoked", "cancelled", "not authorized")):
        return "INACTIVE"

    active_signals = ("authorized for hire", "active", "authorized", "common", "contract")
    if any(s in auth for s in active_signals):
        return "ACTIVE"
    if any(s in page_text_lower for s in ("authorized for hire", "active")):
        return "ACTIVE"

    return "INACTIVE"


# ─────────────────────────────────────────────────────────────────────────────
# Master HTML parser  (called by both requests path and Playwright path)
# ─────────────────────────────────────────────────────────────────────────────

def _parse_snapshot_html(html: str) -> dict[str, Any]:
    """
    Parse a FMCSA Company Snapshot HTML page.
    Returns a dict with '_ok' (bool) and all extracted fields.
    """
    soup = BeautifulSoup(html, "lxml")
    page_text = soup.get_text(" ", strip=True)
    page_lower = page_text.lower()

    # ── Detect page problems ──────────────────────────────────────────────────
    problem = _detect_page_problem(page_lower)
    if problem:
        return {"_ok": False, "_problem": problem}

    # ── Extract all data ──────────────────────────────────────────────────────
    fields   = _extract_all_fields(soup)

    # If key identity fields are empty the page had no real carrier data
    if not fields.get("legal_name") and not fields.get("usdot_number"):
        return {"_ok": False, "_problem": "not_found"}
    op_class = _parse_named_table(soup, _TABLE_OP_CLASS)
    cargo    = _parse_named_table(soup, _TABLE_CARGO)
    carr_op  = _parse_named_table(soup, _TABLE_CARRIER_OP)
    insp     = _parse_inspection_stats(soup)

    # Clean MC number — strip "MC-" prefix so callers get bare digits
    mc_raw   = fields.get("mc_mx_raw", "")
    mc_clean = re.sub(r"^MC[- ]?", "", mc_raw, flags=re.IGNORECASE).strip()
    # If multiple numbers (comma-separated), take the first
    mc_clean = mc_clean.split(",")[0].split(";")[0].strip()

    # Headline OOS percentage — Vehicle takes priority, else Driver
    oos_pct = (insp.get("vehicle_oos_pct") or
               insp.get("driver_oos_pct") or "")

    carrier_status = _derive_carrier_status(fields, page_lower)

    return {
        "_ok": True,
        # Identity
        "legal_name":               fields.get("legal_name",    ""),
        "dba_name":                 fields.get("dba_name",      ""),
        "usdot_number":             fields.get("usdot_number",  ""),
        "mc_number":                mc_clean,
        "mc_mx_raw":                mc_raw,
        "state_carrier_id":         fields.get("state_carrier_id", ""),
        "duns_number":              fields.get("duns_number",   ""),
        # Contact
        "physical_address":         fields.get("physical_address",  ""),
        "mailing_address":          fields.get("mailing_address",   ""),
        "phone":                    fields.get("phone",              ""),
        # Classification
        "entity_type":              fields.get("entity_type",           ""),
        "usdot_status":             fields.get("operating_authority_status", ""),
        "operating_authority_status": fields.get("operating_authority_status", ""),
        "safety_rating":            fields.get("safety_rating",        ""),
        "safety_rating_date":       fields.get("safety_rating_date",   ""),
        "review_date":              fields.get("review_date",          ""),
        "oos_date":                 fields.get("oos_date",             ""),
        # Fleet
        "power_units":              fields.get("power_units",  ""),
        "drivers":                  fields.get("drivers",      ""),
        "mcs150_date":              fields.get("mcs150_date",  ""),
        "mcs150_mileage":           fields.get("mcs150_mileage", ""),
        # Lists (pipe-joined for Excel; callers can also use the raw lists)
        "operation_classification": op_class,
        "carrier_operation":        carr_op,
        "cargo_carried":            cargo,
        # Stats
        "out_of_service_percentage": oos_pct,
        "inspection_stats":          insp,
        # Derived
        "carrier_status": carrier_status,    # ACTIVE / INACTIVE / OUT_OF_SERVICE
    }


# ─────────────────────────────────────────────────────────────────────────────
# Network layer 1 — requests  (fast, no browser)
# ─────────────────────────────────────────────────────────────────────────────

def _make_session(proxy: str | None = None) -> requests.Session:
    """Create a requests.Session with retry logic and optional proxy."""
    session = requests.Session()
    try:
        retry = Retry(
            total=1,        # only 1 retry for status-code errors
            connect=0,      # no retries on connection failures (fast fail)
            read=0,         # no retries on read timeouts (fast fail)
            backoff_factor=0.5,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET", "POST"],
        )
    except TypeError:
        retry = Retry(                          # type: ignore[call-arg]
            total=1,
            connect=0,
            read=0,
            backoff_factor=0.5,
            status_forcelist=[429, 500, 502, 503, 504],
            method_whitelist=["GET", "POST"],
        )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://",  adapter)
    if proxy:
        session.proxies = {"http": proxy, "https": proxy}
        log.info(f"Using proxy: {proxy}")
    return session




def _requests_get(
    search_value: str,
    query_param: str,
    session: requests.Session,
) -> tuple[str | None, str]:
    """
    GET https://safer.fmcsa.dot.gov/query.asp with FMCSA parameters.
    Returns (html | None, error_message).
    """
    params = {
        **_QUERY_BASE,
        "query_param":  query_param,
        "query_string": search_value,
    }
    headers = {
        "User-Agent":                random.choice(_USER_AGENTS),
        "Accept":                    "text/html,application/xhtml+xml,*/*;q=0.8",
        "Accept-Language":           "en-US,en;q=0.9",
        "Accept-Encoding":           "gzip, deflate, br",
        "Connection":                "keep-alive",
        "Referer":                   "https://safer.fmcsa.dot.gov/CompanySnapshot.aspx",
        "Upgrade-Insecure-Requests": "1",
        "Cache-Control":             "no-cache",
    }
    try:
        resp = session.get(
            FMCSA_URL, params=params, headers=headers,
            timeout=15, allow_redirects=True,
        )
        resp.raise_for_status()
        return resp.text, ""
    except requests.exceptions.Timeout:
        return None, "timeout"
    except requests.exceptions.HTTPError as e:
        return None, f"HTTP {e.response.status_code}"
    except requests.exceptions.RequestException as e:
        return None, str(e)


# ─────────────────────────────────────────────────────────────────────────────
# Network layer 2 — Playwright  (fallback browser, harder to block)
# ─────────────────────────────────────────────────────────────────────────────

async def _playwright_get(
    search_value: str,
    query_param: str,
    headless: bool = True,
) -> tuple[str | None, str]:
    """
    Navigate to FMCSA via Playwright Chromium and return page HTML.
    Returns (html | None, error_message).
    Dynamic import supports runtime installation via pip.
    """
    # Dynamic import — works even if playwright was installed after module load
    try:
        from playwright.async_api import (
            async_playwright as _async_playwright,
            TimeoutError as _PWTimeoutDyn,
        )
    except ImportError:
        return None, "playwright not installed — run: pip install playwright && playwright install chromium"

    # Optional stealth (check dynamically)
    _stealth_fn = None
    try:
        from playwright_stealth import stealth_async as _stealth_async
        _stealth_fn = _stealth_async
    except ImportError:
        pass

    from urllib.parse import urlencode
    params = {**_QUERY_BASE, "query_param": query_param, "query_string": search_value}
    url = f"{FMCSA_URL}?{urlencode(params)}"

    try:
        async with _async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=headless,
                args=[
                    "--no-sandbox",
                    "--disable-blink-features=AutomationControlled",
                    "--disable-dev-shm-usage",
                    "--disable-infobars",
                    "--disable-extensions",
                ],
            )
            ctx = await browser.new_context(
                user_agent=random.choice(_USER_AGENTS),
                viewport={"width": random.randint(1280, 1440),
                          "height": random.randint(768, 900)},
                locale="en-US",
                timezone_id="America/Chicago",
                extra_http_headers={
                    "Accept-Language":           "en-US,en;q=0.9",
                    "Referer":                   "https://safer.fmcsa.dot.gov/CompanySnapshot.aspx",
                    "Upgrade-Insecure-Requests": "1",
                },
            )
            # Mask automation signals
            await ctx.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
                Object.defineProperty(navigator, 'plugins',   {get: () => [1,2,3,4,5]});
                Object.defineProperty(navigator, 'languages', {get: () => ['en-US','en']});
                window.chrome = { runtime: {} };
            """)

            page = await ctx.new_page()
            if _stealth_fn is not None:
                await _stealth_fn(page)

            await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            try:
                await page.wait_for_load_state("networkidle", timeout=15_000)
            except _PWTimeoutDyn:
                pass   # old static pages never fire networkidle — fine

            html = await page.content()
            await browser.close()
            return html, ""

    except Exception as exc:
        return None, str(exc)


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def _api_get(
    search_value: str,
    search_type: str,
    web_key: str,
    session: requests.Session,
) -> dict[str, Any] | None:
    """
    Fetch carrier data from FMCSA official REST API.
    Returns parsed dict (same shape as scrape_carrier result) or None on error.
    API docs: https://mobile.fmcsa.dot.gov/qc/services/carriers/{dotNumber}?webKey={key}
    """
    try:
        if search_type == "MC":
            url = f"{FMCSA_API_URL}/docket-number/{search_value}?webKey={web_key}"
        else:
            url = f"{FMCSA_API_URL}/{search_value}?webKey={web_key}"

        headers = {"Accept": "application/json", "User-Agent": random.choice(_USER_AGENTS)}
        resp = session.get(url, headers=headers, timeout=15)

        if resp.status_code == 404:
            return {"status": "not_found", "error_detail": "Not found via API"}
        if resp.status_code == 401:
            return {"status": "error", "error_detail": "Invalid FMCSA API key"}
        resp.raise_for_status()

        data = resp.json()
        # API wraps response in {"content": {"carrier": {...}}}
        carrier = (data.get("content") or {}).get("carrier")
        if not carrier:
            # Some responses use {"carrierList": {"content": [...]}}
            clist = data.get("carrierList", {}).get("content", [])
            carrier = clist[0] if clist else None
        if not carrier:
            return {"status": "not_found", "error_detail": "No carrier in API response"}

        # Map API JSON keys → our standard field names
        def _s(val: Any) -> str:
            return str(val).strip() if val is not None else ""

        oos_date = _s(carrier.get("oosDate") or carrier.get("safetyRatingDate") or "")
        op_status = _s(carrier.get("operatingStatus") or carrier.get("statusCode") or "")
        # Derive status
        fields_tmp = {"oos_date": oos_date, "operating_authority_status": op_status, "entity_type": ""}
        carrier_status = _derive_carrier_status(fields_tmp, op_status.lower())

        phone = _s(carrier.get("telephone") or carrier.get("phone") or "")
        if phone.isdigit() and len(phone) == 10:
            phone = f"({phone[:3]}) {phone[3:6]}-{phone[6:]}"

        address_parts = [
            carrier.get("phyStreet") or "",
            carrier.get("phyCity") or "",
            carrier.get("phyState") or "",
            carrier.get("phyZipcode") or "",
        ]
        phys_addr = ", ".join(p for p in address_parts if p)

        mail_parts = [
            carrier.get("mailingStreet") or carrier.get("mailStreet") or "",
            carrier.get("mailingCity") or carrier.get("mailCity") or "",
            carrier.get("mailingState") or carrier.get("mailState") or "",
            carrier.get("mailingZipcode") or carrier.get("mailZipcode") or "",
        ]
        mail_addr = ", ".join(p for p in mail_parts if p)

        return {
            "status":          "found",
            "fetch_method":    "api",
            "search_value":    search_value,
            "search_type":     search_type,
            "scraped_at":      datetime.utcnow().isoformat() + "Z",
            "error_detail":    "",
            "legal_name":      _s(carrier.get("legalName")),
            "dba_name":        _s(carrier.get("dbaName")),
            "usdot_number":    _s(carrier.get("dotNumber")),
            "mc_number":       _s(carrier.get("mcNumber") or carrier.get("docketNumber") or ""),
            "mc_mx_raw":       "",
            "state_carrier_id": _s(carrier.get("stateCarrierId") or ""),
            "duns_number":     "",
            "physical_address": phys_addr,
            "mailing_address": mail_addr,
            "phone":           phone,
            "entity_type":     _s(carrier.get("entityType") or ""),
            "usdot_status":    op_status,
            "operating_authority_status": op_status,
            "safety_rating":   _s(carrier.get("safetyRating") or ""),
            "safety_rating_date": _s(carrier.get("safetyRatingDate") or ""),
            "review_date":     _s(carrier.get("reviewDate") or ""),
            "oos_date":        oos_date,
            "power_units":     _s(carrier.get("powerUnits") or carrier.get("totalPowerUnits") or ""),
            "drivers":         _s(carrier.get("totalDrivers") or carrier.get("drivers") or ""),
            "mcs150_date":     _s(carrier.get("mcs150FormDate") or ""),
            "mcs150_mileage":  _s(carrier.get("mcs150Mileage") or ""),
            "operation_classification": [],
            "carrier_operation":        [],
            "cargo_carried":            [],
            "out_of_service_percentage": "",
            "inspection_stats":          {},
            "carrier_status":  carrier_status,
            "raw_html":        "",
        }

    except Exception as exc:
        log.warning(f"  API error: {exc}")
        return None


def scrape_carrier(
    search_value: str,
    search_type: str = "USDOT",
    *,
    headless: bool = True,
    delay_min: float = DEFAULT_DELAY_MIN,
    delay_max: float = DEFAULT_DELAY_MAX,
    use_playwright_fallback: bool = True,
    include_raw_html: bool = True,
    web_key: str = "",
    _session: requests.Session | None = None,
) -> dict[str, Any]:
    """
    Scrape FMCSA Company Snapshot for one carrier.

    Parameters
    ----------
    search_value            MC/USDOT number or company name.
                            "MC" prefix is stripped automatically.
    search_type             "USDOT" | "MC" | "NAME"
    headless                Playwright browser visibility (True = hidden).
    delay_min / delay_max   Random seconds to wait after the response
                            (anti-blocking; fires after fetch, before return).
    use_playwright_fallback Launch browser if requests returns blocked/403.
    include_raw_html        Attach full HTML to result["raw_html"] for debugging.
    _session                Pass an existing requests.Session to reuse connections.

    Returns
    -------
    dict — every key is always present (empty string / empty list when absent):

    Tracking keys
        status               "found" | "not_found" | "blocked" | "error"
        fetch_method         "requests" | "playwright"
        search_value         cleaned input value
        search_type          "USDOT" / "MC" / "NAME"
        scraped_at           ISO-8601 UTC timestamp
        error_detail         error message when status != "found"

    Carrier data keys
        legal_name, dba_name
        usdot_number, mc_number, mc_mx_raw, state_carrier_id, duns_number
        physical_address, mailing_address, phone
        entity_type, usdot_status, operating_authority_status
        safety_rating, safety_rating_date, review_date, oos_date
        power_units, drivers, mcs150_date, mcs150_mileage
        operation_classification (list), carrier_operation (list), cargo_carried (list)
        out_of_service_percentage   (Vehicle OOS% — headline number)
        inspection_stats            (full dict: vehicle_inspections, driver_oos_pct, ...)
        carrier_status              "ACTIVE" | "INACTIVE" | "OUT_OF_SERVICE"
        raw_html                    full page HTML (when include_raw_html=True)
    """
    # ── Normalise inputs ──────────────────────────────────────────────────────
    search_type  = search_type.upper().strip()
    search_value = search_value.strip()

    if search_type not in SEARCH_TYPE_MAP:
        raise ValueError(
            f"search_type must be one of {list(SEARCH_TYPE_MAP.keys())}, got '{search_type}'"
        )

    # Strip MC/DOT prefixes — FMCSA form wants bare digits
    if search_type in ("USDOT", "MC"):
        search_value = re.sub(
            r"^(MC|DOT|USDOT)\s*[#\-\s]?", "", search_value, flags=re.IGNORECASE
        ).strip()

    query_param = SEARCH_TYPE_MAP[search_type]

    # ── Empty result skeleton ─────────────────────────────────────────────────
    result: dict[str, Any] = {
        "status":        "error",
        "fetch_method":  "",
        "search_value":  search_value,
        "search_type":   search_type,
        "scraped_at":    datetime.utcnow().isoformat() + "Z",
        # Identity
        "legal_name": "", "dba_name": "", "usdot_number": "",
        "mc_number": "", "mc_mx_raw": "", "state_carrier_id": "", "duns_number": "",
        # Contact
        "physical_address": "", "mailing_address": "", "phone": "",
        # Classification
        "entity_type": "", "usdot_status": "",
        "operating_authority_status": "",
        "safety_rating": "", "safety_rating_date": "",
        "review_date": "", "oos_date": "",
        # Fleet
        "power_units": "", "drivers": "", "mcs150_date": "", "mcs150_mileage": "",
        # Lists
        "operation_classification": [], "carrier_operation": [], "cargo_carried": [],
        # Stats
        "out_of_service_percentage": "",
        "inspection_stats": {},
        # Derived
        "carrier_status": "",
        # Debug
        "raw_html":    "" if include_raw_html else None,
        "error_detail": "",
    }

    # ── Step 0 — Official FMCSA API (fastest, never IP-blocked) ─────────────
    own_session = _session is None
    sess = _session or _make_session()

    if web_key and search_type in ("USDOT", "MC"):
        log.info(f"[api] {search_type}:{search_value}")
        api_result = _api_get(search_value, search_type, web_key, sess)
        if api_result is not None:
            result.update(api_result)
            # Apply delay even for API calls (polite)
            wait = random.uniform(min(delay_min, 2), min(delay_max, 5))
            time.sleep(wait)
            if own_session:
                sess.close()
            return result

    # ── Step 1 — requests (HTML scraper) ─────────────────────────────────────
    log.info(f"[requests] {search_type}:{search_value}")
    html, err = _requests_get(search_value, query_param, sess)

    # Detect if the response itself is a blocked page (200 OK with block content)
    if html and _detect_page_problem(html[:2000].lower()) == "blocked":
        log.warning("  requests blocked — switching to Playwright")
        html, err = None, "server blocked requests"

    # ── Step 2 — Playwright fallback ──────────────────────────────────────────
    if html is None and use_playwright_fallback:
        log.info(f"[playwright] {search_type}:{search_value}  ({err})")
        html, err = asyncio.run(
            _playwright_get(search_value, query_param, headless=headless)
        )
        result["fetch_method"] = "playwright"
    else:
        result["fetch_method"] = "requests"

    if html is None:
        result["status"]       = "error"
        result["error_detail"] = err
        if own_session:
            sess.close()
        return result

    # ── Step 3 — Anti-blocking delay ──────────────────────────────────────────
    wait = random.uniform(delay_min, delay_max)
    log.info(f"  Waiting {wait:.1f}s …")
    time.sleep(wait)

    # ── Step 4 — Parse ────────────────────────────────────────────────────────
    parsed = _parse_snapshot_html(html)

    if not parsed.get("_ok"):
        problem = parsed.get("_problem", "unknown")
        result["status"]       = problem
        result["error_detail"] = f"Page problem: {problem}"
        if include_raw_html:
            result["raw_html"] = html
        if own_session:
            sess.close()
        return result

    # ── Step 5 — Populate result ──────────────────────────────────────────────
    for key, val in parsed.items():
        if not key.startswith("_") and key in result:
            result[key] = val

    # 'status' in parsed is carrier_status (ACTIVE/INACTIVE); keep it separate
    result["carrier_status"] = parsed.get("carrier_status", "")
    result["status"]         = "found"

    if include_raw_html:
        result["raw_html"] = html

    if own_session:
        sess.close()

    log.info(
        f"  ✓  {result['legal_name'] or '(name not found)'}  |  "
        f"USDOT {result['usdot_number']}  |  {result['carrier_status']}"
    )
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Batch helper
# ─────────────────────────────────────────────────────────────────────────────

def scrape_batch(
    carriers: list[dict[str, str]],
    delay_min: float = DEFAULT_DELAY_MIN,
    delay_max: float = DEFAULT_DELAY_MAX,
    include_raw_html: bool = False,
    use_playwright_fallback: bool = True,
) -> list[dict[str, Any]]:
    """
    Scrape multiple carriers sequentially with a shared requests.Session.

    Parameters
    ----------
    carriers : list of {"value": "123456", "type": "USDOT"} dicts

    Example
    -------
    results = scrape_batch([
        {"value": "1597181", "type": "USDOT"},
        {"value": "MC193369", "type": "MC"},
    ])
    """
    session = _make_session()
    results: list[dict[str, Any]] = []
    total = len(carriers)

    for i, carrier in enumerate(carriers, 1):
        value = carrier.get("value", "").strip()
        stype = carrier.get("type", "USDOT").upper()
        log.info(f"[{i}/{total}] {stype}:{value}")

        res = scrape_carrier(
            value, stype,
            delay_min=delay_min,
            delay_max=delay_max,
            include_raw_html=include_raw_html,
            use_playwright_fallback=use_playwright_fallback,
            _session=session,
        )
        results.append(res)

    session.close()
    return results


# ─────────────────────────────────────────────────────────────────────────────
# Offline helper (no network)
# ─────────────────────────────────────────────────────────────────────────────

def parse_saved_html(html: str) -> dict[str, Any]:
    """Parse a saved FMCSA snapshot HTML file without any network call."""
    return _parse_snapshot_html(html)


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def _cli() -> None:
    p = argparse.ArgumentParser(
        prog="fmcsa_scraper",
        description="Dispatch DOS — FMCSA Carrier Lookup",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python fmcsa_scraper.py --value 1597181   --type USDOT
  python fmcsa_scraper.py --value MC193369  --type MC
  python fmcsa_scraper.py --value 793594    --type USDOT --output result.json
  python fmcsa_scraper.py --parse-html saved_page.html
""",
    )
    p.add_argument("--value",       "-v", default=None)
    p.add_argument("--type",        "-t", default="USDOT", choices=["USDOT","MC","NAME"])
    p.add_argument("--output",      "-o", default=None, metavar="FILE.json")
    p.add_argument("--show-browser",      action="store_true")
    p.add_argument("--delay-min",         type=float, default=DEFAULT_DELAY_MIN)
    p.add_argument("--delay-max",         type=float, default=DEFAULT_DELAY_MAX)
    p.add_argument("--no-html",           action="store_true")
    p.add_argument("--parse-html",        default=None, metavar="FILE.html")
    args = p.parse_args()

    if args.parse_html:
        with open(args.parse_html, encoding="utf-8", errors="replace") as f:
            result = parse_saved_html(f.read())
    elif args.value:
        result = scrape_carrier(
            args.value, args.type,
            headless=not args.show_browser,
            delay_min=args.delay_min,
            delay_max=args.delay_max,
            include_raw_html=not args.no_html,
        )
    else:
        p.print_help(); sys.exit(0)

    print_result = {k: v for k, v in result.items() if k != "raw_html"}
    print(json.dumps(print_result, indent=2, default=str))

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump({k: v for k, v in result.items() if k != "raw_html"},
                      f, indent=2, default=str)
        print(f"\nSaved → {args.output}")


if __name__ == "__main__":
    _cli()
