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
from datetime import datetime, timedelta, timezone
from email.header import Header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from html import escape
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

try:  # Chrome'un bağlantı imzasını taklit eder; Amazon engellerini azaltır
    from curl_cffi import requests as browser_requests
except ImportError:
    browser_requests = None

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

# Günlük özet raporun gönderileceği saat (Türkiye saati, 0-23). "off" ise kapalı.
SUMMARY_HOUR = (os.environ.get("SUMMARY_HOUR") or "9").strip().lower()

TR_TZ = timezone(timedelta(hours=3))
TR_MONTHS = ["Ocak", "Şubat", "Mart", "Nisan", "Mayıs", "Haziran", "Temmuz",
             "Ağustos", "Eylül", "Ekim", "Kasım", "Aralık"]


class Blocked(Exception):
    """Amazon isteği CAPTCHA veya benzeri bir engelle karşıladı."""


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

if browser_requests is not None:
    session = browser_requests.Session(impersonate="chrome")
    session.headers.update({"Accept-Language": "tr-TR,tr;q=0.9,en;q=0.6"})
    print("Bağlantı: curl_cffi (Chrome taklidi)")
else:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "tr-TR,tr;q=0.9,en;q=0.6",
        }
    )
    print("Bağlantı: requests (curl_cffi kurulu değil)")

_warmed_up = False


def warm_up():
    """Ana sayfayı bir kez ziyaret edip çerezleri alır (gerçek ziyaretçi gibi)."""
    global _warmed_up
    if _warmed_up:
        return
    _warmed_up = True
    try:
        session.get(BASE + "/", timeout=30)
        time.sleep(random.uniform(2, 4))
    except Exception as exc:
        print(f"Not: Ana sayfa ziyareti başarısız ({exc})")


def page_title(html):
    match = re.search(r"<title[^>]*>(.*?)</title>", html, re.S | re.I)
    return match.group(1).strip()[:80] if match else "?"


def looks_blocked(html):
    lowered = html.lower()
    return (
        "/errors/validatecaptcha" in lowered
        or "api-services-support@amazon.com" in lowered
        or ("captcha" in lowered and "robot" in lowered)
    )


def fetch(url, retries=3):
    warm_up()
    last_error = None
    for attempt in range(1, retries + 1):
        try:
            response = session.get(url, timeout=30)
            status = response.status_code
            if status in (503, 429) or looks_blocked(response.text):
                kind = "CAPTCHA" if looks_blocked(response.text) else f"HTTP {status}"
                last_error = Blocked(
                    f"{kind}, deneme {attempt}/{retries}, sayfa başlığı: "
                    f"'{page_title(response.text)}', adres: {url}"
                )
                print(f"  ...engel: {last_error}")
            elif status == 404:
                return None
            elif status >= 400:
                last_error = RuntimeError(f"HTTP {status}")
            else:
                return response.text
        except Exception as exc:
            last_error = exc
        time.sleep(8 * attempt + random.uniform(0, 4))
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

PRICE_SELECTORS = [
    "#corePrice_feature_div .a-offscreen",
    "#corePriceDisplay_desktop_feature_div .a-offscreen",
    "#apex_desktop .a-offscreen",
    "#price_inside_buybox",
    "#priceblock_ourprice",
]


def extract_price(soup):
    for selector in PRICE_SELECTORS:
        el = soup.select_one(selector)
        if el and el.get_text(strip=True):
            return el.get_text(strip=True)
    return ""


def extract_image(soup):
    img = soup.select_one("#landingImage, #imgBlkFront, #main-image")
    if not img:
        return ""
    src = img.get("data-old-hires") or img.get("src") or ""
    return src if src.startswith("https://") else ""


def check_product(asin):
    """Ürün sayfasını okuyup durum, başlık, fiyat ve görsel bilgisini döndürür."""
    result = {"status": "unknown", "title": "", "price": "", "image": ""}
    html = fetch(f"{BASE}/dp/{asin}")
    if html is None:
        return result

    soup = BeautifulSoup(html, "html.parser")
    title_el = soup.select_one("#productTitle")
    result["title"] = title_el.get_text(" ", strip=True) if title_el else ""
    result["image"] = extract_image(soup)

    if not title_el and not soup.select_one("#dp, #ppd"):
        return result

    availability = soup.select_one("#availability")
    avail_text = availability.get_text(" ", strip=True).lower() if availability else ""
    can_buy = soup.select_one("#add-to-cart-button, #buy-now-button") is not None
    other_sellers = soup.select_one(
        "#buybox-see-all-buying-choices, #aod-ingress-link, #olpLinkWidget_feature_div a"
    ) is not None

    if any(phrase in avail_text for phrase in OUT_OF_STOCK_PHRASES) or not can_buy:
        result["status"] = "other_sellers" if other_sellers else "out_of_stock"
    else:
        result["status"] = "in_stock"
        result["price"] = extract_price(soup)
    return result


def counts_as_available(status):
    if status == "in_stock":
        return True
    return status == "other_sellers" and COUNT_OTHER_SELLERS


# --------------------------------------------------------------------------
# Rapor biçimlendirme
# --------------------------------------------------------------------------

STATUS_INFO = {
    # durum: (etiket, emoji, yazı rengi, arka plan, sıralama)
    "in_stock": ("Stokta", "🟢", "#1a7f37", "#dafbe1", 1),
    "other_sellers": ("Diğer satıcılarda", "🟡", "#9a6700", "#fff8c5", 2),
    "out_of_stock": ("Stokta yok", "🔴", "#cf222e", "#ffebe9", 3),
    "unknown": ("Kontrol edilemedi", "⚪", "#57606a", "#eaeef2", 4),
}


def now_tr():
    return datetime.now(TR_TZ)


def format_dt(iso_value, with_time=True):
    if not iso_value:
        return "–"
    try:
        dt = datetime.fromisoformat(iso_value).astimezone(TR_TZ)
    except ValueError:
        return "–"
    text = f"{dt.day} {TR_MONTHS[dt.month - 1]}"
    if dt.year != now_tr().year:
        text += f" {dt.year}"
    if with_time:
        text += f", {dt:%H:%M}"
    return text


def sorted_products(products, new_asins):
    def key(item):
        asin, info = item
        is_new = 0 if asin in new_asins else 1
        order = STATUS_INFO.get(info.get("status"), STATUS_INFO["unknown"])[4]
        return (is_new, order, info.get("title", "").lower())
    return sorted(products.items(), key=key)


def count_available(products):
    return sum(1 for p in products.values() if counts_as_available(p.get("status")))


def status_badge(status):
    label, _, color, bg, _ = STATUS_INFO.get(status, STATUS_INFO["unknown"])
    return (
        f'<span style="display:inline-block;padding:3px 10px;border-radius:12px;'
        f'background:{bg};color:{color};font-size:12px;font-weight:600;'
        f'white-space:nowrap;">{label}</span>'
    )


def build_html_email(heading, intro, products, new_asins):
    font = "-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif"
    total = len(products)
    available = count_available(products)

    highlight = ""
    if new_asins:
        cards = []
        for asin in new_asins:
            info = products.get(asin, {})
            price = escape(info.get("price") or "")
            price_html = (
                '<span style="font-size:16px;font-weight:700;color:#1a7f37;'
                f'margin-right:12px;">{price}</span>' if price else ""
            )
            cards.append(
                f'<tr><td style="padding:10px 0;border-top:1px solid #b7e4c1;">'
                f'<div style="font-size:15px;font-weight:600;color:#1f2328;">'
                f'{escape(info.get("title") or asin)}</div>'
                f'<div style="margin-top:6px;">'
                f'{price_html}'
                f'<a href="{BASE}/dp/{asin}" style="display:inline-block;background:#1a7f37;'
                f'color:#ffffff;text-decoration:none;padding:7px 16px;border-radius:6px;'
                f'font-size:13px;font-weight:600;">Ürüne git →</a></div></td></tr>'
            )
        highlight = (
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
            f'style="background:#f0fff4;border:1px solid #b7e4c1;border-radius:8px;'
            f'margin:0 0 24px 0;"><tr><td style="padding:16px 18px;">'
            f'<div style="font-size:13px;font-weight:700;color:#1a7f37;'
            f'text-transform:uppercase;letter-spacing:.5px;">🎉 Yeni stoğa girenler</div>'
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
            f'style="margin-top:8px;">{"".join(cards)}</table></td></tr></table>'
        )

    rows = []
    for asin, info in sorted_products(products, set(new_asins)):
        title = escape(info.get("title") or asin)
        price = escape(info.get("price") or "")
        image = info.get("image") or ""
        img_cell = (
            f'<img src="{escape(image)}" width="48" height="48" alt="" '
            f'style="width:48px;height:48px;object-fit:contain;border-radius:4px;'
            f'background:#ffffff;display:block;">'
            if image else
            '<div style="width:48px;height:48px;border-radius:4px;background:#f6f8fa;"></div>'
        )
        new_mark = (
            ' <span style="color:#1a7f37;font-size:11px;font-weight:700;">YENİ</span>'
            if asin in new_asins else ""
        )
        price_line = (
            f'<div style="font-size:13px;color:#1f2328;font-weight:600;margin-top:3px;">{price}</div>'
            if price else ""
        )
        rows.append(
            f'<tr>'
            f'<td style="padding:10px 8px 10px 0;border-bottom:1px solid #eaeef2;width:48px;'
            f'vertical-align:middle;">{img_cell}</td>'
            f'<td style="padding:10px 8px;border-bottom:1px solid #eaeef2;vertical-align:middle;">'
            f'<a href="{BASE}/dp/{asin}" style="color:#0969da;text-decoration:none;'
            f'font-size:14px;line-height:1.35;">{title}</a>{new_mark}{price_line}</td>'
            f'<td style="padding:10px 8px;border-bottom:1px solid #eaeef2;vertical-align:middle;'
            f'text-align:center;">{status_badge(info.get("status"))}</td>'
            f'<td style="padding:10px 0 10px 8px;border-bottom:1px solid #eaeef2;'
            f'vertical-align:middle;font-size:12px;color:#57606a;white-space:nowrap;">'
            f'{format_dt(info.get("changed_at"), with_time=False)}</td>'
            f'</tr>'
        )

    th_base = ("font-size:11px;color:#57606a;font-weight:600;text-transform:uppercase;"
               "letter-spacing:.5px;padding:0 8px 8px;border-bottom:2px solid #d0d7de;")
    th = f'style="text-align:left;{th_base}"'
    th_center = f'style="text-align:center;{th_base}"'

    return f"""<!DOCTYPE html>
<html lang="tr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"></head>
<body style="margin:0;padding:0;background:#f6f8fa;font-family:{font};">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f6f8fa;">
<tr><td align="center" style="padding:24px 12px;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0"
 style="max-width:640px;background:#ffffff;border:1px solid #d0d7de;border-radius:10px;">
<tr><td style="padding:22px 24px 6px;">
  <div style="font-size:12px;color:#57606a;font-weight:600;letter-spacing:.5px;">
    AMAZON.COM.TR · STOK TAKİBİ</div>
  <h1 style="margin:6px 0 4px;font-size:21px;color:#1f2328;">{escape(heading)}</h1>
  <p style="margin:0 0 18px;font-size:14px;color:#57606a;">{escape(intro)}</p>
  <table role="presentation" cellpadding="0" cellspacing="0" style="margin:0 0 22px;">
    <tr>
      <td style="padding:10px 16px;background:#dafbe1;border-radius:8px;">
        <div style="font-size:22px;font-weight:700;color:#1a7f37;">{available}</div>
        <div style="font-size:12px;color:#1a7f37;">stokta</div></td>
      <td style="width:10px;"></td>
      <td style="padding:10px 16px;background:#f6f8fa;border-radius:8px;">
        <div style="font-size:22px;font-weight:700;color:#1f2328;">{total - available}</div>
        <div style="font-size:12px;color:#57606a;">bekleniyor</div></td>
      <td style="width:10px;"></td>
      <td style="padding:10px 16px;background:#f6f8fa;border-radius:8px;">
        <div style="font-size:22px;font-weight:700;color:#1f2328;">{total}</div>
        <div style="font-size:12px;color:#57606a;">toplam ürün</div></td>
    </tr>
  </table>
  {highlight}
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
    <tr><th {th} colspan="2">Ürün</th><th {th_center}>Durum</th><th {th}>Değişiklik</th></tr>
    {"".join(rows)}
  </table>
</td></tr>
<tr><td style="padding:16px 24px 22px;font-size:12px;color:#8c959f;">
  Son kontrol: {format_dt(now_tr().isoformat())} (Türkiye saati)<br>
  Bu e-posta stok takipçiniz tarafından otomatik olarak gönderildi.
</td></tr>
</table></td></tr></table></body></html>"""


def build_plain_text(heading, intro, products, new_asins, compact=False):
    lines = [heading, intro, ""]
    if new_asins:
        lines.append("🎉 YENİ STOĞA GİRENLER")
        for asin in new_asins:
            info = products.get(asin, {})
            price = f" — {info['price']}" if info.get("price") else ""
            lines.append(f"• {info.get('title') or asin}{price}")
            lines.append(f"  {BASE}/dp/{asin}")
        lines.append("")
    total = len(products)
    lines.append(f"📋 LİSTE DURUMU ({count_available(products)}/{total} stokta)")
    for asin, info in sorted_products(products, set(new_asins)):
        emoji = STATUS_INFO.get(info.get("status"), STATUS_INFO["unknown"])[1]
        title = info.get("title") or asin
        if compact and len(title) > 55:
            title = title[:52] + "..."
        lines.append(f"{emoji} {title}")
        if not compact:
            lines.append(f"   {BASE}/dp/{asin}")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Bildirimler
# --------------------------------------------------------------------------

def send_telegram(text):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not (token and chat_id):
        return False
    if len(text) > 4000:
        text = text[:3990] + "\n..."
    response = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data={"chat_id": chat_id, "text": text, "disable_web_page_preview": "true"},
        timeout=30,
    )
    response.raise_for_status()
    return True


def send_email(subject, plain_body, html_body=None):
    user = os.environ.get("SMTP_USER")
    password = os.environ.get("SMTP_PASSWORD")
    to_addr = os.environ.get("EMAIL_TO") or user
    if not (user and password and to_addr):
        return False
    host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    port = int(os.environ.get("SMTP_PORT", "465"))

    message = MIMEMultipart("alternative")
    message["Subject"] = Header(subject, "utf-8")
    message["From"] = user
    message["To"] = to_addr
    message.attach(MIMEText(plain_body, "plain", "utf-8"))
    if html_body:
        message.attach(MIMEText(html_body, "html", "utf-8"))

    with smtplib.SMTP_SSL(host, port, timeout=30) as server:
        server.login(user, password)
        server.sendmail(user, [a.strip() for a in to_addr.split(",")], message.as_string())
    return True


def notify(subject, plain_body, html_body=None, telegram_body=None):
    sent_any = False
    for name, sender in (
        ("Telegram", lambda: send_telegram(f"{subject}\n\n{telegram_body or plain_body}")),
        ("E-posta", lambda: send_email(subject, plain_body, html_body)),
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


def send_report(subject, heading, intro, products, new_asins=()):
    new_asins = list(new_asins)
    notify(
        subject,
        build_plain_text(heading, intro, products, new_asins),
        build_html_email(heading, intro, products, new_asins),
        build_plain_text(heading, intro, products, new_asins, compact=True),
    )


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


def summary_due(state, now):
    if SUMMARY_HOUR in ("off", "kapali", "kapalı", "-1"):
        return False
    try:
        hour = int(SUMMARY_HOUR)
    except ValueError:
        hour = 9
    return now.hour >= hour and state.get("last_summary_date") != now.date().isoformat()


def run():
    state = load_state()
    old_products = state.get("products", {})
    now = now_tr()
    now_iso = now.isoformat(timespec="minutes")

    try:
        targets = collect_targets()
        if not targets:
            print("Takip edilecek ürün yok. WISHLIST_URLS ayarını kontrol edin.")
            return

        products = {}
        newly_available = []
        print(f"{len(targets)} ürün kontrol ediliyor...")
        for asin, list_title in targets.items():
            result = check_product(asin)
            old = old_products.get(asin, {})
            status = result["status"]
            print(f"  {asin}: {status}  ({(result['title'] or list_title)[:60]})")

            entry = dict(old)
            entry["title"] = result["title"] or old.get("title") or list_title or asin
            entry["image"] = result["image"] or old.get("image", "")
            entry["checked_at"] = now_iso

            if status == "unknown":
                # Bu kez okunamadıysa önceki bilinen durumu koru
                entry.setdefault("status", "unknown")
            else:
                previous = old.get("status")
                if status != previous:
                    entry["changed_at"] = now_iso
                if (counts_as_available(status) and previous is not None
                        and previous != "unknown" and not counts_as_available(previous)):
                    newly_available.append(asin)
                entry["status"] = status
                entry["price"] = result["price"]
            entry.setdefault("changed_at", now_iso)
            products[asin] = entry
            polite_pause()

        # Listeden çıkarılan ürünler de durum dosyasından silinir
        state["products"] = products
        state["blocked_streak"] = 0

    except Blocked as exc:
        state["blocked_streak"] = state.get("blocked_streak", 0) + 1
        print(f"UYARI: Amazon isteği engelledi ({exc}). Bu çalışma atlandı.")
        if state["blocked_streak"] == BLOCK_WARNING_AFTER:
            notify(
                "⚠️ Amazon stok takibi: erişim engelleniyor",
                f"Son {BLOCK_WARNING_AFTER} kontrolde Amazon istekleri engelledi. "
                "Kontroller devam edecek; sorun sürerse kontrol sıklığını azaltmayı "
                "veya scripti kendi bilgisayarınızda çalıştırmayı düşünün.",
            )
        save_state(state)
        return

    total = len(products)
    available = count_available(products)

    if not state.get("initialized"):
        send_report(
            f"✅ Stok takibi başladı: {available}/{total} ürün stokta",
            "Stok takibi başladı",
            f"Listenizdeki {total} ürün takibe alındı. Stokta olmayan bir ürün "
            "stoğa girdiğinde size hemen haber vereceğim.",
            products,
        )
        state["initialized"] = True
        state["last_summary_date"] = now.date().isoformat()
    elif newly_available:
        first_title = products[newly_available[0]]["title"]
        if len(newly_available) == 1:
            subject = f"🟢 Stoğa girdi: {first_title[:60]}"
            heading = "Bir ürün stoğa girdi!"
        else:
            subject = f"🟢 {len(newly_available)} ürün stoğa girdi"
            heading = f"{len(newly_available)} ürün stoğa girdi!"
        send_report(
            subject,
            heading,
            "Stok tükenmeden göz atmak isteyebilirsiniz. Listenizin güncel durumu aşağıda.",
            products,
            newly_available,
        )
        if summary_due(state, now):
            state["last_summary_date"] = now.date().isoformat()
    elif summary_due(state, now):
        send_report(
            f"📋 Günlük stok raporu – {now.day} {TR_MONTHS[now.month - 1]}: "
            f"{available}/{total} ürün stokta",
            "Günlük stok raporu",
            "Alışveriş listenizdeki ürünlerin güncel stok durumu.",
            products,
        )
        state["last_summary_date"] = now.date().isoformat()

    save_state(state)


if __name__ == "__main__":
    if "--test-notify" in sys.argv:
        ok = notify("Amazon stok takibi: test", "Bildirimler çalışıyor. 🎉")
        sys.exit(0 if ok else 1)
    run()
