# watchtower.py
# Minimal “no-OpenAI” WatchTower: polls a few crypto/reg/regulator RSS feeds,
# scores items for urgency, and sends high-impact ones to Telegram.
#
# Requires GitHub Actions (or any runner) with these environment variables set:
#  - TELEGRAM_BOT_TOKEN
#  - TELEGRAM_CHAT_ID
#
# Dependencies (put in requirements.txt): feedparser, requests

import os
import time
import json
import hashlib
from html import escape as html_escape
import requests
import feedparser
from datetime import datetime, timezone, timedelta

# ----------------------------
# Config
# ----------------------------
TIMEZONE = "America/New_York"
RECENT_HOURS = 24
DEDUPE_HOURS = 12
IMPACT_THRESHOLD = 9

SOURCES = [
    {"label": "SEC – Press Releases",    "url": "https://www.sec.gov/news/pressreleases.rss"},
    {"label": "SEC – Public Statements", "url": "https://www.sec.gov/news/speeches.rss"},
    {"label": "CFTC – Press Releases",   "url": "https://www.cftc.gov/PressRoom/PressReleases/rss.xml"},
    {"label": "Federal Reserve – Press", "url": "https://www.federalreserve.gov/feeds/press_all.xml"},
    {"label": "BLS – CPI News",          "url": "https://www.bls.gov/feeds/news.release.cpi.rss"},
    {"label": "CoinDesk – All",          "url": "https://www.coindesk.com/arc/outboundfeeds/rss/?outputType=xml"},
    {"label": "CoinTelegraph – All",     "url": "https://cointelegraph.com/rss"},
    {"label": "The Block – News",        "url": "https://www.theblock.co/rss"},
]

STATE_FILE = "state.json"   # best-effort dedupe between runs in the same workspace

# ----------------------------
# Helpers
# ----------------------------
def now_ms() -> int:
    return int(time.time() * 1000)

def h_to_ms(h: float) -> int:
    return int(h * 3600 * 1000)

def md5(s: str) -> str:
    return hashlib.md5(s.encode("utf-8")).hexdigest()

def parse_pub_ms(entry) -> int:
    """
    Try to parse a timestamp from the entry; return ms since epoch (UTC) or 0.
    """
    # feedparser usually gives published_parsed (time.struct_time)
    t = getattr(entry, "published_parsed", None) or getattr(entry, "updated_parsed", None)
    if t:
        try:
            return int(datetime(*t[:6], tzinfo=timezone.utc).timestamp() * 1000)
        except Exception:
            pass
    # fallback: try entry.published or entry.updated strings
    for key in ("published", "updated", "pubDate", "date"):
        val = getattr(entry, key, None)
        if isinstance(val, str):
            try:
                # very lenient parse; if it fails we skip
                dt = feedparser._parse_date(val)
                if dt:
                    return int(datetime(*dt[:6], tzinfo=timezone.utc).timestamp() * 1000)
            except Exception:
                pass
    return 0

def is_recent(pub_ms: int) -> bool:
    if pub_ms == 0:
        return True  # when unknown, don't throw away—let scoring decide
    return (now_ms() - pub_ms) <= h_to_ms(RECENT_HOURS)

def fmt_ts_local(pub_ms: int) -> str:
    if pub_ms == 0:
        return ""
    tz = datetime.now().astimezone().tzinfo
    return datetime.fromtimestamp(pub_ms / 1000, tz=tz).strftime("%-m/%-d/%Y, %-I:%M:%S %p (%Z)")

def load_state() -> dict:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"sent": {}, "last_gc": 0}

def save_state(st: dict) -> None:
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(st, f)
    except Exception:
        pass

# ----------------------------
# Scoring (tweak freely)
# ----------------------------
def score_event(text: str, source_label: str) -> int:
    t = (text or "").lower()
    s = 0
    # Source bump for key US regulators / macro
    if any(k in (source_label or "").lower() for k in ["sec", "cftc", "federal reserve", "bls"]):
        s += 4
    # Macro / policy terms
    if any(k in t for k in ["cpi", "pce"]):
        s += 4
    if any(k in t for k in ["fomc", "powell", "rate", "interest rate", "minutes"]):
        s += 4
    if any(k in t for k in ["sec", "cftc", "ftc", "ofac", "treasury"]):
        s += 3
    # Market structure / funds
    if any(k in t for k in ["etf", "outflow", "inflow"]):
        s += 3
    # Security incidents
    if any(k in t for k in ["hack", "exploit", "breach", "security incident"]):
        s += 5
    # Market halts / suspensions
    if any(k in t for k in ["outage", "downtime", "halt", "suspend"]):
        s += 4
    # Bankruptcies / liquidations / delistings
    if any(k in t for k in ["delist", "insolvency", "bankruptcy", "liquidation"]):
        s += 3
    # Big names bump
    if any(k in t for k in ["binance", "coinbase", "kraken", "tether", "usdt", "circle", "usdc",
                            "microstrategy", "blackrock", "fidelity"]):
        s += 2
    return max(0, min(20, s))

# ----------------------------
# Telegram
# ----------------------------
BOT = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT = os.getenv("TELEGRAM_CHAT_ID")

if not BOT or not CHAT:
    raise RuntimeError("Missing TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID env vars")

def send_telegram(msg: str, loud: bool = True) -> None:
    """
    Sends HTML-formatted message to Telegram. If loud=False, notifications are muted.
    """
    try:
        requests.post(
            f"https://api.telegram.org/bot{BOT}/sendMessage",
            headers={"Content-Type": "application/json"},
            json={
                "chat_id": CHAT,
                "text": msg,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
                "disable_notification": (not loud),
            },
            timeout=15,
        )
    except Exception:
        # don't crash the run because of Telegram transient errors
        pass

# ----------------------------
# Main
# ----------------------------
def main():
    st = load_state()
    sent = st.get("sent", {})
    checked = 0
    alerted = 0
    deduped = 0
    skipped_old = 0

    now = now_ms()

    for src in SOURCES:
        label = src["label"]
        url = src["url"]

        try:
            feed = feedparser.parse(url)
        except Exception:
            continue  # skip bad source

        items = getattr(feed, "entries", []) or []
        for it in items:
            checked += 1

            title = (getattr(it, "title", "") or "").strip()
            link = (getattr(it, "link", "") or "").strip()
            # Try to find a short description
            summary = (
                getattr(it, "summary", None)
                or getattr(it, "content", [{}])[0].get("value", "")
                or getattr(it, "description", "")
            )
            summary = (summary or "").strip()

            pub_ms = parse_pub_ms(it)
            if not is_recent(pub_ms):
                skipped_old += 1
                continue

            # UID and dedupe
            uid = md5(f"{label}|{title}|{link}")
            last_ts = sent.get(uid)
            if last_ts and (now - last_ts) <= h_to_ms(DEDUPE_HOURS):
                deduped += 1
                continue

            # Score & maybe alert
            text_for_score = f"{title} {summary}"
            sc = score_event(text_for_score, label)

            if sc >= IMPACT_THRESHOLD:
                # Build message safely (no backslashes inside f-string expressions)
                lines = []
                lines.append(f"<b>🚨 URGENT {sc}/10</b>")
                if title:
                    lines.append(f"<b>{html_escape(title)}</b>")
                lines.append(f"Source: {html_escape(label)}")
                when_line = fmt_ts_local(pub_ms)
                if when_line:
                    lines.append(f"When: {when_line}")
                if summary:
                    lines.append(html_escape(summary))

                if link:
                    # Proper HTML anchor without backslashes in expressions
                    link_html = f'<a href="{html_escape(link)}">Open</a>'
                    lines.append(link_html)

                msg = "\n".join(lines)
                send_telegram(msg, loud=True)
                sent[uid] = now_ms()
                alerted += 1

    # Heartbeat if nothing fired
    if alerted == 0:
        hb_lines = [
            "◯ Watchtower heartbeat — no urgent alerts.",
            f"Checked: {checked}, Skipped old: {skipped_old}, Deduped: {deduped}",
        ]
        send_telegram("\n".join(hb_lines), loud=False)

    # Garbage-collect old dedupe entries
    # (keep map from growing forever within a single runner workspace)
    keys_to_drop = []
    for k, ts in sent.items():
        if (now_ms() - int(ts)) > h_to_ms(DEDUPE_HOURS):
            keys_to_drop.append(k)
    for k in keys_to_drop:
        sent.pop(k, None)

    st["sent"] = sent
    st["last_gc"] = now_ms()
    save_state(st)


if __name__ == "__main__":
    main()
