"""
fmcsa_playwright_scraper.py
============================
Standalone Playwright scraper for FMCSA Company Snapshot data.
Target: https://safer.fmcsa.dot.gov/query.asp

Install:
    pip install playwright
    playwright install chromium

Usage:
    python fmcsa_playwright_scraper.py                  # runs built-in demo
    python fmcsa_playwright_scraper.py --csv out.csv    # saves to CSV
"""

from __future__ import annotations

import csv
import re
import time
import random
import argparse
from datetime import datetime
from typing import Any

from playwright.sync_api import sync_playwright, Page, TimeoutError as PWTimeout


# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

FMCSA_BASE = "https://safer.fmcsa.dot.gov/query.asp"

# Realistic browser user-agents (rotated per request)
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36 Edg/123.0.0.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
]

# Text patterns that mean the carrier was not found
NOT_FOUND_PATTERNS = [
    "no records matching",
    "record not found",
    "the record matching",
    "could not be found",
    "0 records found",
    "no information available",
]

# All field labels as they appear on the FMCSA page
# Format: our_key -> label text inside <th>
FIELD_LABELS = {
    "usdot_number":               "USDOT Number:",
    "legal_name":                 "Legal Name:",
    "dba_name":                   "DBA Name:",
    "physical_address":           "Physical Address:",
    "mailing_address":            "Mailing Address:",
    "phone":                      "Phone:",
    "entity_type":                "Entity Type:",
    "operating_authority_status": "Operating Authority Status:",
    "safety_rating":              "Safety Rating:",
    "safety_rating_date":         "Rating Date:",
    "oos_date":                   "Out of Service Date:",
    "power_units":                "Power Units:",
    "drivers":                    "Drivers:",
    "mcs150_date":                "MCS-150 Form Date:",
    "mcs150_mileage":             "MCS-150 Mileage (Year):",
    "state_carrier_id":           "State Carrier ID Number:",
    "mc_number":                  "MC/MX/FF Number(s):",
    "duns_number":                "DUNS Number:",
    "review_date":                "Review Date:",
}

# Table sections identified by their summary attribute
TABLE_OP_CLASS   = "Operation Classification"
TABLE_CARGO      = "Cargo Carried"
TABLE_CARRIER_OP = "Carrier Operation"


# ─────────────────────────────────────────────────────────────────────────────
# HTML parsing helpers  (work on the page's DOM via Playwright)
# ─────────────────────────────────────────────────────────────────────────────

def _clean(text: str | None) -> str:
    """Collapse whitespace, strip leading/trailing spaces."""
    if not text:
        return ""
    return re.sub(r"[\s\r\n]+", " ", str(text)).strip()


def _extract_field(page: Page, label: str) -> str:
    """
    Find a field value by matching the <th> label text, then reading the
    adjacent <td> in the same <tr>.

    FMCSA page structure:
        <tr>
            <th>Legal Name:</th>
            <td>ACME TRUCKING INC</td>
        </tr>
    """
    try:
        # Use XPath: find <th> whose text contains the label, go to parent <tr>, get first <td>
        label_bare = label.rstrip(":").strip()
        xpath = (
            f"//th[normalize-space(.)='{label}']"
            f"/parent::tr/td[1]"
            f" | "
            f"//th[contains(normalize-space(.),'{label_bare}')]"
            f"/parent::tr/td[1]"
        )
        td = page.locator(xpath).first
        if td.count() == 0:
            return ""
        return _clean(td.inner_text(timeout=3000))
    except Exception:
        return ""


def _extract_table_items(page: Page, summary: str) -> list[str]:
    """
    Extract checked items from a named checkbox-style table section.
    FMCSA uses <table summary="Cargo Carried"> etc. for lists.
    """
    try:
        rows = page.locator(f'table[summary="{summary}"] td').all()
        items: list[str] = []
        seen: set[str] = set()
        for cell in rows:
            text = _clean(cell.inner_text(timeout=2000))
            # Skip short cells, lone digits, symbols
            if len(text) > 2 and not re.fullmatch(r"[\d\W]+", text) and text not in seen:
                seen.add(text)
                items.append(text)
        return items
    except Exception:
        return []


def _derive_status(fields: dict[str, str]) -> str:
    """
    Derive ACTIVE / INACTIVE / OUT_OF_SERVICE from scraped fields.
    Priority: OOS date present → OUT_OF_SERVICE
              auth status inactive/revoked → INACTIVE
              auth status says active → ACTIVE
              default → INACTIVE
    """
    oos = fields.get("oos_date", "").lower().strip()
    if oos and oos not in ("", "none", "n/a", "-"):
        return "OUT_OF_SERVICE"

    auth = (fields.get("operating_authority_status", "") + " " +
            fields.get("entity_type", "")).lower()

    if any(s in auth for s in ("inactive", "revoked", "cancelled", "not authorized")):
        return "INACTIVE"
    if any(s in auth for s in ("authorized for hire", "active", "authorized",
                                "common", "contract")):
        return "ACTIVE"
    return "INACTIVE"


# ─────────────────────────────────────────────────────────────────────────────
# Core scrape function
# ─────────────────────────────────────────────────────────────────────────────

def scrape_usdot(page: Page, usdot: str, delay_min: float = 4.0,
                 delay_max: float = 9.0) -> dict[str, Any]:
    """
    Scrape one carrier by USDOT number.

    Parameters
    ----------
    page       : Playwright Page object (reused across calls)
    usdot      : USDOT number (digits only, or prefixed — prefix stripped)
    delay_min  : min seconds to wait after page load
    delay_max  : max seconds to wait after page load

    Returns
    -------
    dict with keys: status, usdot_input, legal_name, dba_name,
    physical_address, mailing_address, phone, power_units, drivers,
    operation_classification, carrier_operation, cargo_carried,
    safety_rating, oos_date, entity_type, mc_number,
    carrier_status, scraped_at, error_detail
    """
    # Clean the input — strip any MC/DOT prefix
    usdot_clean = re.sub(r"^(USDOT|DOT)\s*[#\-\s]?", "", str(usdot),
                         flags=re.IGNORECASE).strip()

    result: dict[str, Any] = {
        "status":                    "error",
        "usdot_input":               usdot,
        "usdot_number":              "",
        "legal_name":                "",
        "dba_name":                  "",
        "mc_number":                 "",
        "physical_address":          "",
        "mailing_address":           "",
        "phone":                     "",
        "entity_type":               "",
        "operating_authority_status": "",
        "safety_rating":             "",
        "safety_rating_date":        "",
        "oos_date":                  "",
        "power_units":               "",
        "drivers":                   "",
        "mcs150_date":               "",
        "mcs150_mileage":            "",
        "operation_classification":  [],
        "carrier_operation":         [],
        "cargo_carried":             [],
        "carrier_status":            "",
        "scraped_at":                datetime.utcnow().isoformat() + "Z",
        "error_detail":              "",
    }

    # Build the query URL
    url = (
        f"{FMCSA_BASE}"
        f"?searchType=ANY"
        f"&query_type=queryCarrierSnapshot"
        f"&query_param=USDOT"
        f"&query_string={usdot_clean}"
    )

    try:
        # Navigate — wait for network to settle
        page.goto(url, wait_until="domcontentloaded", timeout=45_000)
        try:
            page.wait_for_load_state("networkidle", timeout=12_000)
        except PWTimeout:
            pass  # Old ASP pages sometimes never fire networkidle — that's fine

        # Get full page text for quick checks
        page_text = page.inner_text("body") or ""
        page_lower = page_text.lower()

        # ── Check: Not Found ─────────────────────────────────────────────────
        if any(p in page_lower for p in NOT_FOUND_PATTERNS):
            result["status"] = "not_found"
            result["error_detail"] = "Carrier not found in FMCSA database"
            time.sleep(random.uniform(delay_min, delay_max))
            return result

        # ── Check: Blocked / CAPTCHA ─────────────────────────────────────────
        blocked_signals = ["captcha", "access denied", "403 forbidden",
                           "robot check", "unusual traffic"]
        if any(s in page_lower for s in blocked_signals):
            result["status"] = "blocked"
            result["error_detail"] = "Page blocked / CAPTCHA detected"
            time.sleep(random.uniform(delay_min, delay_max))
            return result

        # ── Extract single-value fields ───────────────────────────────────────
        for key, label in FIELD_LABELS.items():
            result[key] = _extract_field(page, label)

        # If key identity fields are empty, page had no real data
        if not result.get("legal_name") and not result.get("usdot_number"):
            result["status"] = "not_found"
            result["error_detail"] = "No carrier data found on page"
            time.sleep(random.uniform(delay_min, delay_max))
            return result

        # ── Extract list/table sections ───────────────────────────────────────
        result["operation_classification"] = _extract_table_items(page, TABLE_OP_CLASS)
        result["carrier_operation"]        = _extract_table_items(page, TABLE_CARRIER_OP)
        result["cargo_carried"]            = _extract_table_items(page, TABLE_CARGO)

        # ── Clean MC number (strip "MC-" prefix) ──────────────────────────────
        mc_raw = result.get("mc_number", "")
        mc_clean = re.sub(r"^MC[- ]?", "", mc_raw, flags=re.IGNORECASE).strip()
        result["mc_number"] = mc_clean.split(",")[0].split(";")[0].strip()

        # ── Derive overall carrier status ──────────────────────────────────────
        result["carrier_status"] = _derive_status(result)
        result["status"] = "found"

    except PWTimeout:
        result["status"] = "error"
        result["error_detail"] = f"Page timed out for USDOT {usdot_clean}"
    except Exception as exc:
        result["status"] = "error"
        result["error_detail"] = str(exc)

    # Human-like delay before next request
    time.sleep(random.uniform(delay_min, delay_max))
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Retry wrapper
# ─────────────────────────────────────────────────────────────────────────────

def scrape_usdot_with_retry(page: Page, usdot: str, max_retries: int = 2,
                             delay_min: float = 4.0,
                             delay_max: float = 9.0) -> dict[str, Any]:
    """
    Scrape one USDOT number with automatic retries on error.
    not_found and blocked are NOT retried (definitive answers).
    """
    result: dict[str, Any] = {}

    for attempt in range(1, max_retries + 2):   # attempts: 1, 2, 3 (if max_retries=2)
        result = scrape_usdot(page, usdot, delay_min=delay_min, delay_max=delay_max)

        if result["status"] in ("found", "not_found", "blocked"):
            # Definitive answer — no point retrying
            break

        if attempt <= max_retries:
            # Transient error — wait longer before retry
            backoff = random.uniform(8, 15) * attempt
            print(f"  ⚠ Attempt {attempt} failed ({result['error_detail'][:60]}) "
                  f"— retrying in {backoff:.0f}s …")
            time.sleep(backoff)

    result["attempts"] = attempt  # how many tries it took
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Batch scraper
# ─────────────────────────────────────────────────────────────────────────────

def scrape_batch(
    usdot_list: list[str],
    csv_path:   str | None = None,
    headless:   bool = True,
    delay_min:  float = 4.0,
    delay_max:  float = 9.0,
    max_retries: int  = 2,
) -> list[dict[str, Any]]:
    """
    Scrape multiple USDOT numbers sequentially using a single browser session.

    Parameters
    ----------
    usdot_list  : list of USDOT number strings
    csv_path    : if given, results are saved to this CSV file
    headless    : True = invisible browser, False = visible (for debugging)
    delay_min   : min seconds between requests
    delay_max   : max seconds between requests
    max_retries : number of retries per carrier on transient errors

    Returns
    -------
    list of result dicts, one per USDOT number
    """
    results: list[dict[str, Any]] = []
    total = len(usdot_list)

    with sync_playwright() as pw:
        # Launch Chromium browser
        browser = pw.chromium.launch(
            headless=headless,
            args=[
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--disable-infobars",
            ],
        )

        # Create a browser context with realistic settings
        ctx = browser.new_context(
            user_agent=random.choice(USER_AGENTS),
            viewport={"width": random.randint(1280, 1440),
                      "height": random.randint(768, 900)},
            locale="en-US",
            timezone_id="America/Chicago",
            extra_http_headers={
                "Accept-Language":           "en-US,en;q=0.9",
                "Referer":                   "https://safer.fmcsa.dot.gov/",
                "Upgrade-Insecure-Requests": "1",
            },
        )

        # Mask webdriver fingerprint
        ctx.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            Object.defineProperty(navigator, 'plugins',   {get: () => [1,2,3,4,5]});
            Object.defineProperty(navigator, 'languages', {get: () => ['en-US','en']});
            window.chrome = { runtime: {} };
        """)

        page = ctx.new_page()

        # Warm up: visit the FMCSA homepage first to get real session cookies
        print("  Warming up session …")
        try:
            page.goto("https://safer.fmcsa.dot.gov/CompanySnapshot.aspx",
                      wait_until="domcontentloaded", timeout=30_000)
            time.sleep(random.uniform(2, 4))
        except Exception:
            pass   # non-fatal

        # Scrape each number
        for idx, usdot in enumerate(usdot_list, 1):
            usdot = str(usdot).strip()
            if not usdot:
                continue

            print(f"[{idx}/{total}] Scraping USDOT: {usdot} …", end=" ", flush=True)

            res = scrape_usdot_with_retry(
                page, usdot,
                max_retries=max_retries,
                delay_min=delay_min,
                delay_max=delay_max,
            )
            results.append(res)

            # Print one-line summary
            status = res["status"]
            if status == "found":
                name = res.get("legal_name") or "(no name)"
                cs   = res.get("carrier_status", "")
                print(f"✓  {name}  [{cs}]")
            elif status == "not_found":
                print("✗  Not found")
            elif status == "blocked":
                print("⊘  Blocked")
            else:
                print(f"✗  Error: {res.get('error_detail','')[:60]}")

        browser.close()

    # Save to CSV if requested
    if csv_path and results:
        _save_csv(results, csv_path)
        print(f"\n✅  Saved {len(results)} records → {csv_path}")

    return results


# ─────────────────────────────────────────────────────────────────────────────
# CSV export
# ─────────────────────────────────────────────────────────────────────────────

# Flat CSV column order
CSV_COLUMNS = [
    "usdot_input", "status", "carrier_status", "attempts", "scraped_at",
    "usdot_number", "legal_name", "dba_name", "mc_number",
    "physical_address", "mailing_address", "phone",
    "entity_type", "operating_authority_status",
    "safety_rating", "safety_rating_date", "oos_date",
    "power_units", "drivers", "mcs150_date", "mcs150_mileage",
    "operation_classification", "carrier_operation", "cargo_carried",
    "error_detail",
]


def _save_csv(results: list[dict[str, Any]], path: str) -> None:
    """Write results list to a CSV file."""
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for row in results:
            flat = dict(row)
            # Convert lists to pipe-separated strings for CSV
            for key in ("operation_classification", "carrier_operation", "cargo_carried"):
                val = flat.get(key, [])
                flat[key] = " | ".join(val) if isinstance(val, list) else str(val)
            writer.writerow(flat)


# ─────────────────────────────────────────────────────────────────────────────
# CLI / demo
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="FMCSA Carrier Snapshot Scraper (Playwright)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python fmcsa_playwright_scraper.py
  python fmcsa_playwright_scraper.py --usdot 1597181 793594
  python fmcsa_playwright_scraper.py --usdot 1597181 --csv results.csv
  python fmcsa_playwright_scraper.py --usdot 1597181 --visible
""",
    )
    parser.add_argument("--usdot",    nargs="+", default=["1597181", "793594"],
                        help="USDOT number(s) to scrape (space-separated)")
    parser.add_argument("--csv",      default=None, metavar="FILE",
                        help="Save results to this CSV file")
    parser.add_argument("--visible",  action="store_true",
                        help="Show browser window (useful for debugging)")
    parser.add_argument("--delay-min", type=float, default=4.0,
                        help="Min seconds between requests (default: 4)")
    parser.add_argument("--delay-max", type=float, default=9.0,
                        help="Max seconds between requests (default: 9)")
    parser.add_argument("--retries",  type=int, default=2,
                        help="Max retries per carrier on error (default: 2)")
    args = parser.parse_args()

    print(f"\n🚛  FMCSA Playwright Scraper")
    print(f"   Numbers  : {args.usdot}")
    print(f"   Headless : {not args.visible}")
    print(f"   Delay    : {args.delay_min}–{args.delay_max}s")
    print(f"   Retries  : {args.retries}")
    print(f"   Output   : {args.csv or '(console only)'}")
    print()

    results = scrape_batch(
        usdot_list  = args.usdot,
        csv_path    = args.csv,
        headless    = not args.visible,
        delay_min   = args.delay_min,
        delay_max   = args.delay_max,
        max_retries = args.retries,
    )

    # Summary
    found     = sum(1 for r in results if r["status"] == "found")
    not_found = sum(1 for r in results if r["status"] == "not_found")
    errors    = sum(1 for r in results if r["status"] not in ("found", "not_found"))

    print(f"\n── Summary ──────────────────────────────")
    print(f"   Total    : {len(results)}")
    print(f"   Found    : {found}")
    print(f"   Not Found: {not_found}")
    print(f"   Errors   : {errors}")

    if not args.csv and results:
        print("\n── First result (dict) ──────────────────")
        r = results[0]
        for k, v in r.items():
            if v:
                print(f"   {k:35} {v}")


if __name__ == "__main__":
    main()
