# sentiment_summary.py
# Scans RSS headlines (news_sources.json), classifies coin mentions as bullish/bearish/neutral,
# and sends a summary ONLY when a coin's sentiment is >= 75% one-sided.
# Reuses the same secrets and deps as HypeWatcher.

import os, json, re, html
from datetime import datetime, timezone, timedelta

import requests
import feedparser
from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

# ====== Config ======
RECENT_HOURS            = 24     # lookback window for headlines
MIN_MENTIONS_PER_COIN   = 4      # require at least N mentions to avoid tiny-sample noise
STRONG_THRESHOLD_PCT    = 75     # "only show if >= 75% one-sided"
MAX_ENTRIES_PER_FEED    = 50

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID")
assert BOT_TOKEN, "Missing TELEGRAM_BOT_TOKEN secret"
assert CHAT_ID,   "Missing TELEGRAM_CHAT_ID secret"

# Coin name/alias matching (simple and fast)
COIN_MAP = {
    "BTC": ["bitcoin", "btc"],
    "ETH": ["ethereum", "eth"],
    "SOL": ["solana", "sol"],
    "BNB": ["bnb", "binance coin"],
    "DOGE": ["doge", "dogecoin"],
    "ADA": ["cardano", "ada"],
    # Add more if you like:
    # "XRP": ["xrp", "ripple"],
    # "LINK": ["chainlink", "link"],
}

analyzer = SentimentIntensityAnalyzer()

# ====== Helpers ======
def now_utc():
    return datetime.now(timezone.utc)

def load_sources():
    with open("news_sources.json", "r", encoding="utf-8") as f:
        data = json.load(f)
    return data["sources"]

def parse_time(entry):
    for k in ("published_parsed","updated_parsed"):
        if getattr(entry, k, None):
            try:
                return datetime(*getattr(entry, k)[:6], tzinfo=timezone.utc)
            except Exception:
                pass
    return now_utc()

def clean(s: str) -> str:
    if not s: return ""
    return html.unescape(re.sub(r"\s+", " ", s)).strip()

def detect_coins(text: str):
    """Return a set of coin symbols mentioned in text."""
    low = text.lower()
    found = set()
    for sym, aliases in COIN_MAP.items():
        if any(a in low for a in aliases):
            found.add(sym)
    return found

def classify_sentiment(title: str, summary: str) -> str:
    """Return 'bull', 'bear', or 'neutral' based on VADER compound."""
    t = clean(title)
    s = clean(summary)
    vs_t = analyzer.polarity_scores(t)["compound"]
    vs_s = analyzer.polarity_scores(s)["compound"] if s else 0.0
    score = 0.7*vs_t + 0.3*vs_s
    if score > 0.10:
        return "bull"
    elif score < -0.10:
        return "bear"
    else:
        return "neutral"

def send_telegram(text: str, silent: bool = False):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
        "disable_notification": silent
    }
    r = requests.post(url, json=payload, timeout=20)
    r.raise_for_status()

# ====== Main ======
def run():
    cutoff = now_utc() - timedelta(hours=RECENT_HOURS)
    counts = {sym: {"bull": 0, "bear": 0, "neutral": 0, "total": 0} for sym in COIN_MAP.keys()}

    # Gather mentions across feeds
    for src in load_sources():
        url = src["url"]
        try:
            feed = feedparser.parse(url)
        except Exception:
            continue

        for e in feed.entries[:MAX_ENTRIES_PER_FEED]:
            when = parse_time(e)
            if when < cutoff:
                continue
            title   = clean(getattr(e, "title", ""))
            summary = clean(getattr(e, "summary", ""))
            if not title and not summary:
                continue

            coins = detect_coins(f"{title} {summary}")
            if not coins:
                continue

            label = classify_sentiment(title, summary)
            for sym in coins:
                counts[sym][label] += 1
                counts[sym]["total"] += 1

    # Build strong-only summary (>= 75% one-sided, with min mentions)
    lines = []
    for sym, c in counts.items():
        total = c["total"]
        if total < MIN_MENTIONS_PER_COIN:
            continue
        bull_pct = int(round(100.0 * c["bull"] / total))
        bear_pct = int(round(100.0 * c["bear"] / total))

        if bull_pct >= STRONG_THRESHOLD_PCT:
            lines.append(f"{sym}: 📈 <b>{bull_pct}% bullish</b> ({total} mentions)")
        elif bear_pct >= STRONG_THRESHOLD_PCT:
            lines.append(f"{sym}: 📉 <b>{bear_pct}% bearish</b> ({total} mentions)")

    if lines:
        msg = "🧠 <b>Strong News Sentiment (last 24h)</b> — threshold ≥ {}%\n".format(STRONG_THRESHOLD_PCT)
        msg += "\n".join(lines)
        send_telegram(msg, silent=False)
    else:
        send_telegram("🧠 Sentiment summary: no coins ≥ {}% one-sided this cycle.".format(STRONG_THRESHOLD_PCT), silent=True)

if __name__ == "__main__":
    run()
