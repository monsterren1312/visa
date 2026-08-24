#!/usr/bin/env python3
"""
Immigration USA Telegram Bot

Парсит RSS-ленты новостей об иммиграции, грин-картах и визах США,
переводит на русский через Anthropic API, публикует в Telegram-канал
с картинками (если есть в источнике).

Запускается по расписанию (GitHub Actions cron), максимум MAX_POSTS_PER_RUN
постов за запуск. Структура полностью повторяет la-news-bot.
"""

import os
import json
import time
import random
import hashlib
import logging
from datetime import datetime, timezone

import feedparser
import requests
from anthropic import Anthropic

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("immigration-usa-bot")

# ---------------------------------------------------------------------------
# Конфигурация
# ---------------------------------------------------------------------------

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]

MAX_POSTS_PER_RUN = int(os.environ.get("MAX_POSTS_PER_RUN", "1"))
MIN_INTERVAL_MINUTES = int(os.environ.get("MIN_INTERVAL_MINUTES", "60"))
MAX_INTERVAL_MINUTES = int(os.environ.get("MAX_INTERVAL_MINUTES", "180"))
STATE_FILE = os.environ.get("STATE_FILE", "state/seen.json")

CHANNEL_SIGNATURE = "🇺🇸 Иммиграция и визы США"
CHANNEL_URL = os.environ.get("CHANNEL_URL", "https://t.me/YOUR_CHANNEL_USERNAME")

# Источники по грин-картам, визам, USCIS-процедурам. Официальный сайт USCIS
# отдаёт 403 автоматическим запросам, поэтому используются юридические/
# новостные блоги, которые публикуют то же самое быстрее и без блокировки.
RSS_SOURCES = [
    {"name": "Immigration Impact", "url": "https://immigrationimpact.com/feed/"},
    {"name": "Boundless", "url": "https://www.boundless.com/blog/feed/"},
    {"name": "CitizenPath", "url": "https://citizenpath.com/news/feed/"},
    {"name": "Murthy Law Firm", "url": "https://www.murthy.com/feed/"},
]

TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
anthropic_client = Anthropic(api_key=ANTHROPIC_API_KEY)


def load_state() -> dict:
    if not os.path.exists(STATE_FILE):
        return {"seen_hashes": [], "seen_links": [], "next_post_not_before": None}
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        state = json.load(f)
    state.setdefault("next_post_not_before", None)
    return state


def save_state(state: dict) -> None:
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    state["seen_hashes"] = state["seen_hashes"][-500:]
    state["seen_links"] = state["seen_links"][-500:]
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def schedule_next_post(state: dict) -> None:
    delay_minutes = random.uniform(MIN_INTERVAL_MINUTES, MAX_INTERVAL_MINUTES)
    next_time = datetime.now(timezone.utc).timestamp() + delay_minutes * 60
    state["next_post_not_before"] = next_time
    log.info(f"Следующий пост не раньше чем через {delay_minutes:.1f} мин")


def is_too_early(state: dict) -> bool:
    not_before = state.get("next_post_not_before")
    if not_before is None:
        return False
    return datetime.now(timezone.utc).timestamp() < not_before


def content_hash(title: str, summary: str) -> str:
    normalized = (title + summary).lower().strip()
    normalized = "".join(ch for ch in normalized if ch.isalnum() or ch.isspace())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def strip_html(text: str) -> str:
    import re
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def extract_image(entry):
    if "media_content" in entry and entry.media_content:
        url = entry.media_content[0].get("url")
        if url:
            return url
    if "media_thumbnail" in entry and entry.media_thumbnail:
        url = entry.media_thumbnail[0].get("url")
        if url:
            return url
    if "links" in entry:
        for link in entry.links:
            if link.get("type", "").startswith("image/"):
                return link.get("href")
    if "summary" in entry:
        import re
        match = re.search(r'<img[^>]+src="([^"]+)"', entry.summary)
        if match:
            return match.group(1)
    return None


# Ключевые слова для базовой фильтрации: пропускаем только то, что реально
# касается иммиграции/виз/грин-карт, даже если источник иногда пишет про
# смежные юридические темы (это может пригодиться, если позже добавите более
# широкие юридические блоги в список источников).
FILTER_KEYWORDS = [
    "immigra", "visa", "green card", "uscis", "citizenship", "naturaliz",
    "asylum", "refugee", "deport", "border", "h-1b", "h1b", "green-card",
    "permanent resident", "work permit", "ead", "daca", "sponsor",
]


def is_relevant(title: str, summary: str) -> bool:
    text = (title + " " + summary).lower()
    return any(kw in text for kw in FILTER_KEYWORDS)


def fetch_candidates(state: dict) -> list:
    candidates = []
    for source in RSS_SOURCES:
        try:
            feed = feedparser.parse(source["url"])
        except Exception as e:
            log.warning(f"Не удалось загрузить {source['name']}: {e}")
            continue
        if feed.bozo and not feed.entries:
            log.warning(f"Лента {source['name']} вернула ошибку без записей, пропускаю")
            continue
        for entry in feed.entries[:10]:
            link = entry.get("link", "")
            title = entry.get("title", "").strip()
            summary = entry.get("summary", "") or entry.get("description", "")
            summary = strip_html(summary)[:800]
            if not link or not title:
                continue
            if not is_relevant(title, summary):
                continue
            if link in state["seen_links"]:
                continue
            h = content_hash(title, summary)
            if h in state["seen_hashes"]:
                continue
            image_url = extract_image(entry)
            candidates.append({
                "source": source["name"],
                "link": link,
                "title": title,
                "summary": summary,
                "image_url": image_url,
                "hash": h,
                "published": entry.get("published", ""),
            })
    candidates.sort(key=lambda c: c["published"], reverse=True)
    return candidates


def rewrite_in_russian(title: str, summary: str, source_name: str):
    prompt = f"""Ты редактор Telegram-канала про иммиграцию, грин-карты и визы США на русском языке.

Вот новость на английском (источник: {source_name}):

Заголовок: {title}
Описание: {summary}

Переведи и оформи это как короткий пост для Telegram на русском языке:
- Заголовок с эмодзи по теме (1 эмодзи), выделенный жирным (Telegram Markdown: *текст*)
- 2-4 предложения по существу, нейтральный новостной тон, никакой "воды"
- Если в новости есть конкретные цифры, сроки или требования (сборы, дедлайны, категории виз) — сохрани их точно
- НЕ упоминай название источника и НЕ добавляй ссылки на источник в текст
- НЕ добавляй хэштеги
- НЕ добавляй никакую подпись/подвал — это будет добавлено отдельно
- Пиши только сам текст поста, без пояснений от себя, без кавычек вокруг всего текста

Ответь только готовым текстом поста."""
    try:
        response = anthropic_client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=500,
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(block.text for block in response.content if hasattr(block, "text")).strip()
        return text if text else None
    except Exception as e:
        log.error(f"Ошибка при обращении к Anthropic API: {e}")
        return None


def build_final_text(body: str) -> str:
    signature = f"[{CHANNEL_SIGNATURE}]({CHANNEL_URL})"
    return f"{body}\n\n{signature}"


def send_to_telegram(text: str, image_url) -> bool:
    try:
        if image_url:
            resp = requests.post(
                f"{TELEGRAM_API}/sendPhoto",
                data={
                    "chat_id": TELEGRAM_CHAT_ID,
                    "photo": image_url,
                    "caption": text,
                    "parse_mode": "Markdown",
                },
                timeout=30,
            )
            if resp.ok and resp.json().get("ok"):
                return True
            log.warning(f"sendPhoto не удался ({resp.text[:200]}), пробую без картинки")
        resp = requests.post(
            f"{TELEGRAM_API}/sendMessage",
            data={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": text,
                "parse_mode": "Markdown",
                "disable_web_page_preview": True,
            },
            timeout=30,
        )
        if resp.ok and resp.json().get("ok"):
            return True
        log.error(f"sendMessage не удался: {resp.text[:300]}")
        return False
    except Exception as e:
        log.error(f"Ошибка при отправке в Telegram: {e}")
        return False


def main():
    log.info("Запуск Immigration USA Bot")
    state = load_state()

    if is_too_early(state):
        remaining = (state["next_post_not_before"] - datetime.now(timezone.utc).timestamp()) / 60
        log.info(f"Ещё не время для следующего поста (осталось ~{remaining:.1f} мин), завершение без публикации")
        return

    candidates = fetch_candidates(state)
    log.info(f"Найдено {len(candidates)} новых кандидатов из {len(RSS_SOURCES)} источников")

    if not candidates:
        log.info("Новых новостей нет, завершение")
        return

    posted = 0
    for item in candidates:
        if posted >= MAX_POSTS_PER_RUN:
            break
        log.info(f"Обрабатываю: [{item['source']}] {item['title'][:80]}")
        body = rewrite_in_russian(item["title"], item["summary"], item["source"])
        if not body:
            log.warning("Не удалось переписать текст, пропускаю эту новость")
            continue
        final_text = build_final_text(body)
        success = send_to_telegram(final_text, item["image_url"])
        if success:
            log.info("Опубликовано успешно")
            state["seen_links"].append(item["link"])
            state["seen_hashes"].append(item["hash"])
            posted += 1
            schedule_next_post(state)
            save_state(state)
            if posted < MAX_POSTS_PER_RUN:
                time.sleep(5)
        else:
            log.error("Публикация не удалась, эта новость будет предложена повторно в следующий раз")

    log.info(f"Готово. Опубликовано постов за этот запуск: {posted}")


if __name__ == "__main__":
    main()
