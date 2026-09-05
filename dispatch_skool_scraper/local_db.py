"""
local_db.py
────────────
Optional local-cache lookup tier for fmcsa_scraper.py.

If dispatch_skool_scraper/data/fmcsa_local.db exists (built via
build_local_db.py from FMCSA's own public bulk census/safety files), USDOT
and MC lookups are answered instantly from it instead of hitting
safer.fmcsa.dot.gov — no network round-trip, no blocking risk.

If the file does NOT exist (e.g. on Streamlit Cloud, where the multi-hundred
MB db is intentionally not deployed), every function here is a silent no-op
and fmcsa_scraper.py's existing API → requests → Playwright tiers behave
exactly as they did before this module existed. This module must never raise
past its own boundary and must never be the reason a lookup fails.

Important limitation (by design, see project memory bug #1 — wrong OOS
status from a bad date fallback): the bulk census file only carries FMCSA's
registration status (ACTIVE / INACTIVE), not the safety "Out of Service"
order status. So local hits are only ever tagged ACTIVE or INACTIVE, never
OUT_OF_SERVICE — we do not guess at OOS from data that doesn't contain it.
A caller that specifically needs authoritative OOS status should treat a
local-db "not found" (None) the same as any other tier and fall through to
the live scrape, which this module's caller already does.
"""

from __future__ import annotations

import os
import re
import sqlite3
import threading
from datetime import datetime
from typing import Any

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "fmcsa_local.db")

_local = threading.local()


def _conn() -> sqlite3.Connection | None:
    if not os.path.isfile(DB_PATH):
        return None
    conn = getattr(_local, "conn", None)
    if conn is not None:
        return conn
    try:
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        _local.conn = conn
        return conn
    except Exception:
        return None


def available() -> bool:
    """True if the local database file is present and openable."""
    return _conn() is not None


def _parse_bracket_list(raw: str) -> list[str]:
    """'[General Freight, Beverages]' -> ['General Freight', 'Beverages']"""
    if not raw:
        return []
    inner = raw.strip().strip("[]").strip()
    if not inner:
        return []
    return [p.strip() for p in inner.split(",") if p.strip()]


def _format_phone(raw: str) -> str:
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) == 10:
        return f"({digits[:3]}) {digits[3:6]}-{digits[6:]}"
    return raw or ""


def _parse_operation_class(raw: str) -> list[str]:
    """
    Same as _parse_bracket_list, but re-joins FMCSA's "PRIVATE PASSENGER"
    category with its qualifier. The census file stores this one compound
    category as two comma-separated tokens ("PRIVATE PASSENGER, BUSINESS" /
    "PRIVATE PASSENGER, NON-BUSINESS") indistinguishable from the list's own
    comma delimiter, so a plain split breaks it into two unrelated-looking
    items. No other operation-class value follows this pattern.
    """
    parts = _parse_bracket_list(raw)
    merged: list[str] = []
    i = 0
    while i < len(parts):
        if parts[i] == "PRIVATE PASSENGER" and i + 1 < len(parts) and parts[i + 1] in ("BUSINESS", "NON-BUSINESS"):
            merged.append(f"PRIVATE PASSENGER ({parts[i + 1]})")
            i += 2
        else:
            merged.append(parts[i])
            i += 1
    return merged


def _join_addr(street: str, city: str, state: str, zip_: str) -> str:
    parts = [p for p in (street, city, state, zip_) if p]
    return ", ".join(parts)


def _carrier_status(raw_status: str) -> str:
    s = (raw_status or "").strip().upper()
    if s == "ACTIVE":
        return "ACTIVE"
    if s == "INACTIVE":
        return "INACTIVE"
    return ""  # unknown — let caller fall through to live scrape rather than guess


def _row_to_result(row: sqlite3.Row, search_value: str, search_type: str, safety_row: sqlite3.Row | None) -> dict[str, Any]:
    op_class = _parse_operation_class(row["operation_class"])
    carr_op = _parse_bracket_list(row["carrier_operation"])
    cargo = _parse_bracket_list(row["cargo_carried"])

    inspection_stats: dict[str, str] = {}
    oos_pct = ""
    if safety_row is not None:
        mapping = {
            "driver_inspections": "driver_inspections",
            "driver_oos_inspections": "driver_oos_count",
            "driver_oos_rate": "driver_oos_pct",
            "vehicle_inspections": "vehicle_inspections",
            "vehicle_oos_inspections": "vehicle_oos_count",
            "vehicle_oos_rate": "vehicle_oos_pct",
            "hazmat_inspections": "hazmat_inspections",
            "hazmat_oos_inspections": "hazmat_oos_count",
            "hazmat_oos_rate": "hazmat_oos_pct",
            "fatal_crashes": "crash_fatal",
            "injury_crashes": "crash_injury",
            "towaway_crashes": "crash_tow",
            "total_crashes": "crash_total",
        }
        for src_key, dst_key in mapping.items():
            val = safety_row[src_key]
            if val not in (None, ""):
                inspection_stats[dst_key] = val
        oos_pct = inspection_stats.get("vehicle_oos_pct") or inspection_stats.get("driver_oos_pct") or ""

    return {
        "status": "found",
        "fetch_method": "local_db",
        "search_value": search_value,
        "search_type": search_type,
        "scraped_at": datetime.utcnow().isoformat() + "Z",
        "error_detail": "",
        "legal_name": row["legal_name"] or "",
        "dba_name": row["dba_name"] or "",
        "usdot_number": row["dot_number"] or "",
        "mc_number": row["mc_number"] or "",
        "mc_mx_raw": f"MC-{row['mc_number']}" if row["mc_number"] else "",
        "state_carrier_id": "",
        "duns_number": row["dun_bradstreet_no"] or "",
        "physical_address": _join_addr(row["phy_street"], row["phy_city"], row["phy_state"], row["phy_zip"]),
        "mailing_address": _join_addr(row["mail_street"], row["mail_city"], row["mail_state"], row["mail_zip"]),
        "phone": _format_phone(row["telephone"]),
        "entity_type": row["entity_type"] or "",
        "usdot_status": row["authority_status"] or "",
        "operating_authority_status": row["authority_status"] or "",
        "safety_rating": "",
        "safety_rating_date": "",
        "review_date": "",
        "oos_date": "",  # not present in census file — never guessed, see module docstring
        "power_units": row["total_power_units"] or "",
        "drivers": row["total_drivers"] or "",
        "mcs150_date": row["mcs150_date"] or "",
        "mcs150_mileage": row["mcs150_mileage"] or "",
        "operation_classification": op_class,
        "carrier_operation": carr_op,
        "cargo_carried": cargo,
        "out_of_service_percentage": oos_pct,
        "inspection_stats": inspection_stats,
        "carrier_status": _carrier_status(row["carrier_status"]),
        "raw_html": "",
    }


def lookup_local(search_value: str, search_type: str) -> dict[str, Any] | None:
    """
    Look up a carrier in the local cache DB.

    Returns a fully-shaped result dict (same keys as fmcsa_scraper.scrape_carrier)
    on a hit, or None if there's no DB, no match, or the carrier_status couldn't
    be confidently classified — in every None case the caller should fall
    through to the live scrape tiers exactly as if this module didn't exist.
    """
    conn = _conn()
    if conn is None:
        return None
    search_type = (search_type or "").upper().strip()
    value = re.sub(r"\D", "", search_value or "")
    if not value:
        return None

    try:
        if search_type == "USDOT":
            cur = conn.execute("SELECT * FROM carriers WHERE dot_number = ?", (value,))
        elif search_type == "MC":
            cur = conn.execute("SELECT * FROM carriers WHERE mc_number = ?", (value,))
        else:
            return None  # NAME search not supported by local cache
        row = cur.fetchone()
        if row is None:
            return None

        status = _carrier_status(row["carrier_status"])
        if not status:
            return None  # unrecognized status — don't risk a wrong answer, fall through

        safety_cur = conn.execute("SELECT * FROM safety WHERE dot_number = ?", (row["dot_number"],))
        safety_row = safety_cur.fetchone()

        return _row_to_result(row, search_value, search_type, safety_row)
    except Exception:
        return None
