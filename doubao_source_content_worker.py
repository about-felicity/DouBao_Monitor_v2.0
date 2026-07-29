import argparse
import csv
import hashlib
import html as html_module
import ipaddress
import json
import os
import re
import socket
import sqlite3
import sys
import tempfile
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

import requests
from lxml import etree, html

import doubao_dashboard_server as dashboard
import doubao_brand_settings as brand_settings


BASE_DIR = Path(__file__).resolve().parent
SCRAPLING_DIR = BASE_DIR / "Scrapling"
REFS_CSV = BASE_DIR / "doubao_refs_result.csv"
PRODUCTS_CSV = BASE_DIR / "doubao_products_result.csv"
INDEX_PATH = BASE_DIR / "doubao_source_content_index.json"
DB_PATH = BASE_DIR / "doubao_source_content.db"
LOCK_PATH = BASE_DIR / "doubao_source_content_worker.lock"
LOG_PATH = BASE_DIR / "doubao_source_content_worker.log"
CST = timezone(timedelta(hours=8))

MAX_BYTES = max(500_000, int(os.environ.get("DOUBAO_CONTENT_MAX_BYTES", "4000000") or 4000000))
MAX_TEXT_CHARS = max(20_000, int(os.environ.get("DOUBAO_CONTENT_MAX_TEXT", "300000") or 300000))
WORKERS = max(1, min(12, int(os.environ.get("DOUBAO_CONTENT_WORKERS", "6") or 6)))
BATCH_SIZE = max(1, int(os.environ.get("DOUBAO_CONTENT_BATCH", "120") or 120))
REQUEST_TIMEOUT = max(5, int(os.environ.get("DOUBAO_CONTENT_TIMEOUT", "18") or 18))
HOST_DELAY = max(0.0, float(os.environ.get("DOUBAO_CONTENT_HOST_DELAY", "0.35") or 0.35))
MIN_CONTENT_CHARS = 80

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/pdf;q=0.9,*/*;q=0.7",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.6",
    "Accept-Encoding": "identity",
    "Cache-Control": "no-cache",
}

HOST_LOCKS = {}
HOST_LOCKS_GUARD = threading.Lock()
_BRAND_CACHE = {"mtime": None, "value": None}
_URL_CACHE = {"mtime": None, "value": None}


def now_str():
    return datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")


def log(message):
    try:
        with LOG_PATH.open("a", encoding="utf-8") as handle:
            handle.write(now_str() + " " + str(message) + "\n")
    except Exception:
        pass


def atomic_json_write(path, payload):
    fd, temp_name = tempfile.mkstemp(prefix=path.stem + "_", suffix=".json", dir=BASE_DIR)
    os.close(fd)
    try:
        with open(temp_name, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def load_index():
    if not INDEX_PATH.exists():
        return {"version": 1, "updated_at": "", "vocab_hash": "", "entries": {}}
    try:
        data = json.loads(INDEX_PATH.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("index root is not an object")
        if not isinstance(data.get("entries"), dict):
            data["entries"] = {}
        return data
    except Exception as exc:
        log("index load failed: " + repr(exc))
        return {"version": 1, "updated_at": "", "vocab_hash": "", "entries": {}}


def init_db():
    connection = sqlite3.connect(DB_PATH, timeout=30)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=NORMAL")
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS source_content (
            url TEXT PRIMARY KEY,
            final_url TEXT,
            status TEXT NOT NULL,
            fetched_at TEXT,
            content_type TEXT,
            http_status INTEGER,
            extraction_method TEXT,
            extraction_quality TEXT,
            title TEXT,
            content_text TEXT,
            text_length INTEGER DEFAULT 0,
            content_hash TEXT,
            error TEXT,
            attempts INTEGER DEFAULT 0
        )
        """
    )
    connection.commit()
    return connection


def save_db_row(connection, url, result):
    connection.execute(
        """
        INSERT INTO source_content (
            url, final_url, status, fetched_at, content_type, http_status,
            extraction_method, extraction_quality, title, content_text,
            text_length, content_hash, error, attempts
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(url) DO UPDATE SET
            final_url=excluded.final_url,
            status=excluded.status,
            fetched_at=excluded.fetched_at,
            content_type=excluded.content_type,
            http_status=excluded.http_status,
            extraction_method=excluded.extraction_method,
            extraction_quality=excluded.extraction_quality,
            title=excluded.title,
            content_text=excluded.content_text,
            text_length=excluded.text_length,
            content_hash=excluded.content_hash,
            error=excluded.error,
            attempts=excluded.attempts
        """,
        (
            url, result.get("final_url", ""), result.get("status", "error"),
            result.get("fetched_at", ""), result.get("content_type", ""),
            result.get("http_status"), result.get("extraction_method", ""),
            result.get("extraction_quality", ""), result.get("title", ""),
            result.get("content_text", ""), result.get("text_length", 0),
            result.get("content_hash", ""), result.get("error", ""),
            result.get("attempts", 0),
        ),
    )
    connection.commit()


def get_db_text(connection, url):
    row = connection.execute(
        "SELECT content_text FROM source_content WHERE url=? AND status='ok'", (url,)
    ).fetchone()
    return str(row[0] or "") if row else ""


def safe_public_url(url):
    try:
        parsed = urlparse(str(url or "").strip())
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            return False, "unsupported URL scheme"
        addresses = socket.getaddrinfo(parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80))
        for address in addresses:
            ip = ipaddress.ip_address(address[4][0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
                return False, "non-public destination"
        return True, ""
    except Exception as exc:
        return False, "DNS/URL validation failed: " + str(exc)[:180]


def normalize_text(value):
    text = html_module.unescape(str(value or ""))
    text = text.replace("\u200b", "").replace("\ufeff", "").replace("\xa0", " ")
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text)
    return text.strip()


def decode_bytes(raw, content_type=""):
    match = re.search(r"charset\s*=\s*['\"]?([\w.-]+)", content_type or "", re.I)
    encodings = [match.group(1)] if match else []
    encodings += ["utf-8", "gb18030", "gbk", "big5", "latin1"]
    for encoding in encodings:
        try:
            return raw.decode(encoding)
        except Exception:
            continue
    return raw.decode("utf-8", errors="ignore")


def json_text_values(value, output):
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).casefold() in {
                "articlebody", "description", "transcript", "caption", "text", "video_description"
            } and isinstance(item, str):
                cleaned = normalize_text(item)
                if len(cleaned) >= 20:
                    output.append(cleaned)
            else:
                json_text_values(item, output)
    elif isinstance(value, list):
        for item in value:
            json_text_values(item, output)


def embedded_descriptions(raw_html):
    values = []
    for key in ("desc", "description", "video_description", "caption"):
        pattern = re.compile(r'"' + re.escape(key) + r'"\s*:\s*("(?:\\.|[^"\\])*")', re.I)
        for match in pattern.finditer(raw_html):
            try:
                value = normalize_text(json.loads(match.group(1)))
            except Exception:
                continue
            if 20 <= len(value) <= 20000:
                values.append(value)
            if len(values) >= 12:
                return values
    return values


def extract_html_content(raw_html):
    title = ""
    metadata = []
    method = "body_fallback"
    quality = "low"
    try:
        root = html.fromstring(raw_html)
    except (etree.ParserError, ValueError):
        plain = normalize_text(re.sub(r"<[^>]+>", " ", raw_html))
        return "", plain[:MAX_TEXT_CHARS], "html_regex_fallback", "low"

    title_nodes = root.xpath("//title/text()")
    if title_nodes:
        title = normalize_text(title_nodes[0])
    for xpath in (
        "//meta[@name='description']/@content",
        "//meta[@property='og:description']/@content",
        "//meta[@name='twitter:description']/@content",
    ):
        for value in root.xpath(xpath):
            cleaned = normalize_text(value)
            if len(cleaned) >= 20:
                metadata.append(cleaned)

    for raw_json in root.xpath("//script[@type='application/ld+json']/text()"):
        try:
            json_text_values(json.loads(raw_json), metadata)
        except Exception:
            continue
    metadata.extend(embedded_descriptions(raw_html))

    for node in root.xpath("//script|//style|//noscript|//svg|//canvas|//form|//nav|//header|//footer|//aside"):
        parent = node.getparent()
        if parent is not None:
            parent.remove(node)

    candidates = []
    selectors = (
        "//article", "//main", "//*[@id='js_content']", "//*[@id='article-content']",
        "//*[contains(@class,'article-content')]", "//*[contains(@class,'article_content')]",
        "//*[contains(@class,'post-content')]", "//*[contains(@class,'post_content')]",
        "//*[contains(@class,'rich_media_content')]", "//*[contains(@class,'content-detail')]",
        "//*[contains(@class,'detail-content')]",
    )
    seen_nodes = set()
    for selector in selectors:
        for node in root.xpath(selector)[:20]:
            marker = id(node)
            if marker in seen_nodes:
                continue
            seen_nodes.add(marker)
            text = normalize_text(node.text_content())
            if len(text) >= MIN_CONTENT_CHARS:
                candidates.append(text)

    if candidates:
        body = max(candidates, key=len)
        method = "article_node"
        quality = "high" if len(body) >= 300 else "medium"
    else:
        bodies = root.xpath("//body")
        body = normalize_text(bodies[0].text_content()) if bodies else normalize_text(root.text_content())
        quality = "medium" if len(body) >= 500 else "low"

    parts = []
    fingerprints = set()
    for value in metadata + [body]:
        cleaned = normalize_text(value)
        fingerprint = hashlib.sha1(cleaned[:2000].encode("utf-8", errors="ignore")).hexdigest() if cleaned else ""
        if len(cleaned) < 20 or fingerprint in fingerprints:
            continue
        fingerprints.add(fingerprint)
        parts.append(cleaned)
    return title, normalize_text("\n".join(parts))[:MAX_TEXT_CHARS], method, quality


def fetch_with_cdp(url, timeout=30):
    """Fetch a URL via an existing Chrome DevTools Protocol instance.

    Sites such as smzdm.com return a probe/captcha shell to plain HTTP
    clients.  When Chrome is running with --remote-debugging-port=9222
    (see open_chrome_debug.bat), this function renders the page in a real
    browser and returns the visible text.
    """
    try:
        import websocket
    except Exception:
        return None

    cdp_host = os.environ.get("DOUBAO_CDP_HOST", "127.0.0.1")
    cdp_port = int(os.environ.get("DOUBAO_CDP_PORT", "9222"))
    list_url = f"http://{cdp_host}:{cdp_port}/json/list"

    ws_url = None
    try:
        req = urllib.request.Request(list_url)
        with urllib.request.urlopen(req, timeout=5) as resp:
            tabs = json.loads(resp.read().decode("utf-8"))
        for tab in tabs:
            if tab.get("type") == "page" and not tab.get("url", "").startswith(f"http://{cdp_host}:{cdp_port}"):
                ws_url = tab.get("webSocketDebuggerUrl")
                if ws_url:
                    break
        if not ws_url:
            new_url = f"http://{cdp_host}:{cdp_port}/json/new?about:blank"
            req = urllib.request.Request(new_url)
            with urllib.request.urlopen(req, timeout=5) as resp:
                tab = json.loads(resp.read().decode("utf-8"))
            ws_url = tab.get("webSocketDebuggerUrl")
    except Exception as exc:
        log("CDP list failed: " + repr(exc))
        return None

    if not ws_url:
        return None

    ws = None
    try:
        ws = websocket.create_connection(ws_url, timeout=10)
        next_id = [0]

        def send(method, params=None):
            next_id[0] += 1
            payload = {"id": next_id[0], "method": method}
            if params:
                payload["params"] = params
            ws.send(json.dumps(payload))
            return next_id[0]

        def recv_until(deadline, predicate):
            while time.time() < deadline:
                remaining = deadline - time.time()
                if remaining <= 0:
                    break
                ws.settimeout(min(1.5, remaining))
                try:
                    msg = json.loads(ws.recv())
                except Exception:
                    continue
                if predicate(msg):
                    return msg
            return None

        send("Page.enable")
        send("Page.navigate", {"url": url})

        nav_deadline = time.time() + timeout
        loaded = recv_until(nav_deadline, lambda m: m.get("method") == "Page.loadEventFired")
        if not loaded:
            log("CDP navigation timed out for " + url)
            return None

        # Allow lazy JS rendering before reading text.
        time.sleep(2.5)

        send("Runtime.evaluate", {"expression": "document.body ? document.body.innerText : ''", "returnByValue": True})
        body_msg = recv_until(
            time.time() + 5,
            lambda m: "result" in m and isinstance(m["result"].get("result", {}).get("value"), str),
        )
        body_text = ""
        if body_msg:
            body_text = body_msg["result"]["result"]["value"]

        if len(body_text) < MIN_CONTENT_CHARS:
            return None

        send("Runtime.evaluate", {"expression": "document.title", "returnByValue": True})
        title_msg = recv_until(
            time.time() + 3,
            lambda m: "result" in m and isinstance(m["result"].get("result", {}).get("value"), str),
        )
        title = ""
        if title_msg:
            title = title_msg["result"]["result"]["value"]

        content_text = normalize_text(body_text)[:MAX_TEXT_CHARS]
        return {
            "status": "ok",
            "fetched_at": now_str(),
            "attempts": 1,
            "http_status": 200,
            "content_type": "text/html",
            "final_url": url,
            "title": normalize_text(title),
            "extraction_method": "cdp_chrome",
            "extraction_quality": "high",
            "content_text": content_text,
            "text_length": len(content_text),
            "content_hash": hashlib.sha256(content_text.encode("utf-8")).hexdigest(),
            "error": "",
            "next_retry_at": "",
        }
    except Exception as exc:
        log("CDP fetch failed: " + repr(exc))
        return None
    finally:
        if ws:
            try:
                ws.close()
            except Exception:
                pass


def brand_vocabulary():
    mtime = (
        PRODUCTS_CSV.stat().st_mtime_ns if PRODUCTS_CSV.exists() else 0,
        brand_settings.SETTINGS_PATH.stat().st_mtime_ns
        if brand_settings.SETTINGS_PATH.exists() else 0,
    )
    if _BRAND_CACHE["mtime"] == mtime and _BRAND_CACHE["value"] is not None:
        return _BRAND_CACHE["value"]
    brands = set(dashboard.KNOWN_BRANDS)
    configured = brand_settings.load_settings()
    brands.update(item["name"] for item in brand_settings.vocabulary(configured))
    if PRODUCTS_CSV.exists():
        with PRODUCTS_CSV.open("r", encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                value = str(row.get("brand_name") or "").strip()
                if value and not dashboard.is_invalid_brand_candidate(value):
                    brands.add(dashboard.canonical_brand_name(value))
    brands.discard("")
    ordered = sorted(brands, key=lambda value: value.casefold())
    own_rule_fingerprint = json.dumps(
        dashboard.OWN_PRODUCT_RULES, ensure_ascii=False, sort_keys=True
    )
    brand_settings_fingerprint = json.dumps(
        configured, ensure_ascii=False, sort_keys=True
    )
    digest = hashlib.sha256(
        (
            "\0".join(ordered) + "\0" + own_rule_fingerprint
            + "\0" + brand_settings_fingerprint
            + "\0own-schema:" + str(dashboard.OWN_PRODUCT_SCHEMA_VERSION)
        ).encode("utf-8")
    ).hexdigest()[:16]
    value = (ordered, digest)
    _BRAND_CACHE.update({"mtime": mtime, "value": value})
    return value


def detect_brands(content_text, brands):
    if not content_text:
        return []
    return sorted(
        (brand for brand in brands if dashboard.title_mentions_brand(content_text, brand)),
        key=lambda value: value.casefold(),
    )


def retry_delay_seconds(attempts, blocked=False):
    schedule = [60, 300, 1800, 7200, 21600, 43200, 86400, 172800]
    delay = schedule[min(max(0, attempts - 1), len(schedule) - 1)]
    return max(delay, 21600) if blocked else delay


def is_skipped_source_url(url):
    # The old requests-only worker skipped smzdm entirely. Scrapling can fetch
    # it with a browser TLS fingerprint, so article sources are no longer
    # silently omitted from owned-brand analysis.
    return False


def fetch_http_with_scrapling(url):
    """Return a lightweight HTTP response tuple using the bundled Scrapling."""
    if SCRAPLING_DIR.exists() and str(SCRAPLING_DIR) not in sys.path:
        sys.path.insert(0, str(SCRAPLING_DIR))
    from scrapling.fetchers import Fetcher

    page = Fetcher.get(
        url,
        impersonate="chrome",
        stealthy_headers=True,
        follow_redirects="safe",
        timeout=REQUEST_TIMEOUT,
        retries=2,
    )
    raw = page.body
    if isinstance(raw, str):
        raw = raw.encode("utf-8", errors="ignore")
    raw = bytes(raw or b"")[:MAX_BYTES]
    headers = page.headers
    content_type = str(
        headers.get("Content-Type")
        or headers.get("content-type")
        or ""
    )
    return {
        "status_code": int(page.status),
        "final_url": str(page.url or url),
        "content_type": content_type,
        "raw": raw,
        "transport": "scrapling",
    }


def fetch_http_with_requests(url):
    response = requests.get(
        url, headers=HEADERS, timeout=(6, REQUEST_TIMEOUT), stream=True,
        allow_redirects=True,
    )
    raw_parts = []
    total = 0
    for chunk in response.iter_content(65536):
        if not chunk:
            continue
        total += len(chunk)
        if total > MAX_BYTES:
            raw_parts.append(chunk[: max(0, MAX_BYTES - (total - len(chunk)))])
            break
        raw_parts.append(chunk)
    return {
        "status_code": int(response.status_code),
        "final_url": str(response.url),
        "content_type": str(response.headers.get("Content-Type") or ""),
        "raw": b"".join(raw_parts),
        "transport": "requests",
    }


def iso_after(seconds):
    return (datetime.now(CST) + timedelta(seconds=seconds)).isoformat(timespec="seconds")


def fetch_one(url, previous_attempts=0):
    attempts = previous_attempts + 1
    fetched_at = now_str()
    safe, reason = safe_public_url(url)
    if not safe:
        return {
            "status": "error", "fetched_at": fetched_at, "attempts": attempts,
            "error": reason, "next_retry_at": iso_after(retry_delay_seconds(attempts, True)),
            "content_text": "", "text_length": 0,
        }

    host = urlparse(url).hostname or ""
    if is_skipped_source_url(url):
        return {
            "status": "skipped",
            "fetched_at": fetched_at,
            "attempts": attempts,
            "final_url": url,
            "error": "按配置跳过什么值得买正文",
            "next_retry_at": "",
            "content_text": "",
            "text_length": 0,
        }
    with HOST_LOCKS_GUARD:
        host_lock = HOST_LOCKS.setdefault(host, threading.Lock())
    try:
        with host_lock:
            try:
                response = fetch_http_with_scrapling(url)
            except Exception as scrapling_exc:
                log("Scrapling fallback for %s: %s" % (
                    url, repr(scrapling_exc)[:240],
                ))
                response = fetch_http_with_requests(url)
            final_url = response["final_url"]
            final_safe, final_reason = safe_public_url(final_url)
            if not final_safe:
                raise ValueError("unsafe redirect: " + final_reason)
            raw = response["raw"]
            if HOST_DELAY:
                time.sleep(HOST_DELAY)
        content_type = response["content_type"]
        status_code = response["status_code"]
        transport = response["transport"]
        if status_code >= 400:
            blocked = status_code in (401, 403, 407, 409, 429) or status_code >= 500
            return {
                "status": "blocked" if blocked else "error", "fetched_at": fetched_at,
                "attempts": attempts, "http_status": status_code, "content_type": content_type,
                "final_url": final_url, "error": "HTTP %s" % status_code,
                "next_retry_at": iso_after(retry_delay_seconds(attempts, blocked)),
                "content_text": "", "text_length": 0,
            }
        if "pdf" in content_type.casefold() or final_url.casefold().endswith(".pdf"):
            return {
                "status": "unsupported", "fetched_at": fetched_at, "attempts": attempts,
                "http_status": status_code, "content_type": content_type, "final_url": final_url,
                "error": "PDF正文解析器暂不可用", "next_retry_at": iso_after(7 * 86400),
                "content_text": "", "text_length": 0,
            }
        decoded = decode_bytes(raw, content_type)
        title, content_text, method, quality = extract_html_content(decoded)
        if len(content_text) < MIN_CONTENT_CHARS:
            return {
                "status": "empty", "fetched_at": fetched_at, "attempts": attempts,
                "http_status": status_code, "content_type": content_type, "final_url": final_url,
                "title": title, "extraction_method": transport + "_" + method,
                "extraction_quality": quality,
                "error": "正文过短或页面仅有脚本壳", "next_retry_at": iso_after(retry_delay_seconds(attempts)),
                "content_text": content_text, "text_length": len(content_text),
            }
        return {
            "status": "ok", "fetched_at": fetched_at, "attempts": attempts,
            "http_status": status_code, "content_type": content_type, "final_url": final_url,
            "title": title, "extraction_method": transport + "_" + method,
            "extraction_quality": quality,
            "content_text": content_text, "text_length": len(content_text),
            "content_hash": hashlib.sha256(content_text.encode("utf-8")).hexdigest(),
            "error": "", "next_retry_at": "",
        }
    except Exception as exc:
        return {
            "status": "error", "fetched_at": fetched_at, "attempts": attempts,
            "error": (type(exc).__name__ + ": " + str(exc))[:500],
            "next_retry_at": iso_after(retry_delay_seconds(attempts)),
            "content_text": "", "text_length": 0,
        }


def collect_urls():
    if not REFS_CSV.exists():
        return []
    mtime = REFS_CSV.stat().st_mtime_ns
    if _URL_CACHE["mtime"] == mtime and _URL_CACHE["value"] is not None:
        return _URL_CACHE["value"]
    latest = {}
    with REFS_CSV.open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            url = str(row.get("href") or "").strip()
            if not url:
                continue
            try:
                run_no = int(str(row.get("run_no") or "0") or 0)
            except (TypeError, ValueError):
                run_no = 0
            current = latest.get(url)
            if current is None or run_no > current[0]:
                latest[url] = (run_no, str(row.get("title") or ""))
    value = sorted(
        ({"url": url, "run_no": value[0], "title": value[1]} for url, value in latest.items()),
        key=lambda item: (-item["run_no"], item["url"]),
    )
    _URL_CACHE.update({"mtime": mtime, "value": value})
    return value


def article_urls(urls):
    selected = []
    for item in urls:
        row = {"href": item["url"], "title": item.get("title", "")}
        source_type, _media, _host, _note = dashboard.source_for(row, {}, {})
        if "文章" in source_type:
            selected.append(item)
    return selected


def due(entry, vocab_hash):
    if not entry:
        return True
    if entry.get("status") == "ok" and entry.get("vocab_hash") != vocab_hash:
        return True
    if entry.get("status") == "ok":
        return False
    if entry.get("status") == "skipped":
        return True
    next_retry = str(entry.get("next_retry_at") or "")
    if not next_retry:
        return True
    try:
        return datetime.fromisoformat(next_retry) <= datetime.now(CST)
    except Exception:
        return True


def public_entry(result, brands, vocab_hash):
    content_text = result.get("content_text", "")
    hits = detect_brands(content_text, brands) if result.get("status") == "ok" else []
    configured = brand_settings.load_settings()
    group_lookup = {
        dashboard.canonical_brand_name(item["name"]): item["group"]
        for item in brand_settings.vocabulary(configured)
    }
    return {
        "status": result.get("status", "error"),
        "fetched_at": result.get("fetched_at", ""),
        "next_retry_at": result.get("next_retry_at", ""),
        "attempts": result.get("attempts", 0),
        "http_status": result.get("http_status"),
        "content_type": result.get("content_type", ""),
        "final_url": result.get("final_url", ""),
        "title": result.get("title", ""),
        "extraction_method": result.get("extraction_method", ""),
        "extraction_quality": result.get("extraction_quality", ""),
        "text_length": result.get("text_length", 0),
        "content_hash": result.get("content_hash", ""),
        "brand_mentions": hits,
        "owned_brand_mentions": [
            brand for brand in hits if group_lookup.get(brand) == "owned"
        ],
        "competitor_brand_mentions": [
            brand for brand in hits if group_lookup.get(brand) == "competitor"
        ],
        "own_product_mentions": dashboard.own_product_mentions(content_text),
        "own_product_schema_version": dashboard.OWN_PRODUCT_SCHEMA_VERSION,
        "excerpt": normalize_text(content_text)[:1200],
        "error": result.get("error", ""),
        "vocab_hash": vocab_hash,
    }


def refresh_vocab_only(connection, url, entry, brands, vocab_hash):
    content_text = get_db_text(connection, url)
    if not content_text:
        return None
    updated = dict(entry)
    hits = detect_brands(content_text, brands)
    configured = brand_settings.load_settings()
    group_lookup = {
        dashboard.canonical_brand_name(item["name"]): item["group"]
        for item in brand_settings.vocabulary(configured)
    }
    updated["brand_mentions"] = hits
    updated["owned_brand_mentions"] = [
        brand for brand in hits if group_lookup.get(brand) == "owned"
    ]
    updated["competitor_brand_mentions"] = [
        brand for brand in hits if group_lookup.get(brand) == "competitor"
    ]
    updated["own_product_mentions"] = dashboard.own_product_mentions(content_text)
    updated["own_product_schema_version"] = dashboard.OWN_PRODUCT_SCHEMA_VERSION
    updated["vocab_hash"] = vocab_hash
    return updated


def pid_is_running(pid):
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes
            handle = ctypes.windll.kernel32.OpenProcess(0x00100000, False, pid)
            if not handle:
                return False
            try:
                code = ctypes.c_ulong()
                if not ctypes.windll.kernel32.GetExitCodeProcess(
                    handle, ctypes.byref(code)
                ):
                    return False
                return code.value == 259  # STILL_ACTIVE
            finally:
                ctypes.windll.kernel32.CloseHandle(handle)
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def acquire_lock():
    if LOCK_PATH.exists():
        try:
            owner_pid = LOCK_PATH.read_text(
                encoding="ascii", errors="ignore"
            ).strip()
            age = time.time() - LOCK_PATH.stat().st_mtime
            if age < 15 * 60 and pid_is_running(owner_pid):
                return False
        except Exception:
            pass
        try:
            LOCK_PATH.unlink()
        except Exception:
            return False
    try:
        LOCK_PATH.write_text(str(os.getpid()), encoding="ascii")
        return True
    except Exception:
        return False


def heartbeat():
    try:
        LOCK_PATH.touch()
    except Exception:
        pass


def release_lock():
    try:
        if LOCK_PATH.exists() and LOCK_PATH.read_text(encoding="ascii", errors="ignore").strip() == str(os.getpid()):
            LOCK_PATH.unlink()
    except Exception:
        pass


def dashboard_running():
    try:
        with socket.create_connection(("127.0.0.1", dashboard.PORT), timeout=1):
            return True
    except OSError:
        return False


def process_batch(index, connection, urls, brands, vocab_hash, limit):
    entries = index.setdefault("entries", {})
    candidates = []
    skipped_now = 0
    for item in urls:
        url = item["url"]
        if not is_skipped_source_url(url):
            candidates.append(item)
            continue
        if (entries.get(url) or {}).get("status") == "skipped":
            continue
        result = {
            "status": "skipped",
            "fetched_at": now_str(),
            "attempts": int((entries.get(url) or {}).get("attempts") or 0),
            "final_url": url,
            "error": "按配置跳过什么值得买正文",
            "next_retry_at": "",
            "content_text": "",
            "text_length": 0,
        }
        save_db_row(connection, url, result)
        entries[url] = public_entry(result, brands, vocab_hash)
        skipped_now += 1
    pending = [
        item for item in candidates
        if due(entries.get(item["url"]), vocab_hash)
    ]
    if not pending:
        if skipped_now:
            index["vocab_hash"] = vocab_hash
            index["updated_at"] = now_str()
            atomic_json_write(INDEX_PATH, index)
        return skipped_now, 0
    selected = []
    for item in pending:
        entry = entries.get(item["url"]) or {}
        if entry.get("status") == "ok" and entry.get("vocab_hash") != vocab_hash:
            updated = refresh_vocab_only(connection, item["url"], entry, brands, vocab_hash)
            if updated:
                entries[item["url"]] = updated
                continue
        selected.append(item)
        if len(selected) >= limit:
            break
    if not selected:
        index["vocab_hash"] = vocab_hash
        index["updated_at"] = now_str()
        atomic_json_write(INDEX_PATH, index)
        return 0, len(pending)

    results = {}
    with ThreadPoolExecutor(max_workers=WORKERS, thread_name_prefix="source-content") as pool:
        futures = {
            pool.submit(fetch_one, item["url"], int((entries.get(item["url"]) or {}).get("attempts") or 0)): item
            for item in selected
        }
        for future in as_completed(futures):
            item = futures[future]
            try:
                results[item["url"]] = future.result()
            except Exception as exc:
                results[item["url"]] = {
                    "status": "error", "fetched_at": now_str(), "attempts": 1,
                    "error": repr(exc)[:500], "next_retry_at": iso_after(300),
                    "content_text": "", "text_length": 0,
                }
            heartbeat()

    ok = 0
    for url, result in results.items():
        save_db_row(connection, url, result)
        entries[url] = public_entry(result, brands, vocab_hash)
        if result.get("status") == "ok":
            ok += 1
        log(
            "%s status=%s chars=%s brands=%s url=%s error=%s"
            % (
                "done" if result.get("status") == "ok" else "retry",
                result.get("status"), result.get("text_length", 0),
                len(entries[url].get("brand_mentions") or []), url,
                str(result.get("error") or "")[:160],
            )
        )
    index["vocab_hash"] = vocab_hash
    index["updated_at"] = now_str()
    atomic_json_write(INDEX_PATH, index)
    return skipped_now + len(selected), max(0, len(pending) - len(selected))


def main():
    parser = argparse.ArgumentParser(description="Incrementally archive public source-page content.")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--limit", type=int, default=BATCH_SIZE)
    parser.add_argument(
        "--articles-only",
        action="store_true",
        help="Prioritize public article pages for owned-brand body analysis.",
    )
    args = parser.parse_args()
    if not acquire_lock():
        log("skip: worker already running")
        return
    connection = None
    try:
        connection = init_db()
        index = load_index()
        log("watch start workers=%s batch=%s" % (WORKERS, args.limit))
        while args.once or dashboard_running():
            heartbeat()
            brands, vocab_hash = brand_vocabulary()
            urls = collect_urls()
            if args.articles_only:
                urls = article_urls(urls)
            processed, remaining = process_batch(
                index, connection, urls, brands, vocab_hash, max(1, args.limit)
            )
            counts = {}
            for entry in index.get("entries", {}).values():
                status = str(entry.get("status") or "missing")
                counts[status] = counts.get(status, 0) + 1
            log("pass processed=%s remaining=%s total=%s states=%s" % (processed, remaining, len(urls), counts))
            if args.once:
                break
            # Publish in larger, less frequent batches so the dashboard can
            # finish one statistics refresh before the content index changes.
            time.sleep(30 if processed else 45)
        log("watch stop")
    finally:
        if connection is not None:
            connection.close()
        release_lock()


if __name__ == "__main__":
    main()
