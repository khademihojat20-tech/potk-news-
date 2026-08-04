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
from google import genai

# -----------------------------
# تنظیمات
# -----------------------------
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHANNEL_ID = os.environ.get("TELEGRAM_CHANNEL_ID")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
GEMINI_MODEL = "gemini-2.5-flash"

CHANNEL_NAME = "پتک نیوز"
SEEN_FILE = Path("seen.json")
CACHE_FILE = Path("cache.json")

MAX_ITEMS_PER_RUN = 3
RUN_TIMES = ["08:00", "12:00", "16:00", "20:00"]
BREAKING_CHECK_MINUTES = 20

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

client = genai.Client(api_key=GEMINI_API_KEY)

# -----------------------------
# فایل‌های حافظه
# -----------------------------
def load_json(path, default):
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return default

def save_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

seen = set(load_json(SEEN_FILE, []))
cache = load_json(CACHE_FILE, {})

# -----------------------------
# ابزارهای کمکی
# -----------------------------
def article_id(entry):
    base = entry.get("id") or entry.get("link") or entry.get("title", "")
    return hashlib.sha256(base.encode("utf-8")).hexdigest()

def rate_limited_call(prompt):
    """تماس امن با Gemini — با مدیریت خطای 429"""
    while True:
        try:
            resp = client.models.generate_content(model=GEMINI_MODEL, contents=prompt)
            return (resp.text or "").strip()
        except Exception as e:
            if "429" in str(e):
                log.warning("سهمیه‌ی Gemini پر شده — صبر می‌کنیم...")
                time.sleep(15)
                continue
            raise e

# -----------------------------
# جمع‌آوری خبرهای جدید
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
                "link": entry.get("link", ""),
                "source": parsed.feed.get("title", url),
            })
    return fresh

# -----------------------------
# بازنویسی کامل — با کش
# -----------------------------
def rewrite(article):
    if article["id"] in cache:
        return cache[article["id"]]

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
- در پایان، یک خط منبع بنویس: «منبع: {article['source']}»
- در انتها ۲ تا ۳ هشتگ فارسی مرتبط اضافه کن.
- از خودت مقدمه یا توضیح اضافه نکن؛ فقط متن نهایی پست خبری را بده.

عنوان اصلی خبر:
{article['title']}

متن/خلاصه خبر:
{article['summary']}
"""

    result = rate_limited_call(prompt)
    cache[article["id"]] = result
    save_json(CACHE_FILE, cache)
    return result

# -----------------------------
# تشخیص خبر فوری — کم‌مصرف
# -----------------------------
def is_breaking(article):
    prompt = f"""
عنوان خبر: {article['title']}

آیا این خبر مهم و فوری است؟ فقط بله یا خیر.
"""
    answer = rate_limited_call(prompt)
    return answer.startswith("بله")

# -----------------------------
# ارسال به تلگرام
# -----------------------------
def post(text, link, breaking=False):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    prefix = "🚨 <b>خبر فوری</b>\n\n" if breaking else ""
    payload = {
        "chat_id": TELEGRAM_CHANNEL_ID,
        "text": f"{prefix}{text}\n\n🔗 {link}\n\n📡 {CHANNEL_NAME}",
        "parse_mode": "HTML",
    }
    r = requests.post(url, json=payload)
    return r.status_code == 200

# -----------------------------
# چرخه‌ی اصلی
# -----------------------------
def run_cycle():
    log.info("شروع بررسی خبرهای جدید...")
    fresh = fetch_new_articles()

    if not fresh:
        log.info("خبر جدیدی نیست.")
        return

    published = 0
    for article in fresh:
        if published >= MAX_ITEMS_PER_RUN:
            break

        try:
            text = rewrite(article)
            if post(text, article["link"]):
                seen.add(article["id"])
                published += 1
                log.info(f"منتشر شد: {article['title'][:50]}")
                time.sleep(2)
        except Exception as e:
            log.error(f"خطا در پردازش: {e}")

    save_json(SEEN_FILE, list(seen))
    log.info(f"پایان چرخه — {published} خبر منتشر شد.")

# -----------------------------
# خبر فوری
# -----------------------------
def check_breaking():
    fresh = fetch_new_articles()
    if not fresh:
        return

    for article in fresh:
        try:
            if is_breaking(article):
                text = rewrite(article)
                if post(text, article["link"], breaking=True):
                    seen.add(article["id"])
                    save_json(SEEN_FILE, list(seen))
                    log.info(f"🚨 خبر فوری منتشر شد: {article['title'][:50]}")
        except Exception as e:
            log.error(f"خطا در خبر فوری: {e}")

# -----------------------------
# اجرای اصلی
# -----------------------------
def main():
    log.info("پتک نیوز — ربات راه‌اندازی شد.")

    for t in RUN_TIMES:
        schedule.every().day.at(t).do(run_cycle)

    schedule.every(BREAKING_CHECK_MINUTES).minutes.do(check_breaking)

    run_cycle()

    while True:
        schedule.run_pending()
        time.sleep(20)

if __name__ == "__main__":
    main()
