"""Trendyol'da anahtar kelimeyle ürün arayıp ürün koduna göre açar.

Ürün, URL'deki kod ile tanınır: ...-p-969494132

Kullanım:
    python trendyol_search.py --keyword "parmak eldiven" --product-id 825575043 --loop
    python trendyol_search.py --keyword "parmaklık" --product-id 969494132 --headed
"""

from __future__ import annotations

import argparse
import base64
import os
import re
import select
import socket
import ssl
import sys
import time
import uuid
from datetime import datetime
from threading import Event, Lock, Thread
from urllib.parse import quote_plus, unquote, urlparse
from zoneinfo import ZoneInfo

from playwright.sync_api import (
    Locator,
    Page,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)

TRENDYOL_HOME = "https://www.trendyol.com/"
FOREIGN_STOREFRONT = re.compile(
    r"https://www\.trendyol\.com/(de|en|fr|at|nl|be|pl|gr|ro|bg|cz|sk|hu|sa|ae|kw|qa|az|kz)(?:-[a-zA-Z]{2})?(?=/|\?|$)",
    re.I,
)
PRODUCT_HREF = re.compile(r"-p-\d+")
PRODUCT_ID_IN_URL = re.compile(r"-p-(\d+)")
HEAVY_ASSET = re.compile(
    r"\.(?:png|jpe?g|gif|webp|avif|svg|ico|bmp|woff2?|ttf|otf|eot|mp4|webm|m4v)(?:\?|$)",
    re.I,
)

PRODUCT_CARD_SELECTORS = [
    "div.p-card-wrppr a[href*='-p-']",
    "a.p-card-wrppr[href*='-p-']",
    "div.prdct-cntnr-wrppr a[href*='-p-']",
    "[data-testid='product-card'] a[href*='-p-']",
    "a[href*='-p-']:not([href*='sepet'])",
]
MAX_SEARCH_PAGES = 30
PAGE_SIZE = 24
ISTANBUL_TZ = ZoneInfo("Europe/Istanbul")
HOURLY_HISTORY_LIMIT = 168
# Turlar arası bekleme (saniye). 0 = ara vermeden tekrar ara.
CHECK_INTERVAL_SECONDS = int(os.environ.get("CHECK_INTERVAL_SECONDS", "3"))
# Cloudflare ara sayfası geçmezse yeni IP denemeden önce bekleme.
CHALLENGE_BACKOFF_SECONDS = int(os.environ.get("CHALLENGE_BACKOFF_SECONDS", "20"))
CHALLENGE_MARKERS = (
    "just a moment",
    "bir dakika lütfen",
    "güvenlik doğrulaması",
    "attention required",
    "checking your browser",
    "cf-chl",
    "cloudflare",
    "kötü niyetli bot",
)

FIND_ON_PAGE_JS = """(productId) => {
  const re = new RegExp('-p-' + productId + '(?:[/?#]|$)');
  const ids = [];
  const seen = new Set();
  let href = '';
  for (const a of document.querySelectorAll('a[href*="-p-"]')) {
    const raw = a.getAttribute('href') || '';
    const m = raw.match(/-p-(\\d+)/);
    if (!m || seen.has(m[1])) continue;
    seen.add(m[1]);
    ids.push(m[1]);
    if (!href && m[1] === productId && re.test(raw)) href = raw;
  }
  if (!href) {
    for (const el of document.querySelectorAll('[data-id], [data-product-id], [data-contentid]')) {
      const id = el.getAttribute('data-id') || el.getAttribute('data-product-id') || el.getAttribute('data-contentid') || '';
      if (id !== productId) continue;
      const a = el.closest('a') || el.querySelector('a[href*="-p-"]');
      href = (a && a.getAttribute('href')) || ('/p-' + id);
      if (!seen.has(id)) { seen.add(id); ids.push(id); }
      break;
    }
  }
  const text = (document.body && document.body.innerText || '').slice(0, 2500);
  const blob = ((document.title || '') + ' ' + text).toLowerCase();
  return {
    ids,
    href,
    count: document.querySelectorAll('a[href*="-p-"]').length,
    blocked: /captcha|access denied|just a moment|bir dakika lütfen|güvenlik doğrulaması|unusual traffic|cf-chl|checking your browser/.test(blob),
  };
}"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Trendyol'da anahtar kelimeyle ürün ara ve ürünü aç."
    )
    parser.add_argument(
        "--keyword",
        "-k",
        required=True,
        help="Arama kutusuna yazılacak anahtar kelime",
    )
    parser.add_argument(
        "--product-id",
        "--id",
        dest="product_id",
        default="",
        help="Trendyol ürün kodu (örnek: 825575043). URL'deki -p- numarasından alınır.",
    )
    parser.add_argument(
        "--product",
        "-p",
        default="",
        help="Açılacak ürünün başlığında geçmesi gereken metin (opsiyonel)",
    )
    parser.add_argument(
        "--index",
        "-i",
        type=int,
        default=1,
        help="Sonuç listesinde açılacak ürün sırası (1'den başlar, varsayılan: 1)",
    )
    parser.add_argument(
        "--headed",
        action="store_true",
        help="Tarayıcı penceresini göster",
    )
    parser.add_argument(
        "--keep-open",
        action="store_true",
        help="Ürün açıldıktan sonra tarayıcıyı kapatma",
    )
    parser.add_argument(
        "--from-home",
        action="store_true",
        help="Ana sayfa arama kutusunu kullan (daha yavaş)",
    )
    parser.add_argument(
        "--loop",
        nargs="?",
        const=0,
        type=int,
        default=1,
        metavar="N",
        help="Tekrar et. --loop sonsuz, --loop 20 yirmi kez.",
    )
    return parser.parse_args()


def search_url(keyword: str, page_index: int = 1) -> str:
    url = f"{TRENDYOL_HOME}sr?q={quote_plus(keyword)}"
    if page_index > 1:
        url += f"&pi={page_index}"
    return url


def turkey_url(url: str) -> str:
    if not url:
        return url
    return FOREIGN_STOREFRONT.sub("https://www.trendyol.com", url, count=1)


def is_foreign_storefront(url: str) -> bool:
    return bool(FOREIGN_STOREFRONT.search(url or ""))


def normalize_product_id(value: str) -> str:
    raw = (value or "").strip()
    if not raw:
        return ""
    match = PRODUCT_ID_IN_URL.search(raw)
    if match:
        return match.group(1)
    if re.fullmatch(r"\d+", raw):
        return raw
    raise ValueError(
        "Ürün kodu sayı olmalı veya içinde -p-969494132 gibi bir kod bulunan URL olmalı."
    )


def product_id_href_pattern(product_id: str) -> re.Pattern[str]:
    return re.compile(rf"-p-{re.escape(product_id)}(?:[/?#]|$)")


def url_has_product_id(url: str, product_id: str = "") -> bool:
    if product_id:
        return bool(product_id_href_pattern(product_id).search(url))
    return bool(PRODUCT_HREF.search(url))


def absolute_product_url(href: str | None) -> str:
    if not href:
        return ""
    if href.startswith("http"):
        return href
    if href.startswith("/"):
        return "https://www.trendyol.com" + href
    return ""


def attach_bandwidth_limits(context) -> None:
    """HTML/JS kalsın; görsel, font, video ve tracker'lar proxy kotasını yakmasın."""
    context.route(HEAVY_ASSET, lambda route: route.abort())
    context.route(
        re.compile(
            r"(google-analytics|googletagmanager|googlesyndication|doubleclick|"
            r"facebook\.net|hotjar|clarity\.ms|criteo|adjust\.com|newrelic)",
            re.I,
        ),
        lambda route: route.abort(),
    )


def dismiss_overlays(page: Page) -> None:
    page.evaluate(
        """() => {
            const labels = [
                'Tüm Tanımlama Bilgilerini Kabul Et',
                'Tümünü Kabul Et',
                'Kabul Et',
                'Accept All',
                'Alle akzeptieren',
                'Accept all',
            ];
            for (const button of document.querySelectorAll('button')) {
                const text = (button.innerText || '').trim();
                if (labels.some((label) => text.includes(label))) {
                    button.click();
                    break;
                }
            }
            document.querySelector('#onetrust-accept-btn-handler')?.click();
            document.querySelector('.modal-section-close')?.click();
            document.querySelector('.modal-close')?.click();
        }"""
    )


def pick_turkey_country(page: Page) -> bool:
    clicked = page.evaluate(
        """() => {
          const labels = ['Türkiye', 'Turkey', 'Turkiye'];
          for (const el of document.querySelectorAll('a, button, [role="button"], span')) {
            const text = (el.innerText || el.textContent || '').trim();
            if (labels.some((label) => text === label || text.startsWith(label + ' ') || text.startsWith(label + '\\n'))) {
              el.click();
              return true;
            }
          }
          const link = document.querySelector('a[href*="country=TR"], a[href*="countryCode=TR"], a[href*="storeFrontId=1"]');
          if (link) { link.click(); return true; }
          return false;
        }"""
    )
    return bool(clicked)


def force_turkey_storefront(page: Page) -> None:
    current = page.url or ""
    low = current.lower()
    if "select-country" in low or "ulke-sec" in low or "choose-country" in low:
        if pick_turkey_country(page):
            try:
                page.wait_for_load_state("domcontentloaded", timeout=15000)
            except PlaywrightTimeoutError:
                pass
            current = page.url or ""
    rewritten = turkey_url(current)
    if rewritten != current and is_foreign_storefront(current):
        print(f"Yabancı vitrin -> TR: {current[:80]}", flush=True)
        page.goto(rewritten, wait_until="commit", timeout=navigation_timeout_ms())
        try:
            page.wait_for_function(
                "() => !!(document.body && document.body.innerText.trim().length > 20)",
                timeout=12000,
            )
        except PlaywrightTimeoutError:
            pass
        dismiss_overlays(page)


def product_links(page: Page) -> Locator:
    for selector in PRODUCT_CARD_SELECTORS:
        links = page.locator(selector)
        try:
            links.first.wait_for(state="attached", timeout=2500)
            if links.count() > 0:
                return links
        except PlaywrightTimeoutError:
            continue
    return page.locator("a[href*='-p-']")


def unique_listing_ids(page: Page) -> list[str]:
    ids = page.evaluate(
        """() => {
          const ids = [];
          const seen = new Set();
          for (const a of document.querySelectorAll('a[href*="-p-"]')) {
            const m = (a.getAttribute('href') || '').match(/-p-(\\d+)/);
            if (!m || seen.has(m[1])) continue;
            seen.add(m[1]);
            ids.push(m[1]);
          }
          return ids;
        }"""
    )
    return [str(item) for item in (ids or [])]


def current_hour_start() -> datetime:
    return datetime.now(ISTANBUL_TZ).replace(minute=0, second=0, microsecond=0)


def hour_iso_from_point(point: dict) -> str | None:
    hour = point.get("hour")
    if hour:
        return str(hour)
    ts = point.get("ts")
    if not ts:
        return None
    return (
        datetime.fromtimestamp(float(ts), ISTANBUL_TZ)
        .replace(minute=0, second=0, microsecond=0)
        .isoformat()
    )


def normalize_hourly_history(history: list | None) -> list:
    by_hour: dict[str, dict] = {}
    for point in history or []:
        if point is None or point.get("overall") is None:
            continue
        hour_iso = hour_iso_from_point(point)
        if not hour_iso:
            continue
        hour = datetime.fromisoformat(hour_iso)
        if hour.tzinfo is None:
            hour = hour.replace(tzinfo=ISTANBUL_TZ)
        if hour_iso in by_hour:
            continue
        by_hour[hour_iso] = {
            "hour": hour_iso,
            "label": point.get("label") or hour.astimezone(ISTANBUL_TZ).strftime("%d.%m %H:00"),
            "overall": int(point["overall"]),
            "page": point.get("page") or point.get("listing_page"),
            "rank": point.get("rank"),
            "ts": point.get("ts") or hour.timestamp(),
        }
    ordered = [by_hour[key] for key in sorted(by_hour)]
    return ordered[-HOURLY_HISTORY_LIMIT:]


def record_hourly_rank(
    stats: dict,
    overall: int,
    listing_page: int,
    rank: int,
) -> bool:
    hour = current_hour_start()
    hour_iso = hour.isoformat()
    history = stats.setdefault("history", [])
    stats["history"] = history
    if history and hour_iso_from_point(history[-1]) == hour_iso:
        return False
    history.append(
        {
            "hour": hour_iso,
            "label": hour.strftime("%d.%m %H:00"),
            "overall": overall,
            "page": listing_page,
            "rank": rank,
            "ts": time.time(),
        }
    )
    if len(history) > HOURLY_HISTORY_LIMIT:
        del history[:-HOURLY_HISTORY_LIMIT]
    return True


def rank_from_listing(page: Page, product_id: str) -> tuple[int, int, int]:
    ids = unique_listing_ids(page)
    if product_id in ids:
        overall = ids.index(product_id) + 1
    else:
        overall = max(len(ids), 1)
    listing_page = (overall - 1) // PAGE_SIZE + 1
    rank = (overall - 1) % PAGE_SIZE + 1
    return overall, listing_page, rank


def listing_page_snapshot(page: Page) -> dict:
    try:
        return page.evaluate(
            """() => {
              const text = (document.body && document.body.innerText || '')
                .replace(/\\s+/g, ' ').trim().slice(0, 360);
              return {
                url: location.href || '',
                title: document.title || '',
                links: document.querySelectorAll('a[href*="-p-"]').length,
                text,
              };
            }"""
        ) or {}
    except Exception:
        return {"url": getattr(page, "url", ""), "title": "", "links": 0, "text": ""}


def snapshot_is_challenge(snap: dict) -> bool:
    if int(snap.get("links") or 0) > 0:
        return False
    blob = f"{snap.get('title') or ''} {snap.get('text') or ''}".lower()
    return any(marker in blob for marker in CHALLENGE_MARKERS)


def classify_listing_failure(snap: dict) -> str:
    url = str(snap.get("url") or "")
    title = str(snap.get("title") or "")
    text = str(snap.get("text") or "")
    blob = f"{url} {title} {text}".lower()
    links = int(snap.get("links") or 0)
    if "about:blank" in url or url == "":
        return "sayfa hiç açılmadı (about:blank)"
    if snapshot_is_challenge(snap):
        return "Cloudflare / bot duvarı"
    if is_foreign_storefront(url) or "/de/" in url or "suchergebnisse" in blob:
        return "Almanya/yabancı vitrin — Türkiye sitesi değil (VPS IP)"
    if "captcha" in blob:
        return "captcha"
    if any(word in blob for word in ("access denied", "403", "request blocked", "erişim engell")):
        return "IP/erişim engeli"
    if any(word in blob for word in ("proxy", "dataimpulse", "authentication failed", "407")):
        return "proxy kimlik doğrulama hatası"
    if any(word in blob for word in ("tüm tanımlama", "kabul et", "çerez", "cookie")):
        return "çerez ekranı, ürün listesi yok"
    if "/sr" not in url and "trendyol.com" in url:
        return f"arama sayfasına girmedi, yönlendi: {url[:80]}"
    if "trendyol.com" in url and links == 0:
        return "Trendyol açıldı ama ürün kartı yok (JS/proxy yavaş veya boş sonuç)"
    if links == 0:
        return f"ürün linki yok, yabancı sayfa: {title[:80] or url[:80]}"
    return f"{links} ürün linki var ama aranan kod yok"


def listing_failure_message(page: Page, reason: str) -> str:
    snap = listing_page_snapshot(page)
    diagnosis = classify_listing_failure(snap)
    url = str(snap.get("url") or "")[:90]
    title = str(snap.get("title") or "")[:60]
    text = str(snap.get("text") or "")[:120]
    message = f"{reason} | {diagnosis} | {url} | {title} | {text}"
    print(f"Liste hatası: {message}", flush=True)
    return message[:400]


def wait_for_listing(page: Page, timeout_ms: int) -> bool:
    ready = page.locator("a[href*='-p-']").first
    try:
        ready.wait_for(state="attached", timeout=timeout_ms)
        return True
    except PlaywrightTimeoutError:
        return False


def wait_out_challenge(page: Page, timeout_ms: int = 12000) -> bool:
    """JS kontrol sayfası kendiliğinden geçerse ürün listesini bekle."""
    if wait_for_listing(page, 1500):
        return True
    if not snapshot_is_challenge(listing_page_snapshot(page)):
        return wait_for_listing(page, min(timeout_ms, 8000))
    print("Cloudflare ara sayfası: geçmesi bekleniyor", flush=True)
    try:
        page.wait_for_function(
            """() => {
              if (document.querySelector('a[href*="-p-"]')) return true;
              const blob = ((document.title || '') + ' ' + (document.body && document.body.innerText || '')).toLowerCase();
              const challenge = /just a moment|bir dakika lütfen|güvenlik doğrulaması|attention required|checking your browser|cf-chl|cloudflare|kötü niyetli bot/.test(blob);
              return !challenge && !!(document.body && document.body.innerText.trim().length > 20);
            }""",
            timeout=timeout_ms,
        )
    except PlaywrightTimeoutError:
        return False
    dismiss_overlays(page)
    return wait_for_listing(page, 12000)


def find_product_in_results(
    page: Page,
    keyword: str,
    product_id: str,
) -> tuple[Locator, int, int, int]:
    empty_pages = 0

    for page_index in range(1, MAX_SEARCH_PAGES + 1):
        if page_index > 1:
            goto(page, search_url(keyword, page_index))
            dismiss_overlays(page)

        listing_wait = 25000 if page_index == 1 else 10000
        if not wait_for_listing(page, listing_wait):
            if page_index == 1:
                if wait_out_challenge(page, 12000):
                    pass
                else:
                    page.wait_for_timeout(2500)
                    dismiss_overlays(page)
                    if not wait_for_listing(page, 8000):
                        raise RuntimeError(
                            listing_failure_message(page, "Arama listesi yüklenmedi")
                        )
            else:
                empty_pages += 1
                if empty_pages >= 2:
                    raise RuntimeError(
                        listing_failure_message(page, "Arama listesi yüklenmedi")
                    )
                continue
        empty_pages = 0

        info = {"href": "", "ids": []}
        for _ in range(5):
            info = page.evaluate(FIND_ON_PAGE_JS, product_id)
            if info.get("blocked"):
                if wait_out_challenge(page, 12000):
                    continue
                raise RuntimeError(listing_failure_message(page, "Bot/captcha sayfası"))
            if info.get("href"):
                ids = [str(item) for item in (info.get("ids") or [])]
                on_page = ids.index(product_id) + 1 if product_id in ids else 1
                overall = (page_index - 1) * PAGE_SIZE + on_page
                card = page.locator(f"a[href*='-p-{product_id}']").first
                return card, page_index, on_page, overall
            page.mouse.wheel(0, 2200)
            page.wait_for_timeout(120)

        if int(info.get("count") or 0) == 0:
            if page_index == 1:
                raise RuntimeError(listing_failure_message(page, "Arama listesi boş"))
            empty_pages += 1
            if empty_pages >= 2:
                raise RuntimeError(listing_failure_message(page, "Arama listesi boş"))

    raise RuntimeError(
        f"Ürün kodu {product_id} ilk {MAX_SEARCH_PAGES} sayfada bulunamadı."
    )


def pick_product_by_id(page: Page, product_id: str) -> Locator:
    href_pattern = product_id_href_pattern(product_id)
    selector = f"a[href*='-p-{product_id}']"
    links = page.locator(selector)
    try:
        links.first.wait_for(state="attached", timeout=10000)
    except PlaywrightTimeoutError:
        pass

    for _ in range(4):
        count = links.count()
        for i in range(count):
            href = links.nth(i).get_attribute("href") or ""
            if href_pattern.search(href):
                return links.nth(i)
        page.mouse.wheel(0, 1800)
        page.wait_for_timeout(250)

    raise RuntimeError(f"Ürün kodu {product_id} arama sonuçlarında bulunamadı.")


def pick_product(
    page: Page,
    product_id: str,
    product_text: str,
    index: int,
) -> Locator:
    if product_id:
        return pick_product_by_id(page, product_id)

    links = product_links(page)
    links.first.wait_for(state="attached", timeout=12000)

    if product_text:
        matched = links.filter(has_text=re.compile(re.escape(product_text), re.I))
        if matched.count() == 0:
            raise RuntimeError(f"'{product_text}' metnini içeren ürün bulunamadı.")
        return matched.first

    if index < 1:
        raise ValueError("index 1 veya daha büyük olmalıdır.")
    total = links.count()
    if index > total:
        raise RuntimeError(f"Sadece {total} ürün bulundu, {index}. ürün açılamaz.")
    return links.nth(index - 1)


def extract_product_image(page: Page, card: Locator | None = None) -> str:
    if card is not None:
        try:
            src = card.evaluate(
                """el => {
                  const root = el.closest('.p-card-wrppr, .p-card-chldrn-cntnr, article') || el;
                  const img = root.querySelector('img');
                  if (!img) return '';
                  return img.getAttribute('src')
                    || img.getAttribute('data-src')
                    || (img.currentSrc || '');
                }"""
            )
            if src and str(src).startswith("http"):
                return str(src).split("?")[0]
            if src and str(src).startswith("//"):
                return "https:" + str(src).split("?")[0]
        except Exception:
            pass
    try:
        src = page.evaluate(
            """() => {
              const og = document.querySelector('meta[property="og:image"]');
              if (og && og.content) return og.content;
              const img = document.querySelector('img[src*="cdn.dsmcdn.com"]');
              return img ? (img.getAttribute('src') || '') : '';
            }"""
        )
        if src and str(src).startswith("//"):
            return "https:" + str(src)
        if src and str(src).startswith("http"):
            return str(src)
    except Exception:
        pass
    return ""


def open_product(page: Page, card: Locator, product_id: str = "") -> Page:
    href = absolute_product_url(card.get_attribute("href"))
    if href and url_has_product_id(href, product_id):
        try:
            goto(page, href)
        except Exception as exc:
            if url_has_product_id(page.url, product_id):
                return page
            raise
        return page

    card.click(timeout=5000, force=True, no_wait_after=True)
    page.wait_for_url(PRODUCT_HREF, timeout=8000)
    return page


def search_from_homepage(page: Page, keyword: str) -> None:
    trigger = page.locator("[data-testid='suggestion-placeholder']").first
    try:
        trigger.click(timeout=4000)
    except PlaywrightTimeoutError:
        pass

    search = page.locator("input[data-testid='browsing-search-input']").first
    try:
        search.wait_for(state="visible", timeout=4000)
    except PlaywrightTimeoutError:
        search = page.get_by_placeholder(re.compile(r"ürün|marka|kategori", re.I)).first
        search.wait_for(state="visible", timeout=4000)

    search.fill(keyword)
    search.press("Enter")
    page.wait_for_url(re.compile(r"/sr"), timeout=15000)


_PROXY_SETTINGS_CACHE: list[dict[str, str] | None] | None = None
_SESSION_PROXIES: dict[str, "AuthInjectingProxy"] = {}
_LOCAL_PROXY_LOCK = Lock()
_PROBE_LOCK = Lock()
_PROBE_OK = False


def _split_proxy_url(raw: str) -> tuple[str, str, int | None, str, str]:
    text = raw.strip().strip("\"'")
    if "://" not in text:
        text = f"http://{text}"
    scheme, rest = text.split("://", 1)
    username = ""
    password = ""
    if "@" in rest:
        creds, rest = rest.rsplit("@", 1)
        if ":" in creds:
            username, password = creds.split(":", 1)
        else:
            username = creds
    hostport = rest.split("/", 1)[0]
    port: int | None = None
    if ":" in hostport and hostport.rsplit(":", 1)[-1].isdigit():
        host, port_s = hostport.rsplit(":", 1)
        port = int(port_s)
    else:
        host = hostport
    parsed = urlparse(text)
    host = host or (parsed.hostname or "")
    port = port if port is not None else parsed.port
    username = unquote(os.environ.get("PROXY_USERNAME") or username or parsed.username or "").strip()
    password = unquote(
        os.environ.get("PROXY_PASSWORD")
        or os.environ.get("PROXY_PASS")
        or password
        or parsed.password
        or ""
    ).strip()
    return scheme.lower(), host, port, username, password


def _proxy_disabled() -> bool:
    flag = (os.environ.get("PROXY_DISABLED") or "").strip().lower()
    if flag in {"1", "true", "yes", "on"}:
        return True
    raw = (os.environ.get("PROXY_SERVER") or "").strip().strip("\"'")
    return raw.lower() in {"", "off", "none", "null", "disabled", "false", "0", "-"}


def parse_proxy_settings() -> dict[str, str] | None:
    global _PROXY_SETTINGS_CACHE
    if _PROXY_SETTINGS_CACHE is not None:
        return _PROXY_SETTINGS_CACHE[0]
    if _proxy_disabled():
        print("Proxy: kapalı (PROXY_DISABLED veya PROXY_SERVER boş)", flush=True)
        _PROXY_SETTINGS_CACHE = [None]
        return None
    raw = (os.environ.get("PROXY_SERVER") or "").strip().strip("\"'")
    scheme, host, port, username, password = _split_proxy_url(raw)
    if not host:
        print("Proxy: PROXY_SERVER host okunamadı", flush=True)
        _PROXY_SETTINGS_CACHE = [None]
        return None
    if scheme.startswith("socks"):
        print(
            "Chromium SOCKS5 şifre desteklemez; DataImpulse HTTP 823 kullanılıyor.",
            flush=True,
        )
        scheme = "http"
        if port in {None, 824}:
            port = 823
    if port is None:
        port = 823
    user_hint = f"{username[:4]}… len={len(username)}" if username else "YOK"
    print(
        f"Proxy: {scheme}://{host}:{port} user={user_hint} auth="
        f"{'yes' if username and password else 'NO'}",
        flush=True,
    )
    settings = {
        "scheme": scheme,
        "host": host,
        "port": str(port),
        "username": username,
        "password": password,
    }
    _PROXY_SETTINGS_CACHE = [settings]
    return settings


def build_proxy_config(session_id: str | None = None) -> dict[str, str] | None:
    """Playwright'a verilen proxy. Auth yerel tünele gömülür."""
    local = ensure_session_proxy(session_id or "probe")
    if local is None:
        return None
    return {"server": f"http://127.0.0.1:{local.port}"}


def with_sessid(username: str, session_id: str) -> str:
    raw = (username or "").strip()
    if ";sessid." in raw:
        raw = raw.split(";sessid.")[0]
    token = re.sub(r"[^a-zA-Z0-9]", "", session_id)[:16] or "1"
    return f"{raw};sessid.{token}"


def new_session_id() -> str:
    return uuid.uuid4().hex[:12]


def _basic_token(username: str, password: str) -> str:
    return base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")


def _read_http_headers(sock: socket.socket) -> bytes:
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            break
        buf += chunk
        if len(buf) > 65536:
            break
    return buf


def probe_proxy() -> None:
    global _PROBE_OK
    with _PROBE_LOCK:
        if _PROBE_OK:
            return
        _probe_proxy_once()
        _PROBE_OK = True


def _probe_proxy_once() -> None:
    settings = parse_proxy_settings()
    if not settings:
        return
    username = settings["username"]
    password = settings["password"]
    if not username or not password:
        raise RuntimeError(
            "PROXY_SERVER içinde login:şifre yok. Coolify Environment'ta tek satır: "
            "http://LOGIN:SIFRE@gw.dataimpulse.com:823 (407 NO_USER bunun yüzünden çıkar)."
        )
    host = settings["host"]
    port = int(settings["port"])
    token = _basic_token(username, password)
    sock = socket.create_connection((host, port), timeout=25)
    try:
        sock.sendall(
            (
                "CONNECT api.ipify.org:443 HTTP/1.1\r\n"
                "Host: api.ipify.org:443\r\n"
                f"Proxy-Authorization: Basic {token}\r\n"
                "\r\n"
            ).encode("ascii")
        )
        response = _read_http_headers(sock)
        status = response.split(b"\r\n", 1)[0].decode("latin1", "replace")
        if b" 200 " not in response.split(b"\r\n", 1)[0]:
            raise RuntimeError(status[:180] or "proxy CONNECT reddetti")
        tls = ssl.create_default_context().wrap_socket(sock, server_hostname="api.ipify.org")
        try:
            tls.sendall(b"GET / HTTP/1.1\r\nHost: api.ipify.org\r\nConnection: close\r\n\r\n")
            body = b""
            while True:
                chunk = tls.recv(4096)
                if not chunk:
                    break
                body += chunk
        finally:
            try:
                tls.close()
            except Exception:
                pass
        text = body.decode("utf-8", "replace")
        ip = text.rsplit("\r\n\r\n", 1)[-1].strip()[:64]
        print(f"Proxy ön kontrol OK, çıkış IP: {ip}", flush=True)
    except OSError as exc:
        raise RuntimeError(
            f"DataImpulse {host}:{port} bağlanamadı ({exc}). VPS çıkış/firewall kontrol et."
        ) from exc
    except Exception as exc:
        text = str(exc)
        if "NO_USER" in text:
            raise RuntimeError(
                "DataImpulse 407 NO_USER: login/plan yok veya şifre yanlış. "
                "Dashboard > Proxy Access login/şifreyi kopyala; Coolify'da "
                "PROXY_SERVER=http://LOGIN:SIFRE@gw.dataimpulse.com:823 olsun."
            ) from exc
        raise RuntimeError(f"DataImpulse proxy ön kontrol başarısız: {text[:180]}") from exc
    finally:
        try:
            sock.close()
        except Exception:
            pass
    ensure_session_proxy("probe")


class AuthInjectingProxy(Thread):
    """Chromium 407 challenge beklemesin diye DataImpulse'a Basic auth'u ilk CONNECT'te ekler."""

    def __init__(self, host: str, port: int, username: str, password: str) -> None:
        super().__init__(name="auth-http-proxy", daemon=True)
        self.upstream = (host, port)
        self.token = _basic_token(username, password)
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(128)
        self.port = int(self._sock.getsockname()[1])
        self._stop = Event()

    def stop(self) -> None:
        self._stop.set()
        try:
            self._sock.close()
        except Exception:
            pass

    def run(self) -> None:
        self._sock.settimeout(1.0)
        while not self._stop.is_set():
            try:
                client, _addr = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            Thread(target=self._handle, args=(client,), daemon=True).start()

    def _handle(self, client: socket.socket) -> None:
        upstream: socket.socket | None = None
        try:
            client.settimeout(30)
            request = _read_http_headers(client)
            if b"\r\n\r\n" not in request:
                return
            header, rest = request.split(b"\r\n\r\n", 1)
            first = header.split(b"\r\n", 1)[0].decode("latin1", "replace")
            parts = first.split()
            if len(parts) < 2:
                return
            method, target = parts[0], parts[1]
            upstream = socket.create_connection(self.upstream, timeout=25)
            auth = f"Proxy-Authorization: Basic {self.token}\r\n"
            if method.upper() == "CONNECT":
                payload = (
                    f"CONNECT {target} HTTP/1.1\r\n"
                    f"Host: {target}\r\n"
                    f"{auth}"
                    "\r\n"
                ).encode("latin1")
                upstream.sendall(payload)
                reply = _read_http_headers(upstream)
                client.sendall(reply)
                status = reply.split(b"\r\n", 1)[0]
                if b" 200 " not in status:
                    return
                if rest:
                    upstream.sendall(rest)
            else:
                lines = header.decode("latin1", "replace").split("\r\n")
                lines = [line for line in lines if not line.lower().startswith("proxy-authorization")]
                if lines:
                    lines.insert(1, f"Proxy-Authorization: Basic {self.token}")
                upstream.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("latin1") + rest)
            _pipe_sockets(client, upstream)
        except Exception:
            pass
        finally:
            for sock in (client, upstream):
                if sock is None:
                    continue
                try:
                    sock.close()
                except Exception:
                    pass


def _pipe_sockets(left: socket.socket, right: socket.socket) -> None:
    sockets = [left, right]
    while True:
        readable, _, _ = select.select(sockets, [], [], 60)
        if not readable:
            return
        for sock in readable:
            other = right if sock is left else left
            data = sock.recv(65536)
            if not data:
                return
            other.sendall(data)


def ensure_session_proxy(session_id: str) -> AuthInjectingProxy | None:
    settings = parse_proxy_settings()
    if not settings:
        return None
    if not settings["username"] or not settings["password"]:
        return None
    sid = session_id or "probe"
    username = with_sessid(settings["username"], sid)
    with _LOCAL_PROXY_LOCK:
        proxy = _SESSION_PROXIES.get(sid)
        if proxy is None:
            proxy = AuthInjectingProxy(
                settings["host"],
                int(settings["port"]),
                username,
                settings["password"],
            )
            proxy.start()
            _SESSION_PROXIES[sid] = proxy
            print(
                f"Yerel proxy tüneli: 127.0.0.1:{proxy.port} sessid={sid} -> {settings['host']}:{settings['port']}",
                flush=True,
            )
        return proxy


def drop_session_proxy(session_id: str) -> None:
    with _LOCAL_PROXY_LOCK:
        proxy = _SESSION_PROXIES.pop(session_id, None)
    if proxy is None:
        return
    try:
        proxy.stop()
    except Exception:
        pass
    print(f"Proxy oturumu kapatıldı: {session_id}", flush=True)


def navigation_timeout_ms() -> int:
    return 90000 if (os.environ.get("PROXY_SERVER") or "").strip() else 20000


def goto(page: Page, url: str) -> None:
    timeout = navigation_timeout_ms()
    last_error: Exception | None = None
    for attempt in range(2):
        try:
            page.goto(url, wait_until="commit", timeout=timeout)
            try:
                page.wait_for_function(
                    """() => {
                      if (document.querySelector('a[href*="-p-"]')) return true;
                      const blob = ((document.title || '') + ' ' + (document.body && document.body.innerText || '')).toLowerCase();
                      const challenge = /just a moment|bir dakika lütfen|güvenlik doğrulaması|attention required|checking your browser|cf-chl|cloudflare|kötü niyetli bot/.test(blob);
                      if (challenge) return true;
                      return !!(document.title || (document.body && document.body.innerText.trim().length > 20));
                    }""",
                    timeout=15000,
                )
            except PlaywrightTimeoutError:
                pass
            if snapshot_is_challenge(listing_page_snapshot(page)):
                if not wait_out_challenge(page, 12000):
                    raise RuntimeError(
                        listing_failure_message(page, "Arama listesi yüklenmedi")
                    )
            dismiss_overlays(page)
            force_turkey_storefront(page)
            return
        except Exception as exc:
            last_error = exc
            message = str(exc)
            if "ERR_SOCKS" in message or "ERR_PROXY" in message or "ERR_TUNNEL" in message:
                break
            page.wait_for_timeout(800)
    text = str(last_error or "timeout")
    raise RuntimeError(
        "Trendyol HTTP proxy üzerinden açılmadı. DataImpulse bakiyesi ve "
        f"PROXY_SERVER=http://LOGIN:SIFRE@gw.dataimpulse.com:823 kontrol et. ({text[:180]})"
    )


def new_browser_context(browser):
    context = browser.new_context(
        locale="tr-TR",
        timezone_id="Europe/Istanbul",
        viewport={"width": 1280, "height": 800},
        ignore_https_errors=True,
        extra_http_headers={
            "Accept-Language": "tr-TR,tr;q=0.9,en;q=0.8",
            "culture": "tr-TR",
            "storefront-id": "1",
        },
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        ),
    )
    try:
        context.add_cookies(
            [
                {"name": "language", "value": "tr", "url": TRENDYOL_HOME},
                {"name": "culture", "value": "tr-TR", "url": TRENDYOL_HOME},
                {"name": "storeFrontId", "value": "1", "url": TRENDYOL_HOME},
                {"name": "countryCode", "value": "TR", "url": TRENDYOL_HOME},
            ]
        )
    except Exception:
        pass
    attach_bandwidth_limits(context)
    return context


def is_challenge_error(exc: BaseException) -> bool:
    text = str(exc).lower()
    return any(
        marker in text
        for marker in (
            "cloudflare",
            "bot duvarı",
            "bot/captcha",
            "güvenlik doğrulaması",
            "bir dakika",
        )
    )


def recycle_context(browser, context, page):
    try:
        page.close()
    except Exception:
        pass
    try:
        context.close()
    except Exception:
        pass
    context = new_browser_context(browser)
    page = new_page(context)
    return context, page


def rotate_proxy_browser(playwright, headed: bool, session_id: str, browser, context, page):
    try:
        page.close()
    except Exception:
        pass
    try:
        context.close()
    except Exception:
        pass
    try:
        browser.close()
    except Exception:
        pass
    drop_session_proxy(session_id)
    session_id = new_session_id()
    browser = launch_browser(playwright, headed=headed, session_id=session_id)
    context = new_browser_context(browser)
    page = new_page(context)
    print(f"Cloudflare: yeni DataImpulse sessid={session_id}", flush=True)
    return session_id, browser, context, page


def launch_browser(playwright, headed: bool, session_id: str | None = None):
    proxy = build_proxy_config(session_id)
    args = [
        "--disable-blink-features=AutomationControlled",
        "--disable-notifications",
        "--disable-dev-shm-usage",
        "--blink-settings=imagesEnabled=false",
        "--disable-remote-fonts",
    ]
    if os.environ.get("PLAYWRIGHT_DOCKER") == "1":
        args.extend(["--no-sandbox", "--disable-gpu"])
    if proxy:
        args.append("--disable-http2")
    return playwright.chromium.launch(
        headless=not headed,
        args=args,
        proxy=proxy,
    )


def new_page(context) -> Page:
    page = context.new_page()
    page.set_default_timeout(navigation_timeout_ms())
    return page


def run_once(
    page: Page,
    keyword: str,
    product_id: str,
    product: str,
    index: int,
    from_home: bool,
) -> tuple[Page, int, int, int, str]:
    if from_home:
        goto(page, TRENDYOL_HOME)
        dismiss_overlays(page)
        search_from_homepage(page, keyword)
    else:
        goto(page, search_url(keyword))
    dismiss_overlays(page)

    listing_page = 1
    rank = index
    overall = index
    if product_id:
        card, listing_page, rank, overall = find_product_in_results(page, keyword, product_id)
    else:
        card = pick_product(
            page,
            product_id=product_id,
            product_text=product,
            index=index,
        )
    page = open_product(page, card, product_id=product_id)

    if product_id and not url_has_product_id(page.url, product_id):
        raise RuntimeError(
            f"Açılan sayfa ürün kodu {product_id} ile eşleşmiyor: {page.url}"
        )
    image_url = extract_product_image(page, card)
    return page, listing_page, rank, overall, image_url


def run_job_loop(
    keyword: str,
    product_id: str,
    stop_event: Event,
    stats: dict,
    headed: bool = False,
    from_home: bool = False,
    on_history=None,
    on_image=None,
    should_continue=None,
) -> None:
    keyword = keyword.strip()
    product_id = normalize_product_id(product_id)
    if not keyword:
        raise ValueError("Anahtar kelime boş olamaz.")

    stats["status"] = "running"
    stats["history"] = normalize_hourly_history(stats.get("history") or [])
    probe_proxy()

    yielded = False
    session_id = new_session_id()
    with sync_playwright() as playwright:
        browser = launch_browser(playwright, headed=headed, session_id=session_id)
        context = new_browser_context(browser)
        page = new_page(context)
        challenge_streak = 0
        try:
            while not stop_event.is_set():
                stats["attempts"] = int(stats.get("attempts", 0)) + 1
                started = time.perf_counter()
                try:
                    page, listing_page, rank, overall, image_url = run_once(
                        page,
                        keyword=keyword,
                        product_id=product_id,
                        product="",
                        index=1,
                        from_home=from_home,
                    )
                    challenge_streak = 0
                    prev_overall = stats.get("overall")
                    prev_page = stats.get("listing_page")
                    prev_rank = stats.get("rank")
                    stats["listing_page"] = listing_page
                    stats["rank"] = rank
                    stats["overall"] = overall
                    stats["overall_delta"] = (
                        None if prev_overall is None else overall - int(prev_overall)
                    )
                    if prev_page is None or prev_rank is None:
                        stats["page_delta"] = None
                        stats["rank_delta"] = None
                    else:
                        stats["page_delta"] = listing_page - int(prev_page)
                        stats["rank_delta"] = rank - int(prev_rank)
                    stats["success"] = int(stats.get("success", 0)) + 1
                    best = stats.get("best_overall")
                    if best is None or overall < int(best):
                        stats["best_overall"] = overall
                    if record_hourly_rank(stats, overall, listing_page, rank) and on_history:
                        on_history(list(stats.get("history") or []))
                    if image_url and on_image:
                        on_image(image_url)
                    stats["last_url"] = page.url
                    target = int(stats.get("target_rank") or 0)
                    reached_target = target > 0 and overall <= target
                    if reached_target:
                        stats["status"] = "done"
                        stop_event.set()
                    page.close()
                    page = new_page(context)
                    if reached_target:
                        break
                except Exception as exc:
                    stats["fail"] = int(stats.get("fail", 0)) + 1
                    stats["last_error"] = str(exc).split("\n", 1)[0][:400]
                    if is_challenge_error(exc):
                        challenge_streak += 1
                        wait_s = min(CHALLENGE_BACKOFF_SECONDS * challenge_streak, 90)
                        print(
                            f"Cloudflare: DataImpulse IP değişiyor, {wait_s}s bekleniyor",
                            flush=True,
                        )
                        stop_event.wait(wait_s)
                        if stop_event.is_set():
                            break
                        session_id, browser, context, page = rotate_proxy_browser(
                            playwright, headed, session_id, browser, context, page
                        )
                    else:
                        try:
                            page.close()
                        except Exception:
                            pass
                        page = new_page(context)
                stats["last_duration"] = round(time.perf_counter() - started, 2)
                if should_continue is not None and not should_continue():
                    yielded = True
                    break
                if CHECK_INTERVAL_SECONDS > 0:
                    stop_event.wait(CHECK_INTERVAL_SECONDS)
        finally:
            try:
                context.close()
            except Exception:
                pass
            try:
                browser.close()
            except Exception:
                pass
            drop_session_proxy(session_id)
            if stats.get("status") == "done":
                pass
            elif stop_event.is_set():
                if stats.get("status") not in {"done", "stopped"}:
                    stats["status"] = "stopped"
            elif yielded:
                pass
            else:
                stats["status"] = "error"


def run(
    keyword: str,
    product_id: str,
    product: str,
    index: int,
    headed: bool,
    keep_open: bool,
    from_home: bool,
    loop_count: int,
) -> None:
    keyword = keyword.strip()
    product_id = normalize_product_id(product_id)
    if not keyword:
        raise ValueError("Anahtar kelime boş olamaz.")
    if loop_count < 0:
        raise ValueError("loop 0 (sonsuz) veya pozitif bir sayı olmalı.")

    infinite = loop_count == 0
    print(f"Aranıyor: {keyword}", flush=True)
    if product_id:
        print(f"Ürün kodu: {product_id}", flush=True)
    print("Döngü: sonsuz (Ctrl+C ile durdur)" if infinite else f"Döngü: {loop_count} tur", flush=True)

    overall_started = time.perf_counter()
    completed = 0
    probe_proxy()

    with sync_playwright() as playwright:
        session_id = new_session_id()
        browser = launch_browser(playwright, headed=headed, session_id=session_id)
        context = new_browser_context(browser)
        page = new_page(context)

        try:
            while infinite or completed < loop_count:
                attempt = completed + 1
                started = time.perf_counter()
                try:
                    page, listing_page, rank, overall, image_url = run_once(
                        page,
                        keyword=keyword,
                        product_id=product_id,
                        product=product,
                        index=index,
                        from_home=from_home,
                    )
                    completed += 1
                    elapsed = time.perf_counter() - started
                    total = time.perf_counter() - overall_started
                    print(
                        f"Tur {completed}: {overall}. sıra (sayfa {listing_page} / sıra {rank}) | {elapsed:.1f}s | {page.url}",
                        flush=True,
                    )
                    page.close()
                    page = new_page(context)
                except Exception as exc:
                    print(f"Tur {attempt} hata: {exc}", flush=True)
                    try:
                        page.close()
                    except Exception:
                        pass
                    page = new_page(context)
                    if not infinite:
                        raise

            if keep_open and headed:
                print("Tarayıcı açık bırakıldı. Kapatmak için Enter'a basın.", flush=True)
                try:
                    input()
                except EOFError:
                    page.wait_for_timeout(3000)
        except KeyboardInterrupt:
            total = time.perf_counter() - overall_started
            print(f"Durduruldu. Tamamlanan tur: {completed} | toplam {total:.1f}s", flush=True)
            raise
        finally:
            try:
                context.close()
            except Exception:
                pass
            try:
                browser.close()
            except Exception:
                pass
            drop_session_proxy(session_id)


def main() -> int:
    args = parse_args()
    try:
        run(
            keyword=args.keyword,
            product_id=args.product_id,
            product=args.product,
            index=args.index,
            headed=args.headed,
            keep_open=args.keep_open,
            from_home=args.from_home,
            loop_count=args.loop,
        )
    except KeyboardInterrupt:
        print("İptal edildi.")
        return 130
    except Exception as exc:
        print(f"Hata: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
