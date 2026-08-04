#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import json
import time
import hashlib
import logging
from pathlib import Path

import feedparser
import requests
import schedule

# -----------------------------
# تنظیمات
# -----------------------------
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHANNEL_ID = "@potknew"  # کانال مقصد

GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL = "llama-3.1-8b-instant"

CHANNEL_NAME = "پتک نیوز"
SEEN_FILE = Path("seen.json")
CACHE_FILE = Path("cache.json")

MAX_ITEMS_PER_RUN = 3
RUN_TIMES = ["08:00", "12:00", "16:00", "20:00"]
BREAKING_CHECK_MINUTES = 20

SOURCE_CHANNEL_USERNAMES = ["RoidBest", "khabari_18"]  # بدون @

RSS_FEEDS = [
    "https://www.donya-e-eqtesad.com/rss",
    "https://www.eghtesadnews.com/rss",
    "https://www.reuters.com/business/finance/rss",
    "https://www.zoomit.ir/feed/",
    "https://digiato.com/feed",
    "https://techcrunch.com/feed/",
]

logging.basicConfig(level=logging.INFO, format="%(asctime)s [پتک‌نیوز] %(message)s")
log = logging.getLogger("petak-news")

seen = set()
queue_file = Path("queue.json")

def load_json(path, default):
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return default

def save_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

seen = set(load_json(SEEN_FILE, []))
cache = load_json(CACHE_FILE, {})
queue = load_json(queue_file, [])

# -----------------------------
# ابزارهای کمکی
# -----------------------------
def article_id_from_text(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()

def article_id(entry):
    base = entry.get("id") or entry.get("link") or entry.get("title", "")
    return hashlib.sha256(base.encode("utf-8")).hexdigest()

# -----------------------------
# تماس با Groq
# -----------------------------
def call_groq(prompt):
    if not GROQ_API_KEY:
        log.error("متغیر محیطی GROQ_API_KEY تنظیم نشده است.")
        raise RuntimeError("GROQ_API_KEY_MISSING")

    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json",
    }

    payload = {
        "model": GROQ_MODEL,
        "messages": [
            {"role": "user", "content": prompt}
        ],
        "temperature": 0.4,
    }

    r = requests.post(GROQ_URL, headers=headers, json=payload, timeout=30)

    if r.status_code != 200:
        log.error(f"خطا در Groq: {r.status_code} {r.text}")
        raise RuntimeError("GROQ_ERROR")

    data = r.json()
    return data["choices"][0]["message"]["content"].strip()

# -----------------------------
# بازنویسی خبر (RSS یا کانال)
# -----------------------------
def rewrite_text(raw_text, source_label=""):
    aid = article_id_from_text(raw_text)
    if aid in cache:
        return cache[aid]

    prompt = f"""
تو خبرنگار حرفه‌ای کانال خبری «پتک نیوز» هستی.
متن زیر را کامل بخوان و یک خبر تازه، منسجم و قابل‌فهم برای مخاطب فارسی‌زبان بنویس.

قوانین:
- در خط اول، یک عنوان خبری کوتاه و جذاب با ایموجی مرتبط بنویس.
- در بدنه، ۳ تا ۶ جمله بنویس که:
  - ماجرا را از ابتدا تا انتها توضیح دهد،
  - نکات مهم، اعداد، نام‌ها و زمان‌ها را حفظ کند،
  - هیچ تحلیل شخصی یا نظر اضافه نداشته باشد.
- اگر متن اصلی پراکنده است، آن را منظم و قابل‌فهم کن.
- هیچ نام کانال، لینک، یوزرنیم یا تبلیغی از متن اصلی را تکرار نکن.
- در پایان، یک خط منبع کوتاه ثابت بنویس: «🔗 منبع: پتک نیوز»
- در انتها ۲ تا ۳ هشتگ فارسی مرتبط اضافه کن.
- فقط متن نهایی پست خبری را بده.

متن خبر:
{raw_text}
"""

    try:
        result = call_groq(prompt)
    except RuntimeError as e:
        log.error(f"خطا در بازنویسی با Groq: {e}")
        raise

    cache[aid] = result
    save_json(CACHE_FILE, cache)
    return result

def rewrite_article(article):
    text = f"عنوان: {article['title']}\n\nخلاصه:\n{article['summary']}"
    return rewrite_text(text, source_label=article["source"])

# -----------------------------
# تشخیص خبر فوری
# -----------------------------
def is_breaking_text(text):
    # ساده: اگر کلمات کلیدی باشد، فوری
    keywords = ["فوری", "خبر فوری", "urgent", "breaking"]
    t = text.lower()
    return any(k in t for k in keywords)

# -----------------------------
# ارسال به تلگرام
# -----------------------------
def post(text, breaking=False):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    prefix = "🚨 <b>خبر فوری</b>\n\n" if breaking else ""
    payload = {
        "chat_id": TELEGRAM_CHANNEL_ID,
        "text": f"{prefix}{text}\n\n📡 {CHANNEL_NAME}",
        "parse_mode": "HTML",
    }
    r = requests.post(url, json=payload, timeout=20)
    if r.status_code != 200:
        log.error(f"ارسال به تلگرام ناموفق بود: {r.status_code} {r.text}")
        return False
    return True

# -----------------------------
# خواندن کانال‌های تلگرام (منبع)
# -----------------------------
LAST_UPDATE_ID_FILE = Path("last_update_id.json")
last_update_id = load_json(LAST_UPDATE_ID_FILE, 0)

def fetch_from_source_channels():
    global last_update_id
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
    params = {
        "offset": last_update_id + 1,
        "timeout": 10,
    }
    try:
        r = requests.get(url, params=params, timeout=20)
    except Exception as e:
        log.error(f"خطا در getUpdates: {e}")
        return []

    if r.status_code != 200:
        log.error(f"getUpdates ناموفق بود: {r.status_code} {r.text}")
        return []

    data = r.json()
    updates = data.get("result", [])
    messages = []

    for upd in updates:
        last_update_id = upd["update_id"]
        msg = upd.get("message") or upd.get("channel_post")
        if not msg:
            continue

        chat = msg.get("chat", {})
        username = chat.get("username", "")
        if username in SOURCE_CHANNEL_USERNAMES:
            text = msg.get("text") or ""
            if not text.strip():
                continue
            messages.append(text)

    save_json(LAST_UPDATE_ID_FILE, last_update_id)
    return messages

# -----------------------------
# جمع‌آوری خبرهای جدید از RSS
# -----------------------------
def fetch_new_articles():
    fresh = []
    for url in RSS_FEEDS:
        try:
            parsed = feedparser.parse(url)
        except Exception as e:
            log.warning(f"خطا در فید {url}: {e}")
            continue

        for entry in parsed.entries[:6]:
            aid = article_id(entry)
            if aid in seen:
                continue
            fresh.append({
                "id": aid,
                "title": entry.get("title", "").strip(),
                "summary": entry.get("summary", "")[:1500],
            })
    return fresh

# -----------------------------
# صف خبرهای معمولی
# -----------------------------
def add_to_queue(text):
    queue.append(text)
    save_json(queue_file, queue)

def process_queue_once():
    if not queue:
        return
    text = queue.pop(0)
    save_json(queue_file, queue)
    post(text, breaking=False)

# -----------------------------
# چرخه‌ی اصلی RSS
# -----------------------------
def run_cycle_rss():
    log.info("شروع بررسی خبرهای جدید RSS...")
    fresh = fetch_new_articles()

    if not fresh:
        log.info("خبر جدیدی از RSS نیست.")
        return

    published = 0
    for article in fresh:
        if published >= MAX_ITEMS_PER_RUN:
            break

        try:
            text = rewrite_article(article)
            if is_breaking_text(article["title"] + " " + article["summary"]):
                post(text, breaking=True)
            else:
                add_to_queue(text)
            seen.add(article["id"])
            published += 1
            log.info(f"خبر RSS پردازش شد: {article['title'][:50]}")
            time.sleep(2)
        except Exception as e:
            log.error(f"خطا در پردازش خبر RSS «{article['title'][:40]}»: {e}")

    save_json(SEEN_FILE, list(seen))
    log.info(f"پایان چرخه RSS — {published} خبر پردازش شد.")

# -----------------------------
# چرخه‌ی کانال‌های تلگرام
# -----------------------------
def run_cycle_channels():
    log.info("بررسی کانال‌های تلگرام منبع...")
    msgs = fetch_from_source_channels()
    if not msgs:
        log.info("پیام جدیدی از کانال‌های منبع نیست.")
        return

    for raw in msgs:
        try:
            text = rewrite_text(raw_text=raw, source_label="کانال تلگرام")
            if is_breaking_text(raw):
                post(text, breaking=True)
            else:
                add_to_queue(text)
            log.info("پیام کانال منبع پردازش شد.")
            time.sleep(1)
        except Exception as e:
            log.error(f"خطا در پردازش پیام کانال منبع: {e}")

# -----------------------------
# اجرای اصلی
# -----------------------------
def main():
    log.info("پتک نیوز — ربات راه‌اندازی شد.")

    if not TELEGRAM_BOT_TOKEN:
        log.error("TELEGRAM_BOT_TOKEN تنظیم نشده است.")
        return
    if not GROQ_API_KEY:
        log.error("GROQ_API_KEY تنظیم نشده است.")
        return

    # چرخه‌های زمان‌بندی‌شده
    for t in RUN_TIMES:
        schedule.every().day.at(t).do(run_cycle_rss)

    schedule.every(BREAKING_CHECK_MINUTES).minutes.do(run_cycle_channels)
    schedule.every().hour.do(process_queue_once)

    # اجرای اولیه
    run_cycle_rss()
    run_cycle_channels()

    while True:
        schedule.run_pending()
        time.sleep(10)

if __name__ == "__main__":
    main()
