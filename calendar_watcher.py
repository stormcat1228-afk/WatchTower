# calendar_watcher.py
# Auto calendar sentry for crypto-impact macro events.
# - Scrapes official calendars (BLS/BEA/Fed) for CPI/PPI/NFP/PCE/GDP/FOMC
# - Also computes Jobless Claims (Thu 08:30 ET) + NFP (first Fri 08:30 ET)
# - Alerts T-4 days (majors) and T-90 minutes (all), once per event/phase
#
# ENV (GitHub Actions Secrets):
#   TELEGRAM_BOT_TOKEN
#   TELEGRAM_CHAT_ID
#
# Requirements: requests

import os, re, json, hashlib
from pathlib import Path
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import requests

# -------------------- Config --------------------
TIMEZONE = "America/New_York"
TZ = ZoneInfo(TIMEZONE)

MAJORS = {"CPI", "PCE", "PPI", "GDP", "NFP", "FOMC"}
T4D_MINUTES = 4 * 24 * 60      # 5760
T90_MINUTES = 90

STATE_FILE = ".calendar_state.json"  # dedupe memory (committed in runner workspace)

BOT  = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT = os.getenv("TELEGRAM_CHAT_ID")
if not BOT or not CHAT:
    raise SystemExit("Missing TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID")

# -------------------- Utils --------------------
def md5(s: str) -> str:
    return hashlib.md5(s.encode("utf-8")).hexdigest()

def now_utc():
    return datetime.now(ZoneInfo("UTC"))

def to_local(dt):
    return dt.astimezone(TZ)

def fmt_local(dt):
    return to_local(dt).strftime("%a %b %d · %I:%M %p %Z")

def minutes_until(dt_utc, ref_utc=None):
    if ref_utc is None:
        ref_utc = now_utc()
    return int((dt_utc - ref_utc).total_seconds() // 60)

def send(msg: str, loud: bool = False):
    try:
        requests.post(
            f"https://api.telegram.org/bot{BOT}/sendMessage",
            json={
                "chat_id": CHAT,
                "text": msg,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
                "disable_notification": (not loud),
            },
            timeout=20,
        )
    except Exception:
        pass

def load_state():
    p = Path(STATE_FILE)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return {"sent": {}}
    return {"sent": {}}

def save_state(st):
    try:
        Path(STATE_FILE).write_text(json.dumps(st), encoding="utf-8")
    except Exception:
        pass

def mark_sent(st, key):
    st["sent"][key] = int(now_utc().timestamp() * 1000)

def already_sent(st, key) -> bool:
    return key in st.get("sent", {})

# -------------------- HTTP + parsing --------------------
def get(url, timeout=25) -> str:
    r = requests.get(url, timeout=timeout, headers={"User-Agent": "Mozilla/5.0 (CalendarWatcher)"})
    r.raise_for_status()
    return r.text

def parse_dates(text: str, default_hm=(0, 0)) -> list[datetime]:
    """
    Extracts datetimes from text. Supports:
      - ISO-ish: "2025-09-11 08:30"
      - Month name forms: "September 11, 2025 8:30 AM", "Sep 11, 2025"
    If time missing, uses default_hm (hour, minute).
    Returns tz-aware datetimes in local TZ, converted to UTC on return.
    """
    outs = []

    # ISO "YYYY-MM-DD HH:MM"
    for m in re.findall(r"(\d{4}-\d{2}-\d{2})\s+(\d{1,2}:\d{2})", text):
        raw = f"{m[0]} {m[1]}"
        try:
            dt = datetime.strptime(raw, "%Y-%m-%d %H:%M").replace(tzinfo=TZ)
            outs.append(dt)
        except Exception:
            pass

    # Month name with/without time
    mon = r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*"
    # with time
    pat_time = rf"{mon}\s+(\d{{1,2}}),\s*(\d{{4}})\s+(\d{{1,2}}:\d{{2}})\s*(AM|PM)?"
    for m in re.findall(pat_time, text, flags=re.I):
        month, day, year, hm, ampm = m[0], m[1], m[2], m[3], m[4]
        raw = f"{month} {day}, {year} {hm} {ampm or ''}".strip()
        try:
            fmt = "%b %d, %Y %I:%M %p" if ampm else "%b %d, %Y %H:%M"
            dt = datetime.strptime(raw, fmt).replace(tzinfo=TZ)
            outs.append(dt)
        except Exception:
            pass

    # without time (assume default_hm)
    pat_date = rf"{mon}\s+(\d{{1,2}}),\s*(\d{{4}})"
    for m in re.findall(pat_date, text, flags=re.I):
        month, day, year = m[0], m[1], m[2]
        raw = f"{month} {day}, {year}"
        try:
            dt = datetime.strptime(raw, "%b %d, %Y").replace(tzinfo=TZ)
            dt = dt.replace(hour=default_hm[0], minute=default_hm[1])
            outs.append(dt)
        except Exception:
            pass

    # return unique (by minute), as UTC
    uniq = {}
    for dt in outs:
        key = dt.strftime("%Y-%m-%d %H:%M")
        uniq[key] = dt
    return [d.astimezone(ZoneInfo("UTC")) for d in sorted(uniq.values())]

# -------------------- Site scrapers --------------------
def pull_bls_events() -> list[dict]:
    """
    BLS: CPI, PPI, Employment Situation (NFP)
    Default release time if missing: 08:30 ET
    """
    url = "https://www.bls.gov/bls/news-release/home.htm"
    text = get(url)
    events = []
    default_hm = (8, 30)
    for key, label in [
        ("Consumer Price Index", "CPI"),
        ("Producer Price Index", "PPI"),
        ("Employment Situation", "NFP"),
    ]:
        idx = text.lower().find(key.lower())
        if idx == -1:
            continue
        chunk = text[max(0, idx - 1500): idx + 1500]
        dts = parse_dates(chunk, default_hm)
        for dt in dts:
            if dt > now_utc() - timedelta(hours=1):
                events.append({"name": label, "dt": dt, "source": "BLS", "impact": "high"})
    return events

def pull_bea_events() -> list[dict]:
    """
    BEA: PCE/Personal Income & Outlays, GDP
    Default release time if missing: 08:30 ET
    """
    url = "https://www.bea.gov/news/schedule"
    text = get(url)
    events = []
    default_hm = (8, 30)
    for key, label in [
        ("Personal Income and Outlays", "PCE"),
        ("Gross Domestic Product", "GDP"),
    ]:
        idx = text.lower().find(key.lower())
        if idx == -1:
            continue
        chunk = text[max(0, idx - 2500): idx + 2500]
        dts = parse_dates(chunk, default_hm)
        for dt in dts:
            if dt > now_utc() - timedelta(hours=1):
                events.append({"name": label, "dt": dt, "source": "BEA", "impact": "high"})
    return events

def pull_fomc_events() -> list[dict]:
    """
    Federal Reserve: FOMC meeting/decision days
    Default decision time if missing: 14:00 ET
    """
    url = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"
    text = get(url)
    events = []
    default_hm = (14, 0)
    dts = parse_dates(text, default_hm)
    for dt in dts:
        if dt > now_utc() - timedelta(hours=1):
            events.append({"name": "FOMC", "dt": dt, "source": "FED", "impact": "high"})
    return events

# -------------------- Recurrence (computed) --------------------
def build_jobless_claims(horizon_days=60) -> list[dict]:
    """Every Thursday 08:30 ET within horizon."""
    out = []
    start_local = to_local(now_utc())
    end_local = start_local + timedelta(days=horizon_days)
    # move to next Thursday 08:30
    cur = start_local.replace(hour=8, minute=30, second=0, microsecond=0)
    add_days = (3 - cur.weekday()) % 7  # Thu=3
    cur = cur + timedelta(days=add_days)
    while cur <= end_local:
        out.append({"name": "Jobless Claims", "dt": cur.astimezone(ZoneInfo("UTC")), "source": "Rule", "impact": "med"})
        cur += timedelta(days=7)
    return out

def first_friday(year: int, month: int) -> datetime:
    """Return first Friday 08:30 ET of month as UTC."""
    d = datetime(year, month, 1, 8, 30, tzinfo=TZ)
    # Friday=4 (Mon=0)
    offset = (4 - d.weekday()) % 7
    d = d + timedelta(days=offset)
    return d.astimezone(ZoneInfo("UTC"))

def build_nfp(horizon_months=2) -> list[dict]:
    """NFP first Friday 08:30 ET for current + next month(s)."""
    out = []
    local_now = to_local(now_utc())
    year, month = local_now.year, local_now.month
    for i in range(horizon_months):
        m = month + i
        y = year + (m - 1) // 12
        m = ((m - 1) % 12) + 1
        dt = first_friday(y, m)
        if dt >= now_utc() - timedelta(hours=1):
            out.append({"name": "NFP", "dt": dt, "source": "Rule", "impact": "high"})
    return out

# -------------------- Build unified event list --------------------
def build_events():
    events = []

    # Computed rules
    events.extend(build_jobless_claims())
    events.extend(build_nfp())

    # Official calendars (scraped)
    try:
        events.extend(pull_bls_events())
    except Exception as e:
        print("WARN BLS:", e)

    try:
        events.extend(pull_bea_events())
    except Exception as e:
        print("WARN BEA:", e)

    try:
        events.extend(pull_fomc_events())
    except Exception as e:
        print("WARN FOMC:", e)

    # Deduplicate by (name, date) keep earliest time
    seen = {}
    for ev in events:
        k = (ev["name"], to_local(ev["dt"]).date())
        if k not in seen or ev["dt"] < seen[k]["dt"]:
            seen[k] = ev

    # Sort by datetime
    return sorted(seen.values(), key=lambda e: e["dt"])

# -------------------- Alert phases --------------------
def alert_T4d(ev, st):
    """Heads-up ~4 days before (majors only). Fires once when within <= T4D_MINUTES and > 0."""
    if ev["name"] not in MAJORS:
        return False
    mins = minutes_until(ev["dt"])
    if not (0 < mins <= T4D_MINUTES):
        return False
    key = md5(f"{ev['name']}|{ev['dt'].isoformat()}|T-4d")
    if already_sent(st, key):
        return False

    msg = (
        f"⚠️ <b>Heads-up (T-4d): {ev['name']}</b>\n"
        f"When: {fmt_local(ev['dt'])}\n"
        f"Source: {ev.get('source','')}\n"
        f"Why: typically increases volatility across BTC/ETH.\n"
        f"→ Playbook: plan risk; avoid holding fresh positions into the release window."
    )
    send(msg, loud=False)
    mark_sent(st, key)
    return True

def alert_T90(ev, st):
    """Pre-alert 90 minutes before (all events)."""
    mins = minutes_until(ev["dt"])
    if not (0 < mins <= T90_MINUTES):
        return False
    key = md5(f"{ev['name']}|{ev['dt'].isoformat()}|T-90m")
    if already_sent(st, key):
        return False

    # Brief guidance by event
    name = ev["name"].upper()
    guidance = "Expect higher vol; tighten stops; avoid opening new positions right before the print."
    if name in {"CPI", "PCE", "PPI"}:
        guidance = "Inflation print: BTC often reacts fast to surprise. Consider standing aside for first impulse."
    elif name == "FOMC":
        guidance = "Policy + press conference: whipsaw is common around 2:00–2:45 PM ET."
    elif name == "NFP":
        guidance = "Labor surprise swings DXY/USTs → crypto; expect sharp moves."

    msg = (
        f"⏳ <b>T-90m: {ev['name']}</b>\n"
        f"When: {fmt_local(ev['dt'])}\n"
        f"{guidance}"
    )
    send(msg, loud=True)  # louder ping near the event
    mark_sent(st, key)
    return True

# -------------------- Main --------------------
def run_once():
    st = load_state()
    evs = build_events()

    fired = 0
    for ev in evs:
        # Only consider future events
        if ev["dt"] <= now_utc():
            continue
        # Fire phases (order matters: T-4d first, then T-90m)
        if alert_T4d(ev, st):
            fired += 1
        if alert_T90(ev, st):
            fired += 1

    save_state(st)

if __name__ == "__main__":
    run_once()

