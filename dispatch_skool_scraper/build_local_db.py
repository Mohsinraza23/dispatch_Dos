"""
build_local_db.py
──────────────────
Builds a local SQLite carrier lookup database from FMCSA bulk census files
(carriers.csv.gz, safety.csv.gz — insurance files optional, not used yet).

These CSVs are FMCSA's own public census/safety data (free, no API key,
no geo-restriction). See: https://dotlookup.dev/data or FMCSA's own SMS
bulk-download portal for the source files.

The resulting .db file is intentionally NOT committed to git (see
.gitignore: dispatch_skool_scraper/data/) — it's large (several hundred MB)
and regenerable at any time by re-running this script against a fresh
export. On Streamlit Cloud, where this file won't exist, local_db.py's
lookup silently no-ops and the app falls back to its existing scrape tiers
exactly as before — this script has no effect on production behavior.

Usage:
    python build_local_db.py --input-dir "/path/to/downloaded/csvs"
    python build_local_db.py   # defaults to ~/Downloads
"""

from __future__ import annotations

import argparse
import csv
import gzip
import os
import sqlite3
import sys
import time

DEFAULT_OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "fmcsa_local.db")


def _default_input_dir() -> str:
    # WSL path to the Windows Downloads folder used in this project's dev environment
    wsl_downloads = "/mnt/c/Users/DELL/Downloads"
    if os.path.isdir(wsl_downloads):
        return wsl_downloads
    return os.path.expanduser("~/Downloads")


def _open_csv(path: str):
    return gzip.open(path, "rt", encoding="utf-8", errors="replace", newline="")


def build(input_dir: str, out_path: str) -> None:
    carriers_path = os.path.join(input_dir, "carriers.csv.gz")
    safety_path = os.path.join(input_dir, "safety.csv.gz")

    if not os.path.isfile(carriers_path):
        print(f"ERROR: carriers.csv.gz not found in {input_dir}", file=sys.stderr)
        sys.exit(1)

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    tmp_path = out_path + ".building"
    if os.path.exists(tmp_path):
        os.remove(tmp_path)

    t0 = time.time()
    conn = sqlite3.connect(tmp_path)
    conn.execute("PRAGMA journal_mode=OFF")
    conn.execute("PRAGMA synchronous=OFF")

    conn.execute("""
        CREATE TABLE carriers (
            dot_number TEXT PRIMARY KEY,
            mc_number TEXT,
            legal_name TEXT,
            dba_name TEXT,
            carrier_status TEXT,
            authority_status TEXT,
            phy_street TEXT, phy_city TEXT, phy_state TEXT, phy_zip TEXT,
            mail_street TEXT, mail_city TEXT, mail_state TEXT, mail_zip TEXT,
            telephone TEXT,
            entity_type TEXT,
            dun_bradstreet_no TEXT,
            total_power_units TEXT,
            total_drivers TEXT,
            mcs150_date TEXT,
            mcs150_mileage TEXT,
            operation_class TEXT,
            carrier_operation TEXT,
            cargo_carried TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE safety (
            dot_number TEXT PRIMARY KEY,
            driver_inspections TEXT, driver_oos_inspections TEXT, driver_oos_rate TEXT,
            vehicle_inspections TEXT, vehicle_oos_inspections TEXT, vehicle_oos_rate TEXT,
            hazmat_inspections TEXT, hazmat_oos_inspections TEXT, hazmat_oos_rate TEXT,
            fatal_crashes TEXT, injury_crashes TEXT, towaway_crashes TEXT, total_crashes TEXT
        )
    """)

    print(f"Loading carriers from {carriers_path} ...")
    n = 0
    with _open_csv(carriers_path) as f:
        reader = csv.DictReader(f)
        batch = []
        for row in reader:
            dot = (row.get("dot_number") or "").strip()
            if not dot:
                continue
            batch.append((
                dot,
                (row.get("mc_number") or "").strip(),
                row.get("legal_name") or "",
                row.get("dba_name") or "",
                row.get("carrier_status") or "",
                row.get("authority_status") or "",
                row.get("phy_street") or "", row.get("phy_city") or "",
                row.get("phy_state") or "", row.get("phy_zip") or "",
                row.get("mail_street") or "", row.get("mail_city") or "",
                row.get("mail_state") or "", row.get("mail_zip") or "",
                row.get("telephone") or "",
                row.get("entity_type") or "",
                row.get("dun_bradstreet_no") or "",
                row.get("total_power_units") or "",
                row.get("total_drivers") or "",
                row.get("mcs150_date") or "",
                row.get("mcs150_mileage") or "",
                row.get("operation_class") or "",
                row.get("carrier_operation") or "",
                row.get("cargo_carried") or "",
            ))
            n += 1
            if len(batch) >= 20000:
                conn.executemany(
                    "INSERT OR REPLACE INTO carriers VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    batch,
                )
                batch.clear()
                if n % 200000 == 0:
                    print(f"  {n:,} carriers loaded...")
        if batch:
            conn.executemany(
                "INSERT OR REPLACE INTO carriers VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                batch,
            )
    conn.commit()
    print(f"  Done: {n:,} carriers.")

    if os.path.isfile(safety_path):
        print(f"Loading safety stats from {safety_path} ...")
        m = 0
        with _open_csv(safety_path) as f:
            reader = csv.DictReader(f)
            batch = []
            for row in reader:
                dot = (row.get("dot_number") or "").strip()
                if not dot:
                    continue
                batch.append((
                    dot,
                    row.get("driver_inspections") or "", row.get("driver_oos_inspections") or "",
                    row.get("driver_oos_rate") or "",
                    row.get("vehicle_inspections") or "", row.get("vehicle_oos_inspections") or "",
                    row.get("vehicle_oos_rate") or "",
                    row.get("hazmat_inspections") or "", row.get("hazmat_oos_inspections") or "",
                    row.get("hazmat_oos_rate") or "",
                    row.get("fatal_crashes") or "", row.get("injury_crashes") or "",
                    row.get("towaway_crashes") or "", row.get("total_crashes") or "",
                ))
                m += 1
                if len(batch) >= 20000:
                    conn.executemany(
                        "INSERT OR REPLACE INTO safety VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        batch,
                    )
                    batch.clear()
            if batch:
                conn.executemany(
                    "INSERT OR REPLACE INTO safety VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    batch,
                )
        conn.commit()
        print(f"  Done: {m:,} safety records.")
    else:
        print("  safety.csv.gz not found — skipping (optional).")

    print("Building indexes ...")
    conn.execute("CREATE INDEX idx_carriers_mc ON carriers(mc_number)")
    conn.commit()
    conn.execute("VACUUM")
    conn.close()

    os.replace(tmp_path, out_path)
    size_mb = os.path.getsize(out_path) / (1024 * 1024)
    print(f"\nDone in {time.time()-t0:.1f}s → {out_path} ({size_mb:.1f} MB)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--input-dir", default=None, help="Folder containing carriers.csv.gz / safety.csv.gz")
    ap.add_argument("--out", default=DEFAULT_OUT, help="Output .db path")
    args = ap.parse_args()
    build(args.input_dir or _default_input_dir(), args.out)
