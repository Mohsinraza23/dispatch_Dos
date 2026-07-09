"""
ai_tab.py
---------
Dispatch DOS — AI Universal Scraper Tab UI.

Renders the complete "🤖 AI Scraper" tab in the Streamlit app.
Called from app.py when user switches to AI mode.
"""
from __future__ import annotations

import io
import time
from datetime import datetime
from typing import Any

import pandas as pd
import streamlit as st

from ai_scraper_engine import (
    transcribe_audio,
    understand_requirement,
    scrape_fmcsa_carriers,
    scrape_website,
    _ENV_GROQ_KEY,
)


# ─────────────────────────────────────────────────────────────────────────────
# Extra CSS (injected once when AI tab renders)
# ─────────────────────────────────────────────────────────────────────────────

_AI_CSS = """
<style>
/* Chat bubbles */
.chat-wrap { overflow: hidden; margin-bottom: 8px; }
.chat-bubble-user {
    background: linear-gradient(135deg, #1d4ed8, #2563eb);
    color: #fff !important; border-radius: 18px 18px 4px 18px;
    padding: 12px 18px; margin: 6px 0 6px auto;
    max-width: 78%; display: table; float: right;
    font-size: .9rem; line-height: 1.55;
    box-shadow: 0 2px 12px rgba(37,99,235,0.25);
}
.chat-bubble-ai {
    background: #ffffff; color: #0f172a !important;
    border: 1px solid #e2e8f0;
    border-radius: 18px 18px 18px 4px;
    padding: 12px 18px; margin: 6px auto 6px 0;
    max-width: 78%; display: table; float: left;
    font-size: .9rem; line-height: 1.55;
    box-shadow: 0 2px 8px rgba(0,0,0,0.06);
}
.bubble-ts {
    font-size: .62rem; opacity: .5; margin-top: 5px;
    display: block;
}
.chat-user-ts { text-align: right; }
.chat-clearfix { clear: both; }

/* Intent confirmation card */
.ai-intent-card {
    background: #f0fdf4; border: 1px solid #86efac;
    border-radius: 12px; padding: 14px 18px; margin: 12px 0;
    font-size: .85rem; line-height: 1.6;
}
.ai-intent-card b { color: #15803d; }
.ai-intent-card code {
    background: #dcfce7; color: #15803d;
    padding: 2px 6px; border-radius: 4px; font-size: .82rem;
}

/* Language badge */
.lang-badge {
    display: inline-block; background: #eff6ff;
    border: 1px solid #bfdbfe; border-radius: 99px;
    padding: 2px 10px; font-size: .68rem; color: #1d4ed8;
    font-weight: 700; letter-spacing: .3px; margin-left: 8px;
    vertical-align: middle;
}

/* No-key warning */
.ai-nokey {
    background: #fffbeb; border-left: 3px solid #f59e0b;
    border-radius: 0 10px 10px 0; padding: 14px 18px;
    font-size: .88rem; color: #92400e; line-height: 1.7;
}
</style>
"""


# ─────────────────────────────────────────────────────────────────────────────
# Session state initialiser
# ─────────────────────────────────────────────────────────────────────────────

def _init_ai_state() -> None:
    defaults: dict[str, Any] = {
        "ai_chat_history":   [],   # [{"role": "user"|"ai", "text": str, "ts": str}]
        "ai_results":        [],   # list of result dicts from last scrape
        "ai_pending_intent": None, # intent dict waiting for user confirmation
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v


# ─────────────────────────────────────────────────────────────────────────────
# Chat helpers
# ─────────────────────────────────────────────────────────────────────────────

def _add_msg(role: str, text: str) -> None:
    st.session_state.ai_chat_history.append({
        "role": role,
        "text": text,
        "ts":   datetime.now().strftime("%H:%M"),
    })


def _render_chat() -> None:
    history = st.session_state.ai_chat_history
    if not history:
        st.markdown(
            '<div style="text-align:center;color:#94a3b8;padding:36px 0 24px;font-size:.9rem;">'
            '🤖 AI ready — neeche apni requirement type karo ya audio bhejo'
            '</div>',
            unsafe_allow_html=True,
        )
        return

    for msg in history:
        role = msg["role"]
        # Escape HTML but preserve newlines
        text = (
            msg["text"]
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace("\n", "<br>")
        )
        ts = msg.get("ts", "")

        if role == "user":
            st.markdown(
                f'<div class="chat-wrap">'
                f'<div class="chat-bubble-user">{text}'
                f'<span class="bubble-ts chat-user-ts">{ts}</span>'
                f'</div>'
                f'<div class="chat-clearfix"></div>'
                f'</div>',
                unsafe_allow_html=True,
            )
        else:
            st.markdown(
                f'<div class="chat-wrap">'
                f'<div class="chat-bubble-ai">{text}'
                f'<span class="bubble-ts">{ts}</span>'
                f'</div>'
                f'<div class="chat-clearfix"></div>'
                f'</div>',
                unsafe_allow_html=True,
            )


# ─────────────────────────────────────────────────────────────────────────────
# Core logic
# ─────────────────────────────────────────────────────────────────────────────

def _process_input(user_text: str, groq_key: str) -> None:
    """Understand user requirement and store pending intent."""
    _add_msg("user", user_text)

    with st.spinner("AI samajh raha hai..."):
        intent_data = understand_requirement(user_text, groq_key)

    intent  = intent_data.get("intent", "unclear")
    reply   = intent_data.get("reply_to_user", "")
    lang    = intent_data.get("detected_language", "")
    summary = intent_data.get("summary_english", "")

    # Build AI reply text
    if intent == "fmcsa_lookup":
        ids   = intent_data.get("carrier_ids", [])
        extra = f"\n\n🚛 **{len(ids)} carrier ID(s) detected** — confirm karo to scraping shuru hogi."
    elif intent == "web_scrape":
        url   = intent_data.get("website_url", "")
        extra = f"\n\n🌐 **Website:** `{url}` — confirm karo to data extract hoga."
    else:
        extra = ""

    lang_tag = f" [{lang}]" if lang else ""
    ai_text  = f"{reply}{extra}"
    if summary:
        ai_text += f"\n\n*(EN: {summary})*{lang_tag}"

    _add_msg("ai", ai_text)

    if intent in ("fmcsa_lookup", "web_scrape"):
        st.session_state.ai_pending_intent = intent_data
    else:
        st.session_state.ai_pending_intent = None


def _execute_scraping(intent_data: dict, groq_key: str) -> None:
    """Execute the confirmed scraping job."""
    intent = intent_data.get("intent")

    if intent == "fmcsa_lookup":
        ids = intent_data.get("carrier_ids", [])
        if not ids:
            _add_msg("ai", "⚠️ Koi carrier ID nahi mili. Kripya USDOT ya MC number clearly likho.")
            st.session_state.ai_pending_intent = None
            return

        _add_msg("ai", f"🚛 {len(ids)} carrier(s) FMCSA se scrape ho rahe hain... ruko.")
        with st.spinner(f"FMCSA se {len(ids)} carrier(s) scrape ho rahe hain..."):
            results = scrape_fmcsa_carriers(ids)

        found = sum(1 for r in results if r.get("status") == "found")
        _add_msg("ai", f"✅ Done! **{found}/{len(results)}** carriers mili. Neeche table mein results dekhein.")
        st.session_state.ai_results = results

    elif intent == "web_scrape":
        url    = intent_data.get("website_url", "")
        fields = intent_data.get("data_fields", [])
        query  = intent_data.get("search_query", "")

        if not url:
            _add_msg("ai", "⚠️ Website URL nahi mili. Kripya URL mention karein, jaise: https://example.com")
            st.session_state.ai_pending_intent = None
            return

        _add_msg("ai", f"🌐 `{url}` se data extract ho raha hai... ruko.")
        with st.spinner(f"Scraping {url}..."):
            results = scrape_website(url, fields, query, groq_key)

        errors = [r for r in results if "error" in r]
        clean  = [r for r in results if "error" not in r]
        if clean:
            _add_msg("ai", f"✅ Done! **{len(clean)} records** extracted. Neeche table mein dekhein.")
        elif errors:
            _add_msg("ai", f"❌ Error: {errors[0].get('error', 'Unknown error')}")

        st.session_state.ai_results = results

    st.session_state.ai_pending_intent = None


# ─────────────────────────────────────────────────────────────────────────────
# Sidebar widget  (called from within app.py's `with st.sidebar:`)
# ─────────────────────────────────────────────────────────────────────────────

def render_ai_sidebar() -> None:
    """Render AI section in the sidebar. Key stored under session key 'sb_groq_key'."""
    st.markdown('<div class="sb-lbl">🤖 AI Engine (Groq — Free)</div>',
                unsafe_allow_html=True)
    st.text_input(
        "Groq API Key",
        value=_ENV_GROQ_KEY,
        type="password",
        placeholder="gsk_...",
        help="Free API key — console.groq.com · Powers audio + AI understanding",
        label_visibility="collapsed",
        key="sb_groq_key",
    )
    key = st.session_state.get("sb_groq_key", "")
    if key:
        st.markdown(
            '<div style="font-size:.72rem;color:#22c55e;margin-top:-8px">'
            '✓ Groq AI active — audio + chat enabled</div>',
            unsafe_allow_html=True,
        )
    else:
        st.markdown(
            '<div style="font-size:.72rem;color:#f59e0b;margin-top:-8px">'
            '⚠ No key — AI Scraper tab disabled</div>',
            unsafe_allow_html=True,
        )


# ─────────────────────────────────────────────────────────────────────────────
# Main tab renderer
# ─────────────────────────────────────────────────────────────────────────────

def render_ai_tab(groq_key: str = "") -> None:
    """Render the complete AI Universal Scraper tab. Call from app.py."""
    _init_ai_state()

    # Inject extra CSS
    st.markdown(_AI_CSS, unsafe_allow_html=True)

    # ── Header banner ─────────────────────────────────────────────────────────
    st.markdown(
        '<div class="ds-header">'
        '<span style="font-size:3rem">🤖</span>'
        '<div>'
        '<h1>AI Universal Scraper</h1>'
        '<p>Kisi bhi website ka data — text ya audio mein batao, AI samjhega aur scrape karega</p>'
        '</div>'
        '<span class="ds-badge">Powered by Groq · Free</span>'
        '</div>',
        unsafe_allow_html=True,
    )

    # ── No key — show instructions ────────────────────────────────────────────
    if not groq_key:
        st.markdown(
            '<div class="ai-nokey">'
            '<b>⚠️ Groq API Key required.</b><br>'
            'Sidebar mein <b>"AI Engine"</b> section mein key paste karo.<br><br>'
            '📌 Free key kaise milegi:<br>'
            '&nbsp;&nbsp;1. <code>console.groq.com</code> pe jao<br>'
            '&nbsp;&nbsp;2. Sign up (sirf email, no credit card)<br>'
            '&nbsp;&nbsp;3. API Keys → Create → Copy → Sidebar mein paste karo'
            '</div>',
            unsafe_allow_html=True,
        )
        return

    # ── Capabilities bar ──────────────────────────────────────────────────────
    st.markdown("""
<div class="stats-bar">
  <div class="stat-card sc-blue">
    <span class="sc-icon">🚛</span>
    <div class="sc-val">FMCSA</div>
    <div class="sc-lbl">Carrier Lookup</div>
    <div class="sc-sub">USDOT / MC numbers</div>
  </div>
  <div class="stat-card sc-green">
    <span class="sc-icon">🌐</span>
    <div class="sc-val">Any Site</div>
    <div class="sc-lbl">Web Scraping</div>
    <div class="sc-sub">AI extracts data</div>
  </div>
  <div class="stat-card sc-purple">
    <span class="sc-icon">🎤</span>
    <div class="sc-val">99+</div>
    <div class="sc-lbl">Languages</div>
    <div class="sc-sub">Text + Audio input</div>
  </div>
</div>
""", unsafe_allow_html=True)

    # ── Examples ──────────────────────────────────────────────────────────────
    with st.expander("💡 Examples — Kya likh ya bol sakte hain"):
        st.markdown("""
**FMCSA Carrier Lookup:**
- `USDOT 1597181 ka data chahiye`
- `MC193369 aur MC456789 ke baare mein batao`
- `Give me carrier info for USDOT 793594`
- `Carrier 1234567 active hai ya nahi?`

**Website Scraping:**
- `https://example.com se product names aur prices nikaalo`
- `Amazon pe laptop search karo aur first page results do`
- `YellowPages se restaurants ki list New York mein`

**Any Language:**
- اردو: `USDOT 1597181 کی معلومات چاہیے`
- Hindi: `MC193369 ka sara data do`
- English: `Scrape all products from https://site.com`
        """)

    # ── Chat window ───────────────────────────────────────────────────────────
    st.markdown('<div class="sec-head">💬 Chat History</div>', unsafe_allow_html=True)
    _render_chat()

    # ── Pending confirmation ──────────────────────────────────────────────────
    pending = st.session_state.get("ai_pending_intent")
    if pending:
        intent = pending.get("intent", "")
        ids    = pending.get("carrier_ids", [])
        url    = pending.get("website_url", "")
        fields = pending.get("data_fields", [])

        if intent == "fmcsa_lookup":
            ids_preview = ", ".join(ids[:6]) + ("..." if len(ids) > 6 else "")
            st.markdown(
                f'<div class="ai-intent-card">'
                f'<b>🚛 FMCSA Scraping Ready:</b><br>'
                f'Carriers: <code>{ids_preview}</code><br>'
                f'Total: <b>{len(ids)}</b> carrier(s)'
                f'</div>',
                unsafe_allow_html=True,
            )
        elif intent == "web_scrape":
            fields_str = ", ".join(fields) if fields else "all available data"
            st.markdown(
                f'<div class="ai-intent-card">'
                f'<b>🌐 Web Scraping Ready:</b><br>'
                f'URL: <code>{url}</code><br>'
                f'Fields: <b>{fields_str}</b>'
                f'</div>',
                unsafe_allow_html=True,
            )

        c1, c2, _ = st.columns([2, 2, 4])
        if c1.button("✅ Confirm & Scrape", type="primary",
                     key="ai_confirm", use_container_width=True):
            _execute_scraping(pending, groq_key)
            st.rerun()
        if c2.button("✏️ Modify", type="secondary",
                     key="ai_cancel", use_container_width=True):
            st.session_state.ai_pending_intent = None
            _add_msg("ai", "Theek hai, dobara batao kya chahiye.")
            st.rerun()

    # ── Input section ─────────────────────────────────────────────────────────
    st.markdown("---")
    st.markdown('<div class="sec-head">✍️ Apni Requirement Batao</div>',
                unsafe_allow_html=True)

    input_mode = st.radio(
        "Input mode",
        ["✏️ Text (any language)", "🎤 Audio Message"],
        horizontal=True,
        label_visibility="collapsed",
        key="ai_input_mode",
    )

    if input_mode == "✏️ Text (any language)":
        user_text = st.text_area(
            "Requirement",
            height=130,
            placeholder=(
                "Urdu, Hindi, English — koi bhi language chalegi...\n\n"
                "Examples:\n"
                "• USDOT 1597181 ka data chahiye\n"
                "• https://example.com se prices nikaalo\n"
                "• Give me carrier info for MC193369"
            ),
            label_visibility="collapsed",
            key="ai_text_area",
        )
        s_col, _ = st.columns([3, 1])
        if s_col.button("▶ Send", type="primary",
                        key="ai_send", use_container_width=True):
            txt = user_text.strip()
            if txt:
                _process_input(txt, groq_key)
                st.rerun()
            else:
                st.warning("Kuch to likho!")

    else:  # Audio
        st.markdown(
            '<div class="info-box">'
            '🎤 Audio record karo ya file upload karo — '
            'Groq Whisper automatically samjhega. '
            '<b>99+ languages</b> supported: Urdu, Hindi, Arabic, English, sab chalega.'
            '</div>',
            unsafe_allow_html=True,
        )

        # Native audio recorder (Streamlit 1.31+)
        audio_data = None
        audio_name = "recording.wav"
        try:
            recorded = st.audio_input(
                "🎙️ Record audio message",
                key="ai_mic",
                label_visibility="visible",
            )
            if recorded is not None:
                audio_data = recorded.read()
                audio_name = "recording.wav"
        except AttributeError:
            st.caption("Browser recording is not supported in this Streamlit version.")

        # File upload fallback
        uploaded = st.file_uploader(
            "Or upload audio file (MP3 / WAV / M4A / OGG)",
            type=["mp3", "wav", "m4a", "ogg", "webm", "mp4"],
            key="ai_audio_file",
        )
        if uploaded is not None:
            audio_data = uploaded.read()
            audio_name = uploaded.name

        if audio_data:
            if st.button("🎤 Transcribe & Send", type="primary",
                         key="ai_transcribe", use_container_width=False):
                with st.spinner("Audio transcribe ho raha hai (Groq Whisper)..."):
                    try:
                        text = transcribe_audio(audio_data, audio_name, groq_key)
                        if text:
                            st.success(f"**Transcribed:** {text}")
                            time.sleep(0.4)
                            _process_input(text, groq_key)
                            st.rerun()
                        else:
                            st.error("Transcription empty — thoda louder bol ke try karo.")
                    except Exception as e:
                        st.error(f"Transcription failed: {e}")

    # Clear chat button
    if st.session_state.ai_chat_history:
        st.markdown("<br>", unsafe_allow_html=True)
        if st.button("🗑️ Clear Chat & Results", key="ai_clear", type="secondary"):
            st.session_state.ai_chat_history   = []
            st.session_state.ai_results        = []
            st.session_state.ai_pending_intent = None
            st.rerun()

    # ── Results ───────────────────────────────────────────────────────────────
    results = st.session_state.get("ai_results", [])
    if results:
        st.markdown("---")
        st.markdown('<div class="sec-head">📊 Results</div>', unsafe_allow_html=True)

        # Separate errors from data
        errors = [r for r in results if "error" in r]
        data   = [r for r in results if "error" not in r]

        if errors:
            st.warning(f"⚠️ {len(errors)} error(s): {errors[0].get('error', '')[:200]}")

        if data:
            # Detect FMCSA results (have legal_name key)
            is_fmcsa = "legal_name" in data[0]

            if is_fmcsa:
                display = [
                    {
                        "Status":        r.get("status", ""),
                        "Carrier Status": r.get("carrier_status", ""),
                        "Legal Name":    r.get("legal_name", ""),
                        "USDOT":         r.get("usdot_number", ""),
                        "MC #":          r.get("mc_number", ""),
                        "Phone":         r.get("phone", ""),
                        "Address":       r.get("physical_address", ""),
                        "Safety Rating": r.get("safety_rating", ""),
                        "Power Units":   r.get("power_units", ""),
                        "Drivers":       r.get("drivers", ""),
                        "Entity Type":   r.get("entity_type", ""),
                    }
                    for r in data
                ]
            else:
                display = data

            df = pd.DataFrame(display)
            st.dataframe(df, use_container_width=True, hide_index=True, height=320)

            # Download buttons
            ts   = datetime.now().strftime("%Y-%m-%d_%H%M")
            d1, d2 = st.columns(2)

            csv_bytes = df.to_csv(index=False).encode("utf-8")
            d1.download_button(
                "⬇️ Download CSV",
                data=csv_bytes,
                file_name=f"AI_Scrape_{ts}.csv",
                mime="text/csv",
                use_container_width=True,
            )

            buf = io.BytesIO()
            df.to_excel(buf, index=False, engine="openpyxl")
            buf.seek(0)
            d2.download_button(
                "⬇️ Download Excel",
                data=buf.read(),
                file_name=f"AI_Scrape_{ts}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                use_container_width=True,
            )

            st.caption(
                f"{len(data)} records · "
                f"{'FMCSA' if is_fmcsa else 'Web'} scrape · "
                f"{datetime.now().strftime('%Y-%m-%d %H:%M')}"
            )

    # ── Footer note ───────────────────────────────────────────────────────────
    st.markdown(
        '<div style="text-align:center;color:#94a3b8;font-size:.75rem;margin-top:32px;">'
        'AI powered by <b>Groq</b> (Whisper + Llama 3.3) · '
        'FMCSA data from <code>safer.fmcsa.dot.gov</code> · '
        'Free to use'
        '</div>',
        unsafe_allow_html=True,
    )
