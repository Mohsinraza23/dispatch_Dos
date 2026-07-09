"""
app.py
──────
Dispatch DOS — FMCSA Bulk Scraper  ·  Streamlit Web App

Run:
    streamlit run app.py

Requires (same folder):
    fmcsa_scraper.py

Install:
    pip install streamlit pandas openpyxl requests beautifulsoup4 lxml
    pip install playwright && playwright install chromium
"""

from __future__ import annotations

# ── stdlib ────────────────────────────────────────────────────────────────────
import io
import os
import re
import json
import time
import queue
import subprocess
import threading
import random
import concurrent.futures
from datetime import datetime
from typing import Any

# ── third-party ───────────────────────────────────────────────────────────────
import pandas as pd
import streamlit as st
import plotly.express as px

# ── local ─────────────────────────────────────────────────────────────────────
from fmcsa_scraper import (
    scrape_carrier,
    _make_session,
    DEFAULT_DELAY_MIN,
    DEFAULT_DELAY_MAX,
)
from ai_tab import render_ai_tab, render_ai_sidebar

_FMCSA_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def _warm_session(session: Any) -> None:
    """Visit FMCSA homepage first to get real session cookies (avoids bot blocking)."""
    try:
        session.get(
            "https://safer.fmcsa.dot.gov/CompanySnapshot.aspx",
            headers={
                "User-Agent": _FMCSA_UA,
                "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
                "Connection": "keep-alive",
                "Upgrade-Insecure-Requests": "1",
            },
            timeout=15,
            allow_redirects=True,
        )
    except Exception:
        pass   # warmup failure is non-fatal


# ─────────────────────────────────────────────────────────────────────────────
# Page config  ← must be FIRST Streamlit call
# ─────────────────────────────────────────────────────────────────────────────

st.set_page_config(
    page_title="Dispatch DOS · FMCSA Scraper",
    page_icon="🚛",
    layout="wide",
    initial_sidebar_state="expanded",
)


# ── Auto-install Playwright Chromium (needed on Streamlit Cloud) ──────────────
@st.cache_resource(show_spinner=False)
def _install_playwright() -> str:
    import sys
    try:
        # Install playwright package at runtime (not in requirements.txt due to
        # greenlet build failures on Python 3.14 with older playwright versions)
        pip_r = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--quiet", "playwright>=1.49.0"],
            capture_output=True, text=True, timeout=180
        )
        if pip_r.returncode != 0:
            return f"pip install failed: {pip_r.stderr[:200]}"
        # Use python -m playwright to avoid PATH issues with newly installed CLI
        # Note: system deps (libnss3 etc.) are pre-installed via packages.txt
        # so --with-deps is NOT used here (would fail on Streamlit Cloud)
        r = subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chromium"],
            capture_output=True, text=True, timeout=300
        )
        return "ok" if r.returncode == 0 else r.stderr[:200]
    except Exception as e:
        return str(e)

_install_playwright()


# ─────────────────────────────────────────────────────────────────────────────
# CSS
# ─────────────────────────────────────────────────────────────────────────────

st.markdown("""
<style>
/* ══════════════════════════════════════════════════════
   FONTS — Google Fonts via import
══════════════════════════════════════════════════════ */
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800&family=JetBrains+Mono:wght@400;500&display=swap');

/* ══════════════════════════════════════════════════════
   BASE — Light Theme
══════════════════════════════════════════════════════ */
html, body, [class*="css"] {
    font-family: 'Inter', 'Segoe UI', system-ui, sans-serif !important;
    letter-spacing: -0.01em;
    color: #0f172a !important;
}
.stApp { background: #f8fafc !important; }
/* Force main content to start after 64px sidebar */
section[data-testid="stMain"] {
    margin-left: 64px !important;
    padding-left: 0 !important;
}
.block-container {
    padding-top: 1.5rem !important;
    padding-left: 2rem !important;
    padding-right: 2rem !important;
    max-width: 1280px;
}

/* ══════════════════════════════════════════════════════
   SIDEBAR — dark stays (contrast), hover to open
══════════════════════════════════════════════════════ */
section[data-testid="stSidebar"] {
    background: linear-gradient(160deg, #0f172a 0%, #1e293b 60%, #0f172a 100%) !important;
    border-right: 1px solid rgba(59,130,246,0.2) !important;
    width: 64px !important;
    min-width: 64px !important;
    overflow: hidden !important;
    transition: width 0.38s cubic-bezier(0.4,0,0.2,1),
                min-width 0.38s cubic-bezier(0.4,0,0.2,1),
                box-shadow 0.38s ease;
    z-index: 999;
}
section[data-testid="stSidebar"]:hover {
    width: 320px !important;
    min-width: 320px !important;
    box-shadow: 6px 0 32px rgba(0,0,0,0.2), 2px 0 0 rgba(59,130,246,0.15);
}
section[data-testid="stSidebar"] > div:first-child {
    width: 320px !important;
    min-width: 320px !important;
    overflow-y: auto !important;
    overflow-x: hidden !important;
    position: absolute !important;
    left: 0 !important;
    top: 0 !important;
    bottom: 0 !important;
}
[data-testid="collapsedControl"] { display: none !important; }
[data-testid="stSidebar"] * { color: #cbd5e1 !important; text-align: left !important; }
[data-testid="stSidebarContent"] { padding: 0.5rem 1rem !important; }
[data-testid="stSidebar"] hr { border-color: rgba(59,130,246,0.15) !important; }
section[data-testid="stSidebar"] ::-webkit-scrollbar { width: 3px; }
section[data-testid="stSidebar"] ::-webkit-scrollbar-thumb { background: #334155; border-radius: 99px; }
section[data-testid="stSidebar"]::after {
    content: "›";
    position: absolute; right: 10px; top: 50%;
    transform: translateY(-50%);
    color: #3b82f6; font-size: 1.5rem; font-weight: 300;
    transition: opacity 0.25s ease; pointer-events: none;
}
section[data-testid="stSidebar"]:hover::after { opacity: 0; }

.sb-lbl {
    font-size: .65rem; font-weight: 700; letter-spacing: 1.8px;
    text-transform: uppercase; color: #60a5fa !important;
    margin: 18px 0 8px; padding-left: 2px;
}

/* ══════════════════════════════════════════════════════
   HEADER BANNER
══════════════════════════════════════════════════════ */
.ds-header {
    background: linear-gradient(135deg, #1d4ed8 0%, #2563eb 40%, #1e40af 100%);
    border: none;
    border-radius: 18px;
    padding: 26px 32px;
    margin-bottom: 28px;
    display: flex; align-items: center; gap: 18px; flex-wrap: wrap;
    box-shadow: 0 4px 24px rgba(37,99,235,0.3), 0 1px 0 rgba(255,255,255,0.1) inset;
    position: relative; overflow: hidden;
}
.ds-header::before {
    content: "";
    position: absolute; top: -80px; right: -40px;
    width: 280px; height: 280px;
    background: radial-gradient(circle, rgba(255,255,255,0.1) 0%, transparent 65%);
    pointer-events: none;
}
.ds-header h1 {
    color: #ffffff; font-size: 1.75rem; font-weight: 800;
    margin: 0; letter-spacing: -0.03em; line-height: 1.1;
    text-shadow: 0 1px 2px rgba(0,0,0,0.15);
}
.ds-header p { color: rgba(255,255,255,0.75); margin: 5px 0 0; font-size: .88rem; }
.ds-badge {
    background: rgba(255,255,255,0.2);
    color: #fff !important;
    font-size: .62rem; font-weight: 700; padding: 4px 14px;
    border-radius: 99px; letter-spacing: 1px; text-transform: uppercase;
    white-space: nowrap; border: 1px solid rgba(255,255,255,0.3);
    backdrop-filter: blur(4px);
}

/* ══════════════════════════════════════════════════════
   SECTION HEADERS
══════════════════════════════════════════════════════ */
.sec-head {
    font-size: 1rem; font-weight: 700; color: #1e40af;
    padding: 10px 14px;
    background: linear-gradient(90deg, rgba(37,99,235,0.08) 0%, transparent 100%);
    border-left: 3px solid #2563eb;
    border-radius: 0 8px 8px 0;
    margin: 6px 0 20px;
    display: flex; align-items: center; gap: 8px;
}

/* ══════════════════════════════════════════════════════
   METRIC CARDS
══════════════════════════════════════════════════════ */
.mc-row { display: flex; gap: 12px; flex-wrap: wrap; margin-bottom: 20px; }
.mc-card {
    flex: 1; min-width: 110px;
    background: #ffffff;
    border: 1px solid #e2e8f0;
    border-radius: 14px; padding: 16px 14px; text-align: center;
    transition: transform 0.2s ease, box-shadow 0.2s ease, border-color 0.2s ease;
    box-shadow: 0 1px 4px rgba(0,0,0,0.06);
}
.mc-card:hover {
    transform: translateY(-2px);
    box-shadow: 0 8px 24px rgba(0,0,0,0.1);
    border-color: #bfdbfe;
}
.mc-card .val { font-size: 2rem; font-weight: 800; line-height: 1; letter-spacing: -0.03em; }
.mc-card .lbl {
    font-size: .62rem; color: #94a3b8; text-transform: uppercase;
    letter-spacing: 1px; margin-top: 6px; font-weight: 600;
}
.c-blue   { color: #2563eb; }
.c-green  { color: #059669; }
.c-yellow { color: #d97706; }
.c-red    { color: #dc2626; }
.c-purple { color: #7c3aed; }
.c-slate  { color: #64748b; }

/* ══════════════════════════════════════════════════════
   LOG WINDOW  (keep dark — easier to read logs)
══════════════════════════════════════════════════════ */
.log-box {
    background: #0f172a;
    border: 1px solid #1e293b;
    border-radius: 12px; padding: 14px 16px; height: 260px;
    overflow-y: auto;
    font-family: 'JetBrains Mono', 'Cascadia Code', monospace !important;
    font-size: .75rem; line-height: 1.7;
    box-shadow: 0 2px 12px rgba(0,0,0,0.08);
}
.log-box::-webkit-scrollbar { width: 4px; }
.log-box::-webkit-scrollbar-thumb { background: #334155; border-radius: 99px; }
.li { color: #60a5fa; } .ls { color: #34d399; }
.lw { color: #fbbf24; } .le { color: #f87171; }
.ld { color: #334155; } .lt { color: #475569; margin-right: 8px; font-size: .68rem; }

/* ══════════════════════════════════════════════════════
   INFO / WARN BOXES
══════════════════════════════════════════════════════ */
.info-box {
    background: #eff6ff; border-left: 3px solid #2563eb;
    border-radius: 0 10px 10px 0; padding: 11px 16px;
    font-size: .83rem; color: #1d4ed8; margin: 10px 0; line-height: 1.6;
}
.warn-box {
    background: #fffbeb; border-left: 3px solid #f59e0b;
    border-radius: 0 10px 10px 0; padding: 11px 16px;
    font-size: .83rem; color: #92400e; margin: 10px 0; line-height: 1.6;
}

/* ══════════════════════════════════════════════════════
   DIVIDER
══════════════════════════════════════════════════════ */
.div { border: none; border-top: 1px solid #e2e8f0; margin: 28px 0; }

/* ══════════════════════════════════════════════════════
   BUTTONS
══════════════════════════════════════════════════════ */
.stButton > button {
    border-radius: 10px !important; font-weight: 600 !important;
    font-family: 'Inter', sans-serif !important;
    transition: all 0.2s ease !important; letter-spacing: -0.01em !important;
}
.stButton > button[kind="primary"] {
    background: linear-gradient(135deg, #1d4ed8, #2563eb) !important;
    color: #fff !important; border: none !important;
    box-shadow: 0 4px 14px rgba(37,99,235,0.35) !important;
}
.stButton > button[kind="primary"]:hover {
    transform: translateY(-1px) !important;
    box-shadow: 0 6px 20px rgba(37,99,235,0.5) !important;
}
.stButton > button[kind="secondary"] {
    border-color: #cbd5e1 !important;
    background: #ffffff !important; color: #374151 !important;
}
.stButton > button[kind="secondary"]:hover {
    border-color: #2563eb !important;
    background: #eff6ff !important; color: #1d4ed8 !important;
}
.stDownloadButton > button {
    border-radius: 10px !important; font-weight: 600 !important;
    background: linear-gradient(135deg, #059669, #10b981) !important;
    color: #fff !important; border: none !important;
    box-shadow: 0 4px 14px rgba(16,185,129,0.3) !important;
    transition: all 0.2s ease !important;
}
.stDownloadButton > button:hover {
    transform: translateY(-1px) !important;
    box-shadow: 0 6px 20px rgba(16,185,129,0.45) !important;
}

/* ══════════════════════════════════════════════════════
   TABS
══════════════════════════════════════════════════════ */
/* Tab container — full width, dark pill bar */
div[data-testid="stTabs"] {
    margin-top: 1.5rem !important;
    margin-bottom: 20px !important;
}
.stTabs [data-baseweb="tab-list"] {
    gap: 6px !important;
    background: #0f172a !important;
    padding: 6px 8px !important;
    border-radius: 14px !important;
    border: none !important;
    box-shadow: 0 4px 16px rgba(0,0,0,0.18) !important;
    width: fit-content !important;
}
.stTabs [data-baseweb="tab"] {
    border-radius: 10px !important;
    padding: 10px 26px !important;
    font-weight: 700 !important;
    font-size: 1rem !important;
    color: #94a3b8 !important;
    transition: all 0.2s ease !important;
    border: none !important;
    background: transparent !important;
    letter-spacing: -0.01em !important;
}
.stTabs [data-baseweb="tab"]:hover {
    color: #ffffff !important;
    background: rgba(255,255,255,0.08) !important;
}
.stTabs [aria-selected="true"] {
    background: #2563eb !important;
    color: #ffffff !important;
    box-shadow: 0 2px 12px rgba(37,99,235,0.4) !important;
}
/* Hide the bottom underline Streamlit adds */
.stTabs [data-baseweb="tab-highlight"] { display: none !important; }
.stTabs [data-baseweb="tab-border"]    { display: none !important; }

/* ══════════════════════════════════════════════════════
   EXPANDER
══════════════════════════════════════════════════════ */
details {
    border: 1px solid #e2e8f0 !important;
    border-radius: 12px !important;
    background: #ffffff !important;
    margin-bottom: 8px !important;
    box-shadow: 0 1px 4px rgba(0,0,0,0.04) !important;
    transition: border-color 0.2s ease, box-shadow 0.2s ease;
}
details:hover {
    border-color: #bfdbfe !important;
    box-shadow: 0 4px 12px rgba(37,99,235,0.1) !important;
}
details > summary {
    padding: 12px 16px !important; font-weight: 600 !important;
    font-size: .9rem !important; color: #1e293b !important;
    cursor: pointer !important; user-select: none !important;
    border-radius: 12px !important; transition: color 0.2s ease !important;
}
details > summary:hover { color: #1d4ed8 !important; }
details[open] > *:not(summary) { animation: expandIn 0.2s ease; }
@keyframes expandIn {
    from { opacity: 0; transform: translateY(-5px); }
    to   { opacity: 1; transform: translateY(0); }
}

/* ══════════════════════════════════════════════════════
   STREAMLIT NATIVE OVERRIDES
══════════════════════════════════════════════════════ */
/* Progress bar */
.stProgress > div > div > div {
    background: linear-gradient(90deg, #1d4ed8, #60a5fa) !important;
    border-radius: 99px !important;
}
.stProgress > div > div {
    background: #e2e8f0 !important; border-radius: 99px !important;
}

/* st.metric */
[data-testid="stMetric"] {
    background: #ffffff;
    border: 1px solid #e2e8f0;
    border-radius: 12px; padding: 14px 16px !important;
    box-shadow: 0 1px 4px rgba(0,0,0,0.05);
}
[data-testid="stMetricValue"] {
    font-size: 1.6rem !important; font-weight: 800 !important;
    letter-spacing: -0.03em !important; color: #0f172a !important;
}
[data-testid="stMetricLabel"] {
    font-size: .72rem !important; font-weight: 600 !important;
    letter-spacing: .5px !important; color: #64748b !important;
    text-transform: uppercase !important;
}

/* Dataframe */
[data-testid="stDataFrame"] {
    border: 1px solid #e2e8f0 !important;
    border-radius: 12px !important; overflow: hidden !important;
    box-shadow: 0 1px 4px rgba(0,0,0,0.05) !important;
}

/* Text inputs */
.stTextInput > div > div > input,
.stTextArea > div > div > textarea {
    background: #ffffff !important;
    border: 1px solid #cbd5e1 !important;
    border-radius: 10px !important; color: #0f172a !important;
    font-family: 'Inter', sans-serif !important;
    transition: border-color 0.2s ease, box-shadow 0.2s ease !important;
}
.stTextInput > div > div > input:focus,
.stTextArea > div > div > textarea:focus {
    border-color: #2563eb !important;
    box-shadow: 0 0 0 3px rgba(37,99,235,0.12) !important;
}

/* File uploader */
[data-testid="stFileUploader"] {
    border: 2px dashed #bfdbfe !important;
    border-radius: 12px !important;
    background: #eff6ff !important;
    transition: border-color 0.2s ease, background 0.2s ease !important;
}
[data-testid="stFileUploader"]:hover {
    border-color: #2563eb !important;
    background: #dbeafe !important;
}

/* Slider */
[data-baseweb="slider"] [role="slider"] {
    background: #2563eb !important;
    box-shadow: 0 0 0 4px rgba(37,99,235,0.15) !important;
}

/* Selectbox */
[data-baseweb="select"] > div {
    background: #ffffff !important;
    border-color: #cbd5e1 !important;
    border-radius: 10px !important; color: #0f172a !important;
}

/* Caption */
.stCaption { color: #94a3b8 !important; font-size: .78rem !important; }

/* General text color */
p, span, label, div { color: #1e293b; }
h1, h2, h3 { color: #0f172a; font-weight: 700; }

/* ══════════════════════════════════════════════════════
   SCROLLBAR
══════════════════════════════════════════════════════ */
::-webkit-scrollbar { width: 5px; height: 5px; }
::-webkit-scrollbar-track { background: #f1f5f9; }
::-webkit-scrollbar-thumb { background: #cbd5e1; border-radius: 99px; }
::-webkit-scrollbar-thumb:hover { background: #94a3b8; }

/* ══════════════════════════════════════════════════════
   MOBILE RESPONSIVE
══════════════════════════════════════════════════════ */
@media (max-width: 768px) {
    .ds-header { padding: 18px 20px; gap: 12px; }
    .ds-header h1 { font-size: 1.3rem; }
    .ds-header p  { font-size: .8rem; }
    .mc-card  { min-width: 85px; padding: 12px 10px; }
    .mc-card .val { font-size: 1.5rem; }
    .log-box  { height: 190px; }
    .sec-head { font-size: .9rem; }
}
@media (max-width: 480px) {
    .mc-card { flex: 1 1 calc(50% - 8px); min-width: 0; }
    .mc-card .val { font-size: 1.3rem; }
    .ds-header h1 { font-size: 1.1rem; }
}

/* ══════════════════════════════════════════════════════
   1. FADE-IN ENTRANCE — sections slide up on appear
══════════════════════════════════════════════════════ */
@keyframes fadeInUp {
    from { opacity: 0; transform: translateY(22px); }
    to   { opacity: 1; transform: translateY(0); }
}
.ds-header   { animation: fadeInUp 0.55s cubic-bezier(0.22,1,0.36,1) both; }
.sec-head    { animation: fadeInUp 0.45s cubic-bezier(0.22,1,0.36,1) both; }
.mc-row      { animation: fadeInUp 0.5s  cubic-bezier(0.22,1,0.36,1) 0.08s both; }
.info-box,
.warn-box    { animation: fadeInUp 0.4s  cubic-bezier(0.22,1,0.36,1) both; }
details      { animation: fadeInUp 0.38s cubic-bezier(0.22,1,0.36,1) both; }
[data-testid="stDataFrame"] { animation: fadeInUp 0.5s cubic-bezier(0.22,1,0.36,1) 0.05s both; }

/* ══════════════════════════════════════════════════════
   2. LIVE PULSE DOT — blinking indicator while scraping
══════════════════════════════════════════════════════ */
@keyframes pulseDot {
    0%, 100% { opacity: 1;   box-shadow: 0 0 0 0   rgba(34,197,94,0.5); }
    50%       { opacity: 0.7; box-shadow: 0 0 0 6px rgba(34,197,94,0); }
}
.live-dot {
    display: inline-block;
    width: 9px; height: 9px;
    background: #22c55e;
    border-radius: 50%;
    animation: pulseDot 1.4s ease infinite;
    margin-right: 7px;
    vertical-align: middle;
}
.live-badge {
    display: inline-flex; align-items: center;
    background: rgba(34,197,94,0.08);
    border: 1px solid rgba(34,197,94,0.35);
    border-radius: 99px; padding: 5px 14px;
    font-size: .78rem; font-weight: 700; color: #15803d;
    letter-spacing: 0.3px; margin-bottom: 14px;
    animation: fadeInUp 0.3s ease both;
}

/* ══════════════════════════════════════════════════════
   4. LOG LINES SLIDE-IN — each entry from left
══════════════════════════════════════════════════════ */
@keyframes slideInLog {
    from { opacity: 0; transform: translateX(-14px); }
    to   { opacity: 1; transform: translateX(0); }
}
.log-line {
    display: block;
    animation: slideInLog 0.22s cubic-bezier(0.22,1,0.36,1) both;
}

/* ══════════════════════════════════════════════════════
   5. PROGRESS BAR SHIMMER — glowing sweep while active
══════════════════════════════════════════════════════ */
@keyframes shimmer {
    0%   { background-position: -200% center; }
    100% { background-position: 200% center; }
}
.stProgress > div > div > div {
    background: linear-gradient(90deg, #1d4ed8, #60a5fa) !important;
    border-radius: 99px !important;
    position: relative !important;
    overflow: hidden !important;
}
.stProgress > div > div > div::after {
    content: "";
    position: absolute; inset: 0;
    background: linear-gradient(
        90deg,
        transparent 20%,
        rgba(255,255,255,0.45) 50%,
        transparent 80%
    );
    background-size: 200% 100%;
    animation: shimmer 1.6s ease infinite;
}

/* ══════════════════════════════════════════════════════
   6. SIDEBAR TRUCK BOUNCE — on hover
══════════════════════════════════════════════════════ */
@keyframes truckBounce {
    0%   { transform: translateY(0) rotate(0deg); }
    25%  { transform: translateY(-9px) rotate(-3deg); }
    50%  { transform: translateY(-4px) rotate(2deg); }
    75%  { transform: translateY(-7px) rotate(-1deg); }
    100% { transform: translateY(0) rotate(0deg); }
}
.truck-icon {
    display: inline-block;
    transition: transform 0.2s ease;
    cursor: default;
}
.truck-icon:hover {
    animation: truckBounce 0.65s cubic-bezier(0.36,0.07,0.19,0.97) both;
}

/* ══════════════════════════════════════════════════════
   7. SUCCESS ANIMATION — glow banner on completion
══════════════════════════════════════════════════════ */
@keyframes successGlow {
    0%   { box-shadow: 0 0 0  0   rgba(16,185,129,0); }
    40%  { box-shadow: 0 0 28px 8px rgba(16,185,129,0.28); }
    100% { box-shadow: 0 0 0  0   rgba(16,185,129,0); }
}
/* Summary pills inside banner */
.sum-pill {
    display: inline-flex; align-items: center; gap: 6px;
    padding: 5px 14px; border-radius: 99px;
    font-size: .78rem; font-weight: 700; white-space: nowrap;
}
.sum-pill-active { background: #dcfce7; color: #15803d; border: 1px solid #86efac; }
.sum-pill-oos    { background: #fee2e2; color: #b91c1c; border: 1px solid #fca5a5; }
.sum-pill-inactive { background: #fef9c3; color: #92400e; border: 1px solid #fde68a; }
.sum-pill-notfound { background: #f1f5f9; color: #475569; border: 1px solid #cbd5e1; }
.sum-pill-rate   { background: #eff6ff; color: #1d4ed8; border: 1px solid #bfdbfe; }
.sum-pill-time   { background: #f5f3ff; color: #6d28d9; border: 1px solid #ddd6fe; }
.sum-banner {
    background: linear-gradient(135deg, #d1fae5 0%, #ecfdf5 100%);
    border: 1px solid #6ee7b7;
    border-radius: 16px;
    padding: 18px 24px;
    margin-bottom: 20px;
    animation: fadeInUp 0.4s ease both, successGlow 1.8s ease 0.4s both;
}
.sum-banner-top {
    display: flex; align-items: center; gap: 10px;
    font-weight: 800; color: #065f46; font-size: 1rem;
    margin-bottom: 12px;
}
.sum-banner-pills {
    display: flex; flex-wrap: wrap; gap: 8px;
}
.success-banner {
    background: linear-gradient(135deg, #d1fae5 0%, #ecfdf5 100%);
    border: 1px solid #6ee7b7;
    border-radius: 14px;
    padding: 14px 22px;
    display: flex; align-items: center; gap: 12px;
    font-weight: 700; color: #065f46; font-size: .92rem;
    animation: fadeInUp 0.4s ease both, successGlow 1.8s ease 0.4s both;
    margin-bottom: 18px;
}
.success-banner .s-icon { font-size: 1.4rem; }
.success-banner .s-count {
    margin-left: auto; font-size: .78rem; font-weight: 600;
    color: #059669; background: rgba(16,185,129,0.12);
    padding: 3px 12px; border-radius: 99px;
}

/* ══════════════════════════════════════════════════════
   8. FEATURE STATS BAR
══════════════════════════════════════════════════════ */
.stats-bar {
    display: flex; gap: 12px; flex-wrap: wrap;
    margin: 0 0 24px;
    animation: fadeInUp 0.5s cubic-bezier(0.22,1,0.36,1) 0.1s both;
}
.stat-card {
    flex: 1; min-width: 130px;
    background: #ffffff;
    border: 1px solid #e2e8f0;
    border-radius: 16px;
    padding: 18px 16px;
    text-align: center;
    box-shadow: 0 1px 4px rgba(0,0,0,0.05);
    transition: transform 0.22s ease, box-shadow 0.22s ease, border-color 0.22s ease;
    position: relative; overflow: hidden;
}
.stat-card::before {
    content: "";
    position: absolute; top: 0; left: 0; right: 0;
    height: 3px;
    border-radius: 16px 16px 0 0;
}
.stat-card.sc-blue::before   { background: linear-gradient(90deg,#2563eb,#60a5fa); }
.stat-card.sc-green::before  { background: linear-gradient(90deg,#059669,#34d399); }
.stat-card.sc-purple::before { background: linear-gradient(90deg,#7c3aed,#a78bfa); }
.stat-card.sc-orange::before { background: linear-gradient(90deg,#d97706,#fbbf24); }
.stat-card:hover {
    transform: translateY(-3px);
    box-shadow: 0 8px 28px rgba(0,0,0,0.1);
    border-color: #bfdbfe;
}
.stat-card .sc-icon {
    font-size: 1.8rem; line-height: 1;
    margin-bottom: 8px; display: block;
}
.stat-card .sc-val {
    font-size: 1.55rem; font-weight: 800;
    letter-spacing: -0.04em; line-height: 1;
    margin-bottom: 4px;
}
.stat-card.sc-blue   .sc-val { color: #2563eb; }
.stat-card.sc-green  .sc-val { color: #059669; }
.stat-card.sc-purple .sc-val { color: #7c3aed; }
.stat-card.sc-orange .sc-val { color: #d97706; }
.stat-card .sc-lbl {
    font-size: .68rem; font-weight: 600; color: #94a3b8;
    text-transform: uppercase; letter-spacing: 1px;
    margin-bottom: 4px;
}
.stat-card .sc-sub {
    font-size: .72rem; color: #64748b; margin-top: 2px;
}

/* ══════════════════════════════════════════════════════
   9. VISUAL STEP STEPPER
══════════════════════════════════════════════════════ */
.stepper {
    display: flex; align-items: flex-start; gap: 0;
    margin: 0 0 28px;
    animation: fadeInUp 0.5s cubic-bezier(0.22,1,0.36,1) 0.15s both;
}
.step-wrap {
    display: flex; flex-direction: column; align-items: center;
    flex: 1; position: relative;
}
/* connector line between steps */
.step-wrap:not(:last-child)::after {
    content: "";
    position: absolute; top: 20px; left: calc(50% + 20px);
    right: calc(-50% + 20px);
    height: 2px;
    background: #e2e8f0;
    z-index: 0;
    transition: background 0.4s ease;
}
.step-wrap.sw-done:not(:last-child)::after,
.step-wrap.sw-active:not(:last-child)::after {
    background: linear-gradient(90deg, #2563eb, #e2e8f0);
}
.step-wrap.sw-done:not(:last-child)::after {
    background: #2563eb;
}
/* circle */
.step-circle {
    width: 40px; height: 40px;
    border-radius: 50%;
    display: flex; align-items: center; justify-content: center;
    font-size: .9rem; font-weight: 800;
    border: 2px solid #e2e8f0;
    background: #f8fafc; color: #94a3b8;
    position: relative; z-index: 1;
    transition: all 0.3s ease;
}
.sw-done .step-circle {
    background: #2563eb; border-color: #2563eb;
    color: #fff;
    box-shadow: 0 0 0 4px rgba(37,99,235,0.15);
}
.sw-active .step-circle {
    background: #fff; border-color: #2563eb;
    color: #2563eb;
    box-shadow: 0 0 0 4px rgba(37,99,235,0.18);
    animation: stepPulse 2s ease infinite;
}
@keyframes stepPulse {
    0%, 100% { box-shadow: 0 0 0 4px rgba(37,99,235,0.18); }
    50%       { box-shadow: 0 0 0 8px rgba(37,99,235,0.08); }
}
/* labels */
.step-label {
    margin-top: 8px; font-size: .72rem; font-weight: 700;
    color: #94a3b8; text-align: center; letter-spacing: .2px;
    transition: color 0.3s ease;
}
.sw-done  .step-label { color: #2563eb; }
.sw-active .step-label { color: #1e40af; }
.step-sub {
    font-size: .63rem; color: #cbd5e1; text-align: center;
    margin-top: 2px; font-weight: 500;
    transition: color 0.3s ease;
}
.sw-done  .step-sub { color: #93c5fd; }
.sw-active .step-sub { color: #3b82f6; }

/* ══════════════════════════════════════════════════════
   10. HOW-TO GUIDE CARDS
══════════════════════════════════════════════════════ */
.howto-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(190px, 1fr));
    gap: 12px;
    margin: 0 0 24px;
    animation: fadeInUp 0.5s cubic-bezier(0.22,1,0.36,1) 0.12s both;
}
.howto-card {
    background: #ffffff;
    border: 1px solid #e2e8f0;
    border-radius: 14px;
    padding: 18px 16px 16px;
    position: relative;
    transition: transform 0.22s ease, box-shadow 0.22s ease, border-color 0.22s ease;
    box-shadow: 0 1px 4px rgba(0,0,0,0.04);
}
.howto-card:hover {
    transform: translateY(-3px);
    box-shadow: 0 8px 24px rgba(37,99,235,0.1);
    border-color: #bfdbfe;
}
.howto-num {
    position: absolute; top: -12px; left: 16px;
    width: 28px; height: 28px; border-radius: 50%;
    background: linear-gradient(135deg, #1d4ed8, #3b82f6);
    color: #fff; font-size: .78rem; font-weight: 800;
    display: flex; align-items: center; justify-content: center;
    box-shadow: 0 2px 8px rgba(37,99,235,0.35);
}
.howto-icon { font-size: 1.5rem; margin-bottom: 8px; display: block; }
.howto-title {
    font-size: .88rem; font-weight: 700; color: #0f172a;
    margin-bottom: 5px;
}
.howto-desc {
    font-size: .75rem; color: #64748b; line-height: 1.55;
}
.howto-tip {
    margin-top: 16px;
    background: #eff6ff; border-left: 3px solid #2563eb;
    border-radius: 0 8px 8px 0; padding: 10px 14px;
    animation: fadeInUp 0.5s cubic-bezier(0.22,1,0.36,1) 0.18s both;
}
.howto-tip-title {
    font-size: .72rem; font-weight: 700; color: #1d4ed8;
    text-transform: uppercase; letter-spacing: .8px; margin-bottom: 6px;
}
.howto-tip ul {
    margin: 0; padding-left: 16px;
    font-size: .76rem; color: #1e40af; line-height: 1.7;
}

/* ══════════════════════════════════════════════════════
   11. LIVE ID COUNTER
══════════════════════════════════════════════════════ */
.id-counter-row {
    display: flex; align-items: center; justify-content: space-between;
    margin-bottom: 6px;
}
.id-counter-label {
    font-size: .78rem; font-weight: 600; color: #64748b;
}
.id-counter-badge {
    display: inline-flex; align-items: center; gap: 6px;
    padding: 4px 12px; border-radius: 99px;
    font-size: .75rem; font-weight: 700;
    border: 1px solid #bfdbfe;
    background: #eff6ff; color: #1d4ed8;
    transition: all 0.25s ease;
}
.id-counter-badge.has-ids {
    background: #dcfce7; color: #15803d; border-color: #86efac;
}
.id-counter-badge.empty {
    background: #f1f5f9; color: #94a3b8; border-color: #e2e8f0;
}
.id-counter-dupe {
    font-size: .7rem; color: #d97706; font-weight: 600;
    margin-left: 6px;
}

/* ══════════════════════════════════════════════════════
   12. FOOTER
══════════════════════════════════════════════════════ */
.site-footer {
    margin-top: 16px;
    background: #0f172a;
    border-radius: 16px;
    padding: 28px 32px 22px;
    animation: fadeInUp 0.5s cubic-bezier(0.22,1,0.36,1) both;
}
.footer-top {
    display: flex; align-items: center; justify-content: space-between;
    flex-wrap: wrap; gap: 16px;
    padding-bottom: 18px;
    border-bottom: 1px solid rgba(255,255,255,0.07);
    margin-bottom: 16px;
}
.footer-brand {
    display: flex; align-items: center; gap: 10px;
}
.footer-brand-name {
    font-size: 1rem; font-weight: 800; color: #f1f5f9;
    letter-spacing: -0.02em;
}
.footer-brand-tag {
    font-size: .7rem; color: #475569; font-weight: 500;
    margin-top: 1px;
}
.footer-links {
    display: flex; gap: 8px; flex-wrap: wrap;
}
.footer-link {
    display: inline-flex; align-items: center; gap: 5px;
    padding: 6px 14px; border-radius: 8px;
    font-size: .75rem; font-weight: 600;
    border: 1px solid rgba(255,255,255,0.1);
    color: #94a3b8 !important;
    text-decoration: none !important;
    transition: all 0.2s ease;
    background: rgba(255,255,255,0.03);
}
.footer-link:hover {
    background: rgba(37,99,235,0.2);
    border-color: rgba(37,99,235,0.4);
    color: #93c5fd !important;
}
.footer-bottom {
    display: flex; align-items: center; justify-content: space-between;
    flex-wrap: wrap; gap: 10px;
}
.footer-copy {
    font-size: .7rem; color: #334155;
}
.footer-badges {
    display: flex; gap: 6px;
}
.footer-badge {
    font-size: .65rem; font-weight: 700; padding: 3px 10px;
    border-radius: 99px; letter-spacing: .5px;
}
.fb-public  { background: rgba(34,197,94,0.12);  color: #4ade80;  border: 1px solid rgba(34,197,94,0.2);  }
.fb-free    { background: rgba(37,99,235,0.12);  color: #60a5fa;  border: 1px solid rgba(37,99,235,0.2);  }
.fb-secure  { background: rgba(124,58,237,0.12); color: #a78bfa;  border: 1px solid rgba(124,58,237,0.2); }
</style>
""", unsafe_allow_html=True)


# ─────────────────────────────────────────────────────────────────────────────
# Feature — Carrier Risk Scorecard + Authority Flags + PDF Report
# ─────────────────────────────────────────────────────────────────────────────

def _compute_risk_score(row: dict) -> tuple[int, str]:
    """
    Score a carrier 0-100 based on scraped FMCSA data.
    Returns (score, level) where level = 'safe' | 'caution' | 'high_risk'.
    """
    score = 50  # neutral start

    # Safety Rating (±30) — highest weight
    rating = str(row.get("Safety_Rating", "")).lower()
    if "satisfactory" in rating and "un" not in rating:
        score += 30
    elif "unsatisfactory" in rating:
        score -= 30

    # Carrier Status (±35)
    status = str(row.get("Carrier_Status", "")).upper()
    if status == "ACTIVE":
        score += 20
    elif status == "OUT_OF_SERVICE":
        score -= 35
    elif status == "INACTIVE":
        score -= 20

    # Authority Status (±25)
    auth = str(row.get("Operating_Authority_Status", "")).lower()
    if "active" in auth:
        score += 15
    elif "revoked" in auth or "suspended" in auth:
        score -= 25

    # OOS Percentage (±15)
    try:
        oos_raw = str(row.get("OOS_Percentage", "") or "").replace("%", "").strip()
        oos = float(oos_raw) if oos_raw else 0.0
        if oos == 0:
            score += 10
        elif oos < 15:
            score += 5
        elif oos > 35:
            score -= 15
        elif oos > 20:
            score -= 8
    except (ValueError, TypeError):
        pass

    # Total Crashes (±10)
    try:
        crashes_raw = str(row.get("Total_Crashes", "") or "").strip()
        crashes = int(crashes_raw) if crashes_raw.isdigit() else 0
        if crashes == 0:
            score += 5
        elif crashes <= 2:
            score -= 5
        else:
            score -= min(crashes * 3, 15)
    except (ValueError, TypeError):
        pass

    score = max(0, min(100, score))
    level = "safe" if score >= 70 else ("caution" if score >= 45 else "high_risk")
    return score, level


def _risk_badge(score: int, level: str) -> str:
    if level == "safe":       return f"🟢 Safe ({score})"
    if level == "caution":    return f"🟡 Caution ({score})"
    return f"🔴 High Risk ({score})"


def _authority_flags(row: dict) -> str:
    """Return plain-text alert flags for dangerous carriers."""
    auth   = str(row.get("Operating_Authority_Status", "")).lower()
    status = str(row.get("Carrier_Status", "")).upper()
    flags  = []
    if "revoked"       in auth:  flags.append("AUTH REVOKED")
    if "suspended"     in auth:  flags.append("SUSPENDED")
    if status == "OUT_OF_SERVICE": flags.append("OUT OF SERVICE")
    return " | ".join(flags) if flags else "Clear"


def _generate_pdf_report(rows: list[dict]) -> bytes:
    """
    Generate a professional multi-carrier PDF report (one page per carrier).
    Requires fpdf2. Returns PDF bytes or empty bytes if fpdf2 not installed.
    """
    try:
        from fpdf import FPDF
    except ImportError:
        return b""

    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=15)

    found_rows = [r for r in rows
                  if str(r.get("Scrape_Status", "")).lower() in ("found", "success")]
    if not found_rows:
        return b""

    for row in found_rows:
        pdf.add_page()

        # Header bar
        pdf.set_fill_color(29, 78, 216)
        pdf.rect(0, 0, 210, 22, style="F")
        pdf.set_text_color(255, 255, 255)
        pdf.set_font("Helvetica", "B", 14)
        pdf.set_xy(10, 5)
        pdf.cell(190, 12, "DISPATCH DOS  -  Carrier Verification Report", align="C")

        # Carrier name
        pdf.set_text_color(15, 23, 42)
        pdf.set_font("Helvetica", "B", 13)
        pdf.set_xy(10, 28)
        name = str(row.get("Legal_Name", "-"))[:60]
        pdf.cell(130, 8, name)

        # Risk score box (top-right)
        score, level = _compute_risk_score(row)
        risk_colors  = {"safe": (34, 197, 94), "caution": (245, 158, 11), "high_risk": (239, 68, 68)}
        risk_labels  = {"safe": "SAFE", "caution": "CAUTION", "high_risk": "HIGH RISK"}
        r, g, b = risk_colors[level]
        pdf.set_fill_color(r, g, b)
        pdf.set_text_color(255, 255, 255)
        pdf.set_font("Helvetica", "B", 10)
        pdf.set_xy(148, 26)
        pdf.cell(52, 10, f"Risk: {risk_labels[level]}  ({score}/100)", align="C", fill=True)

        # DBA name
        dba = str(row.get("DBA_Name", "")).strip()
        if dba:
            pdf.set_font("Helvetica", "", 9)
            pdf.set_text_color(100, 116, 139)
            pdf.set_xy(10, 36)
            pdf.cell(130, 6, f"DBA: {dba[:50]}")

        # Authority flags bar
        flags_text = _authority_flags(row)
        flag_ok = (flags_text == "Clear")
        pdf.set_fill_color(220, 252, 231) if flag_ok else pdf.set_fill_color(254, 226, 226)
        pdf.set_text_color(21, 128, 61)   if flag_ok else pdf.set_text_color(185, 28, 28)
        pdf.set_font("Helvetica", "B", 8)
        pdf.set_xy(10, 44)
        label = ("Authority & Status: OK - No Issues Found"
                 if flag_ok else f"ALERT: {flags_text}")
        pdf.cell(190, 7, label, fill=True)

        # Divider
        pdf.set_draw_color(226, 232, 240)
        pdf.line(10, 53, 200, 53)

        # Two-column data fields
        fields_left = [
            ("USDOT #",       row.get("USDOT_Number",  "-")),
            ("MC #",          row.get("MC_Number",      "-")),
            ("Status",        row.get("Carrier_Status", "-")),
            ("Authority",     row.get("Operating_Authority_Status", "-")),
            ("Safety Rating", row.get("Safety_Rating",  "-")),
            ("Entity Type",   row.get("Entity_Type",    "-")),
            ("Phone",         row.get("Phone",          "-")),
        ]
        fields_right = [
            ("Power Units",   row.get("Power_Units",     "-")),
            ("Drivers",       row.get("Drivers",         "-")),
            ("OOS %",         row.get("OOS_Percentage",  "-")),
            ("Total Crashes", row.get("Total_Crashes",   "-")),
            ("Vehicle OOS",   row.get("Vehicle_OOS_Pct", "-")),
            ("Driver OOS",    row.get("Driver_OOS_Pct",  "-")),
            ("MCS-150 Date",  row.get("MCS150_Date",     "-")),
        ]

        y0 = 57
        for i, (label, value) in enumerate(fields_left):
            y = y0 + i * 9
            pdf.set_font("Helvetica", "B", 8)
            pdf.set_text_color(71, 85, 105)
            pdf.set_xy(10, y)
            pdf.cell(32, 7, label + ":")
            pdf.set_font("Helvetica", "", 8)
            pdf.set_text_color(15, 23, 42)
            pdf.cell(63, 7, str(value)[:35])

        for i, (label, value) in enumerate(fields_right):
            y = y0 + i * 9
            pdf.set_font("Helvetica", "B", 8)
            pdf.set_text_color(71, 85, 105)
            pdf.set_xy(110, y)
            pdf.cell(32, 7, label + ":")
            pdf.set_font("Helvetica", "", 8)
            pdf.set_text_color(15, 23, 42)
            pdf.cell(58, 7, str(value)[:30])

        # Address
        pdf.set_draw_color(226, 232, 240)
        pdf.line(10, 124, 200, 124)
        pdf.set_font("Helvetica", "B", 8)
        pdf.set_text_color(71, 85, 105)
        pdf.set_xy(10, 127)
        pdf.cell(32, 6, "Address:")
        pdf.set_font("Helvetica", "", 8)
        pdf.set_text_color(15, 23, 42)
        pdf.cell(158, 6, str(row.get("Physical_Address", "-"))[:80])

        # Cargo
        pdf.set_font("Helvetica", "B", 8)
        pdf.set_text_color(71, 85, 105)
        pdf.set_xy(10, 136)
        pdf.cell(32, 6, "Cargo:")
        pdf.set_font("Helvetica", "", 7)
        pdf.set_text_color(15, 23, 42)
        pdf.cell(158, 6, str(row.get("Cargo_Carried", "-"))[:90])

        # Operations
        pdf.set_font("Helvetica", "B", 8)
        pdf.set_text_color(71, 85, 105)
        pdf.set_xy(10, 144)
        pdf.cell(32, 6, "Operations:")
        pdf.set_font("Helvetica", "", 7)
        pdf.set_text_color(15, 23, 42)
        pdf.cell(158, 6, str(row.get("Operation_Classification", "-"))[:90])

        # Page footer
        pdf.set_fill_color(248, 250, 252)
        pdf.rect(0, 282, 210, 15, style="F")
        pdf.set_text_color(148, 163, 184)
        pdf.set_font("Helvetica", "", 7)
        pdf.set_xy(10, 286)
        pdf.cell(95, 5, f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}  |  Dispatch DOS")
        pdf.set_xy(105, 286)
        pdf.cell(95, 5, "Data source: safer.fmcsa.dot.gov  |  Public government records", align="R")

    output = pdf.output()
    return bytes(output) if not isinstance(output, bytes) else output


# ─────────────────────────────────────────────────────────────────────────────
# Output column definitions
# ─────────────────────────────────────────────────────────────────────────────

# Every column that will appear in the output Excel (in display order)
OUTPUT_COLS: list[str] = [
    # Tracking
    "Input_ID", "Scrape_Status", "Carrier_Status", "Fetch_Method",
    "Scraped_At", "Error_Detail",
    # Identity
    "Legal_Name", "DBA_Name", "USDOT_Number", "MC_Number", "MC_MX_Raw",
    "State_Carrier_ID",
    # Contact
    "Physical_Address", "Mailing_Address", "Phone",
    # Classification
    "Entity_Type", "USDOT_Status", "Operating_Authority_Status",
    "Safety_Rating", "Safety_Rating_Date", "OOS_Date",
    # Fleet
    "Power_Units", "Drivers", "MCS150_Date", "MCS150_Mileage",
    # Operations
    "Operation_Classification", "Carrier_Operation", "Cargo_Carried",
    # Inspection stats
    "OOS_Percentage",
    "Vehicle_Inspections", "Vehicle_OOS_Count", "Vehicle_OOS_Pct",
    "Driver_Inspections",  "Driver_OOS_Count",  "Driver_OOS_Pct",
    "Hazmat_Inspections",  "Hazmat_OOS_Count",  "Hazmat_OOS_Pct",
    # Crashes
    "Fatal_Crashes", "Injury_Crashes", "Tow_Crashes", "Total_Crashes",
]

# Map scrape_carrier() result keys → Excel column names
_FIELD_MAP: dict[str, str] = {
    "status":                       "Scrape_Status",
    "carrier_status":               "Carrier_Status",
    "fetch_method":                 "Fetch_Method",
    "scraped_at":                   "Scraped_At",
    "error_detail":                 "Error_Detail",
    "legal_name":                   "Legal_Name",
    "dba_name":                     "DBA_Name",
    "usdot_number":                 "USDOT_Number",
    "mc_number":                    "MC_Number",
    "mc_mx_raw":                    "MC_MX_Raw",
    "state_carrier_id":             "State_Carrier_ID",
    "physical_address":             "Physical_Address",
    "mailing_address":              "Mailing_Address",
    "phone":                        "Phone",
    "entity_type":                  "Entity_Type",
    "usdot_status":                 "USDOT_Status",
    "operating_authority_status":   "Operating_Authority_Status",
    "safety_rating":                "Safety_Rating",
    "safety_rating_date":           "Safety_Rating_Date",
    "oos_date":                     "OOS_Date",
    "power_units":                  "Power_Units",
    "drivers":                      "Drivers",
    "mcs150_date":                  "MCS150_Date",
    "mcs150_mileage":               "MCS150_Mileage",
    "out_of_service_percentage":    "OOS_Percentage",
}

# Map inspection_stats sub-keys → column names
_STATS_MAP: dict[str, str] = {
    "vehicle_inspections": "Vehicle_Inspections",
    "vehicle_oos_count":   "Vehicle_OOS_Count",
    "vehicle_oos_pct":     "Vehicle_OOS_Pct",
    "driver_inspections":  "Driver_Inspections",
    "driver_oos_count":    "Driver_OOS_Count",
    "driver_oos_pct":      "Driver_OOS_Pct",
    "hazmat_inspections":  "Hazmat_Inspections",
    "hazmat_oos_count":    "Hazmat_OOS_Count",
    "hazmat_oos_pct":      "Hazmat_OOS_Pct",
    "crash_fatal":         "Fatal_Crashes",
    "crash_injury":        "Injury_Crashes",
    "crash_tow":           "Tow_Crashes",
    "crash_total":         "Total_Crashes",
}

# Scrape_Status values that count as "not success"
_FAILED_STATUSES = {"Failed", "Not_Found", "Blocked", "Error"}


# ─────────────────────────────────────────────────────────────────────────────
# Session state initialiser
# ─────────────────────────────────────────────────────────────────────────────

def _init() -> None:
    defaults: dict[str, Any] = {
        # Input state
        "carrier_ids":    [],      # deduplicated list ready to scrape
        "dupes_removed":  [],      # removed duplicate strings
        "total_input":    0,
        "total_unique":   0,
        # Scraping runtime
        "is_scraping":    False,
        "stop_event":     None,
        "scrape_thread":  None,
        "log_q":          None,
        "prog_q":         None,
        "result_store":   [],      # thread deposits flat row dicts here
        # Live counters (updated by draining queues)
        "prog_current":   0,
        "prog_total":     0,
        "last_id":        "",
        "counts":         {"success": 0, "not_found": 0, "failed": 0, "blocked": 0},
        "log_lines":      [],      # rendered HTML strings
        # Results
        "results_rows":   [],      # list of flat dicts (one per carrier)
        "output_bytes":   None,    # bytes of Excel workbook for download
        "_settings":      {},      # copy of sidebar settings used for last run
        "show_list_panel": False,  # left slide-in panel visibility
        # ETA tracking
        "scrape_start_time": None,
        "scrape_elapsed":    None,  # total seconds when run finished
        "_toast_shown":      False, # show completion toast only once
        # Resume support
        "_resume_existing_rows": [],  # rows from previous partial run (for resume)
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v


_init()


# ─────────────────────────────────────────────────────────────────────────────
# JS — hover-to-open for all expanders
# ─────────────────────────────────────────────────────────────────────────────

# hover-to-open JS removed (components.html deprecated)


# ─────────────────────────────────────────────────────────────────────────────
# Input processing helpers
# ─────────────────────────────────────────────────────────────────────────────

def _clean_id(raw: str) -> str:
    return raw.strip().strip('"').strip("'")


def _process_input(raw_ids: list[str]) -> tuple[list[str], list[str], int]:
    """
    Clean, validate, and deduplicate a list of carrier ID strings.
    Returns (unique_ids, duplicates_removed, total_input_count).
    """
    cleaned: list[str] = []
    for raw in raw_ids:
        v = _clean_id(raw)
        if v and len(v) > 1:
            cleaned.append(v)

    total_input = len(cleaned)

    # Case-insensitive dedup — keep first occurrence
    seen: set[str] = set()
    unique: list[str] = []
    dupes:  list[str] = []
    for v in cleaned:
        key = v.upper()
        if key in seen:
            dupes.append(v)
        else:
            seen.add(key)
            unique.append(v)

    return unique, dupes, total_input


def _detect_column(df: pd.DataFrame) -> str:
    """Guess the carrier ID column from an Excel file."""
    kw = re.compile(r"mc|dot|usdot|number|num|id|carrier", re.IGNORECASE)
    for col in df.columns:
        if kw.search(str(col)):
            return col
    # Value heuristic: mostly numeric
    id_re = re.compile(r"^\s*(MC|DOT)?\s*\d+\s*$", re.IGNORECASE)
    for col in df.columns:
        sample = df[col].dropna().astype(str).head(20)
        if not sample.empty and sample.apply(lambda v: bool(id_re.match(v))).mean() >= .5:
            return col
    return str(df.columns[0])


# ─────────────────────────────────────────────────────────────────────────────
# Result flattening  (scrape_carrier dict → Excel row dict)
# ─────────────────────────────────────────────────────────────────────────────

def _flatten(input_id: str, result: dict[str, Any]) -> dict[str, str]:
    """Convert a scrape_carrier() result into a flat string dict for Excel."""
    row: dict[str, str] = {col: "" for col in OUTPUT_COLS}
    row["Input_ID"] = input_id

    # Status label mapping
    status_raw = result.get("status", "error")
    row["Scrape_Status"] = {
        "found":     "Success",
        "not_found": "Not_Found",
        "blocked":   "Blocked",
        "error":     "Failed",
    }.get(status_raw, "Failed")

    # Simple field map
    for src, dst in _FIELD_MAP.items():
        if src == "status":
            continue
        row[dst] = str(result.get(src) or "")

    # Lists → pipe-separated strings
    row["Operation_Classification"] = " | ".join(result.get("operation_classification") or [])
    row["Carrier_Operation"]        = " | ".join(result.get("carrier_operation") or [])
    row["Cargo_Carried"]            = " | ".join(result.get("cargo_carried") or [])

    # Inspection stats (nested dict)
    stats = result.get("inspection_stats") or {}
    for src, dst in _STATS_MAP.items():
        row[dst] = str(stats.get(src) or "")

    return row


# ─────────────────────────────────────────────────────────────────────────────
# Excel builder
# ─────────────────────────────────────────────────────────────────────────────

def _auto_col_widths(ws, df: pd.DataFrame) -> None:
    for col_cells in ws.columns:
        w = max((len(str(c.value or "")) for c in col_cells), default=10)
        ws.column_dimensions[col_cells[0].column_letter].width = min(w + 4, 60)


def _build_excel(
    rows: list[dict[str, str]],
    dupes: list[str],
    settings: dict[str, Any],
) -> bytes:
    """Build the output Excel workbook in memory and return bytes."""
    all_df    = pd.DataFrame(rows, columns=OUTPUT_COLS)
    failed_df = all_df[all_df["Scrape_Status"].isin(_FAILED_STATUSES)]
    dupes_df  = pd.DataFrame({"Removed_Duplicate": dupes})

    # Summary
    success = int((all_df["Scrape_Status"] == "Success").sum())
    summary_df = pd.DataFrame({
        "Metric": [
            "Total Input Records", "Unique Records Scraped",
            "Duplicates Removed", "Successfully Scraped",
            "Not Found", "Failed / Error", "Blocked",
            "Delay Range (s)", "Max Concurrent", "Exported At",
        ],
        "Value": [
            st.session_state.total_input,
            st.session_state.total_unique,
            len(dupes),
            success,
            int((all_df["Scrape_Status"] == "Not_Found").sum()),
            int((all_df["Scrape_Status"] == "Failed").sum()),
            int((all_df["Scrape_Status"] == "Blocked").sum()),
            f"{settings.get('delay_min', DEFAULT_DELAY_MIN)}–"
            f"{settings.get('delay_max', DEFAULT_DELAY_MAX)}s",
            settings.get("max_concurrent", 1),
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        ],
    })

    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        all_df.to_excel(   writer, sheet_name="All_Results",    index=False)
        failed_df.to_excel(writer, sheet_name="Failed_Records", index=False)
        summary_df.to_excel(writer, sheet_name="Summary",       index=False)
        dupes_df.to_excel(  writer, sheet_name="Duplicates",    index=False)

        for name, df in [("All_Results", all_df), ("Failed_Records", failed_df),
                          ("Summary", summary_df), ("Duplicates", dupes_df)]:
            _auto_col_widths(writer.sheets[name], df)

    buf.seek(0)
    return buf.read()


# ─────────────────────────────────────────────────────────────────────────────
# Background scraper thread
# ─────────────────────────────────────────────────────────────────────────────

def _guess_type(cid: str) -> str:
    """Detect whether a carrier ID looks like USDOT (digits only) or MC."""
    bare = re.sub(r"^(MC|DOT|USDOT)\s*[#\-\s]?", "", cid, flags=re.IGNORECASE).strip()
    if bare.isdigit() and not cid.upper().startswith("MC"):
        return "USDOT"
    return "MC"


def _scraper_thread(
    carrier_ids: list[str],
    settings: dict[str, Any],
    log_q:  queue.Queue,
    prog_q: queue.Queue,
    result_store: list,
    stop_event: threading.Event,
    _existing_rows: list | None = None,   # rows already done (resume support)
    _dupes_list:    list | None = None,   # duplicates list (for progress save)
) -> None:
    """
    Runs in a daemon thread.
    Processes carriers concurrently (max_concurrent workers).
    Each worker has its own requests.Session.
    skip_type_retry=True skips MC fallback (used for range search).
    Auto-saves progress to disk every 50 carriers for large jobs.
    """
    def _log(level: str, msg: str) -> None:
        log_q.put({
            "t":   datetime.now().strftime("%H:%M:%S"),
            "lvl": level,
            "msg": msg,
        })

    total          = len(carrier_ids)
    flat_rows: list[dict[str, str] | None] = [None] * total
    lock           = threading.Lock()
    completed      = [0]
    processed_ids: set[str] = set()
    _prev_rows     = _existing_rows or []
    _dupes         = _dupes_list or []

    # ── Try Playwright (sync API) as primary engine ────────────────────────
    # Playwright acts like a real browser — bypasses FMCSA IP blocking
    _pw_obj     = None
    _pw_browser = None
    _pw_ctx     = None
    _pw_ok      = False

    try:
        from playwright.sync_api import sync_playwright as _sync_pw
        _pw_obj     = _sync_pw().start()
        _pw_browser = _pw_obj.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--disable-infobars",
                "--disable-extensions",
            ],
        )
        _pw_ctx = _pw_browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            locale="en-US",
            timezone_id="America/Chicago",
            extra_http_headers={
                "Accept-Language":           "en-US,en;q=0.9",
                "Referer":                   "https://safer.fmcsa.dot.gov/",
                "Upgrade-Insecure-Requests": "1",
            },
        )
        # Mask automation fingerprint
        _pw_ctx.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
            "Object.defineProperty(navigator,'plugins',{get:()=>[1,2,3,4,5]});"
            "Object.defineProperty(navigator,'languages',{get:()=>['en-US','en']});"
            "window.chrome={runtime:{}};"
        )
        # Warmup: visit homepage to get real FMCSA session cookies
        _wp = _pw_ctx.new_page()
        _wp.goto("https://safer.fmcsa.dot.gov/CompanySnapshot.aspx",
                 wait_until="domcontentloaded", timeout=30_000)
        _wp.close()
        _pw_ok = True
        _log("success", "✓ Real browser ready — FMCSA IP blocking bypassed")
    except Exception as _e:
        _log("warn", f"Browser unavailable ({str(_e)[:80]}) — using HTTP fallback")
        for _obj in [_pw_browser, _pw_obj]:
            if _obj:
                try:
                    if hasattr(_obj, "close"):
                        _obj.close()
                    else:
                        _obj.stop()
                except Exception:
                    pass
        _pw_ok = False

    # ── Playwright scrape helper ───────────────────────────────────────────
    def _pw_scrape_one(cid: str) -> dict:
        from fmcsa_playwright_scraper import scrape_usdot as _scrape_fn
        page = _pw_ctx.new_page()
        try:
            res = _scrape_fn(
                page, cid,
                delay_min=max(2.0, settings.get("delay_min", 3.0)),
                delay_max=max(4.0, settings.get("delay_max", 7.0)),
            )
        finally:
            try:
                page.close()
            except Exception:
                pass
        # Normalise to match scrape_carrier() result format for _flatten()
        res.setdefault("fetch_method",            "playwright")
        res.setdefault("usdot_status",            res.get("operating_authority_status", ""))
        res.setdefault("mc_mx_raw",               "")
        res.setdefault("state_carrier_id",        "")
        res.setdefault("duns_number",             "")
        res.setdefault("review_date",             "")
        res.setdefault("inspection_stats",        {})
        res.setdefault("out_of_service_percentage", "")
        return res

    # ─────────────────────────────────────────────────────────────────────
    # PATH A — Playwright (sequential, single shared browser)
    # ─────────────────────────────────────────────────────────────────────
    if _pw_ok:
        for i, cid in enumerate(carrier_ids):
            if stop_event.is_set():
                break

            primary = _guess_type(cid)
            _log("info", f"[{i+1}/{total}] Searching {primary}: {cid}")

            try:
                res = _pw_scrape_one(cid)
            except Exception as exc:
                res = {"status": "error", "error_detail": str(exc),
                       "fetch_method": "playwright"}

            # If not found as USDOT (plain digits), retry as MC — same as HTTP path
            if res.get("status") == "not_found" and primary == "USDOT":
                _log("warn", f"  Not found as USDOT → retrying as MC: {cid}")
                try:
                    res = _pw_scrape_one("MC" + cid)
                except Exception as exc2:
                    res = {"status": "error", "error_detail": str(exc2),
                           "fetch_method": "playwright"}

            row           = _flatten(cid, res)
            scrape_status = row["Scrape_Status"]

            if scrape_status == "Success":
                _log("success",
                     f"  ✓  {row['Legal_Name'] or cid} "
                     f"| USDOT {row['USDOT_Number']} | {row['Carrier_Status']}")
            elif scrape_status == "Not_Found":
                _log("warn",  f"  ✗  {cid}  → Not Found")
            elif scrape_status == "Blocked":
                _log("error", f"  ⊘  {cid}  → Blocked / CAPTCHA")
            else:
                _log("error",
                     f"  ✗  {cid}  → {row['Error_Detail'][:80] or 'Error'}")

            with lock:
                flat_rows[i] = row
                completed[0] += 1
                done = completed[0]
                processed_ids.add(cid)

            prog_q.put({"current": done, "total": total,
                        "cid": cid, "status": scrape_status})

            # Auto-save progress every 50 carriers
            if done % 50 == 0:
                remaining   = [c for c in carrier_ids if c not in processed_ids]
                done_so_far = _prev_rows + [r for r in flat_rows if r is not None]
                _save_progress_to_disk(remaining, done_so_far, _dupes)
                _log("info", f"💾 Progress saved ({done}/{total})")

        # Close browser when done
        try:
            _pw_browser.close()
        except Exception:
            pass
        try:
            _pw_obj.stop()
        except Exception:
            pass

    # ─────────────────────────────────────────────────────────────────────
    # PATH B — HTTP requests fallback (when Playwright unavailable)
    # ─────────────────────────────────────────────────────────────────────
    else:
        max_workers      = min(int(settings.get("max_concurrent", 2)), 3)
        skip_type_retry  = settings.get("skip_type_retry", False)
        pw_enabled       = settings.get("playwright_fallback", False)
        conn_failures    = [0]
        _IP_BLOCK_WARNED = [False]

        def _process_one(i: int, cid: str) -> None:
            if stop_event.is_set():
                return

            primary  = _guess_type(cid)
            fallback = "MC" if primary == "USDOT" else "USDOT"
            sess     = _make_session(proxy=settings.get("proxy"))
            _warm_session(sess)

            _log("info", f"[{i+1}/{total}] Searching {primary}: {cid}")

            web_key = settings.get("web_key", "")
            try:
                res = scrape_carrier(
                    cid, primary,
                    headless=settings.get("headless", True),
                    delay_min=settings.get("delay_min", DEFAULT_DELAY_MIN),
                    delay_max=settings.get("delay_max", DEFAULT_DELAY_MAX),
                    use_playwright_fallback=pw_enabled,
                    include_raw_html=False,
                    web_key=web_key,
                    _session=sess,
                )
            except Exception as exc:
                res = {"status": "error", "error_detail": str(exc)}

            if res.get("fetch_method") == "api":
                _log("info", "  → FMCSA API used")

            if res.get("status") == "not_found" and not skip_type_retry:
                _log("warn", f"  Not found as {primary} → retrying as {fallback} …")
                time.sleep(random.uniform(3, 6))
                try:
                    res = scrape_carrier(
                        cid, fallback,
                        headless=settings.get("headless", True),
                        delay_min=settings.get("delay_min", DEFAULT_DELAY_MIN),
                        delay_max=settings.get("delay_max", DEFAULT_DELAY_MAX),
                        use_playwright_fallback=pw_enabled,
                        include_raw_html=False,
                        web_key=web_key,
                        _session=sess,
                    )
                except Exception as exc:
                    res = {"status": "error",
                           "error_detail": f"Fallback failed: {exc}"}

            sess.close()
            row           = _flatten(cid, res)
            scrape_status = row["Scrape_Status"]

            if scrape_status == "Success":
                _log("success",
                     f"  ✓  {row['Legal_Name'] or cid} "
                     f"| USDOT {row['USDOT_Number']} | {row['Carrier_Status']}")
                with lock:
                    conn_failures[0] = 0
            elif scrape_status == "Not_Found":
                _log("warn",  f"  ✗  {cid}  → Not Found")
                with lock:
                    conn_failures[0] = 0
            elif scrape_status == "Blocked":
                _log("error", f"  ⊘  {cid}  → Blocked / CAPTCHA")
            else:
                err_detail  = row["Error_Detail"] or ""
                is_conn_err = "Max retries" in err_detail or "ConnectionError" in err_detail
                with lock:
                    if is_conn_err:
                        conn_failures[0] += 1
                        if conn_failures[0] >= 3 and not _IP_BLOCK_WARNED[0]:
                            _IP_BLOCK_WARNED[0] = True
                            _log("error",
                                 "⚠️  FMCSA is blocking this IP — "
                                 "try again later or use smaller batches.")
                    else:
                        conn_failures[0] = 0
                _log("error",
                     f"  ✗  {cid}  → {err_detail[:80] or 'Connection failed'}")

            with lock:
                flat_rows[i] = row
                completed[0] += 1
                done = completed[0]
                processed_ids.add(cid)

            prog_q.put({"current": done, "total": total,
                        "cid": cid, "status": scrape_status})

            # Auto-save progress every 50 carriers
            if done % 50 == 0:
                remaining   = [c for c in carrier_ids if c not in processed_ids]
                done_so_far = _prev_rows + [r for r in flat_rows if r is not None]
                _save_progress_to_disk(remaining, done_so_far, _dupes)
                _log("info", f"💾 Progress saved ({done}/{total})")

        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(_process_one, i, cid): cid
                for i, cid in enumerate(carrier_ids)
            }
            for fut in concurrent.futures.as_completed(futures):
                if stop_event.is_set():
                    break
                try:
                    fut.result()
                except Exception:
                    pass

    finished = [r for r in flat_rows if r is not None]
    result_store.clear()
    result_store.extend(finished)
    _log("success", "─" * 50)
    if stop_event.is_set():
        _log("warn", f"Stopped — {len(finished)} records saved (partial results).")
    else:
        _log("success", f"Done! {len(finished)} records processed successfully.")


# ─────────────────────────────────────────────────────────────────────────────
# Queue drainer  (called every rerun while scraping is active)
# ─────────────────────────────────────────────────────────────────────────────

_LOG_CSS = {"info": "li", "success": "ls", "warn": "lw", "error": "le", "debug": "ld"}


def _render_log_line(entry: dict) -> str:
    cls = _LOG_CSS.get(entry["lvl"], "li")
    msg = entry["msg"].replace("<", "&lt;").replace(">", "&gt;")
    return f'<span class="log-line"><span class="lt">{entry["t"]}</span><span class="{cls}">{msg}</span></span>'


# ─────────────────────────────────────────────────────────────────────────────
# Disk-based progress save / resume  (for large 5000+ carrier jobs)
# ─────────────────────────────────────────────────────────────────────────────

_PROGRESS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scrape_progress.json")


def _save_progress_to_disk(remaining_ids: list, completed_rows: list, dupes: list) -> None:
    """Save current scraping progress to disk so it can be resumed later."""
    try:
        data = {
            "saved_at":       datetime.now().isoformat(),
            "remaining_ids":  remaining_ids,
            "completed_rows": completed_rows,
            "dupes":          dupes,
            "done_count":     len(completed_rows),
            "remaining_count": len(remaining_ids),
        }
        with open(_PROGRESS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, default=str)
    except Exception:
        pass  # progress save failure is non-fatal


def _load_progress_from_disk() -> dict | None:
    """Load saved progress file if it exists and is valid."""
    if not os.path.exists(_PROGRESS_FILE):
        return None
    try:
        with open(_PROGRESS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if data.get("remaining_ids") or data.get("completed_rows"):
            return data
    except Exception:
        pass
    return None


def _clear_progress_file() -> None:
    """Delete the progress file after a completed run."""
    try:
        if os.path.exists(_PROGRESS_FILE):
            os.remove(_PROGRESS_FILE)
    except Exception:
        pass


def _drain_queues() -> None:
    """Pull all pending queue items into session_state. Check thread liveness."""
    # Logs
    lq = st.session_state.log_q
    if lq:
        while not lq.empty():
            st.session_state.log_lines.append(_render_log_line(lq.get_nowait()))

    # Progress
    pq = st.session_state.prog_q
    if pq:
        while not pq.empty():
            p = pq.get_nowait()
            st.session_state.prog_current = p["current"]
            st.session_state.prog_total   = p["total"]
            st.session_state.last_id      = p["cid"]
            s = p["status"]
            c = st.session_state.counts
            if s == "Success":   c["success"]   += 1
            elif s == "Not_Found": c["not_found"] += 1
            elif s == "Blocked":   c["blocked"]   += 1
            else:                  c["failed"]    += 1

    # Thread finished?
    thread = st.session_state.scrape_thread
    if thread and not thread.is_alive():
        st.session_state.is_scraping = False
        # Compute and save total elapsed time
        if st.session_state.scrape_start_time and st.session_state.scrape_elapsed is None:
            st.session_state.scrape_elapsed = time.time() - st.session_state.scrape_start_time
        new_rows  = st.session_state.result_store
        # Merge with any rows that were completed in a previous partial run (resume)
        prev_rows = st.session_state.get("_resume_existing_rows", [])
        all_rows  = prev_rows + new_rows
        if all_rows:
            settings = st.session_state.get("_settings", {})
            st.session_state.output_bytes = _build_excel(
                all_rows, st.session_state.dupes_removed, settings
            )
            st.session_state.results_rows = all_rows
        # Only clear progress file on natural completion — keep it if user pressed Stop
        # so they can resume later from the last checkpoint.
        _stop_ev = st.session_state.get("stop_event")
        if not (_stop_ev and _stop_ev.is_set()):
            _clear_progress_file()


# ─────────────────────────────────────────────────────────────────────────────
# UI helpers
# ─────────────────────────────────────────────────────────────────────────────

def _metric(val: Any, label: str, colour: str) -> str:
    return (
        f'<div class="mc-card">'
        f'<div class="val {colour}">{val}</div>'
        f'<div class="lbl">{label}</div>'
        f'</div>'
    )


def _cards(*items: tuple[Any, str, str]) -> None:
    st.markdown(
        '<div class="mc-row">' +
        "".join(_metric(v, l, c) for v, l, c in items) +
        "</div>",
        unsafe_allow_html=True,
    )


# ─────────────────────────────────────────────────────────────────────────────
# ── SIDEBAR ──────────────────────────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────────────────────

with st.sidebar:
    st.markdown(
        '<div style="text-align:center;padding:18px 0 10px">'
        '<span class="truck-icon" style="font-size:2.8rem">🚛</span><br>'
        '<span style="font-size:1.05rem;font-weight:700;color:#f1f5f9">'
        'Dispatch DOS</span><br>'
        '<span style="font-size:.75rem;color:#64748b">FMCSA Bulk Scraper</span>'
        '</div>',
        unsafe_allow_html=True,
    )
    st.markdown("---")

    st.markdown('<div class="sb-lbl">⚙️ Scraper Settings</div>', unsafe_allow_html=True)

    delay_min = st.slider("Min delay (seconds)", 2, 40, 3, 1,
                          help="Min pause between FMCSA requests.")
    delay_max = st.slider("Max delay (seconds)", delay_min, 60,
                          max(8, delay_min + 2), 1,
                          help="Max pause between FMCSA requests.")

    st.markdown('<div class="sb-lbl">🔑 FMCSA API Key (Recommended)</div>',
                unsafe_allow_html=True)
    fmcsa_web_key = st.text_input(
        "FMCSA Web Key", value="", type="password",
        placeholder="Paste your free API key here",
        help="Get a FREE key at li.fmcsa.dot.gov — bypasses IP blocking completely.",
        label_visibility="collapsed",
    )
    if fmcsa_web_key:
        st.markdown(
            '<div style="font-size:.72rem;color:#22c55e;margin-top:-8px">'
            '✓ API mode — no IP blocking</div>',
            unsafe_allow_html=True,
        )
    else:
        st.markdown(
            '<div style="font-size:.72rem;color:#f59e0b;margin-top:-8px">'
            '⚠ No key — uses HTML scraping (may get blocked)</div>',
            unsafe_allow_html=True,
        )

    st.markdown('<div class="sb-lbl">🛡️ Options</div>', unsafe_allow_html=True)

    playwright_fallback = st.toggle(
        "Browser fallback", value=False,
        help="Use a real browser if the fast method gets blocked (experimental).",
    )

    st.markdown('<div class="sb-lbl">🔄 Proxy (optional)</div>', unsafe_allow_html=True)
    proxy_input = st.text_input(
        "Proxy URL", value="", placeholder="http://user:pass@host:port",
        help="Optional HTTP/HTTPS proxy. Leave blank to use your direct IP.",
        label_visibility="collapsed",
    )
    if proxy_input.strip():
        st.markdown(
            '<div style="font-size:.72rem;color:#22c55e;margin-top:-8px">'
            '✓ Proxy set</div>',
            unsafe_allow_html=True,
        )

    st.markdown("---")
    render_ai_sidebar()

    st.markdown("---")
    st.markdown('<div class="sb-lbl">ℹ️ About</div>', unsafe_allow_html=True)
    st.markdown(
        '<div style="font-size:.76rem;color:#64748b;line-height:1.6">'
        'Data from <code>safer.fmcsa.dot.gov</code><br>'
        'Delay: {d_min}–{d_max}s between requests'
        '</div>'.format(d_min=delay_min, d_max=delay_max),
        unsafe_allow_html=True,
    )

# Pack settings for use by the scraper thread
_current_settings: dict[str, Any] = {
    "delay_min":           delay_min,
    "delay_max":           delay_max,
    "max_concurrent":      3,
    "playwright_fallback": playwright_fallback,
    "headless":            True,
    "proxy":               proxy_input.strip() or None,
    "skip_type_retry":     False,   # set True for range search
    "web_key":             fmcsa_web_key.strip(),
}


# ─────────────────────────────────────────────────────────────────────────────
# ── MAIN CONTENT ─────────────────────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────────────────────

# ── Tabs ────────────────────────────────────────────────────────────────────────
_tab_fmcsa, _tab_ai = st.tabs(["🚛 FMCSA Scraper", "🤖 AI Scraper"])

with _tab_ai:
    render_ai_tab(groq_key=st.session_state.get("sb_groq_key", ""))

with _tab_fmcsa:
    # ── Banner ────────────────────────────────────────────────────────────────────
    st.markdown(
        '<div class="ds-header">'
        '<span style="font-size:3rem">🚛</span>'
        '<div>'
        '<h1>FMCSA Bulk Carrier Lookup</h1>'
        '<p>Upload carrier list → deduplicate → scrape FMCSA → download enriched Excel</p>'
        '</div>'
        '<span class="ds-badge">Dispatch DOS</span>'
        '</div>',
        unsafe_allow_html=True,
    )
    
    # ── Feature Stats Bar ─────────────────────────────────────────────────────────
    st.markdown("""
    <div class="stats-bar">
      <div class="stat-card sc-blue">
        <span class="sc-icon">🚛</span>
        <div class="sc-val">5000+</div>
        <div class="sc-lbl">Carriers Per Run</div>
        <div class="sc-sub">Bulk scrape in one go</div>
      </div>
      <div class="stat-card sc-green">
        <span class="sc-icon">📊</span>
        <div class="sc-val">30+</div>
        <div class="sc-lbl">Data Fields</div>
        <div class="sc-sub">Name · Address · Safety · Fleet</div>
      </div>
      <div class="stat-card sc-purple">
        <span class="sc-icon">⚡</span>
        <div class="sc-val">Auto</div>
        <div class="sc-lbl">Dedup + Detect</div>
        <div class="sc-sub">MC / USDOT auto-detected</div>
      </div>
      <div class="stat-card sc-orange">
        <span class="sc-icon">📥</span>
        <div class="sc-val">Free</div>
        <div class="sc-lbl">Excel + CSV Export</div>
        <div class="sc-sub">4-sheet report, instant download</div>
      </div>
    </div>
    """, unsafe_allow_html=True)
    
    # ── How-to Guide Cards ────────────────────────────────────────────────────────
    st.markdown("""
    <div class="howto-grid">
    
      <div class="howto-card">
        <div class="howto-num">1</div>
        <span class="howto-icon">📋</span>
        <div class="howto-title">Load Carrier List</div>
        <div class="howto-desc">
          Paste MC or USDOT numbers — one per line, or comma separated.
          Or upload an <b>Excel / CSV</b> file directly.
        </div>
      </div>
    
      <div class="howto-card">
        <div class="howto-num">2</div>
        <span class="howto-icon">🔍</span>
        <div class="howto-title">Preview & Dedup</div>
        <div class="howto-desc">
          Tool auto-removes duplicates and shows a preview.
          Review your list before scraping starts.
        </div>
      </div>
    
      <div class="howto-card">
        <div class="howto-num">3</div>
        <span class="howto-icon">🚀</span>
        <div class="howto-title">Start Scraping</div>
        <div class="howto-desc">
          Click <b>▶ Start Scraping</b>. Watch live logs.
          Hit <b>⏹ Stop</b> anytime — partial results are always saved.
        </div>
      </div>
    
      <div class="howto-card">
        <div class="howto-num">4</div>
        <span class="howto-icon">📥</span>
        <div class="howto-title">Download Report</div>
        <div class="howto-desc">
          Get a full <b>Excel report</b> (4 sheets) + Active Only <b>CSV</b>.
          Filter by status before downloading.
        </div>
      </div>
    
    </div>
    
    <div class="howto-tip">
      <div class="howto-tip-title">💡 Pro Tips</div>
      <ul>
        <li>Plain numbers (<code>1597181</code>) or prefixed (<code>MC193369</code>) — both work</li>
        <li>Tool auto-detects USDOT vs MC and retries if not found</li>
        <li>Add a free FMCSA API key (sidebar) to bypass IP blocking completely</li>
        <li>Enable <b>Browser Fallback</b> in sidebar if you see many Blocked results</li>
      </ul>
    </div>
    """, unsafe_allow_html=True)
    
    
    # ── Visual Step Stepper ───────────────────────────────────────────────────────
    def _render_stepper() -> None:
        has_ids     = bool(st.session_state.carrier_ids)
        is_scraping = st.session_state.is_scraping
        has_results = bool(st.session_state.results_rows)
    
        if has_results:
            active = 4
        elif is_scraping:
            active = 3
        elif has_ids:
            active = 2
        else:
            active = 1
    
        steps = [
            ("1", "Load List",  "Paste / Upload IDs"),
            ("2", "Preview",    "Dedup & review"),
            ("3", "Scraping",   "Live FMCSA fetch"),
            ("4", "Download",   "Excel + CSV ready"),
        ]
    
        def _cls(n: int) -> str:
            if n < active:  return "step-wrap sw-done"
            if n == active: return "step-wrap sw-active"
            return "step-wrap"
    
        def _circle(n: int, label: str) -> str:
            if n < active:
                return '<div class="step-circle">✓</div>'
            return f'<div class="step-circle">{label}</div>'
    
        html = '<div class="stepper">'
        for i, (num, lbl, sub) in enumerate(steps, 1):
            html += (
                f'<div class="{_cls(i)}">'
                f'{_circle(i, num)}'
                f'<div class="step-label">{lbl}</div>'
                f'<div class="step-sub">{sub}</div>'
                f'</div>'
            )
        html += '</div>'
        st.markdown(html, unsafe_allow_html=True)
    
    _render_stepper()
    
    
    # ─────────────────────────────────────────────────────────────────────────────
    # SECTION 1 — Input
    # ─────────────────────────────────────────────────────────────────────────────
    
    st.markdown('<div class="sec-head">📂 Step 1 — Load Carrier List</div>',
                unsafe_allow_html=True)
    
    # ── Resume banner — shown when a saved checkpoint exists ─────────────────────
    _saved_prog = _load_progress_from_disk()
    if _saved_prog and not st.session_state.is_scraping:
        _done_c   = _saved_prog.get("done_count", 0)
        _rem_c    = _saved_prog.get("remaining_count", 0)
        _saved_at = _saved_prog.get("saved_at", "")[:16].replace("T", " ")
        st.markdown(
            f'<div class="warn-box" style="margin-bottom:14px;">'
            f'<b>💾 Unfinished job found</b> — saved at {_saved_at} &nbsp;·&nbsp; '
            f'<b>{_done_c}</b> done &nbsp;·&nbsp; <b>{_rem_c}</b> remaining'
            f'</div>',
            unsafe_allow_html=True,
        )
        _rb1, _rb2, _ = st.columns([2, 2, 4])
        if _rb1.button("▶ Resume Previous Job", type="primary", key="btn_resume"):
            _rem_ids  = _saved_prog.get("remaining_ids", [])
            _prev_done = _saved_prog.get("completed_rows", [])
            _prev_dupes = _saved_prog.get("dupes", [])
            unique, _, _ = _process_input(_rem_ids)
            st.session_state.carrier_ids           = unique
            st.session_state.dupes_removed         = _prev_dupes
            st.session_state.total_input           = _done_c + _rem_c
            st.session_state.total_unique          = _done_c + len(unique)
            st.session_state._resume_existing_rows = _prev_done
            st.session_state.results_rows          = []
            st.session_state.output_bytes          = None
            st.session_state.counts                = {
                "success":   sum(1 for r in _prev_done if r.get("Scrape_Status") == "Success"),
                "not_found": sum(1 for r in _prev_done if r.get("Scrape_Status") == "Not_Found"),
                "failed":    sum(1 for r in _prev_done if r.get("Scrape_Status") == "Failed"),
                "blocked":   sum(1 for r in _prev_done if r.get("Scrape_Status") == "Blocked"),
            }
            st.session_state.log_lines = []
            st.rerun()
        if _rb2.button("Discard & Start Fresh", type="secondary", key="btn_discard"):
            _clear_progress_file()
            st.rerun()
    
    tab_enter, tab_upload = st.tabs(["📋 Enter Carriers", "📎 Upload Excel / CSV"])
    
    raw_ids_from_input: list[str] = []
    input_triggered = False
    
    # ── Tab A: Enter Carriers (Paste + Range combined) ────────────────────────────
    with tab_enter:
        input_mode = st.radio(
            "How do you want to enter carriers?",
            ["✏️ Paste Numbers", "🔢 Range Search"],
            horizontal=True,
            label_visibility="collapsed",
        )
    
        st.markdown("<div style='margin-top:0.5rem'></div>", unsafe_allow_html=True)
    
        if input_mode == "✏️ Paste Numbers":
            # ── Live ID counter (reads textarea value from session state) ──────────
            _raw_paste = st.session_state.get("paste_area", "") or ""
            _live_ids  = [v.strip() for v in re.split(r"[\n,;\t]+", _raw_paste)
                          if v.strip() and len(v.strip()) > 1]
            _live_count = len(_live_ids)
            _live_dupes = _live_count - len(set(v.upper() for v in _live_ids))
    
            if _live_count == 0:
                _badge_cls  = "empty"
                _badge_text = "📋 Paste IDs below"
            else:
                _badge_cls  = "has-ids"
                _badge_text = f"✓ {_live_count} IDs detected"
            _dupe_html = (
                f'<span class="id-counter-dupe">· {_live_dupes} duplicate{"s" if _live_dupes != 1 else ""}</span>'
                if _live_dupes > 0 else ""
            )
    
            st.markdown(
                f'<div class="id-counter-row">'
                f'  <span class="id-counter-label">Carrier IDs</span>'
                f'  <span>'
                f'    <span class="id-counter-badge {_badge_cls}">{_badge_text}</span>'
                f'    {_dupe_html}'
                f'  </span>'
                f'</div>',
                unsafe_allow_html=True,
            )
    
            pasted = st.text_area(
                "Paste carrier IDs", height=200,
                placeholder="MC193369\n1597181\n793594\nMC123456, MC789012\n...",
                label_visibility="collapsed", key="paste_area",
            )
    
            if _live_count > 0:
                st.markdown(
                    f'<div class="info-box" style="margin-top:6px;">'
                    f'One entry per line · comma/semicolon/tab separated · '
                    f'<b>{_live_count} IDs ready</b>'
                    + (f' · <span style="color:#d97706">{_live_dupes} duplicate{"s" if _live_dupes!=1 else ""} will be removed</span>' if _live_dupes else "")
                    + '</div>',
                    unsafe_allow_html=True,
                )
            else:
                st.markdown(
                    '<div class="info-box">One entry per line — or comma/semicolon separated. '
                    'Accepts plain numbers, MC prefix, DOT prefix.</div>',
                    unsafe_allow_html=True,
                )
    
            if st.button("Process List", type="primary", key="btn_paste"):
                if not pasted.strip():
                    st.warning("Please paste at least one carrier ID.")
                else:
                    raw_ids_from_input = re.split(r"[\n,;\t]+", pasted)
                    input_triggered = True
    
        else:  # Range Search
            st.markdown(
                '<div class="info-box">Enter a <b>Start</b> and <b>End</b> number — '
                'the tool auto-generates every number in between and scrapes them all.</div>',
                unsafe_allow_html=True,
            )
            r1, r2 = st.columns(2)
            range_start = r1.number_input("Start Number", min_value=1, value=1000,
                                           step=1, key="range_start")
            range_end   = r2.number_input("End Number",   min_value=1, value=5000,
                                           step=1, key="range_end")
    
            range_count = int(range_end) - int(range_start) + 1
            _MAX_RANGE  = 5000
    
            if range_end >= range_start:
                # Range search always uses 1 worker
                est_range_min = max(1, int(range_count * (delay_min + delay_max) / 2 / 60))
                est_range_max = max(1, int(range_count * delay_max / 60))
                if range_count <= _MAX_RANGE:
                    _est_hrs = est_range_max // 60
                    _est_rem = est_range_max % 60
                    _est_str = (f"~{_est_hrs}h {_est_rem}m" if _est_hrs else f"~{est_range_min}–{est_range_max} min")
                    st.markdown(
                        f'<div class="info-box">'
                        f'📦 <b>{range_count} numbers</b> will be generated '
                        f'({int(range_start)} → {int(range_end)}) &nbsp;·&nbsp; '
                        f'⏱ Est. time: <b>{_est_str}</b> &nbsp;·&nbsp; '
                        f'💾 Auto-save every 50 carriers'
                        f'</div>',
                        unsafe_allow_html=True,
                    )
                else:
                    st.warning(
                        f"Range too large: {range_count} numbers. "
                        f"Maximum allowed is {_MAX_RANGE}. Please reduce the range."
                    )
    
            if st.button("Generate & Load Range", type="primary", key="btn_range"):
                if range_end < range_start:
                    st.error("End number must be ≥ Start number.")
                elif range_count > _MAX_RANGE:
                    st.error(f"Range too large ({range_count}). Maximum is {_MAX_RANGE}.")
                else:
                    raw_ids_from_input = [str(n) for n in range(int(range_start),
                                                                  int(range_end) + 1)]
                    input_triggered = True
                    st.session_state["_range_search"] = True
                    st.success(f"Generated {range_count} numbers "
                               f"({int(range_start)} → {int(range_end)}).")
    
    # ── Tab B: File Upload ────────────────────────────────────────────────────────
    with tab_upload:
        st.markdown(
            '<div class="info-box">Upload <b>.xlsx</b> or <b>.csv</b>. '
            'The scraper auto-detects the MC/DOT column (or specify it below).</div>',
            unsafe_allow_html=True,
        )
        uploaded = st.file_uploader(
            "Drop file here", type=["xlsx", "csv"],
            label_visibility="collapsed", key="file_upload",
        )
        col_hint = st.text_input(
            "Column name (optional)",
            placeholder="e.g.  MC Number   ← leave blank to auto-detect",
            key="col_hint",
        )
        if uploaded:
            if st.button("Process File", type="primary", key="btn_file"):
                try:
                    if uploaded.name.endswith(".csv"):
                        df_raw = pd.read_csv(uploaded, dtype=str)
                    else:
                        df_raw = pd.read_excel(uploaded, dtype=str)
                    if df_raw.empty:
                        st.error("File appears to be empty.")
                    else:
                        col = (col_hint.strip()
                               if col_hint.strip() and col_hint.strip() in df_raw.columns
                               else _detect_column(df_raw))
                        raw_ids_from_input = df_raw[col].dropna().astype(str).tolist()
                        input_triggered = True
                        st.success(f"Loaded **{len(raw_ids_from_input)}** rows from column **'{col}'**.")
                except Exception as exc:
                    st.error(f"Could not read file: {exc}")
    
    
    # ── Apply dedup when input is submitted ───────────────────────────────────────
    if input_triggered and raw_ids_from_input:
        unique, dupes, total_in = _process_input(raw_ids_from_input)
        st.session_state.carrier_ids    = unique
        st.session_state.dupes_removed  = dupes
        st.session_state.total_input    = total_in
        st.session_state.total_unique   = len(unique)
        # Reset any previous results
        st.session_state.results_rows        = []
        st.session_state.output_bytes        = None
        st.session_state.counts              = {"success": 0, "not_found": 0,
                                                "failed": 0, "blocked": 0}
        st.session_state.log_lines           = []
        # New input = fresh run — clear resume state so old data doesn't bleed in
        st.session_state._resume_existing_rows = []
    
    
    # ─────────────────────────────────────────────────────────────────────────────
    # SECTION 2 — Preview & Dedup Stats
    # ─────────────────────────────────────────────────────────────────────────────
    
    if st.session_state.carrier_ids:
        st.markdown('<hr class="div">', unsafe_allow_html=True)
        st.markdown('<div class="sec-head">📊 Step 2 — Preview & Deduplication</div>',
                    unsafe_allow_html=True)
    
        # Metric cards
        _cards(
            (st.session_state.total_input,  "Total Input",        "c-blue"),
            (st.session_state.total_unique, "Unique Records",     "c-green"),
            (len(st.session_state.dupes_removed), "Duplicates Removed", "c-yellow"),
        )
    
        # Preview table
        preview_n = min(50, len(st.session_state.carrier_ids))
        st.dataframe(
            pd.DataFrame({"#": range(1, preview_n + 1),
                          "Carrier_ID": st.session_state.carrier_ids[:preview_n]}),
            use_container_width=True, height=200, hide_index=True,
            column_config={
                "#":          st.column_config.NumberColumn(width="small"),
                "Carrier_ID": st.column_config.TextColumn("Carrier ID", width="large"),
            },
        )
        if len(st.session_state.carrier_ids) > preview_n:
            st.caption(f"Showing first {preview_n} of "
                       f"{len(st.session_state.carrier_ids)} unique records.")
    
        # Duplicates expander
        if st.session_state.dupes_removed:
            with st.expander(f"🔍 {len(st.session_state.dupes_removed)} duplicates removed"):
                st.dataframe(
                    pd.DataFrame({"Duplicate": st.session_state.dupes_removed}),
                    use_container_width=True, height=180, hide_index=True,
                )
    
    
    # ─────────────────────────────────────────────────────────────────────────────
    # SECTION 3 — Scraping
    # ─────────────────────────────────────────────────────────────────────────────
    
    if st.session_state.carrier_ids:
        st.markdown('<hr class="div">', unsafe_allow_html=True)
        st.markdown('<div class="sec-head">🚀 Step 3 — Scrape FMCSA</div>',
                    unsafe_allow_html=True)
    
        n_carriers   = len(st.session_state.carrier_ids)
        avg_delay    = (delay_min + delay_max) / 2
        # Use 1 worker for range search (set before thread start), 3 for paste/upload
        _is_range    = st.session_state.get("_range_search", False)
        _workers     = 1 if _is_range else _current_settings.get("max_concurrent", 3)
        est_min      = max(1, int(n_carriers * avg_delay / 60 / _workers))
        est_max      = max(1, int(n_carriers * delay_max / 60 / _workers))
    
        # Settings summary + prominent time estimate
        c1, c2 = st.columns(2)
        c1.metric("Records to Scrape", n_carriers)
        c2.metric("Delay Between Requests", f"{delay_min}–{delay_max}s")
    
        st.markdown(
            f'<div class="info-box" style="margin-top:0.6rem;">'
            f'⏱ <b>Estimated completion time:</b> '
            f'<span style="color:#3b82f6;font-size:1.05em;font-weight:700;">'
            f'~{est_min}–{est_max} minutes</span> '
            f'<span style="color:#64748b;font-size:0.88em;">'
            f'({n_carriers} carriers · {_workers} workers · {delay_min}–{delay_max}s delay)'
            f'</span> — keep this tab open while scraping'
            f'</div>',
            unsafe_allow_html=True,
        )
    
        # ── NOT SCRAPING — show Start button ─────────────────────────────────────
        if not st.session_state.is_scraping:
            btn_col, _ = st.columns([2, 6])
            if btn_col.button("▶  Start Scraping", type="primary",
                              use_container_width=True, key="btn_start"):
                # Initialise runtime objects
                lq = queue.Queue()
                pq = queue.Queue()
                rs: list = []
                stop_ev = threading.Event()
    
                st.session_state.log_q        = lq
                st.session_state.prog_q       = pq
                st.session_state.result_store = rs
                st.session_state.stop_event   = stop_ev
                st.session_state.log_lines    = []
                st.session_state.prog_current = 0
                st.session_state.prog_total   = n_carriers
                st.session_state.last_id      = ""
                st.session_state.counts       = {"success": 0, "not_found": 0,
                                                  "failed": 0, "blocked": 0}
                st.session_state.results_rows  = []
                st.session_state.output_bytes  = None
                st.session_state._settings     = _current_settings
                st.session_state.scrape_elapsed = None
                st.session_state["_toast_shown"] = False
    
                # Fresh run (not a resume) → clear any leftover progress file
                if not st.session_state.get("_resume_existing_rows"):
                    _clear_progress_file()
    
                # Apply range-search optimisations BEFORE thread starts (avoids race condition)
                if st.session_state.get("_range_search", False):
                    _current_settings["skip_type_retry"] = True
                    _current_settings["max_concurrent"]  = 1  # 1 worker to avoid IP blocking
                    st.session_state["_range_search"] = False
    
                thread = threading.Thread(
                    target=_scraper_thread,
                    args=(
                        st.session_state.carrier_ids,
                        _current_settings,
                        lq, pq, rs, stop_ev,
                        st.session_state.get("_resume_existing_rows", []),
                        st.session_state.dupes_removed,
                    ),
                    daemon=True,
                )
                thread.start()
                st.session_state.scrape_thread   = thread
                st.session_state.is_scraping     = True
                st.session_state.scrape_start_time = time.time()
                st.rerun()
    
        # ── ACTIVELY SCRAPING — live progress ─────────────────────────────────────
        else:
            _drain_queues()
    
            cur    = st.session_state.prog_current
            total  = st.session_state.prog_total
            counts = st.session_state.counts
            pct    = cur / total if total > 0 else 0
    
            # 2. Live pulse dot badge
            st.markdown(
                '<div class="live-badge">'
                '<span class="live-dot"></span> LIVE — Scraping in progress'
                '</div>',
                unsafe_allow_html=True,
            )
    
            # Stop button
            s_col, _ = st.columns([2, 6])
            if s_col.button("⏹  Stop Scraping", type="secondary",
                            use_container_width=True, key="btn_stop"):
                ev = st.session_state.stop_event
                if ev:
                    ev.set()
    
            st.markdown("<br>", unsafe_allow_html=True)
    
            # Progress bar + status text + ETA
            st.progress(pct)
            last = st.session_state.last_id
            eta_str = ""
            elapsed_str = ""
            start_t = st.session_state.scrape_start_time
            if start_t and cur > 0:
                elapsed   = time.time() - start_t
                avg_per   = elapsed / cur
                remaining = avg_per * (total - cur)
                elapsed_str = (f"{int(elapsed//60)}m {int(elapsed%60)}s"
                               if elapsed >= 60 else f"{int(elapsed)}s")
                remain_min  = int(remaining // 60)
                remain_sec  = int(remaining % 60)
                eta_str     = (f"~{remain_min}m {remain_sec}s remaining"
                               if remain_min > 0 else f"~{remain_sec}s remaining")
            st.markdown(
                f"**{cur} / {total}** scraped"
                + (f"  ·  last: `{last}`" if last else "")
                + (f"  ·  ⏱ elapsed: **{elapsed_str}**  ·  ETA: **{eta_str}**"
                   if eta_str else ""),
            )
    
            # Live counter cards
            _cards(
                (counts["success"],   "Success",   "c-green"),
                (counts["not_found"], "Not Found", "c-yellow"),
                (counts["failed"],    "Failed",    "c-red"),
                (counts["blocked"],   "Blocked",   "c-purple"),
                (total - cur,         "Remaining", "c-slate"),
            )
    
            # Live log
            with st.expander("📋 Live Logs", expanded=True):
                last_120 = "".join(st.session_state.log_lines[-120:])
                st.markdown(
                    f'<div class="log-box">{last_120}</div>',
                    unsafe_allow_html=True,
                )
    
            # Keep polling until thread is done
            if st.session_state.is_scraping:
                time.sleep(1.5)
                st.rerun()
    
    
    # ─────────────────────────────────────────────────────────────────────────────
    # SECTION 4 — Results
    # ─────────────────────────────────────────────────────────────────────────────
    
    rows = st.session_state.results_rows
    if rows:
        st.markdown('<hr class="div">', unsafe_allow_html=True)
        st.markdown('<div class="sec-head">✅ Step 4 — Results</div>',
                    unsafe_allow_html=True)
    
        results_df = pd.DataFrame(rows, columns=OUTPUT_COLS)
        counts     = st.session_state.counts
    
        # ── Toast notification (shows only once per run) ───────────────────────────
        if not st.session_state.get("_toast_shown", False):
            st.session_state["_toast_shown"] = True
            st.toast(
                f"✅ Done! {counts['success']} carriers found out of {len(results_df)} processed.",
                icon="🚛",
            )
    
        # ── Compute summary numbers ────────────────────────────────────────────────
        status_series  = results_df["Carrier_Status"].str.upper()
        n_active       = int((status_series == "ACTIVE").sum())
        n_oos          = int((status_series == "OUT_OF_SERVICE").sum())
        n_inactive     = int((status_series == "INACTIVE").sum())
        n_total        = len(results_df)
        n_success      = counts["success"]
        n_not_found    = counts["not_found"]
        success_rate   = round(n_success / n_total * 100, 1) if n_total else 0
    
        elapsed_sec = st.session_state.get("scrape_elapsed")
        if elapsed_sec:
            elapsed_min = int(elapsed_sec // 60)
            elapsed_s   = int(elapsed_sec % 60)
            time_str    = f"{elapsed_min}m {elapsed_s}s" if elapsed_min else f"{elapsed_s}s"
        else:
            time_str = ""
    
        # ── Rich summary banner ────────────────────────────────────────────────────
        pills_html = (
            f'<span class="sum-pill sum-pill-rate">📊 {success_rate}% success rate</span>'
            f'<span class="sum-pill sum-pill-active">🟢 {n_active} Active</span>'
            f'<span class="sum-pill sum-pill-oos">🔴 {n_oos} Out of Service</span>'
            f'<span class="sum-pill sum-pill-inactive">🟡 {n_inactive} Inactive</span>'
            f'<span class="sum-pill sum-pill-notfound">⚫ {n_not_found} Not Found</span>'
            + (f'<span class="sum-pill sum-pill-time">⏱ {time_str}</span>' if time_str else "")
        )
        st.markdown(
            f'<div class="sum-banner">'
            f'  <div class="sum-banner-top">✅ Scraping Complete — {n_success} carriers found out of {n_total}</div>'
            f'  <div class="sum-banner-pills">{pills_html}</div>'
            f'</div>',
            unsafe_allow_html=True,
        )
    
        # Summary metrics
        _cards(
            (len(results_df),         "Total Processed", "c-blue"),
            (counts["success"],        "Success",         "c-green"),
            (counts["not_found"],      "Not Found",       "c-yellow"),
            (counts["failed"],         "Failed",          "c-red"),
            (counts["blocked"],        "Blocked",         "c-purple"),
        )
    
        # counter animation JS removed (components.html deprecated)
    
        # ── Active Only filter toggle ──────────────────────────────────────────────
        def _carrier_badge(status: str) -> str:
            s = status.upper()
            if s == "ACTIVE":            return "🟢 Active"
            if s == "OUT_OF_SERVICE":    return "🔴 Out of Service"
            if s == "INACTIVE":          return "🟡 Inactive"
            return "⚫ —"
    
        # ── Pie chart: status distribution ────────────────────────────────────────
        status_counts = results_df["Carrier_Status"].str.upper().value_counts().reset_index()
        status_counts.columns = ["Status", "Count"]
        color_map = {"ACTIVE": "#22c55e", "INACTIVE": "#f59e0b", "OUT_OF_SERVICE": "#ef4444", "UNKNOWN": "#94a3b8"}
        pie_fig = px.pie(
            status_counts, names="Status", values="Count",
            color="Status", color_discrete_map=color_map,
            title="Carrier Status Distribution",
            hole=0.45,
        )
        pie_fig.update_traces(textposition="inside", textinfo="percent+label")
        pie_fig.update_layout(
            margin=dict(t=40, b=0, l=0, r=0),
            height=280,
            showlegend=False,
            title_font_size=13,
            paper_bgcolor="rgba(0,0,0,0)",
            plot_bgcolor="rgba(0,0,0,0)",
            font_color="#f1f5f9",
        )
        pie_col, _ = st.columns([2, 3])
        pie_col.plotly_chart(pie_fig, use_container_width=True)
    
        # ── Status filter ──────────────────────────────────────────────────────────
        f_col, _ = st.columns([3, 5])
        status_filter = f_col.selectbox(
            "Filter by status",
            ["ALL", "ACTIVE", "INACTIVE", "OUT_OF_SERVICE"],
            index=0,
            label_visibility="collapsed",
        )
    
        # ── Risk Scorecard summary row ─────────────────────────────────────────────
        risk_data = [_compute_risk_score(r) for r in rows]
        n_safe      = sum(1 for _, l in risk_data if l == "safe")
        n_caution   = sum(1 for _, l in risk_data if l == "caution")
        n_high_risk = sum(1 for _, l in risk_data if l == "high_risk")
        avg_score   = int(sum(s for s, _ in risk_data) / len(risk_data)) if risk_data else 0

        st.markdown(
            f'<div style="display:flex;gap:10px;flex-wrap:wrap;margin:10px 0 18px;">'
            f'<div style="background:#dcfce7;border:1px solid #86efac;border-radius:10px;'
            f'padding:10px 18px;text-align:center;min-width:110px;">'
            f'<div style="font-size:1.4rem;font-weight:800;color:#15803d">{n_safe}</div>'
            f'<div style="font-size:.75rem;color:#166534">🟢 Safe</div></div>'
            f'<div style="background:#fef9c3;border:1px solid #fde047;border-radius:10px;'
            f'padding:10px 18px;text-align:center;min-width:110px;">'
            f'<div style="font-size:1.4rem;font-weight:800;color:#92400e">{n_caution}</div>'
            f'<div style="font-size:.75rem;color:#78350f">🟡 Caution</div></div>'
            f'<div style="background:#fee2e2;border:1px solid #fca5a5;border-radius:10px;'
            f'padding:10px 18px;text-align:center;min-width:110px;">'
            f'<div style="font-size:1.4rem;font-weight:800;color:#b91c1c">{n_high_risk}</div>'
            f'<div style="font-size:.75rem;color:#991b1b">🔴 High Risk</div></div>'
            f'<div style="background:#eff6ff;border:1px solid #bfdbfe;border-radius:10px;'
            f'padding:10px 18px;text-align:center;min-width:110px;">'
            f'<div style="font-size:1.4rem;font-weight:800;color:#1d4ed8">{avg_score}</div>'
            f'<div style="font-size:.75rem;color:#1e40af">Avg Score</div></div>'
            f'</div>',
            unsafe_allow_html=True,
        )

        # Build preview dataframe with badge + risk + flags columns
        preview_cols = [
            "Input_ID", "Scrape_Status", "Carrier_Status",
            "Legal_Name", "DBA_Name", "USDOT_Number", "MC_Number",
            "Physical_Address", "Phone",
            "Safety_Rating", "OOS_Percentage",
            "Power_Units", "Drivers",
            "Total_Crashes",
        ]
        preview_df = results_df[[c for c in preview_cols if c in results_df.columns]].copy()
        preview_df.insert(2, "Status_Badge",
                          preview_df["Carrier_Status"].apply(_carrier_badge))

        # Add Risk Score + Authority Flags columns (only for successfully scraped rows)
        def _safe_risk_badge(r: dict) -> str:
            if str(r.get("Scrape_Status", "")).lower() != "success":
                return "—"
            s, l = _compute_risk_score(r)
            return _risk_badge(s, l)

        def _safe_auth_flags(r: dict) -> str:
            if str(r.get("Scrape_Status", "")).lower() != "success":
                return "—"
            return _authority_flags(r)

        preview_df["Risk_Score"] = [_safe_risk_badge(r) for r in rows]
        preview_df["Auth_Flags"] = [_safe_auth_flags(r) for r in rows]

        if status_filter != "ALL":
            preview_df = preview_df[preview_df["Carrier_Status"].str.upper() == status_filter]
            st.caption(f"Showing {len(preview_df)} {status_filter} carriers out of {len(results_df)} total.")

        # Row color: red override for flagged carriers, else status color
        def _style_row(row: pd.Series):
            flags = str(row.get("Auth_Flags", ""))
            if flags not in ("Clear", "—", ""):
                return ["background-color:#fff1f2; color:#881337"] * len(row)
            s = str(row.get("Carrier_Status", "")).upper()
            if s == "ACTIVE":
                return ["background-color:#dcfce7; color:#15803d"] * len(row)
            if s == "OUT_OF_SERVICE":
                return ["background-color:#fee2e2; color:#b91c1c"] * len(row)
            if s == "INACTIVE":
                return ["background-color:#fef9c3; color:#92400e"] * len(row)
            return [""] * len(row)

        styled_df = preview_df.style.apply(_style_row, axis=1)

        st.dataframe(
            styled_df, use_container_width=True, height=380, hide_index=True,
            column_config={
                "Input_ID":       st.column_config.TextColumn("Input ID",      width="small"),
                "Scrape_Status":  st.column_config.TextColumn("Scrape",        width="small"),
                "Status_Badge":   st.column_config.TextColumn("Status",        width="medium"),
                "Carrier_Status": None,
                "Risk_Score":     st.column_config.TextColumn("Risk Score",    width="medium"),
                "Auth_Flags":     st.column_config.TextColumn("Auth Alert",    width="medium"),
                "Legal_Name":     st.column_config.TextColumn("Legal Name",    width="large"),
                "DBA_Name":       st.column_config.TextColumn("DBA",           width="medium"),
                "USDOT_Number":   st.column_config.TextColumn("USDOT",         width="small"),
                "MC_Number":      st.column_config.TextColumn("MC #",          width="small"),
                "Physical_Address": st.column_config.TextColumn("Address",     width="large"),
                "Phone":          st.column_config.TextColumn("Phone",         width="small"),
                "Safety_Rating":  st.column_config.TextColumn("Safety Rating", width="medium"),
                "OOS_Percentage": st.column_config.TextColumn("OOS %",         width="small"),
                "Power_Units":    st.column_config.TextColumn("Units",         width="small"),
                "Drivers":        st.column_config.TextColumn("Drivers",       width="small"),
                "Total_Crashes":  st.column_config.TextColumn("Crashes",       width="small"),
            },
        )
    
        # Failed records expander
        failed_df = results_df[results_df["Scrape_Status"].isin(_FAILED_STATUSES)]
        if not failed_df.empty:
            with st.expander(f"⚠️  {len(failed_df)} records need attention"):
                st.dataframe(
                    failed_df[["Input_ID", "Scrape_Status", "Error_Detail"]],
                    use_container_width=True, height=200, hide_index=True,
                )
    
        # ── Action buttons ────────────────────────────────────────────────────────
        st.markdown("<br>", unsafe_allow_html=True)
        dl_col, active_col, pdf_col, retry_col, new_col = st.columns([3, 3, 3, 2, 2])
    
        ts = datetime.now().strftime("%Y-%m-%d")
        filename = f"DispatchDOS_FMCSA_{ts}.xlsx"
    
        # Build Excel if not already built (e.g. after a stop)
        if st.session_state.output_bytes is None and rows:
            st.session_state.output_bytes = _build_excel(
                rows, st.session_state.dupes_removed, _current_settings
            )
    
        if st.session_state.output_bytes:
            dl_col.download_button(
                "⬇️  Download All (Excel)",
                data=st.session_state.output_bytes,
                file_name=filename,
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                type="primary",
                use_container_width=True,
            )
    
        # Download Active Only as CSV
        active_rows = [r for r in rows
                       if str(r.get("Carrier_Status", "")).upper() == "ACTIVE"]
        if active_rows:
            active_df = pd.DataFrame(active_rows)
            active_csv = active_df.to_csv(index=False).encode("utf-8")
            active_col.download_button(
                f"🟢  Active Only ({len(active_rows)})",
                data=active_csv,
                file_name=f"DispatchDOS_Active_{ts}.csv",
                mime="text/csv",
                use_container_width=True,
            )
    
        # PDF Report button
        try:
            from fpdf import FPDF as _FPDF  # noqa: F401 — check availability
            _pdf_ok = True
        except ImportError:
            _pdf_ok = False

        if _pdf_ok:
            if "pdf_bytes_cache" not in st.session_state:
                st.session_state.pdf_bytes_cache = None
            # Show download button if already generated, else show generate button
            if st.session_state.pdf_bytes_cache:
                pdf_col.download_button(
                    "📄  Download PDF",
                    data=st.session_state.pdf_bytes_cache,
                    file_name=f"DispatchDOS_Report_{ts}.pdf",
                    mime="application/pdf",
                    use_container_width=True,
                    key="dl_pdf",
                )
            else:
                if pdf_col.button(
                    "📄  PDF Report",
                    use_container_width=True,
                    key="btn_pdf",
                ):
                    with st.spinner("PDF generating..."):
                        st.session_state.pdf_bytes_cache = _generate_pdf_report(rows)
                    st.rerun()
        else:
            pdf_col.caption("PDF: `pip install fpdf2`")

        if retry_col.button(
            "🔄  Retry Failed",
            use_container_width=True,
            disabled=failed_df.empty,
            key="btn_retry",
        ):
            retry_ids = failed_df["Input_ID"].tolist()
            unique, dupes, total_in = _process_input(retry_ids)
            st.session_state.carrier_ids    = unique
            st.session_state.dupes_removed  = dupes
            st.session_state.total_input    = total_in
            st.session_state.total_unique   = len(unique)
            st.session_state.results_rows   = []
            st.session_state.output_bytes   = None
            st.session_state.counts         = {"success": 0, "not_found": 0,
                                                "failed": 0, "blocked": 0}
            st.session_state.log_lines      = []
            st.rerun()
    
        if new_col.button("🆕  New Session", use_container_width=True, key="btn_new"):
            for k in list(st.session_state.keys()):
                del st.session_state[k]
            _clear_progress_file()
            st.rerun()
    
        # Excel sheet description
        st.markdown(
            '<div class="info-box">'
            'Excel contains 4 sheets: '
            '<b>All_Results</b> (all carriers + all data columns), '
            '<b>Failed_Records</b> (non-success rows for follow-up), '
            '<b>Summary</b> (run statistics), '
            '<b>Duplicates</b> (removed duplicate IDs).'
            '</div>',
            unsafe_allow_html=True,
        )
    
    # ─────────────────────────────────────────────────────────────────────────────
    # FOOTER
    # ─────────────────────────────────────────────────────────────────────────────
    st.markdown("""
    <div class="site-footer">
      <div class="footer-top">
        <div class="footer-brand">
          <span style="font-size:1.6rem">🚛</span>
          <div>
            <div class="footer-brand-name">Dispatch DOS</div>
            <div class="footer-brand-tag">FMCSA Bulk Carrier Lookup Tool</div>
          </div>
        </div>
        <div class="footer-links">
          <a class="footer-link"
             href="https://safer.fmcsa.dot.gov/CompanySnapshot.aspx"
             target="_blank">🔗 FMCSA Website</a>
          <a class="footer-link"
             href="https://li.fmcsa.dot.gov/liview/pkg_web_key.pkg_add_update_webkey?pv_action=REGWEBKEY"
             target="_blank">🔑 Get Free API Key</a>
          <a class="footer-link"
             href="https://github.com/Mohsinraza23/dispatch_Dos"
             target="_blank">⭐ GitHub</a>
        </div>
      </div>
      <div class="footer-bottom">
        <div class="footer-copy">
          © 2026 Dispatch DOS · Data sourced from
          <code style="color:#475569;font-size:.68rem">safer.fmcsa.dot.gov</code>
          · Public government records
        </div>
        <div class="footer-badges">
          <span class="footer-badge fb-public">✓ Public Data</span>
          <span class="footer-badge fb-secure">🔒 No Login</span>
        </div>
      </div>
    </div>
    """, unsafe_allow_html=True)
