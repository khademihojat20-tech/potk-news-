#!/usr/bin/env python3
"""
پتک نیوز — ربات کاملاً خودکار انتشار اخبار اقتصاد و تکنولوژی در تلگرام
=======================================================================

نحوه‌ی کار:
  1) از یک لیست فید RSS (اقتصاد و تکنولوژی) خبرهای جدید را می‌خواند.
  2) خبرهایی که قبلاً منتشر نشده‌اند را تشخیص می‌دهد (فایل seen.json).
  3) هر خبر را با مدل Claude به فارسیِ روان و در قالب خبری بازنویسی/خلاصه می‌کند.
  4) پست نهایی را با برندینگ «پتک نیوز» در کانال تلگرام منتشر می‌کند.
  5) به صورت زمان‌بندی‌شده (چند بار در روز) در پس‌زمینه اجرا می‌شود.

شما فقط باید:
  - یک ربات تلگرام از @BotFather بسازید و توکنش را بگیرید.
  - ربات را ادمین کانال «پتک نیوز» کنید (با اجازه‌ی ارسال پیام).
  - یک API Key از Anthropic بگیرید.
  - مقادیر زیر را در متغیرهای محیطی (environment variables) قرار دهید:
        TELEGRAM_BOT_TOKEN
        TELEGRAM_CHANNEL_ID     (مثلاً: @petak_news یا -1001234567890)
        ANTHROPIC_API_KEY

اجرا:
    pip install feedparser requests anthropic schedule --break-system-packages
    python3 petak_news_bot.py
"""

import os
import json
import time
import hashlib
import logging
from pathlib import Path

import feedparser
import requests
import schedule
from anthropic import Anthropic

# ------------------------------------------------------------------
# تنظیمات
# ------------------------------------------------------------------

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHANNEL_ID = os.environ.get("TELEGRAM_CHANNEL_ID")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")

CHANNEL_NAME = "پتک نیوز"
SEEN_FILE = Path(__file__).parent / "seen.json"
MAX_ITEMS_PER_RUN = 4          # حداکثر چند خبر در هر بار اجرا منتشر شود
RUN_TIMES = ["08:00", "12:00", "16:00", "20:00"]   # چند بار در روز (به وقت سرور)
BREAKING_CHECK_MINUTES = 15     # هر چند دقیقه یک‌بار دنبال خبر فوری بگردد

# فیدهای RSS — می‌توانید هرکدام را حذف/اضافه کنید
RSS_FEEDS = [
    # اقتصاد
    "https://www.donya-e-eqtesad.com/rss",
    "https://www.eghtesadnews.com/rss",
    "https://www.reuters.com/business/finance/rss",
    # تکنولوژی
    "https://www.zoomit.ir/feed/",
    "https://digiato.com/feed",
    "https://techcrunch.com/feed/",
]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [پتک‌نیوز] %(message)s",
)
log = logging.getLogger("petak-news")

client = Anthropic(api_key=ANTHROPIC_API_KEY)


# ------------------------------------------------------------------
# مدیریت خبرهای دیده‌شده (جلوگیری از تکرار)
# ------------------------------------------------------------------

def load_seen() -> set:
    if SEEN_FILE.exists():
        return set(json.loads(SEEN_FILE.read_text(encoding="utf-8")))
    return set()


def save_seen(seen: set) -> None:
    # فقط ۵۰۰ مورد آخر را نگه می‌داریم که فایل بزرگ نشود
    trimmed = list(seen)[-500:]
    SEEN_FILE.write_text(json.dumps(trimmed, ensure_ascii=False), encoding="utf-8")


def article_id(entry) -> str:
    base = entry.get("id") or entry.get("link") or entry.get("title", "")
    return hashlib.sha256(base.encode("utf-8")).hexdigest()


# ------------------------------------------------------------------
# جمع‌آوری خبرهای جدید از همه‌ی فیدها
# ------------------------------------------------------------------

def fetch_new_articles(seen: set):
    fresh = []
    for url in RSS_FEEDS:
        try:
            parsed = feedparser.parse(url)
        except Exception as e:
            log.warning(f"خطا در خواندن فید {url}: {e}")
            continue

        for entry in parsed.entries[:8]:
            aid = article_id(entry)
            if aid in seen:
                continue
            fresh.append({
                "id": aid,
                "title": entry.get("title", "").strip(),
                "summary": entry.get("summary", "")[:1200],
                "link": entry.get("link", ""),
                "source": parsed.feed.get("title", url),
            })
    return fresh


# ------------------------------------------------------------------
# بازنویسی/ترجمه‌ی خبر به فارسی با Claude
# ------------------------------------------------------------------

def rewrite_in_persian(article: dict) -> str:
    prompt = f"""تو خبرنگار کانال خبری «پتک نیوز» هستی. متن زیر را بخوان و یک پست خبری کوتاه، دقیق و روان به زبان فارسی بنویس.

قوانین:
- عنوان کوتاه و جذاب در خط اول (با ایموجی مناسب موضوع در ابتدا).
- بدنه‌ی خبر در ۲ تا ۴ جمله، فقط واقعیت‌ها، بدون نظر شخصی.
- در پایان، یک خط منبع بنویس: «منبع: {article['source']}»
- در انتها ۲ تا ۳ هشتگ فارسی مرتبط اضافه کن.
- هیچ متنی از خودت (مثل «البته» یا مقدمه) اضافه نکن، فقط خروجی نهایی پست را بده.

عنوان اصلی خبر: {article['title']}
خلاصه/متن خبر: {article['summary']}
"""
    resp = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=500,
        messages=[{"role": "user", "content": prompt}],
    )
    text = "".join(block.text for block in resp.content if block.type == "text")
    return text.strip()


# ------------------------------------------------------------------
# تشخیص خبر فوری/مهم
# ------------------------------------------------------------------

def is_breaking_news(article: dict) -> bool:
    """با کمک مدل تشخیص می‌دهد آیا این خبر به‌اندازه‌ای مهم است که فوری منتشر شود."""
    prompt = f"""عنوان خبر: {article['title']}
خلاصه: {article['summary'][:500]}

آیا این خبر به قدری مهم/فوری است که باید بلافاصله و جدا از انتشار معمول در یک کانال خبری اقتصاد و تکنولوژی اطلاع‌رسانی شود؟
معیار «مهم»: اتفاقات بزرگ و تأثیرگذار مثل تغییرات ناگهانی نرخ ارز/بازار، تحریم یا رفع تحریم، ورشکستگی یا سقوط شرکت بزرگ، حمله‌ی سایبری گسترده، تصمیم مهم بانک مرکزی/فدرال‌رزرو، عرضه‌ی محصول یا فناوری بسیار بزرگ (مثلاً از اپل/گوگل/OpenAI)، یا رویدادهای مشابه با تأثیر گسترده.
خبرهای عادی و روزمره (تحلیل‌های کلی، گزارش‌های معمولی، اخبار کوچک شرکتی) را «مهم» در نظر نگیر.

فقط با یک کلمه پاسخ بده: بله یا خیر"""
    resp = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=10,
        messages=[{"role": "user", "content": prompt}],
    )
    answer = "".join(b.text for b in resp.content if b.type == "text").strip()
    return answer.startswith("بله")


# ------------------------------------------------------------------
# ارسال به تلگرام
# ------------------------------------------------------------------

def post_to_telegram(text: str, link: str, breaking: bool = False) -> bool:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    prefix = "🚨 <b>خبر فوری</b>\n\n" if breaking else ""
    full_text = f"{prefix}{text}\n\n🔗 {link}\n\n📡 {CHANNEL_NAME}"
    payload = {
        "chat_id": TELEGRAM_CHANNEL_ID,
        "text": full_text,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }
    r = requests.post(url, json=payload, timeout=20)
    if r.status_code != 200:
        log.error(f"ارسال به تلگرام ناموفق بود: {r.text}")
        return False
    return True


# ------------------------------------------------------------------
# چرخه‌ی اصلی
# ------------------------------------------------------------------

def run_cycle():
    if not all([TELEGRAM_BOT_TOKEN, TELEGRAM_CHANNEL_ID, ANTHROPIC_API_KEY]):
        log.error("متغیرهای محیطی TELEGRAM_BOT_TOKEN / TELEGRAM_CHANNEL_ID / ANTHROPIC_API_KEY تنظیم نشده‌اند.")
        return

    log.info("شروع بررسی خبرهای جدید...")
    seen = load_seen()
    fresh = fetch_new_articles(seen)

    if not fresh:
        log.info("خبر جدیدی پیدا نشد.")
        return

    published = 0
    for article in fresh:
        if published >= MAX_ITEMS_PER_RUN:
            break
        try:
            persian_post = rewrite_in_persian(article)
            ok = post_to_telegram(persian_post, article["link"])
            if ok:
                seen.add(article["id"])
                published += 1
                log.info(f"منتشر شد: {article['title'][:60]}")
                time.sleep(3)  # فاصله‌ی کوتاه بین پست‌ها
        except Exception as e:
            log.error(f"خطا در پردازش خبر «{article['title'][:40]}»: {e}")

    save_seen(seen)
    log.info(f"پایان چرخه — {published} خبر منتشر شد.")


def check_breaking_news():
    """هر چند دقیقه یک‌بار اجرا می‌شود؛ فقط اخبار واقعاً مهم را فوری منتشر می‌کند."""
    if not all([TELEGRAM_BOT_TOKEN, TELEGRAM_CHANNEL_ID, ANTHROPIC_API_KEY]):
        return

    seen = load_seen()
    fresh = fetch_new_articles(seen)
    if not fresh:
        return

    for article in fresh:
        try:
            if is_breaking_news(article):
                persian_post = rewrite_in_persian(article)
                ok = post_to_telegram(persian_post, article["link"], breaking=True)
                if ok:
                    seen.add(article["id"])
                    log.info(f"🚨 خبر فوری منتشر شد: {article['title'][:60]}")
                    save_seen(seen)
        except Exception as e:
            log.error(f"خطا در بررسی خبر فوری «{article['title'][:40]}»: {e}")


def main():
    log.info(f"{CHANNEL_NAME} — ربات خودکار راه‌اندازی شد.")
    for t in RUN_TIMES:
        schedule.every().day.at(t).do(run_cycle)

    schedule.every(BREAKING_CHECK_MINUTES).minutes.do(check_breaking_news)

    # یک بار هم موقع استارت اجرا شود
    run_cycle()

    while True:
        schedule.run_pending()
        time.sleep(30)


if __name__ == "__main__":
    main()
