#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import json
import time
import re
import hashlib
import logging
import threading
import difflib
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

# مسیر ذخیره‌ی داده‌ها — روی Railway باید یک Volume بسازید و مسیرش را
# در متغیر محیطی DATA_DIR بدهید (مثلاً /data)، وگرنه با هر ری‌دیپلوی
# فایل‌های seen/cache پاک می‌شوند و اخبار قدیمی دوباره پست می‌شوند.
DATA_DIR = Path(os.environ.get("DATA_DIR", "."))
DATA_DIR.mkdir(parents=True, exist_ok=True)

SEEN_FILE = DATA_DIR / "seen.json"
CACHE_FILE = DATA_DIR / "cache.json"
LAST_UPDATE_ID_FILE = DATA_DIR / "last_update_id.json"
RECENT_TITLES_FILE = DATA_DIR / "recent_titles.json"

RECENT_TITLES_MAX = 80          # چند عنوان اخیر برای تشخیص تکرار نگه داشته شود
TITLE_SIMILARITY_THRESHOLD = 0.72   # بالاتر از این حد = خبر تکراری در نظر گرفته می‌شود

MAX_ITEMS_PER_RUN = 5          # تعداد خبر RSS در هر چرخه
RUN_TIMES = ["08:00", "12:00", "16:00", "20:00"]
BREAKING_CHECK_MINUTES = 20    # بررسی کانال‌های منبع

SOURCE_CHANNEL_USERNAMES = ["RoidBest", "khabari_18"]  # بدون @ — ربات باید ادمین این کانال‌ها باشد

RSS_FEEDS = [
    # اقتصاد
    "https://www.tasnimnews.com/fa/rss/feed/0/0/78",   # اقتصادی — تسنیم
    "https://news.google.com/rss/search?q=%D8%A7%D9%82%D8%AA%D8%B5%D8%A7%D8%AF%20%D8%A7%DB%8C%D8%B1%D8%A7%D9%86&hl=fa&gl=IR&ceid=IR:fa",  # اقتصاد ایران — گوگل نیوز

    # جنگ / نظامی / بین‌الملل
    "https://www.tasnimnews.com/fa/rss/feed/0/0/11",   # نظامی | دفاعی | امنیتی — تسنیم
    "https://www.tasnimnews.com/fa/rss/feed/0/0/8",    # بین‌الملل — تسنیم
    "https://www.aljazeera.com/xml/rss/all.xml",       # الجزیره — جنگ و اخبار بین‌المللی

    # تکنولوژی
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
recent_titles = load_json(RECENT_TITLES_FILE, [])
_titles_lock = threading.Lock()

def is_duplicate_title(title):
    """با مقایسه‌ی شباهت متنی، تشخیص می‌دهد آیا این عنوان همان خبر یک عنوان اخیر است."""
    with _titles_lock:
        for t in recent_titles:
            ratio = difflib.SequenceMatcher(None, title, t).ratio()
            if ratio >= TITLE_SIMILARITY_THRESHOLD:
                return True
    return False

def remember_title(title):
    with _titles_lock:
        recent_titles.append(title)
        del recent_titles[:-RECENT_TITLES_MAX]  # فقط آخرین‌ها نگه داشته شود
        save_json(RECENT_TITLES_FILE, recent_titles)

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
def rewrite_text(raw_text, source_label="نامشخص"):
    aid = article_id_from_text(raw_text)

    with _cache_lock:
        if aid in cache:
            return cache[aid]

    result = call_groq(f"""
تو خبرنگار حرفه‌ای کانال خبری «پتک نیوز» هستی.
متن زیر را کامل بخوان و یک خبر کوتاه، فشرده و پرمحتوا برای مخاطب فارسی‌زبان بنویس.

قوانین:
- در خط اول، یک عنوان خبری کوتاه و جذاب با ایموجی مرتبط بنویس.
- در بدنه، حداکثر ۲ تا ۳ جمله‌ی کوتاه بنویس که:
  - فقط مهم‌ترین نکته‌ی خبر را بگوید (اصل ماجرا، نه جزئیات فرعی)،
  - اعداد، نام‌ها و زمان‌های کلیدی حتماً حفظ شوند،
  - هیچ کلمه یا جمله‌ی زائد، مقدمه‌چینی یا تکرار نداشته باشد،
  - هیچ تحلیل شخصی یا نظر اضافه نداشته باشد.
- هر جمله باید فشرده و پرمعنا باشد؛ چیزی که می‌شود در یک جمله گفت را در دو جمله نگو.
- هیچ نام کانال، لینک، یوزرنیم یا تبلیغی از متن اصلی را تکرار نکن.
- در انتها فقط ۲ هشتگ فارسی مرتبط اضافه کن.
- فقط متن نهایی پست خبری را بده، بدون هیچ توضیح اضافه و بدون خط منبع (منبع جداگانه اضافه می‌شود).

متن خبر:
{raw_text}
""")

    with _cache_lock:
        cache[aid] = result
        save_json(CACHE_FILE, cache)

    return result

def rewrite_article(article):
    text = f"عنوان: {article['title']}\n\nخلاصه:\n{article['summary']}"
    return rewrite_text(text, source_label=article.get("source", "نامشخص"))

# -----------------------------
# تشخیص خبر فوری
# -----------------------------
def is_breaking_text(text):
    keywords = ["فوری", "خبر فوری", "urgent", "breaking"]
    t = text.lower()
    return any(k in t for k in keywords)

# -----------------------------
# استخراج عکس/ویدیو از فید RSS
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


def extract_video_from_entry(entry):
    # ۱) media_content با نوع ویدیو
    media = entry.get("media_content")
    if media:
        for m in media:
            m_type = m.get("type", "") or m.get("medium", "")
            if "video" in m_type:
                url = m.get("url")
                if url:
                    return url

    # ۲) enclosure با نوع ویدیو (رایج در پادکست/ویدیوکست‌ها)
    for link in entry.get("links", []):
        if link.get("type", "").startswith("video/"):
            return link.get("href")

    # ۳) لینک مستقیم به فایل mp4/mov/webm داخل خلاصه
    html = entry.get("summary", "") or entry.get("description", "")
    match = re.search(r'(https?://[^\s"\'<>]+\.(?:mp4|mov|webm))', html)
    if match:
        return match.group(1)

    return None

# -----------------------------
# ارسال به تلگرام
# -----------------------------
def post(text, breaking=False, image_url=None, video_url=None, source_label="نامشخص"):
    prefix = "🚨 <b>خبر فوری</b>\n\n" if breaking else ""
    full_text = f"{prefix}{text}\n\n🔗 منبع: {source_label}\n\n📡 {CHANNEL_NAME}"
    # کپشن تلگرام (برای عکس/ویدیو) حداکثر ۱۰۲۴ کاراکتر است
    caption = full_text[:1024]

    if video_url:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendVideo"
        payload = {
            "chat_id": TELEGRAM_CHANNEL_ID,
            "video": video_url,
            "caption": caption,
            "parse_mode": "HTML",
            "supports_streaming": True,
        }
        r = requests.post(url, json=payload, timeout=60)
        if r.status_code == 200:
            return True
        log.warning(f"ارسال ویدیو ناموفق بود ({r.status_code})، تلاش با عکس/متن...")
        # اگر ویدیو شکست خورد، به عکس یا متن برمی‌گردیم

    if image_url:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
        payload = {
            "chat_id": TELEGRAM_CHANNEL_ID,
            "photo": image_url,
            "caption": caption,
            "parse_mode": "HTML",
        }
        r = requests.post(url, json=payload, timeout=20)
        if r.status_code == 200:
            return True
        log.warning(f"ارسال عکس ناموفق بود ({r.status_code})، پیام به‌صورت متنی ارسال می‌شود.")
        # اگر عکس هم شکست خورد، به حالت متنی ساده برمی‌گردیم

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
            video_file_id = None
            if msg.get("video"):
                video_file_id = msg["video"]["file_id"]
            messages.append({
                "text": text,
                "photo_file_id": photo_file_id,
                "video_file_id": video_file_id,
                "source": f"@{username}",
            })

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
                "video_url": extract_video_from_entry(entry),
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

    # قبل از صرف زمان/درخواست مدل، خبرهای با عنوان مشابه را حذف می‌کنیم
    to_process = []
    for article in fresh:
        if is_duplicate_title(article["title"]):
            log.info(f"خبر تکراری/مشابه رد شد: {article['title'][:50]}")
            with _seen_lock:
                seen.add(article["id"])
                save_json(SEEN_FILE, list(seen))
            continue
        to_process.append(article)

    futures = {}
    with ThreadPoolExecutor(max_workers=4) as executor:
        for article in to_process:
            futures[executor.submit(rewrite_article, article)] = article

        for future in as_completed(futures):
            article = futures[future]
            try:
                text = future.result()
                combined = article["title"] + " " + article["summary"]
                post(
                    text,
                    breaking=is_breaking_text(combined),
                    image_url=article.get("image_url"),
                    video_url=article.get("video_url"),
                    source_label=article.get("source", "نامشخص"),
                )
                log.info(f"خبر RSS منتشر شد: {article['title'][:50]}")
                remember_title(article["title"])
                with _seen_lock:
                    seen.add(article["id"])
                    save_json(SEEN_FILE, list(seen))
                time.sleep(1)
            except Exception as e:
                log.error(f"خطا در پردازش خبر RSS «{article['title'][:40]}»: {e}")

    log.info(f"پایان چرخه RSS — {len(to_process)} خبر پردازش شد.")

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
        futures = {executor.submit(rewrite_text, m["text"], m.get("source", "کانال تلگرام")): m for m in msgs}

        for future in as_completed(futures):
            m = futures[future]
            try:
                text = future.result()
                post(
                    text,
                    breaking=is_breaking_text(m["text"]),
                    image_url=m.get("photo_file_id"),
                    video_url=m.get("video_file_id"),
                    source_label=m.get("source", "نامشخص"),
                )
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
