"""Trendyol'da anahtar kelimeyle ürün arayıp ürün koduna göre açar.

Ürün, URL'deki kod ile tanınır: ...-p-969494132

Kullanım:
    python trendyol_search.py --keyword "parmak eldiven" --product-id 825575043 --loop
    python trendyol_search.py --keyword "parmaklık" --product-id 969494132 --headed
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
from datetime import datetime
from threading import Event
from urllib.parse import quote_plus
from zoneinfo import ZoneInfo

from playwright.sync_api import (
    Locator,
    Page,
    Route,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)

TRENDYOL_HOME = "https://www.trendyol.com/"
PRODUCT_HREF = re.compile(r"-p-\d+")
PRODUCT_ID_IN_URL = re.compile(r"-p-(\d+)")

BLOCKED_RESOURCE_TYPES = {"image", "media", "font"}
BLOCKED_URL_PARTS = (
    "google-analytics",
    "googletagmanager",
    "doubleclick",
    "facebook.net",
    "hotjar",
    "newrelic",
    "clarity.ms",
    "criteo",
    "adjust.com",
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

LISTING_IDS_JS = """() => {
  const collect = (nodes) => {
    const ids = [];
    const seen = new Set();
    for (const a of nodes) {
      const m = (a.getAttribute('href') || '').match(/-p-(\\d+)/);
      if (!m || seen.has(m[1])) continue;
      seen.add(m[1]);
      ids.push(m[1]);
    }
    return ids;
  };
  const cards = document.querySelectorAll(
    'div.p-card-wrppr a[href*="-p-"], a.p-card-wrppr[href*="-p-"], .prdct-cntnr-wrppr a[href*="-p-"]'
  );
  const fromCards = collect(cards);
  return fromCards.length ? fromCards : collect(document.querySelectorAll('a[href*="-p-"]'));
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


def block_heavy_resources(route: Route) -> None:
    request = route.request
    if request.resource_type in BLOCKED_RESOURCE_TYPES:
        route.abort()
        return
    url = request.url.lower()
    if any(part in url for part in BLOCKED_URL_PARTS):
        route.abort()
        return
    route.continue_()


def dismiss_overlays(page: Page) -> None:
    page.evaluate(
        """() => {
            const labels = [
                'Tüm Tanımlama Bilgilerini Kabul Et',
                'Tümünü Kabul Et',
                'Kabul Et',
                'Accept All',
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
    ids = page.evaluate(LISTING_IDS_JS)
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


def find_product_in_results(
    page: Page,
    keyword: str,
    product_id: str,
) -> tuple[Locator, int, int, int]:
    href_pattern = product_id_href_pattern(product_id)

    for page_index in range(1, MAX_SEARCH_PAGES + 1):
        if page_index > 1:
            page.goto(
                search_url(keyword, page_index),
                wait_until="domcontentloaded",
                timeout=20000,
            )
            dismiss_overlays(page)

        try:
            page.locator("a[href*='-p-']").first.wait_for(state="attached", timeout=8000)
        except PlaywrightTimeoutError:
            if page_index == 1:
                continue
            break

        links = page.locator(f"a[href*='-p-{product_id}']")
        for _ in range(12):
            count = links.count()
            for i in range(count):
                href = links.nth(i).get_attribute("href") or ""
                if not href_pattern.search(href):
                    continue
                ids = unique_listing_ids(page)
                if product_id in ids:
                    on_page = ids.index(product_id) + 1
                else:
                    on_page = 1
                overall = (page_index - 1) * PAGE_SIZE + on_page
                return links.nth(i), page_index, on_page, overall
            page.mouse.wheel(0, 1800)
            page.wait_for_timeout(200)

        if page.locator("a[href*='-p-']").count() == 0:
            break

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
            page.goto(href, wait_until="domcontentloaded", timeout=20000)
        except Exception as exc:
            if "ERR_ABORTED" in str(exc) and url_has_product_id(page.url, product_id):
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


def launch_browser(playwright, headed: bool):
    args = [
        "--disable-blink-features=AutomationControlled",
        "--disable-notifications",
        "--disable-dev-shm-usage",
    ]
    if os.environ.get("PLAYWRIGHT_DOCKER") == "1":
        args.extend(["--no-sandbox", "--disable-gpu"])
    return playwright.chromium.launch(
        headless=not headed,
        args=args,
    )


def new_page(context) -> Page:
    page = context.new_page()
    page.route("**/*", block_heavy_resources)
    page.set_default_timeout(12000)
    return page


def run_once(
    page: Page,
    keyword: str,
    product_id: str,
    product: str,
    index: int,
    from_home: bool,
) -> tuple[Page, int, int, int, str]:
    page.goto(TRENDYOL_HOME, wait_until="domcontentloaded", timeout=20000)
    dismiss_overlays(page)

    if from_home:
        search_from_homepage(page, keyword)
    else:
        page.goto(search_url(keyword), wait_until="domcontentloaded", timeout=20000)
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
) -> None:
    keyword = keyword.strip()
    product_id = normalize_product_id(product_id)
    if not keyword:
        raise ValueError("Anahtar kelime boş olamaz.")

    stats["status"] = "running"
    stats["last_error"] = ""
    stats["history"] = normalize_hourly_history(stats.get("history") or [])

    with sync_playwright() as playwright:
        browser = launch_browser(playwright, headed=headed)
        context = browser.new_context(
            locale="tr-TR",
            timezone_id="Europe/Istanbul",
            viewport={"width": 1280, "height": 800},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
        )
        page = new_page(context)
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
                    stats["last_error"] = str(exc).split("\n", 1)[0][:240]
                    try:
                        page.close()
                    except Exception:
                        pass
                    page = new_page(context)
                stats["last_duration"] = round(time.perf_counter() - started, 2)
        finally:
            try:
                context.close()
            except Exception:
                pass
            try:
                browser.close()
            except Exception:
                pass
            if stats.get("status") == "done":
                pass
            elif not stop_event.is_set():
                stats["status"] = "error"
            else:
                stats["status"] = "stopped"


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

    with sync_playwright() as playwright:
        browser = launch_browser(playwright, headed=headed)
        context = browser.new_context(
            locale="tr-TR",
            timezone_id="Europe/Istanbul",
            viewport={"width": 1280, "height": 800},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
        )
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
            context.close()
            browser.close()


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
