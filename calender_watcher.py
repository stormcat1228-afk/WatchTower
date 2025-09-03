import os
import time
import datetime
import requests

# === CONFIG ===
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# List of recurring events (name, date, time, type)
EVENTS = [
    {"name": "US Jobs Report (NFP)", "date": "2025-09-06", "time": "08:30", "impact": "High"},
    {"name": "US CPI Inflation Report", "date": "2025-09-11", "time": "08:30", "impact": "High"},
    {"name": "FOMC Meeting", "date": "2025-09-18", "time": "14:00", "impact": "High"},
    {"name": "Fed Minutes Release", "date": "2025-09-25", "time": "14:00", "impact": "Medium"}
]

# === HELPERS ===
def send_telegram(message: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}
    try:
        requests.post(url, data=payload)
    except Exception as e:
        print("Error sending Telegram message:", e)

def check_events():
    now = datetime.datetime.now()
    for event in EVENTS:
        event_dt = datetime.datetime.strptime(event["date"] + " " + event["time"], "%Y-%m-%d %H:%M")

        # 3 days warning
        if now + datetime.timedelta(days=3) >= event_dt > now + datetime.timedelta(days=2, hours=23):
            send_telegram(f"⚠️ Heads up: {event['name']} in 3 days.\nImpact: {event['impact']}")

        # 1.5 hours warning
        if now + datetime.timedelta(hours=1, minutes=30) >= event_dt > now + datetime.timedelta(hours=1, minutes=29):
            send_telegram(f"⚠️ Reminder: {event['name']} in ~1.5 hours.\nImpact: {event['impact']}")

        # Event time
        if now >= event_dt and now <= event_dt + datetime.timedelta(minutes=1):
            send_telegram(f"📊 {event['name']} results just released! Markets may react strongly.")

# === LOOP ===
if __name__ == "__main__":
    send_telegram("✅ Calendar Watcher started. Monitoring events...")
    while True:
        check_events()
        time.sleep(60)  # check every minute
