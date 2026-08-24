#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Поиск помещений под ателье в Астане на Krisha.kz и OLX.kz.
Каждый запуск: собирает объявления по бюджету, сравнивает с уже
виденными (data/seen.json) и шлёт в Telegram только новые.
"""

import os
import re
import json
import time
import requests
from bs4 import BeautifulSoup

# ---------- Настройки (можно менять) ----------
BUDGET_MAX = 150_000          # максимальная цена аренды, тг/мес
MAX_KRISHA_PAGES = 5          # сколько страниц Krisha листать (сортировка "дешевые")
SEEN_FILE = "data/seen.json"

KRISHA_URL = "https://krisha.kz/arenda/kommercheskaya-nedvizhimost/astana/"
OLX_URL = "https://www.olx.kz/nedvizhimost/kommercheskie-pomeshcheniya/arenda/astana/"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Referer": "https://www.olx.kz/",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "same-origin",
    "Cache-Control": "max-age=0",
}

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")  # запасной вариант, если Worker недоступен
WORKER_URL = os.environ.get("WORKER_URL")  # напр. https://atelier-bot.assaiynerkegul.workers.dev/subscribers
WORKER_API_KEY = os.environ.get("WORKER_API_KEY")


def get_subscribers():
    """Берёт список chat_id подписчиков с Cloudflare Worker.
    Если Worker недоступен — используем запасной TELEGRAM_CHAT_ID."""
    if WORKER_URL and WORKER_API_KEY:
        try:
            resp = requests.get(
                WORKER_URL,
                headers={"X-API-Key": WORKER_API_KEY},
                timeout=15,
            )
            resp.raise_for_status()
            ids = resp.json()
            if ids:
                print(f"Подписчиков через Worker: {len(ids)}")
                return [str(i) for i in ids]
        except requests.RequestException as e:
            print(f"[worker] не удалось получить список подписчиков: {e}")

    if TELEGRAM_CHAT_ID:
        print("Использую запасной TELEGRAM_CHAT_ID")
        return [TELEGRAM_CHAT_ID]

    return []


# ---------- Хранилище уже виденных объявлений ----------
def load_seen():
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f))
    return set()


def save_seen(seen_ids):
    os.makedirs(os.path.dirname(SEEN_FILE), exist_ok=True)
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(seen_ids), f, ensure_ascii=False, indent=2)


# ---------- Krisha.kz ----------
def parse_price(text):
    """'350 000 ₸ за месяц' -> 350000"""
    m = re.search(r"([\d\s]+)\s*₸", text)
    if not m:
        return None
    digits = re.sub(r"\D", "", m.group(1))
    return int(digits) if digits else None


def get_krisha_listings(budget=BUDGET_MAX, max_pages=MAX_KRISHA_PAGES):
    results = []
    for page in range(1, max_pages + 1):
        url = KRISHA_URL if page == 1 else f"{KRISHA_URL}?sort_by=price-asc&page={page}"
        if page == 1:
            url = f"{KRISHA_URL}?sort_by=price-asc"
        try:
            resp = requests.get(url, headers=HEADERS, timeout=20)
            resp.raise_for_status()
        except requests.RequestException as e:
            print(f"[krisha] ошибка запроса стр. {page}: {e}")
            break

        soup = BeautifulSoup(resp.text, "lxml")
        cards = soup.select("div.a-card, div.a-list__item, a.a-card__image")
        # Более надёжный способ: искать все ссылки на объявления
        links = soup.select('a[href^="/a/show/"]')
        seen_ids_on_page = set()
        stop = False

        for a in links:
            href = a.get("href", "")
            m = re.search(r"/a/show/(\d+)", href)
            if not m:
                continue
            listing_id = m.group(1)
            if listing_id in seen_ids_on_page:
                continue
            seen_ids_on_page.add(listing_id)

            # Ищем родительский блок карточки для цены/адреса
            card = a.find_parent("div")
            card_text = card.get_text(" ", strip=True) if card else a.get_text(" ", strip=True)

            price = parse_price(card_text)
            title = a.get_text(strip=True) or "Без названия"

            if price is None:
                continue
            if price > budget:
                # так как сортировка "дешевые сначала", дальше можно остановиться
                stop = True
                continue

            results.append({
                "id": f"krisha_{listing_id}",
                "title": title,
                "price": price,
                "url": f"https://krisha.kz/a/show/{listing_id}",
                "source": "Krisha.kz",
            })

        if stop:
            break
        time.sleep(1)

    return results


# ---------- OLX.kz ----------
def get_olx_listings(budget=BUDGET_MAX):
    results = []
    session = requests.Session()
    session.headers.update(HEADERS)

    try:
        # "прогреваем" сессию визитом на главную — иногда снимает часть защиты
        session.get("https://www.olx.kz/", timeout=15)
        time.sleep(1)
        resp = session.get(OLX_URL, timeout=20)
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"[olx] ошибка запроса: {e}")
        return results

    soup = BeautifulSoup(resp.text, "lxml")
    cards = soup.select('div[data-cy="l-card"]')

    for card in cards:
        a = card.select_one("a")
        if not a:
            continue
        href = a.get("href", "")
        m = re.search(r"ID(\w+)\.html", href) or re.search(r"/d/obyavlenie/[^/]*-(\w+)\.html", href)
        listing_id = m.group(1) if m else href

        title_el = card.select_one("h4, h6")
        title = title_el.get_text(strip=True) if title_el else a.get_text(strip=True)

        price_el = card.select_one('[data-testid="ad-price"]')
        price = parse_price(price_el.get_text(" ", strip=True)) if price_el else None

        if price is None or price > budget:
            continue

        full_url = href if href.startswith("http") else f"https://www.olx.kz{href}"

        results.append({
            "id": f"olx_{listing_id}",
            "title": title,
            "price": price,
            "url": full_url,
            "source": "OLX.kz",
        })

    return results


# ---------- Telegram ----------
def send_telegram(chat_id, text):
    if not TELEGRAM_TOKEN:
        print("Telegram не настроен (нет TELEGRAM_BOT_TOKEN). Вывожу в консоль:\n", text)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        r = requests.post(url, data={
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }, timeout=15)
        if not r.ok:
            print(f"Ошибка отправки в Telegram (chat_id={chat_id}):", r.text)
    except requests.RequestException as e:
        print(f"Ошибка отправки в Telegram (chat_id={chat_id}):", e)


def format_listing(item):
    return f"🏠 <b>{item['title']}</b>\n💰 {item['price']:,} ₸/мес\n📍 {item['source']}\n{item['url']}".replace(",", " ")


# ---------- Основной запуск ----------
def main():
    print("Ищу объявления на Krisha.kz...")
    krisha = get_krisha_listings()
    print(f"  найдено подходящих: {len(krisha)}")

    print("Ищу объявления на OLX.kz...")
    olx = get_olx_listings()
    print(f"  найдено подходящих: {len(olx)}")

    all_listings = krisha + olx
    seen = load_seen()
    new_listings = [x for x in all_listings if x["id"] not in seen]

    print(f"Новых объявлений: {len(new_listings)}")

    subscribers = get_subscribers()
    print(f"Получателей рассылки: {len(subscribers)}")

    if new_listings and subscribers:
        header = f"🔎 Новые варианты помещений под ателье в Астане (до {BUDGET_MAX:,} ₸/мес):\n\n".replace(",", " ")
        for chat_id in subscribers:
            send_telegram(chat_id, header.strip())
            for item in new_listings:
                send_telegram(chat_id, format_listing(item))
                time.sleep(0.3)
    elif not subscribers:
        print("Нет ни одного подписчика — рассылку некому отправлять.")
    else:
        print("Новых объявлений нет — уведомление не отправляется.")

    # обновляем базу увиденных (не даём файлу расти бесконечно — храним последние 2000)
    seen.update(x["id"] for x in all_listings)
    if len(seen) > 2000:
        seen = set(list(seen)[-2000:])
    save_seen(seen)


if __name__ == "__main__":
    main()
