#!/usr/bin/env python3
"""
Amazon.com.tr alışveriş listesi stok takipçisi.

Paylaşılan (herkese açık) alışveriş listelerini okur, her ürünün sayfasını
kontrol eder ve stokta olmayan bir ürün stoğa girdiğinde e-posta ve/veya
Telegram ile bildirim gönderir.

Ayarlar ortam değişkenleriyle yapılır (README.md'ye bakın).
Bildirimleri denemek için:  python stock_watcher.py --test-notify
"""

import json
import os
import random
import re
import smtplib
import sys
import time
from email.header import Header
from email.mime.text import MIMEText
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

BASE = "https://www.amazon.com.tr"
STATE_FILE = os.environ.get("STATE_FILE", "state.json")
MAX_WISHLIST_PAGES = 20
BLOCK_WARNING_AFTER = 6  # art arda kaç engellenmiş çalışmadan sonra uyarı gönderilsin

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

OUT_OF_STOCK_PHRASES = [
    "şu anda mevcut değil",
    "stokta yok",
    "stokta bulunmamaktadır",
    "currently unavailable",
    "temporarily out of stock",
]


def env_bool(name, default=False):
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "evet", "yes", "on")


# "true" ise ürün Amazon'da yokken başka satıcılarda varsa da "stokta" sayılır
COUNT_OTHER_SELLERS = env_bool("COUNT_OTHER_SELLERS", False)


class Blocked(Exception):
    """Amazon isteği CAPTCHA veya benzeri bir engelle karşıladı."""


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

session = requests.Session()
session.headers.update(
    {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "tr-TR,tr;q=0.9,en;q=0.6",
    }
)


def looks_blocked(html):
    lowered = html.lower()
    return (
        "/errors/validatecaptcha" in lowered
        or "api-services-support@amazon.com" in lowered
        or ("captcha" in lowered and "robot" in lowered)
    )


def fetch(url, retries=3):
    last_error = None
    for attempt in range(1, retries + 1):
        try:
            response = session.get(url, timeout=30)
            if response.status_code in (503, 429) or looks_blocked(response.text):
                last_error = Blocked(f"Engellendi: {url}")
            elif response.status_code == 404:
                return None
            else:
                response.raise_for_status()
                return response.text
        except requests.RequestException as exc:
            last_error = exc
        time.sleep(5 * attempt + random.uniform(0, 3))
    if isinstance(last_error, Blocked):
        raise last_error
    raise RuntimeError(f"İstek başarısız: {url} ({last_error})")


def polite_pause():
    time.sleep(random.uniform(3, 7))


# --------------------------------------------------------------------------
# Alışveriş listesi
# --------------------------------------------------------------------------

ASIN_RE = re.compile(r"/dp/([A-Z0-9]{10})")
WISHLIST_ID_RE = re.compile(r"/(?:hz/wishlist/ls|gp/registry/wishlist)/([A-Z0-9]+)", re.I)


def wishlist_start_url(url):
    match = WISHLIST_ID_RE.search(url)
    if not match:
        raise ValueError(f"Alışveriş listesi bağlantısı tanınamadı: {url}")
    return f"{BASE}/hz/wishlist/ls/{match.group(1)}?viewType=list"


def asin_from_item(li):
    params = li.get("data-reposition-action-params")
    if params:
        try:
            external = json.loads(params).get("itemExternalId", "")
            match = re.search(r"ASIN:([A-Z0-9]{10})", external)
            if match:
                return match.group(1)
        except (ValueError, AttributeError):
            pass
    for link in li.select("a[href*='/dp/']"):
        match = ASIN_RE.search(link.get("href", ""))
        if match:
            return match.group(1)
    return None


def parse_wishlist_page(html):
    soup = BeautifulSoup(html, "html.parser")
    items = {}
    for li in soup.select("#g-items li"):
        asin = asin_from_item(li)
        if not asin:
            continue
        name_link = li.select_one("a[id^='itemName_']")
        title = ""
        if name_link:
            title = name_link.get("title") or name_link.get_text(" ", strip=True)
        items[asin] = title

    next_url = None
    more_input = soup.select_one("input[name='showMoreUrl']")
    if more_input and more_input.get("value"):
        next_url = urljoin(BASE, more_input["value"])
    else:
        more_link = soup.select_one("a.wl-see-more")
        if more_link and more_link.get("href"):
            next_url = urljoin(BASE, more_link["href"])
    return items, next_url


def read_wishlist(url):
    items = {}
    page_url = wishlist_start_url(url)
    seen_urls = set()
    for _ in range(MAX_WISHLIST_PAGES):
        if not page_url or page_url in seen_urls:
            break
        seen_urls.add(page_url)
        html = fetch(page_url)
        if html is None:
            print(f"UYARI: Liste bulunamadı: {url}")
            break
        page_items, page_url = parse_wishlist_page(html)
        new_items = {a: t for a, t in page_items.items() if a not in items}
        if not new_items:
            break
        items.update(new_items)
        polite_pause()
    if not items:
        print(
            f"UYARI: {url} listesinde ürün bulunamadı. "
            "Liste 'Herkese açık' veya 'Paylaşılan' olarak ayarlı mı?"
        )
    return items


# --------------------------------------------------------------------------
# Ürün sayfası
# --------------------------------------------------------------------------

def check_product(asin):
    """('in_stock' | 'other_sellers' | 'out_of_stock' | 'unknown', başlık) döndürür."""
    html = fetch(f"{BASE}/dp/{asin}")
    if html is None:
        return "unknown", ""

    soup = BeautifulSoup(html, "html.parser")
    title_el = soup.select_one("#productTitle")
    title = title_el.get_text(" ", strip=True) if title_el else ""

    if not title_el and not soup.select_one("#dp, #ppd"):
        return "unknown", title

    availability = soup.select_one("#availability")
    avail_text = availability.get_text(" ", strip=True).lower() if availability else ""
    can_buy = soup.select_one("#add-to-cart-button, #buy-now-button") is not None
    other_sellers = soup.select_one(
        "#buybox-see-all-buying-choices, #aod-ingress-link, #olpLinkWidget_feature_div a"
    ) is not None

    if any(phrase in avail_text for phrase in OUT_OF_STOCK_PHRASES) or not can_buy:
        if other_sellers:
            return "other_sellers", title
        return "out_of_stock", title
    return "in_stock", title


def counts_as_available(status):
    if status == "in_stock":
        return True
    return status == "other_sellers" and COUNT_OTHER_SELLERS


# --------------------------------------------------------------------------
# Bildirimler
# --------------------------------------------------------------------------

def send_telegram(text):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not (token and chat_id):
        return False
    response = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data={"chat_id": chat_id, "text": text, "disable_web_page_preview": "true"},
        timeout=30,
    )
    response.raise_for_status()
    return True


def send_email(subject, body):
    user = os.environ.get("SMTP_USER")
    password = os.environ.get("SMTP_PASSWORD")
    to_addr = os.environ.get("EMAIL_TO") or user
    if not (user and password and to_addr):
        return False
    host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    port = int(os.environ.get("SMTP_PORT", "465"))

    message = MIMEText(body, "plain", "utf-8")
    message["Subject"] = Header(subject, "utf-8")
    message["From"] = user
    message["To"] = to_addr

    with smtplib.SMTP_SSL(host, port, timeout=30) as server:
        server.login(user, password)
        server.sendmail(user, [a.strip() for a in to_addr.split(",")], message.as_string())
    return True


def notify(subject, body):
    sent_any = False
    for name, sender in (
        ("Telegram", lambda: send_telegram(f"{subject}\n\n{body}")),
        ("E-posta", lambda: send_email(subject, body)),
    ):
        try:
            if sender():
                print(f"{name} bildirimi gönderildi.")
                sent_any = True
        except Exception as exc:  # bir kanal bozuksa diğeri yine denensin
            print(f"HATA: {name} bildirimi gönderilemedi: {exc}")
    if not sent_any:
        print("UYARI: Hiçbir bildirim kanalı ayarlı değil veya hepsi başarısız oldu.")
    return sent_any


# --------------------------------------------------------------------------
# Durum dosyası
# --------------------------------------------------------------------------

def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return {"products": {}, "blocked_streak": 0, "initialized": False}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2, sort_keys=True)


# --------------------------------------------------------------------------
# Ana akış
# --------------------------------------------------------------------------

def split_env_list(name):
    raw = os.environ.get(name, "")
    return [part.strip() for part in re.split(r"[\s,]+", raw) if part.strip()]


def collect_targets():
    targets = {}
    for url in split_env_list("WISHLIST_URLS"):
        targets.update(read_wishlist(url))
    # İsteğe bağlı: listede olmayan tekil ürünler (bağlantı veya ASIN)
    for entry in split_env_list("PRODUCT_URLS"):
        match = ASIN_RE.search(entry) or re.fullmatch(r"([A-Z0-9]{10})", entry)
        if match:
            targets.setdefault(match.group(1), "")
    return targets


def run():
    state = load_state()
    products = state.setdefault("products", {})

    try:
        targets = collect_targets()
        if not targets:
            print("Takip edilecek ürün yok. WISHLIST_URLS ayarını kontrol edin.")
            return

        newly_available = []
        print(f"{len(targets)} ürün kontrol ediliyor...")
        for asin, list_title in targets.items():
            status, page_title = check_product(asin)
            title = page_title or list_title or asin
            previous = products.get(asin, {}).get("status")
            print(f"  {asin}: {status}  ({title[:60]})")

            if status != "unknown":
                was_available = previous is not None and counts_as_available(previous)
                if counts_as_available(status) and previous is not None and not was_available:
                    newly_available.append((asin, title, status))
                products[asin] = {"status": status, "title": title}
            polite_pause()

        state["blocked_streak"] = 0

    except Blocked as exc:
        state["blocked_streak"] = state.get("blocked_streak", 0) + 1
        print(f"UYARI: Amazon isteği engelledi ({exc}). Bu çalışma atlandı.")
        if state["blocked_streak"] == BLOCK_WARNING_AFTER:
            notify(
                "Amazon stok takibi: erişim engelleniyor",
                f"Son {BLOCK_WARNING_AFTER} kontrolde Amazon istekleri engelledi. "
                "Kontroller devam edecek; sorun sürerse kontrol sıklığını azaltmayı "
                "veya scripti kendi bilgisayarınızda çalıştırmayı düşünün.",
            )
        save_state(state)
        return

    if not state.get("initialized"):
        available_now = [p for p in products.values() if counts_as_available(p["status"])]
        notify(
            "Amazon stok takibi başladı",
            f"{len(products)} ürün takibe alındı. "
            f"Şu anda stokta olan: {len(available_now)}, "
            f"stokta olmayan: {len(products) - len(available_now)}.\n"
            "Stokta olmayan bir ürün stoğa girdiğinde haber vereceğim.",
        )
        state["initialized"] = True
    elif newly_available:
        lines = []
        for asin, title, status in newly_available:
            note = " (yalnızca diğer satıcılarda)" if status == "other_sellers" else ""
            lines.append(f"• {title}{note}\n  {BASE}/dp/{asin}")
        count = len(newly_available)
        subject = (
            f"Stoğa girdi: {newly_available[0][1][:60]}"
            if count == 1
            else f"{count} ürün stoğa girdi"
        )
        notify(subject, "\n\n".join(lines))

    save_state(state)


if __name__ == "__main__":
    if "--test-notify" in sys.argv:
        ok = notify("Amazon stok takibi: test", "Bildirimler çalışıyor. 🎉")
        sys.exit(0 if ok else 1)
    run()
