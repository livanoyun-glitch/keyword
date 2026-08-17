"""Trendyol döngü paneli: ürün/anahtar kelime ekle, canlı başarı takibi."""

from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

from trendyol_search import normalize_hourly_history, normalize_product_id, run_job_loop

HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "8765"))
DATA_DIR = Path(os.environ.get("DATA_DIR", Path(__file__).resolve().parent))
DATA_DIR.mkdir(parents=True, exist_ok=True)
HISTORY_FILE = DATA_DIR / "rank_history.json"
JOBS_FILE = DATA_DIR / "saved_jobs.json"

DEFAULT_JOBS = [
    {"keyword": "parmak eldiven", "product_id": "825575043"},
    {"keyword": "parmak eldiveni", "product_id": "817896706"},
]
DEFAULT_TARGET_RANK = 10
DEFAULT_MAX_CONCURRENT = 8
MAX_CONCURRENT_LIMIT = 50
PRODUCT_IN_TEXT = re.compile(r"-p-(\d+)")

JOBS: dict[str, dict] = {}
PRODUCTS: list[str] = []
KEYWORDS: list[str] = []
TARGET_RANK = DEFAULT_TARGET_RANK
MAX_CONCURRENT = DEFAULT_MAX_CONCURRENT
PRODUCT_IMAGES: dict[str, str] = {}
LOCK = threading.Lock()
HISTORY_LOCK = threading.Lock()
JOBS_FILE_LOCK = threading.Lock()


def unique_keep_order(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out


def parse_product_ids(text: str) -> list[str]:
    found: list[str] = []
    for part in re.split(r"[\s,;]+", (text or "").strip()):
        if not part:
            continue
        match = PRODUCT_IN_TEXT.search(part)
        product_id = match.group(1) if match else normalize_product_id(part)
        if product_id:
            found.append(product_id)
    return unique_keep_order(found)


def parse_keywords(text: str) -> list[str]:
    found: list[str] = []
    for line in re.split(r"[\n,;]+", text or ""):
        keyword = " ".join(line.strip().split())
        if keyword:
            found.append(keyword)
    return unique_keep_order(found)


def history_key(keyword: str, product_id: str) -> str:
    return f"{keyword}|{product_id}"


def load_history_file() -> dict:
    if not HISTORY_FILE.exists():
        return {}
    try:
        return json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_job_history(keyword: str, product_id: str, history: list) -> None:
    with HISTORY_LOCK:
        data = load_history_file()
        data[history_key(keyword, product_id)] = normalize_hourly_history(history)
        HISTORY_FILE.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


def history_for(keyword: str, product_id: str) -> list:
    return normalize_hourly_history(
        load_history_file().get(history_key(keyword, product_id), [])
    )


def best_from_history(history: list) -> int | None:
    ranks = [int(point["overall"]) for point in history if point.get("overall") is not None]
    return min(ranks) if ranks else None


def snapshot_state() -> dict:
    return {
        "products": list(PRODUCTS),
        "keywords": list(KEYWORDS),
        "target_rank": TARGET_RANK,
        "max_concurrent": MAX_CONCURRENT,
        "images": dict(PRODUCT_IMAGES),
        "jobs": [
            {
                "id": job["id"],
                "keyword": job["keyword"],
                "product_id": job["product_id"],
                "enabled": bool(job.get("enabled")),
                "best_overall": job["stats"].get("best_overall"),
                "target_rank": job["stats"].get("target_rank", TARGET_RANK),
            }
            for job in JOBS.values()
        ],
    }


def persist_jobs_locked() -> None:
    payload = snapshot_state()
    with JOBS_FILE_LOCK:
        JOBS_FILE.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


def load_saved_state() -> dict | None:
    if not JOBS_FILE.exists():
        return None
    try:
        data = json.loads(JOBS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return None
    if isinstance(data, list):
        return {
            "products": unique_keep_order(
                [normalize_product_id(str(item.get("product_id") or "")) for item in data]
            ),
            "keywords": unique_keep_order(
                [str(item.get("keyword") or "").strip() for item in data]
            ),
            "target_rank": DEFAULT_TARGET_RANK,
            "max_concurrent": DEFAULT_MAX_CONCURRENT,
            "jobs": data,
        }
    if isinstance(data, dict):
        return data
    return None


def job_workers(job: dict) -> list:
    threads = [thread for thread in (job.get("threads") or []) if thread.is_alive()]
    job["threads"] = threads
    if threads:
        job["thread"] = threads[-1]
    else:
        job["thread"] = None
    return threads


def job_thread_alive(job: dict) -> bool:
    return bool(job_workers(job))


def running_count() -> int:
    return sum(len(job_workers(job)) for job in JOBS.values())


def empty_stats() -> dict:
    return {
        "status": "idle",
        "attempts": 0,
        "success": 0,
        "fail": 0,
        "last_duration": 0,
        "last_url": "",
        "last_error": "",
        "started_at": 0,
        "listing_page": None,
        "rank": None,
        "page_delta": None,
        "rank_delta": None,
        "overall": None,
        "overall_delta": None,
        "best_overall": None,
        "target_rank": TARGET_RANK,
        "history": [],
    }


def public_status(job: dict) -> str:
    stats_status = job["stats"].get("status", "idle")
    if stats_status == "done":
        return "done"
    if job_thread_alive(job):
        return stats_status or "running"
    if job.get("enabled"):
        return "queued"
    if stats_status in {"running", "starting", "stopping"}:
        return "stopped"
    return stats_status or "idle"


def public_job(job: dict) -> dict:
    stats = job["stats"]
    attempts = int(stats.get("attempts", 0))
    success = int(stats.get("success", 0))
    fail = int(stats.get("fail", 0))
    rate = round((success / attempts) * 100, 1) if attempts else 0
    return {
        "id": job["id"],
        "keyword": job["keyword"],
        "product_id": job["product_id"],
        "status": public_status(job),
        "enabled": bool(job.get("enabled")),
        "attempts": attempts,
        "success": success,
        "fail": fail,
        "success_rate": rate,
        "last_duration": stats.get("last_duration", 0),
        "last_url": stats.get("last_url", ""),
        "last_error": stats.get("last_error", ""),
        "started_at": stats.get("started_at", 0),
        "listing_page": stats.get("listing_page"),
        "rank": stats.get("rank"),
        "page_delta": stats.get("page_delta"),
        "rank_delta": stats.get("rank_delta"),
        "overall": stats.get("overall"),
        "overall_delta": stats.get("overall_delta"),
        "best_overall": stats.get("best_overall"),
        "target_rank": int(stats.get("target_rank") or TARGET_RANK),
        "workers": len(job_workers(job)),
        "history": normalize_hourly_history(stats.get("history") or []),
    }


def product_group(product_id: str, product_jobs: list) -> dict:
    return {
        "product_id": product_id,
        "image_url": PRODUCT_IMAGES.get(product_id, ""),
        "jobs": product_jobs,
        "running": sum(int(job.get("workers") or 0) for job in product_jobs),
        "queued": sum(1 for job in product_jobs if job["status"] == "queued"),
        "done": sum(1 for job in product_jobs if job["status"] == "done"),
    }


def public_state() -> dict:
    jobs = [public_job(job) for job in JOBS.values()]
    grouped = []
    for product_id in PRODUCTS:
        grouped.append(
            product_group(
                product_id,
                [job for job in jobs if job["product_id"] == product_id],
            )
        )
    extra = [job for job in jobs if job["product_id"] not in PRODUCTS]
    if extra:
        by_product: dict[str, list] = {}
        for job in extra:
            by_product.setdefault(job["product_id"], []).append(job)
        for product_id, product_jobs in by_product.items():
            grouped.append(product_group(product_id, product_jobs))
    return {
        "products": list(PRODUCTS),
        "keywords": list(KEYWORDS),
        "target_rank": TARGET_RANK,
        "max_concurrent": MAX_CONCURRENT,
        "jobs": jobs,
        "groups": grouped,
        "active": sum(int(job.get("workers") or 0) for job in jobs),
        "queued": sum(1 for job in jobs if job["status"] == "queued"),
        "attempts": sum(job["attempts"] for job in jobs),
        "success": sum(job["success"] for job in jobs),
        "fail": sum(job["fail"] for job in jobs),
    }


def maybe_start_queued_locked() -> None:
    enabled = [
        job
        for job in JOBS.values()
        if job.get("enabled")
        and job["stats"].get("status") != "done"
        and not job_thread_alive(job)
    ]
    while enabled and running_count() < MAX_CONCURRENT:
        job = min(enabled, key=lambda item: len(job_workers(item)))
        spawn_worker(job)
        enabled.remove(job)


def save_product_image(product_id: str, image_url: str) -> None:
    product_id = normalize_product_id(product_id)
    if not product_id or not image_url:
        return
    with LOCK:
        if PRODUCT_IMAGES.get(product_id) == image_url:
            return
        PRODUCT_IMAGES[product_id] = image_url
        persist_jobs_locked()


def spawn_worker(job: dict) -> None:
    job["enabled"] = True
    if not job_thread_alive(job):
        job["stop"].clear()
    job["stats"]["status"] = "starting"
    job["stats"]["started_at"] = time.time()
    job["stats"]["target_rank"] = TARGET_RANK

    def worker() -> None:
        try:
            run_job_loop(
                keyword=job["keyword"],
                product_id=job["product_id"],
                stop_event=job["stop"],
                stats=job["stats"],
                on_history=lambda history: save_job_history(
                    job["keyword"], job["product_id"], history
                ),
                on_image=lambda url: save_product_image(job["product_id"], url),
            )
        except Exception as exc:
            job["stats"]["status"] = "error"
            job["stats"]["last_error"] = str(exc).split("\n", 1)[0][:240]
        finally:
            with LOCK:
                if job["stats"].get("status") == "done":
                    job["enabled"] = False
                    job["stop"].set()
                persist_jobs_locked()
                maybe_start_queued_locked()

    thread = threading.Thread(target=worker, name=f"job-{job['id']}", daemon=True)
    job.setdefault("threads", []).append(thread)
    job["thread"] = thread
    thread.start()


def remember_catalog(keyword: str, product_id: str) -> None:
    if product_id and product_id not in PRODUCTS:
        PRODUCTS.append(product_id)
    if keyword and keyword not in KEYWORDS:
        KEYWORDS.append(keyword)


def add_job(
    keyword: str,
    product_id: str,
    autostart: bool = True,
    job_id: str | None = None,
    persist: bool = True,
) -> dict:
    keyword = keyword.strip()
    product_id = normalize_product_id(product_id)
    if not keyword:
        raise ValueError("Anahtar kelime boş olamaz.")
    if not product_id:
        raise ValueError("Ürün kodu gerekli.")

    with LOCK:
        remember_catalog(keyword, product_id)
        for job in JOBS.values():
            if job["keyword"] == keyword and job["product_id"] == product_id:
                if autostart:
                    job["enabled"] = True
                    if job["stats"].get("status") == "done":
                        job["stats"]["status"] = "queued"
                    maybe_start_queued_locked()
                    if persist:
                        persist_jobs_locked()
                return public_job(job)

        history = history_for(keyword, product_id)
        job = {
            "id": job_id or uuid.uuid4().hex[:8],
            "keyword": keyword,
            "product_id": product_id,
            "enabled": bool(autostart),
            "stats": empty_stats(),
            "stop": threading.Event(),
            "thread": None,
            "threads": [],
        }
        job["stats"]["history"] = history
        job["stats"]["best_overall"] = best_from_history(history)
        job["stats"]["target_rank"] = TARGET_RANK
        JOBS[job["id"]] = job
        if autostart:
            maybe_start_queued_locked()
        if persist:
            persist_jobs_locked()
        return public_job(job)


def add_products(text: str) -> list[str]:
    ids = parse_product_ids(text)
    if not ids:
        raise ValueError("Ürün kodu bulunamadı.")
    with LOCK:
        for product_id in ids:
            if product_id not in PRODUCTS:
                PRODUCTS.append(product_id)
        persist_jobs_locked()
    return ids


def add_keywords(text: str) -> list[str]:
    keywords = parse_keywords(text)
    if not keywords:
        raise ValueError("Anahtar kelime bulunamadı.")
    with LOCK:
        for keyword in keywords:
            if keyword not in KEYWORDS:
                KEYWORDS.append(keyword)
        persist_jobs_locked()
    return keywords


def assign_keywords(product_ids: list[str] | None = None, keywords: list[str] | None = None) -> int:
    created = 0
    with LOCK:
        targets = unique_keep_order(product_ids or list(PRODUCTS))
        words = unique_keep_order(keywords or list(KEYWORDS))
        if not targets:
            raise ValueError("Önce ürün ekleyin.")
        if not words:
            raise ValueError("Önce kelime ekleyin.")
        existing = {(job["keyword"], job["product_id"]) for job in JOBS.values()}
        for product_id in targets:
            remember_catalog("", product_id)
            for keyword in words:
                remember_catalog(keyword, product_id)
                if (keyword, product_id) in existing:
                    continue
                history = history_for(keyword, product_id)
                job = {
                    "id": uuid.uuid4().hex[:8],
                    "keyword": keyword,
                    "product_id": product_id,
                    "enabled": False,
                    "stats": empty_stats(),
                    "stop": threading.Event(),
                    "thread": None,
                    "threads": [],
                }
                job["stats"]["history"] = history
                job["stats"]["best_overall"] = best_from_history(history)
                JOBS[job["id"]] = job
                existing.add((keyword, product_id))
                created += 1
        persist_jobs_locked()
    return created


def stop_job(job_id: str) -> dict:
    with LOCK:
        job = JOBS.get(job_id)
        if not job:
            raise KeyError("Döngü bulunamadı.")
        job["enabled"] = False
        job["stop"].set()
        if job["stats"].get("status") == "done":
            pass
        elif job_thread_alive(job):
            job["stats"]["status"] = "stopping"
        else:
            job["stats"]["status"] = "stopped"
        persist_jobs_locked()
        maybe_start_queued_locked()
        return public_job(job)


def start_job(job_id: str) -> dict:
    with LOCK:
        job = JOBS.get(job_id)
        if not job:
            raise KeyError("Döngü bulunamadı.")
        job["enabled"] = True
        if job["stats"].get("status") == "done":
            job["stats"]["status"] = "queued"
        persist_jobs_locked()
        maybe_start_queued_locked()
        return public_job(job)


def start_product(product_id: str) -> int:
    product_id = normalize_product_id(product_id)
    started = 0
    with LOCK:
        for job in JOBS.values():
            if job["product_id"] != product_id:
                continue
            if job["stats"].get("status") == "done":
                continue
            job["enabled"] = True
            if not job_thread_alive(job):
                started += 1
        persist_jobs_locked()
        maybe_start_queued_locked()
    return started


def stop_product(product_id: str) -> int:
    product_id = normalize_product_id(product_id)
    stopped = 0
    with LOCK:
        for job in JOBS.values():
            if job["product_id"] != product_id:
                continue
            if not job.get("enabled") and not job_thread_alive(job):
                continue
            job["enabled"] = False
            job["stop"].set()
            if job["stats"].get("status") != "done":
                job["stats"]["status"] = "stopping" if job_thread_alive(job) else "stopped"
            stopped += 1
        persist_jobs_locked()
        maybe_start_queued_locked()
    return stopped


def delete_job(job_id: str) -> None:
    with LOCK:
        job = JOBS.pop(job_id, None)
        if not job:
            raise KeyError("Döngü bulunamadı.")
        job["enabled"] = False
        job["stop"].set()
        persist_jobs_locked()
        maybe_start_queued_locked()


def delete_product(product_id: str) -> None:
    product_id = normalize_product_id(product_id)
    with LOCK:
        if product_id in PRODUCTS:
            PRODUCTS.remove(product_id)
        PRODUCT_IMAGES.pop(product_id, None)
        for job in list(JOBS.values()):
            if job["product_id"] != product_id:
                continue
            job["enabled"] = False
            job["stop"].set()
            JOBS.pop(job["id"], None)
        persist_jobs_locked()
        maybe_start_queued_locked()


def delete_keyword(keyword: str) -> None:
    keyword = keyword.strip()
    with LOCK:
        if keyword in KEYWORDS:
            KEYWORDS.remove(keyword)
        for job in list(JOBS.values()):
            if job["keyword"] != keyword:
                continue
            job["enabled"] = False
            job["stop"].set()
            JOBS.pop(job["id"], None)
        persist_jobs_locked()
        maybe_start_queued_locked()


def update_settings(target_rank: int | None = None, max_concurrent: int | None = None) -> None:
    global TARGET_RANK, MAX_CONCURRENT
    with LOCK:
        if target_rank is not None:
            TARGET_RANK = max(1, int(target_rank))
            for job in JOBS.values():
                job["stats"]["target_rank"] = TARGET_RANK
        if max_concurrent is not None:
            MAX_CONCURRENT = max(1, min(MAX_CONCURRENT_LIMIT, int(max_concurrent)))
        persist_jobs_locked()
        maybe_start_queued_locked()


def restore_jobs() -> None:
    global TARGET_RANK, MAX_CONCURRENT
    saved = load_saved_state()
    if saved is None:
        for item in DEFAULT_JOBS:
            add_job(item["keyword"], item["product_id"], autostart=False, persist=False)
        with LOCK:
            persist_jobs_locked()
        return
    TARGET_RANK = max(1, int(saved.get("target_rank") or DEFAULT_TARGET_RANK))
    MAX_CONCURRENT = max(1, min(MAX_CONCURRENT_LIMIT, int(saved.get("max_concurrent") or DEFAULT_MAX_CONCURRENT)))
    with LOCK:
        PRODUCTS[:] = unique_keep_order(
            [
                pid
                for pid in (normalize_product_id(str(item)) for item in saved.get("products") or [])
                if pid
            ]
        )
        KEYWORDS[:] = unique_keep_order(
            [str(item).strip() for item in saved.get("keywords") or [] if str(item).strip()]
        )
        for pid, url in (saved.get("images") or {}).items():
            pid = normalize_product_id(str(pid))
            if pid and url:
                PRODUCT_IMAGES[pid] = str(url)
    for item in saved.get("jobs") or []:
        keyword = str(item.get("keyword") or "").strip()
        product_id = str(item.get("product_id") or "")
        saved_id = str(item.get("id") or "").strip() or None
        try:
            job = add_job(keyword, product_id, autostart=False, job_id=saved_id, persist=False)
        except ValueError:
            continue
        with LOCK:
            real = JOBS.get(job["id"])
            if not real:
                continue
            if item.get("best_overall") is not None:
                real["stats"]["best_overall"] = item.get("best_overall")
            real["enabled"] = False
            if real["stats"].get("status") in {"running", "starting", "stopping", "queued"}:
                real["stats"]["status"] = "stopped"
    with LOCK:
        persist_jobs_locked()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args) -> None:
        return

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, payload) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send(code, raw, "application/json; charset=utf-8")

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length <= 0:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path in {"/", "/index.html"}:
            html = Path(__file__).with_name("panel.html").read_text(encoding="utf-8")
            self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")
            return
        if path in {"/api/jobs", "/api/state"}:
            with LOCK:
                payload = public_state()
            self._json(200, payload)
            return
        self._json(404, {"error": "Bulunamadı"})

    def do_POST(self) -> None:
        path = urlparse(self.path).path.rstrip("/")
        try:
            data = self._read_json() if path.startswith("/api/") else {}
            if path == "/api/products":
                ids = add_products(data.get("text") or data.get("product_id") or "")
                self._json(201, {"ok": True, "product_ids": ids})
                return
            if path == "/api/keywords":
                keywords = add_keywords(data.get("text") or data.get("keyword") or "")
                self._json(201, {"ok": True, "keywords": keywords})
                return
            if path == "/api/assign":
                created = assign_keywords(data.get("product_ids"), data.get("keywords"))
                self._json(200, {"ok": True, "created": created})
                return
            if path == "/api/settings":
                update_settings(data.get("target_rank"), data.get("max_concurrent"))
                self._json(200, {"ok": True})
                return
            if path == "/api/jobs":
                job = add_job(data.get("keyword", ""), data.get("product_id", ""), autostart=True)
                self._json(201, job)
                return
            if path.endswith("/stop") and "/api/products/" in path:
                product_id = unquote(path.split("/")[-2])
                self._json(200, {"ok": True, "stopped": stop_product(product_id)})
                return
            if path.endswith("/start") and "/api/products/" in path:
                product_id = unquote(path.split("/")[-2])
                self._json(200, {"ok": True, "started": start_product(product_id)})
                return
            if path.endswith("/stop"):
                job_id = path.split("/")[-2]
                self._json(200, stop_job(job_id))
                return
            if path.endswith("/start"):
                job_id = path.split("/")[-2]
                self._json(200, start_job(job_id))
                return
        except ValueError as exc:
            self._json(400, {"error": str(exc)})
            return
        except KeyError as exc:
            self._json(404, {"error": str(exc)})
            return
        self._json(404, {"error": "Bulunamadı"})

    def do_DELETE(self) -> None:
        path = urlparse(self.path).path.rstrip("/")
        try:
            if path.startswith("/api/products/"):
                delete_product(unquote(path.split("/")[-1]))
                self._json(200, {"ok": True})
                return
            if path == "/api/keywords":
                data = self._read_json()
                keyword = str(data.get("keyword") or "").strip()
                if not keyword:
                    raise ValueError("Anahtar kelime gerekli.")
                delete_keyword(keyword)
                self._json(200, {"ok": True})
                return
            job_id = path.rsplit("/", 1)[-1]
            delete_job(job_id)
            self._json(200, {"ok": True})
        except ValueError as exc:
            self._json(400, {"error": str(exc)})
        except KeyError as exc:
            self._json(404, {"error": str(exc)})


def main() -> None:
    restore_jobs()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    url = f"http://{HOST}:{PORT}"
    print(f"Panel: {url}", flush=True)
    if os.environ.get("OPEN_BROWSER", "1") != "0":
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        with LOCK:
            for job in JOBS.values():
                job["stop"].set()
        server.server_close()


if __name__ == "__main__":
    main()
