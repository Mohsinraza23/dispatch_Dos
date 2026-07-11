"""
ai_scraper_engine.py
--------------------
CarrierPulse — Groq-powered AI engine for universal data scraping.

Uses:
  - Groq Whisper large-v3   → audio transcription (99+ languages, FREE)
  - Groq Llama 3.3-70b      → intent understanding + data extraction (FREE)
  - Playwright               → headless browser for any website
  - Existing fmcsa_scraper   → FMCSA carrier lookups
"""
from __future__ import annotations

import re
import json
import asyncio
import logging
import concurrent.futures
from typing import Any

from bs4 import BeautifulSoup

log = logging.getLogger("ai_scraper_engine")

# ── Load Groq API key — .env (local) or Streamlit Cloud secrets (production) ──
import os as _os

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

_ENV_GROQ_KEY = _os.environ.get("GROQ_API_KEY", "")

# Streamlit Cloud: secrets are in st.secrets, not always in os.environ
if not _ENV_GROQ_KEY:
    try:
        import streamlit as _st
        _ENV_GROQ_KEY = (
            _st.secrets.get("GROQ_API_KEY", "")
            or _st.secrets.get("groq_api_key", "")
        )
    except Exception:
        pass

# ─────────────────────────────────────────────────────────────────────────────
# Groq client helper
# ─────────────────────────────────────────────────────────────────────────────

def _groq(api_key: str):
    try:
        from groq import Groq
        return Groq(api_key=api_key)
    except ImportError:
        raise RuntimeError(
            "groq package not installed. Run: pip install groq"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Audio transcription  (Groq Whisper)
# ─────────────────────────────────────────────────────────────────────────────

def transcribe_audio(audio_bytes: bytes, filename: str, api_key: str) -> str:
    """
    Transcribe audio using Groq Whisper large-v3.
    Supports 99+ languages, auto-detects language.
    Returns transcribed text string.
    """
    client = _groq(api_key)
    result = client.audio.transcriptions.create(
        file=(filename, audio_bytes),
        model="whisper-large-v3",
        response_format="text",
    )
    return str(result).strip()


# ─────────────────────────────────────────────────────────────────────────────
# Intent understanding  (Groq Llama)
# ─────────────────────────────────────────────────────────────────────────────

_INTENT_SYSTEM = """\
You are an expert data scraping assistant.
Analyze the user's message and return ONLY a valid JSON object with these exact keys:

{
  "intent":            "fmcsa_lookup" | "web_scrape" | "unclear",
  "confidence":        0-100,
  "website_url":       "full URL to scrape or empty string",
  "website_name":      "site name in English or empty string",
  "data_fields":       ["list", "of", "fields", "to", "extract"],
  "search_query":      "search terms if applicable, else empty string",
  "carrier_ids":       ["USDOT/MC numbers extracted from message, or empty list"],
  "detected_language": "language name in English",
  "summary_english":   "one sentence summary of what user wants in English",
  "reply_to_user":     "helpful reply in the SAME language the user used"
}

Classification rules:
- "fmcsa_lookup": user mentions trucker/carrier/FMCSA/USDOT/MC number/DOT/trucking company
  → extract all USDOT and MC numbers from the message into carrier_ids
- "web_scrape": user mentions a website URL or wants data from a specific website/platform
  → extract the URL and what fields to scrape
- "unclear": requirement is missing key information
  → ask a clarifying question in reply_to_user

IMPORTANT: reply_to_user must be in the EXACT same language the user wrote in."""


def understand_requirement(text: str, api_key: str) -> dict[str, Any]:
    """
    Send user message to Groq Llama and get structured scraping intent.
    Returns dict with intent, carrier_ids, website_url, reply_to_user, etc.
    """
    client = _groq(api_key)
    resp = client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=[
            {"role": "system", "content": _INTENT_SYSTEM},
            {"role": "user",   "content": text},
        ],
        response_format={"type": "json_object"},
        temperature=0.1,
        max_tokens=1024,
    )
    try:
        return json.loads(resp.choices[0].message.content)
    except Exception:
        return {
            "intent": "unclear",
            "confidence": 0,
            "website_url": "",
            "website_name": "",
            "data_fields": [],
            "search_query": "",
            "carrier_ids": [],
            "detected_language": "unknown",
            "summary_english": "",
            "reply_to_user": (
                "Sorry, I could not understand your request. "
                "Please try again with more details."
            ),
        }


# ─────────────────────────────────────────────────────────────────────────────
# FMCSA scraping  (uses existing fmcsa_scraper.py engine)
# ─────────────────────────────────────────────────────────────────────────────

def scrape_fmcsa_carriers(
    carrier_ids: list[str],
    delay_min: float = 3.0,
    delay_max: float = 8.0,
    max_carriers: int = 20,
) -> list[dict[str, Any]]:
    """
    Scrape FMCSA data for a list of carrier IDs.
    Capped at max_carriers to keep AI chat mode responsive.
    Returns list of scrape_carrier() result dicts.
    """
    from fmcsa_scraper import scrape_carrier, _make_session

    session = _make_session()
    results: list[dict[str, Any]] = []

    for cid in carrier_ids[:max_carriers]:
        cid = cid.strip()
        if not cid:
            continue
        search_type = "MC" if cid.upper().startswith("MC") else "USDOT"
        clean = re.sub(
            r"^(MC|DOT|USDOT)\s*[#\-\s]?", "", cid, flags=re.IGNORECASE
        ).strip()
        try:
            res = scrape_carrier(
                clean, search_type,
                delay_min=delay_min,
                delay_max=delay_max,
                include_raw_html=False,
                _session=session,
            )
        except Exception as exc:
            res = {"status": "error", "error_detail": str(exc), "search_value": clean}
        results.append(res)

    session.close()
    return results


# ─────────────────────────────────────────────────────────────────────────────
# Universal website scraper  (Playwright fetch + Groq AI extraction)
# ─────────────────────────────────────────────────────────────────────────────

async def _fetch_html(url: str) -> str:
    """Fetch page HTML using Playwright (falls back to requests if unavailable)."""
    try:
        from playwright.async_api import async_playwright
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage",
                      "--disable-blink-features=AutomationControlled"],
            )
            ctx = await browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                )
            )
            page = await ctx.new_page()
            await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            try:
                await page.wait_for_load_state("networkidle", timeout=8_000)
            except Exception:
                pass
            html = await page.content()
            await browser.close()
            return html
    except Exception:
        # Playwright unavailable — use requests
        import requests
        r = requests.get(
            url, timeout=15,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "Chrome/124.0.0.0 Safari/537.36"
                )
            },
        )
        return r.text


def scrape_website(
    url: str,
    fields: list[str],
    search_query: str,
    api_key: str,
) -> list[dict[str, Any]]:
    """
    Scrape any website:
      1. Playwright loads the page
      2. HTML is cleaned and truncated
      3. Groq Llama extracts the requested fields

    Returns list of result dicts.
    """
    # Ensure URL has scheme
    if not url.startswith("http"):
        url = "https://" + url

    # Build search URL for known search engines
    if search_query:
        if re.search(r"(google\.com|bing\.com|duckduckgo\.com)", url):
            q = search_query.replace(" ", "+")
            if "google" in url:
                url = f"https://www.google.com/search?q={q}"
            elif "bing" in url:
                url = f"https://www.bing.com/search?q={q}"

    # Fetch HTML — run async function safely from Streamlit's sync main thread
    # (asyncio.run() directly can fail with "event loop already running" in Streamlit)
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            html = pool.submit(asyncio.run, _fetch_html(url)).result(timeout=45)
    except Exception as e:
        return [{"error": f"Page load failed: {e}", "url": url}]

    # Clean HTML — remove noise, keep meaningful text
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "nav", "footer", "head", "iframe", "svg"]):
        tag.decompose()
    text = soup.get_text(" ", strip=True)
    text = re.sub(r"\s+", " ", text)[:7000]   # Cap for LLM context

    # AI extraction
    client = _groq(api_key)
    fields_str = ", ".join(fields) if fields else "all relevant data available"
    prompt = f"""\
Extract structured data from this webpage content.

Requested fields: {fields_str}
Search context: {search_query or "general page content"}
Source URL: {url}

Return a JSON object with key "results" containing a list of records.
Each record is a dict mapping field names to their values.
Use empty string for missing fields.
If only one record exists, still wrap it in a list: {{"results": [{{...}}]}}

Webpage content:
{text}"""

    try:
        resp = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a precise data extraction AI. "
                        "Extract structured data from webpage text and return valid JSON only."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            response_format={"type": "json_object"},
            temperature=0.1,
            max_tokens=2048,
        )
        data = json.loads(resp.choices[0].message.content)
        results = data.get("results", [])
        if not results:
            # AI returned flat dict — wrap it
            results = [data]
        return results
    except Exception as e:
        return [{"error": f"AI extraction failed: {e}", "url": url}]
