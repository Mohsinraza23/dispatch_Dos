"""
canada_scraper.py
-----------------
Dispatch DOS -- Canada Carrier Scraper

Strategy:
  1. Search FMCSA SAFER by company name -> filter Country = Canada results
  2. For each Canadian carrier found, fetch full snapshot via existing scraper
  3. Return same field structure as US carriers (compatible with OUTPUT_COLS)

Why FMCSA SAFER?
  Canadian carriers operating in the US must register with FMCSA and get a
  USDOT number. SAFER is the only free, public, scrapable database that covers
  them. Purely domestic Canadian carriers (no US ops) are not indexed here.

Usage:
    from canada_scraper import search_canada_carriers, scrape_canada_batch
    results = search_canada_carriers("maple leaf trucking", max_results=25)
    rows    = scrape_canada_batch(results, delay_min=2, delay_max=5)
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

_SAFER_KEYWORD_URL = "https://safer.fmcsa.dot.gov/keywordx.asp"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://safer.fmcsa.dot.gov/",
}

# Canadian province/territory codes (appear in SAFER "State" column)
_CANADA_PROVINCES = {
    "AB", "BC", "MB", "NB", "NL", "NS", "NT", "NU", "ON",
    "PE", "QC", "SK", "YT",
}

# Country codes SAFER uses for Canada (appear in "Country" column)
_CANADA_COUNTRY_CODES = {"CAN", "CANADA", "CA"}


# -----------------------------------------------------------------------------
# Step 1 -- Company Name Search
# -----------------------------------------------------------------------------

def search_canada_carriers(
    company_name: str,
    max_results: int = 50,
    session: requests.Session | None = None,
) -> list[dict[str, str]]:
    """
    Search FMCSA SAFER for Canadian carriers by company name.

    Returns a list of dicts:
        {dot_number, mc_number, company_name, province, country}

    Only carriers with Country = Canada / Canadian province code are returned.
    """
    company_name = company_name.strip()
    if not company_name:
        return []

    sess = session or requests.Session()
    sess.headers.update(_HEADERS)

    try:
        resp = sess.get(
            _SAFER_KEYWORD_URL,
            params={"searchstring": f"*{company_name.upper()}*", "SEARCHTYPE": ""},
            timeout=20,
        )
        resp.raise_for_status()
    except Exception as exc:
        logger.warning("Canada name search failed: %s", exc)
        return []

    return _parse_keyword_results(resp.text, max_results)


def _parse_keyword_results(html: str, max_results: int) -> list[dict[str, str]]:
    """Parse SAFER keyword search results table, return Canadian carriers only."""
    soup = BeautifulSoup(html, "lxml")
    results: list[dict[str, str]] = []

    for table in soup.find_all("table"):
        headers = [th.get_text(strip=True).lower() for th in table.find_all("th")]
        if not headers or "dot" not in " ".join(headers):
            continue

        # Map column index -> field name
        col_map: dict[int, str] = {}
        for i, h in enumerate(headers):
            if "dot" in h:
                col_map[i] = "dot_number"
            elif "mc" in h or "mx" in h:
                col_map[i] = "mc_number"
            elif "name" in h:
                col_map[i] = "company_name"
            elif "state" in h:
                col_map[i] = "province"
            elif "country" in h:
                col_map[i] = "country"

        if not col_map:
            continue

        rows = table.find_all("tr")[1:]  # skip header
        for row in rows:
            cells = row.find_all("td")
            if not cells:
                continue

            vals: dict[str, str] = {}
            for i, field in col_map.items():
                if i < len(cells):
                    vals[field] = cells[i].get_text(strip=True)

            # Filter: keep only Canadian carriers
            # SAFER "Country" col = "CAN" | "USA" | "MEX"
            # SAFER "State" col   = province code for Canadian carriers
            country  = vals.get("country", "").upper().strip()
            province = vals.get("province", "").upper().strip()

            is_canada = (
                country in _CANADA_COUNTRY_CODES
                or province in _CANADA_PROVINCES
            )

            if not is_canada:
                continue

            # Pull DOT number from anchor tag if present
            dot_idx = next((i for i, f in col_map.items() if f == "dot_number"), None)
            if dot_idx is not None and dot_idx < len(cells):
                link = cells[dot_idx].find("a")
                if link:
                    vals["dot_number"] = link.get_text(strip=True)

            vals.setdefault("country", "Canada")
            results.append(vals)

            if len(results) >= max_results:
                break

        break  # only parse first matching table

    return results


# -----------------------------------------------------------------------------
# Step 2 -- Full Snapshot Scrape
# -----------------------------------------------------------------------------

def scrape_canada_carrier(
    dot_number: str,
    delay_min: float = 2.0,
    delay_max: float = 5.0,
    session: requests.Session | None = None,
) -> dict[str, Any]:
    """
    Scrape full carrier profile for a Canadian carrier using their USDOT number.
    Returns same dict structure as fmcsa_scraper.scrape_carrier().
    Adds carrier_country = "Canada".
    """
    try:
        from fmcsa_scraper import scrape_carrier
    except ImportError:
        from dispatch_skool_scraper.fmcsa_scraper import scrape_carrier

    result = scrape_carrier(
        search_value=dot_number,
        search_type="USDOT",
        delay_min=delay_min,
        delay_max=delay_max,
        _session=session,
    )
    result["carrier_country"] = "Canada"
    return result


def scrape_canada_batch(
    search_results: list[dict[str, str]],
    delay_min: float = 2.0,
    delay_max: float = 5.0,
    stop_event: Any = None,
    progress_callback: Any = None,
) -> list[dict[str, Any]]:
    """
    Batch scrape full profiles for a list of Canadian carriers.

    Parameters
    ----------
    search_results     Output from search_canada_carriers()
    delay_min/max      Random delay between requests (seconds)
    stop_event         threading.Event -- set to stop early
    progress_callback  Callable(current, total, carrier_name) for UI updates

    Returns list of full carrier dicts.
    """
    sess = requests.Session()
    sess.headers.update(_HEADERS)

    rows: list[dict[str, Any]] = []
    total = len(search_results)

    for i, carrier in enumerate(search_results):
        if stop_event and stop_event.is_set():
            break

        dot  = carrier.get("dot_number", "").strip()
        name = carrier.get("company_name", dot)

        if progress_callback:
            progress_callback(i + 1, total, name)

        if not dot:
            rows.append({
                "status": "error",
                "error_detail": "No DOT number in search result",
                "carrier_country": "Canada",
                **carrier,
            })
            continue

        result = scrape_canada_carrier(
            dot_number=dot,
            delay_min=delay_min,
            delay_max=delay_max,
            session=sess,
        )
        rows.append(result)

    return rows


# -----------------------------------------------------------------------------
# Utility -- build flat row compatible with OUTPUT_COLS
# -----------------------------------------------------------------------------

def canada_result_to_row(
    result: dict[str, Any],
    input_name: str,
    scraped_at: str,
) -> dict[str, str]:
    """
    Convert a scrape_canada_carrier() result to a flat OUTPUT_COLS-compatible row.
    Adds Canadian flag emoji to Legal_Name for easy identification.
    """
    status_map = {
        "found":     "found",
        "not_found": "Not_Found",
        "blocked":   "Blocked",
        "error":     "Error",
    }

    raw_status    = result.get("status", "error")
    scrape_status = status_map.get(raw_status, "Error")
    insp          = result.get("inspection_stats", {}) or {}

    legal_name = result.get("legal_name", "")
    if legal_name and not legal_name.startswith("[CA]"):
        legal_name = f"[CA] {legal_name}"

    return {
        "Input_ID":                   input_name,
        "Scrape_Status":              scrape_status,
        "Carrier_Status":             result.get("carrier_status", ""),
        "Fetch_Method":               result.get("fetch_method", ""),
        "Scraped_At":                 scraped_at,
        "Error_Detail":               result.get("error_detail", ""),
        "Legal_Name":                 legal_name,
        "DBA_Name":                   result.get("dba_name", ""),
        "USDOT_Number":               result.get("usdot_number", ""),
        "MC_Number":                  result.get("mc_number", ""),
        "MC_MX_Raw":                  result.get("mc_mx_raw", ""),
        "State_Carrier_ID":           result.get("state_carrier_id", ""),
        "Physical_Address":           result.get("physical_address", ""),
        "Mailing_Address":            result.get("mailing_address", ""),
        "Phone":                      result.get("phone", ""),
        "Entity_Type":                result.get("entity_type", ""),
        "USDOT_Status":               result.get("usdot_status", ""),
        "Operating_Authority_Status": result.get("operating_authority_status", ""),
        "Safety_Rating":              result.get("safety_rating", ""),
        "Safety_Rating_Date":         result.get("safety_rating_date", ""),
        "OOS_Date":                   result.get("oos_date", ""),
        "Power_Units":                result.get("power_units", ""),
        "Drivers":                    result.get("drivers", ""),
        "MCS150_Date":                result.get("mcs150_date", ""),
        "MCS150_Mileage":             result.get("mcs150_mileage", ""),
        "Operation_Classification":   " | ".join(result.get("operation_classification", [])),
        "Carrier_Operation":          " | ".join(result.get("carrier_operation", [])),
        "Cargo_Carried":              " | ".join(result.get("cargo_carried", [])),
        "OOS_Percentage":             result.get("out_of_service_percentage", ""),
        "Vehicle_Inspections":        insp.get("vehicle_inspections", ""),
        "Vehicle_OOS_Count":          insp.get("vehicle_oos_count", ""),
        "Vehicle_OOS_Pct":            insp.get("vehicle_oos_pct", ""),
        "Driver_Inspections":         insp.get("driver_inspections", ""),
        "Driver_OOS_Count":           insp.get("driver_oos_count", ""),
        "Driver_OOS_Pct":             insp.get("driver_oos_pct", ""),
        "Hazmat_Inspections":         insp.get("hazmat_inspections", ""),
        "Hazmat_OOS_Count":           insp.get("hazmat_oos_count", ""),
        "Hazmat_OOS_Pct":             insp.get("hazmat_oos_pct", ""),
        "Fatal_Crashes":              insp.get("crash_fatal", ""),
        "Injury_Crashes":             insp.get("crash_injury", ""),
        "Tow_Crashes":                insp.get("crash_tow", ""),
        "Total_Crashes":              insp.get("crash_total", ""),
    }
