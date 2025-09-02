import os, time, json, hashlib, html
from datetime import datetime, timedelta, timezone
import requests
import feedparser

# ====== CONFIG ======
TIMEZONE = "America/New_York"
RECENT_HOURS = 24
DEDUPE_HOURS = 12
IMPACT_THRESHOLD = 9  # 9/10 = urgent

SOURCES = [
    {"label": "SEC – Press Releases",      "url": "https://www.sec.gov/news/pressreleases.rss"},
    {"label": "SEC – Public Statements",   "url": "https://www.sec.gov/news/speeches.rss"},
    {"label": "CFTC – Press Releases",     "url": "https://www.cftc.gov/PressRoom/PressReleases/rss.xml"},
    {"label": "Federal Reserve – Press",   "url": "https://www.federalreserve.gov/feeds/press_all.xml"},
    {"label": "BLS – CPI News",            "url": "https://www.bls.gov/feeds/news.release.cpi.rss"},
    {"label": "CoinDesk – All",            "url": "https://www.coindesk.com/arc/outboundfeeds/rss/?outputType=xml"},
    {"label": "CoinTelegraph – All",       "url": "https://cointelegraph.com/rss"},
    {"label": "The Block – News",          "url": "https://www.theblock.co/rss"},
]

# Telegram (set as repo secrets where we run)
BOT = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT = os.getenv("TELEGRAM_CHAT_ID")

STATE_FILE = "state.json"  # remembers what we’ve already sent

# ====== helpers ======
def md5(s: str) -> str:
    return hashlib.md5(s.encode("utf-8")).hexdigest()

def is_recent(pub_dt):
    return pub_dt and (datetime.now(timezone.utc) - pub_dt) <= timedelta(hours=RECENT_HOURS)

def score_event(text: str, source: str) -> int:
    t = (text or "").lower()
    s = 0
    if any(x in (source or "").lower() for x in ["sec","cftc","federal reserve","bls"]): s += 4
    if "cpi" in t or "pce" in t: s += 4
    if "fomc" in t or "powell" in t or "interest rate" in t or "minutes" in t: s += 5
    if any(k in t for k in ["sec","cftc","ofac","treasury","nydfs","dfpi"]): s += 3
    if any(k in t for k in ["etf","approval","inflow","outflow"]): s += 3
    if any(k in t for k in ["hack","breach","security incident"]): s += 4
    if any(k in t for k in ["outage","downtime","halt","suspend"]): s += 4
    if any(k in t for k in ["delist","insolvency","bankruptcy","liquidation"]): s += 4
    if any(k in t for k in ["binance","coinbase","kraken","tether","usdt","circle","usdc",
                            "microstrategy","blackrock","fidelity"]): s += 2
    return max(0, min(10, s))

def send_telegram(msg: str, loud=True):
    if not (BOT and CHAT): return
    url = f"https://api.telegram.org/bot{BOT}/sendMessage"
    try:
        requests.post(url, json={
            "chat_id": CHAT, "text": msg, "parse_mode": "HTML",
            "disable_web_page_preview": True, "disable_notification": not loud
        }, timeout=15)
    except Exception:
        pass

def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"sent": {}, "last_gc": 0}

def save_state(st):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(st, f)

def parse_dt(entry):
    try:
        if entry.get("published_parsed"):
            from datetime import timezone
            return datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
    except Exception:
        pass
    return None

def fmt_ts_utc(dt_utc):  # simple: show UTC
    return dt_utc.strftime("%Y-%m-%d %H:%M:%S UTC")

def run_once():
    st = load_state()
    checked = alerted = deduped = skipped_old = 0
    now_ms = time.time() * 1000

    for src in SOURCES:
        try:
            feed = feedparser.parse(src["url"])
            for item in (feed.entries or []):
                checked += 1
                title = (item.get("title") or "").strip()
                link  = (item.get("link") or "").strip()
                summary = (item.get("summary") or item.get("content",[{"value":""}])[0]["value"] or "").strip()
                pub = parse_dt(item)
                if not pub or not is_recent(pub):
                    skipped_old += 1
                    continue

                uid = md5(f'{src["label"]}|{title}|{link}')
                ts = st["sent"].get(uid)
                if ts and (now_ms - ts) <= 3600*1000*DEDUPE_HOURS:
                    deduped += 1
                    continue

                text = f"{title} {summary}"
                sc = score_event(text, src["label"])

                if sc >= IMPACT_THRESHOLD:
                    msg = (
                        f"<b>🚨URGENT {sc}/10</b>\n"
                        f"<b>{html.escape(title)}</b>\n"
                        f"Source: {html.escape(src['label'])}\n"
                        f"When: {fmt_ts_utc(pub)}\n\n"
                        f"{html.escape(summary) if summary else ''}\n"
                        f"{('<a href=\"'+html.escape(link)+'\">Open</a>') if link else ''}"
                    )
                    send_telegram(msg, loud=True)
                    st["sent"][uid] = now_ms
                    alerted += 1
        except Exception:
            pass

    if alerted == 0:
        send_telegram(
            f"🟢 Watchtower heartbeat — no urgent alerts.\n"
            f"Checked: {checked}, Skipped old: {skipped_old}, Deduped: {deduped}",
            loud=False
        )

    if now_ms - st.get("last_gc", 0) > 6*3600*1000:
        st["sent"] = {k:v for k,v in st["sent"].items() if now_ms - v <= 3600*1000*DEDUPE_HOURS}
        st["last_gc"] = now_ms

    save_state(st)

if __name__ == "__main__":
    run_once()
