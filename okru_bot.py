import json
import os
import random
import re
import sys
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://okru.ru"
STATE_FILE = "state.json"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
SECTIONS = [
    "otdelka-remont-stroyka",
    "bytovye-uslugi",
    "drugie-uslugi",
    "nauchastke",
    "mebelnye-uslugi",
]
CLIENT_MARKERS = [
    "нужен", "нужна", "нужно", "надо", "требуется", "ищу", "необходимо",
    "помогите", "подскажите", "посоветуйте", "кто может", "сколько стоит",
    "по моим размерам", "срочно нужен",
]
EXECUTOR_MARKERS = [
    "предлагаю", "выполним", "звоните", "бригада", "опыт", "гарантия",
    "под ключ", "цена договорная", "делаю", "делаем", "производим",
    "по вызову", "выезд", "недорого", "скидки",
]
CLIENT_SECTIONS = {"ischu", "kyply"}
TOPIC_PATTERNS = {
    "электрика": re.compile(r"\bэлектрик\w*\b", re.IGNORECASE),
    "сантехника": re.compile(r"\bсантехник\w*\b", re.IGNORECASE),
    "сварка": re.compile(r"\b(сварк\w*|навес\w*|ворот\w*|калитк\w*|забор\w*)\b", re.IGNORECASE),
    "отделка": re.compile(r"\bотделк\w*\b", re.IGNORECASE),
    "кровля": re.compile(r"\bкровл\w*\b", re.IGNORECASE),
    "стройка": re.compile(r"\bстройк\w*\b", re.IGNORECASE),
    "окна/двери": re.compile(r"\b(окн\w*|двер\w*)\b", re.IGNORECASE),
    "бытовая техника": re.compile(r"\bбытов\w*\s+техник\w*\b", re.IGNORECASE),
    "муж на час": re.compile(r"\bмуж\s+на\s+час\b", re.IGNORECASE),
}

def classify_ad(text, section_slug):
    if section_slug in CLIENT_SECTIONS:
        return True
    lower = text.lower()
    client_score = sum(1 for m in CLIENT_MARKERS if m in lower)
    executor_score = sum(1 for m in EXECUTOR_MARKERS if m in lower)
    if re.search(r"\bнужн\w*", lower) and executor_score == 0:
        client_score += 1
    return (2 * client_score - executor_score) > 0

def detect_topics(text):
    return [t for t, p in TOPIC_PATTERNS.items() if p.search(text)]

def parse_listing(html, section_slug):
    soup = BeautifulSoup(html, "html.parser")
    ads = []
    for art in soup.find_all("article", class_=lambda c: c and "shadowblock" in c):
        link_tag = art.find("a", href=re.compile(r"/[^/]+/[^/]+/\d+$"))
        if not link_tag:
            continue
        href = link_tag.get("href", "")
        parts = [p for p in urlparse(href).path.split("/") if p]
        if len(parts) < 3:
            continue
        try:
            ad_id = int(parts[-1])
        except ValueError:
            continue
        url = urljoin(BASE_URL, href)

        text = ""
        td = art.find("div", class_="text")
        if td:
            text = td.get_text(" ", strip=True)
        else:
            p = art.find("p", id=re.compile(r"onlytext\d+"))
            if p:
                text = p.get_text(" ", strip=True)
        text = re.sub(r"Показать телефон", "", text, flags=re.IGNORECASE)
        text = re.sub(r"Телефон", "", text, flags=re.IGNORECASE)
        text = re.sub(r"Комментарии\s*\(\d+\)", "", text, flags=re.IGNORECASE).strip()

        price = ""
        price_raw = (art.find("div", class_="pblprice") or art).get_text(" ", strip=True)
        m = re.search(r"Цена:\s*([\d\s]+)\s*руб", price_raw, re.IGNORECASE)
        if m:
            v = m.group(1).replace(" ", "")
            if v != "1":
                price = f"{v} руб."

        m = re.search(
            r"(Сегодня,\s*\d{1,2}:\d{2}|\d{1,2}\s+[а-яё]+\s+\d{4})",
            art.get_text(" ", strip=True),
        )
        date = m.group(1) if m else ""

        a_tag = art.find("a", href=re.compile(r"/user/"))
        author = a_tag.get_text(strip=True) if a_tag else ""

        ads.append({
            "id": ad_id,
            "section": section_slug,
            "url": url,
            "text": text,
            "price": price,
            "date": date,
            "author": author,
            "is_client": classify_ad(text, section_slug),
            "topics": detect_topics(text),
        })
    return ads

def send_telegram(token, chat_id, ad):
    topics = ", ".join(ad["topics"]) or "—"
    text = (
        f"🔔 <b>Новая заявка клиента</b>\n"
        f"<b>Темы:</b> {topics}\n\n"
        f"{ad['text'][:600]}\n\n"
        f"💰 {ad['price'] or 'цена не указана'}\n"
        f"🕒 {ad['date'] or '—'}\n"
        f"👤 {ad['author'] or '—'}\n"
        f'<a href="{ad["url"]}">👉 Открыть объявление</a>'
    )
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=15,
        )
        if r.status_code != 200:
            print(f"[TG] Ошибка {r.status_code}: {r.text[:200]}")
    except Exception as e:
        print(f"[TG] Exception: {e}")

def main():
    token = os.environ.get("TG_BOT_TOKEN")
    chat_id = os.environ.get("TG_CHAT_ID")
    if not token or not chat_id:
        print("Ошибка: не заданы TG_BOT_TOKEN и/или TG_CHAT_ID")
        sys.exit(1)

    allowed = None
    raw_filter = os.environ.get("TOPICS_FILTER", "").strip()
    if raw_filter:
        allowed = {t.strip().lower() for t in raw_filter.split(",") if t.strip()}

    state_path = Path(STATE_FILE)
    if state_path.exists():
        state = json.loads(state_path.read_text(encoding="utf-8"))
    else:
        state = {"seen_ids": [], "initialized": False}
    seen = set(state.get("seen_ids", []))
    initialized = state.get("initialized", False)

    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT

    all_ads = []
    for section in SECTIONS:
        for page in (1, 2):
            url = f"{BASE_URL}/{section}" + (f"?str={page}" if page > 1 else "")
            try:
                r = session.get(url, timeout=20)
                if r.status_code != 200:
                    print(f"[!] HTTP {r.status_code} {url}")
                    continue
            except Exception as e:
                print(f"[!] {url}: {e}")
                continue
            ads = parse_listing(r.text, section)
            print(f"{section} стр.{page}: {len(ads)} карточек")
            all_ads.extend(ads)
            if not ads:
                break
            time.sleep(random.uniform(2, 4))

    new_ads = []
    for ad in all_ads:
        if ad["id"] in seen:
            continue
        seen.add(ad["id"])
        new_ads.append(ad)

    new_leads = [a for a in new_ads if a["is_client"] and a["topics"]]
    if allowed:
        new_leads = [a for a in new_leads if set(a["topics"]) & allowed]

    if not initialized:
        print(f"Первый запуск: запоминаю {len(all_ads)} объявлений, без уведомлений")
    else:
        print(f"Новых заявок клиентов: {len(new_leads)}")
        for ad in new_leads:
            print(f"  → {ad['id']}: {ad['topics']} | {ad['text'][:80]}")
            send_telegram(token, chat_id, ad)
            time.sleep(0.5)

    seen_list = list(seen)
    if len(seen_list) > 5000:
        seen_list = seen_list[-5000:]

    state["seen_ids"] = seen_list
    state["initialized"] = True
    state_path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    print(f"Сохранено: {len(state['seen_ids'])} ID")

if __name__ == "__main__":
    main()
