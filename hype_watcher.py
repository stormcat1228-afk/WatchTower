# hype_watcher.py
# Scrapes RSS feeds from news_sources.json, scores hype/sentiment, and
# posts the top items to Telegram.

import os, json, time, re, hashlib, html
from datetime import datetime, timezone
from urllib.parse import urlparse

import requests
import feedparser
from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

# ====== Config ======
TIMEZONE = "America/New_York"
MAX_ITEMS_PER_RUN = 5          # send at most this many items each run
RECENT_HOURS = 6               # ignore items older than this
MIN_SCORE_TO_ALERT = 0.35      # 0..1 final score threshold
STATE_FILE = ".hype_seen.json" # best-effort dedupe; ephemeral on Actions

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID")

assert BOT_TOKEN, "Missing TELEGRAM_BOT_TOKEN secret"
assert CHAT_ID,   "Missing TELEGRAM_CHAT_ID secret"

# ====== Helpers ======
analyzer = SentimentIntensityAnalyzer()

def now_utc():
    return datetime.now(timezone.utc)

def load_sources():
    with open("news_sources.json", "r", encoding="utf-8") as f:
        data = json.load(f)
    return data["sources"]

def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f))
    except Exception:
        return set()

def save_state(seen):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(sorted(list(seen)), f)
    except Exception:
        pass  # runner may be ephemeral; okay if we can't persist

def to_hash(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8", errors="ignore")).hexdigest()

def parse_when(entry):
    # Try published or updated; fall back to now
    for k in ("published_parsed","updated_parsed"):
        if getattr(entry, k, None):
            try:
                return datetime(*getattr(entry, k)[:6], tzinfo=timezone.utc)
            except Exception:
                pass
    return now_utc()

def clean(s):
    if not s: return ""
    return html.unescape(re.sub(r"\s+", " ", s)).strip()

def domain_from_url(u):
    try:
        return urlparse(u).netloc.replace("www.","")
    except Exception:
        return "source"

def send_telegram(text, disable_preview=False):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": disable_preview
    }
    r = requests.post(url, json=payload, timeout=20)
    r.raise_for_status()
    return r.json()

# ====== Scoring ======

# Hype phrases that often precede FOMO moves (both bull and bear)
HYPE_PATTERNS = [
    (r"\ball[-\s]?time high\b|\bATH\b",                            0.35),
    (r"\braz(es|ing)|soars?|surges?|spikes?|skyrockets?\b",        0.25),
    (r"\bplunges?|tanks?|crash(es)?|collaps(es)?\b",               0.25),
    (r"\bETF\b|\bSpot ETF\b",                                      0.20),
    (r"\bbreak(?:s|ing)?\s+(?:out|down)\b",                        0.18),
    (r"\bwhale[s]?\b|\bliquidation[s]?\b",                         0.12),
    (r"\bSEC\b|\bregulator|lawsuit|approval|reject(?:ion)?\b",     0.12),
]

POS_WORDS = {"bullish","optimistic","buy","accumulate","rally","breakout"}
NEG_WORDS = {"bearish","fear","sell","dump","liquidation","crackdown"}

def phrase_boost(text):
    t = text.lower()
    boost = 0.0
    for pat, w in HYPE_PATTERNS:
        if re.search(pat, t, flags=re.I):
            boost += w
    # token-level light boost
    if any(w in t for w in POS_WORDS): boost += 0.05
    if any(w in t for w in NEG_WORDS): boost += 0.05
    return boost

def score_item(source_weight, title, summary):
    # VADER sentiment primarily from title, with a bit of summary
    t = clean(title)
    s = clean(summary)
    vs_title = analyzer.polarity_scores(t)["compound"]
    vs_sum   = analyzer.polarity_scores(s)["compound"] if s else 0.0
    vs = 0.7*vs_title + 0.3*vs_sum

    # Convert VADER from [-1,1] to absolute intensity [0,1]
    sentiment_intensity = abs(vs)

    # Add hype phrase boost
    hype = phrase_boost(f"{t}. {s}")

    # Combine (cap at 1.0), then weight by source_weight (1.0 = neutral)
    raw = min(1.0, sentiment_intensity + hype)
    final = max(0.0, min(1.0, raw * float(source_weight)))
    # Direction for label
    direction = "📈 Bullish" if vs > 0 else ("📉 Bearish" if vs < 0 else "😐 Neutral")
    return final, direction, vs  # keep raw vs for sign

# ====== Main ======

def run():
    seen = load_state()
    cutoff = now_utc().timestamp() - RECENT_HOURS*3600

    items = []
    for src in load_sources():
        url = src["url"]
        weight = src.get("weight", 1.0)
        try:
            feed = feedparser.parse(url)
        except Exception:
            continue
        for e in feed.entries[:30]:
            title   = clean(getattr(e,"title",""))
            summary = clean(getattr(e,"summary",""))
            link    = getattr(e,"link","")
            when    = parse_when(e)
            if when.timestamp() < cutoff:
                continue
            uid = to_hash(f"{title}|{link}")
            if uid in seen:
                continue
            score, direction, vs_raw = score_item(weight, title, summary)
            items.append({
                "uid": uid,
                "score": score,
                "direction": direction,
                "vs": vs_raw,
                "title": title,
                "summary": summary,
                "link": link,
                "when": when,
                "srcname": src.get("name", domain_from_url(link)),
                "domain": domain_from_url(link)
            })

    # Rank by score, most hyped first
    items.sort(key=lambda x: x["score"], reverse=True)

    sent = 0
    for it in items:
        if it["score"] < MIN_SCORE_TO_ALERT:
            continue
        if sent >= MAX_ITEMS_PER_RUN:
            break

        ts = it["when"].astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        percent = int(round(it["score"]*100))
        # Short, scannable message
        msg = (
            f"<b>HYPEWATCH {percent}%</b> — {it['direction']}\n"
            f"<b>{html.escape(it['title'])}</b>\n"
            f"Source: {html.escape(it['srcname'])} ({html.escape(it['domain'])})\n"
            f"When: {ts}\n"
            f"<a href=\"{html.escape(it['link'])}\">Open</a>"
        )
        try:
            send_telegram(msg, disable_preview=True)
            sent += 1
            seen.add(it["uid"])
            time.sleep(0.5)
        except Exception as ex:
            # Don’t crash the whole run if one send fails
            try:
                send_telegram(f"HypeWatcher send failed: {ex}", disable_preview=True)
            except Exception:
                pass

    # Heartbeat if nothing sent
    if sent == 0:
        try:
            send_telegram("🫀 HypeWatcher heartbeat — no high-hype items this cycle.", disable_preview=True)
        except Exception:
            pass

    save_state(seen)

if __name__ == "__main__":
    run()
if __name__ == "__main__": MIN_SCORE_TO_ALERT = 0.75
