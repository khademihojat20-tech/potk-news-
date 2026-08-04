#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import json
import time
import re
import hashlib
import logging
import threading
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

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
LAST_UPDATE_ID_FILE = Path("last_update_id.json")

MAX_ITEMS_PER_RUN = 5          # تعداد خبر RSS در هر چرخه
RUN_TIMES = ["08:00", "12:00", "16:00", "20:00"]
BREAKING_CHECK_MINUTES = 20    # بررسی کانال‌های منبع

SOURCE_CHANNEL_USERNAMES = ["RoidBest", "khabari_18"]  # بدون @ — ربات باید ادمین این کانال‌ها باشد

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

# قفل‌ها برای جلوگیری از نوشتن هم‌زمان و خراب‌شدن فایل‌های JSON
# وقتی چند ترد هم‌زمان روی cache.json و seen.json کار می‌کنند لازم است
_cache_lock = threading.Lock()
_seen_lock = threading.Lock()

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
last_update_id = load_json(LAST_UPDATE_ID_FILE, 0)

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
# بازنویسی متن (Ultra Fast با کش)
# -----------------------------
def rewrite_text(raw_text, source_label=""):
    aid = article_id_from_text(raw_text)

    with _cache_lock:
        if aid in cache:
            return cache[aid]

    result = call_groq(f"""
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
""")

    with _cache_lock:
        cache[aid] = result
        save_json(CACHE_FILE, cache)

    return result

def rewrite_article(article):
    text = f"عنوان: {article['title']}\n\nخلاصه:\n{article['summary']}"
    source_label = article.get("source", "RSS")
    return rewrite_text(text, source_label=source_label)

# -----------------------------
# تشخیص خبر فوری
# -----------------------------
def is_breaking_text(text):
    keywords = ["فوری", "خبر فوری", "urgent", "breaking"]
    t = text.lower()
    return any(k in t for k in keywords)

# -----------------------------
# استخراج عکس از فید RSS
# -----------------------------
def extract_image_from_entry(entry):
    # ۱) media_content یا media_thumbnail (رایج در بیشتر فیدهای خبری)
    for key in ("media_content", "media_thumbnail"):
        media = entry.get(key)
        if media:
            url = media[0].get("url")
            if url:
                return url

    # ۲) enclosure (لینک ضمیمه با نوع عکس)
    for link in entry.get("links", []):
        if link.get("type", "").startswith("image/"):
            return link.get("href")

    # ۳) جستجوی تگ <img> داخل خلاصه/توضیحات HTML
    html = entry.get("summary", "") or entry.get("description", "")
    match = re.search(r'<img[^>]+src="([^"]+)"', html)
    if match:
        return match.group(1)

    return None

# -----------------------------
# ارسال به تلگرام
# -----------------------------
def post(text, breaking=False, image_url=None):
    prefix = "🚨 <b>خبر فوری</b>\n\n" if breaking else ""
    full_text = f"{prefix}{text}\n\n📡 {CHANNEL_NAME}"

    if image_url:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
        payload = {
            "chat_id": TELEGRAM_CHANNEL_ID,
            "photo": image_url,
            # کپشن تلگرام حداکثر ۱۰۲۴ کاراکتر است
            "caption": full_text[:1024],
            "parse_mode": "HTML",
        }
        r = requests.post(url, json=payload, timeout=20)
        if r.status_code == 200:
            return True
        log.warning(f"ارسال عکس ناموفق بود ({r.status_code})، پیام به‌صورت متنی ارسال می‌شود.")
        # اگر عکس شکست خورد، به حالت متنی ساده برمی‌گردیم

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHANNEL_ID,
        "text": full_text,
        "parse_mode": "HTML",
    }
    r = requests.post(url, json=payload, timeout=20)
    if r.status_code != 200:
        log.error(f"ارسال به تلگرام ناموفق بود: {r.status_code} {r.text}")
        return False
    return True

# -----------------------------
# خواندن کانال‌های تلگرام منبع
# -----------------------------
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
            text = msg.get("text") or msg.get("caption") or ""
            if not text.strip():
                continue
            photo_file_id = None
            if msg.get("photo"):
                # بزرگ‌ترین سایز عکس آخرین آیتم لیست است
                photo_file_id = msg["photo"][-1]["file_id"]
            messages.append({"text": text, "photo_file_id": photo_file_id})

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
                "source": parsed.feed.get("title", "RSS"),
                "image_url": extract_image_from_entry(entry),
            })
    return fresh

# -----------------------------
# چرخه‌ی RSS (Ultra Fast با ThreadPool)
# -----------------------------
def run_cycle_rss():
    log.info("شروع بررسی خبرهای جدید RSS...")
    fresh = fetch_new_articles()

    if not fresh:
        log.info("خبر جدیدی از RSS نیست.")
        return

    # فقط تعداد محدود برای جلوگیری از Rate Limit
    fresh = fresh[:MAX_ITEMS_PER_RUN]

    futures = {}
    with ThreadPoolExecutor(max_workers=4) as executor:
        for article in fresh:
            futures[executor.submit(rewrite_article, article)] = article

        for future in as_completed(futures):
            article = futures[future]
            try:
                text = future.result()
                combined = article["title"] + " " + article["summary"]
                post(text, breaking=is_breaking_text(combined), image_url=article.get("image_url"))
                log.info(f"خبر RSS منتشر شد: {article['title'][:50]}")
                with _seen_lock:
                    seen.add(article["id"])
                    save_json(SEEN_FILE, list(seen))
                time.sleep(1)
            except Exception as e:
                log.error(f"خطا در پردازش خبر RSS «{article['title'][:40]}»: {e}")

    log.info(f"پایان چرخه RSS — {len(fresh)} خبر پردازش شد.")

# -----------------------------
# چرخه‌ی کانال‌های تلگرام (Ultra Fast)
# -----------------------------
def run_cycle_channels():
    log.info("بررسی کانال‌های تلگرام منبع...")
    msgs = fetch_from_source_channels()
    if not msgs:
        log.info("پیام جدیدی از کانال‌های منبع نیست.")
        return

    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {executor.submit(rewrite_text, m["text"], "کانال تلگرام"): m for m in msgs}

        for future in as_completed(futures):
            m = futures[future]
            try:
                text = future.result()
                post(text, breaking=is_breaking_text(m["text"]), image_url=m.get("photo_file_id"))
                log.info("پیام کانال منبع منتشر شد.")
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

    # زمان‌بندی‌ها
    for t in RUN_TIMES:
        schedule.every().day.at(t).do(run_cycle_rss)

    schedule.every(BREAKING_CHECK_MINUTES).minutes.do(run_cycle_channels)

    # اجرای اولیه
    run_cycle_rss()
    run_cycle_channels()

    while True:
        schedule.run_pending()
        time.sleep(10)

if __name__ == "__main__":
    main()
