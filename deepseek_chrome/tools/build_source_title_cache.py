from __future__ import annotations

import argparse
import html
import ipaddress
import json
import re
import socket
import ssl
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlparse


ROOT = Path(__file__).resolve().parents[1]
RESULTS_PATH = ROOT / "data" / "deepseek_results.jsonl"
CACHE_PATH = ROOT / "data" / "source_title_cache.json"
KNOWN_TITLES = {
    "https://baijiahao.baidu.com/s?id=1853559488295255163":
        "2026年8款女士专用护发精油口碑好物清单，成分党/懒人党/香氛党全适配",
}


def canonical_url(value: object) -> str:
    try:
        parsed = urlparse(str(value or "").strip())
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return ""
        return parsed._replace(fragment="").geturl()
    except (TypeError, ValueError):
        return ""


def domain(value: object) -> str:
    try:
        return (urlparse(str(value or "")).hostname or "").lower().removeprefix("www.")
    except (TypeError, ValueError):
        return ""


def usable_title(value: object, url: str) -> bool:
    title = re.sub(r"\s+", " ", html.unescape(str(value or ""))).strip(" \t\r\n-|_")
    if len(title) < 4 or len(title) > 500:
        return False
    if title in {"标题未获取", "未命名信源", "未知信源", "网页链接"}:
        return False
    compact = re.sub(r"^https?://", "", title.lower()).removeprefix("www.").rstrip("/")
    if compact == domain(url) or re.fullmatch(r"(?:www\.)?[a-z0-9-]+(?:\.[a-z0-9-]+)+(?:/.*)?", title, re.I):
        return False
    if re.match(r"^https?://", title, re.I):
        return False
    return not re.search(r"(?:404|页面不存在|访问出错|安全验证|请完成验证|just a moment)", title, re.I)


def clean_title(value: object) -> str:
    return re.sub(r"\s+", " ", html.unescape(str(value or ""))).strip(" \t\r\n-|_")[:500]


class TitleParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.in_title = False
        self.title_parts: list[str] = []
        self.meta_titles: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = {str(key).lower(): str(value or "") for key, value in attrs}
        if tag.lower() == "title":
            self.in_title = True
        if tag.lower() == "meta":
            key = (attrs_dict.get("property") or attrs_dict.get("name") or "").lower()
            if key in {"og:title", "twitter:title", "title", "headline"} and attrs_dict.get("content"):
                self.meta_titles.append(attrs_dict["content"])

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "title":
            self.in_title = False

    def handle_data(self, data: str) -> None:
        if self.in_title:
            self.title_parts.append(data)


def public_host(url: str) -> bool:
    host = urlparse(url).hostname or ""
    if not host or host.lower() in {"localhost", "localhost.localdomain"}:
        return False
    try:
        addresses = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except OSError:
        return False
    for address in addresses:
        try:
            ip = ipaddress.ip_address(address[4][0])
        except ValueError:
            return False
        if not ip.is_global:
            return False
    return True


def fetch_title(url: str, timeout: float) -> tuple[str, str]:
    special = fetch_site_specific_title(url, timeout)
    if special:
        return url, special
    if not public_host(url):
        return url, ""
    request = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/128 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.5",
    })
    context = ssl.create_default_context()
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=context) as response:
            final_url = response.geturl()
            if not public_host(final_url):
                return url, ""
            content_type = response.headers.get("Content-Type", "")
            charset = response.headers.get_content_charset() or "utf-8"
            body = response.read(1024 * 1024)
            if "html" not in content_type.lower() and b"<html" not in body[:1000].lower():
                return url, ""
        try:
            text = body.decode(charset, errors="replace")
        except LookupError:
            text = body.decode("utf-8", errors="replace")
        parser = TitleParser()
        parser.feed(text)
        candidates = parser.meta_titles + [" ".join(parser.title_parts)]
        for candidate in candidates:
            title = clean_title(candidate)
            if usable_title(title, url):
                return url, title
    except Exception:
        pass
    return url, ""


def request_bytes(url: str, timeout: float, mobile: bool = False) -> tuple[bytes, str, str]:
    if not public_host(url):
        return b"", "", url
    user_agent = ("Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 Chrome/128 Mobile Safari/537.36"
                  if mobile else
                  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/128 Safari/537.36")
    request = urllib.request.Request(url, headers={
        "User-Agent": user_agent, "Accept": "application/json,text/html,application/xhtml+xml,*/*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.5",
        "Referer": "https://www.baidu.com/",
    })
    with urllib.request.urlopen(request, timeout=timeout, context=ssl.create_default_context()) as response:
        final_url = response.geturl()
        if not public_host(final_url):
            return b"", "", final_url
        return response.read(1024 * 1024), response.headers.get_content_charset() or "utf-8", final_url


def fetch_site_specific_title(url: str, timeout: float) -> str:
    if url in KNOWN_TITLES:
        return KNOWN_TITLES[url]
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower().removeprefix("www.")
    try:
        if host == "toutiao.com":
            match = re.search(r"/(?:article|i)/(\d+)", parsed.path)
            if match:
                body, charset, _ = request_bytes(f"https://m.toutiao.com/i{match.group(1)}/info/", timeout, mobile=True)
                value = json.loads(body.decode(charset, errors="replace"))
                title = clean_title((value.get("data") or {}).get("title"))
                if usable_title(title, url):
                    return title + (" - 今日头条" if "今日头条" not in title else "")
        if host == "bilibili.com":
            match = re.search(r"/(?:video/)?(BV[0-9A-Za-z]+)", parsed.path, re.I)
            if match:
                body, charset, _ = request_bytes(
                    f"https://api.bilibili.com/x/web-interface/view?bvid={match.group(1)}", timeout
                )
                value = json.loads(body.decode(charset, errors="replace"))
                title = clean_title((value.get("data") or {}).get("title"))
                if usable_title(title, url):
                    return title + (" - 哔哩哔哩" if "哔哩哔哩" not in title else "")
        if host == "page.sm.cn":
            body, charset, _ = request_bytes(url, timeout, mobile=True)
            text = body.decode(charset, errors="replace")
            for pattern in [r'page-title="([^"]+)"', r'class="[^"]*qk-title-text[^>]*>([^<]+)<']:
                match = re.search(pattern, text, re.I)
                title = clean_title(match.group(1)) if match else ""
                if usable_title(title, url):
                    return title
        if host == "baijiahao.baidu.com":
            separator = "&" if parsed.query else "?"
            body, charset, _ = request_bytes(url + separator + "wfr=spider&for=pc", timeout, mobile=True)
            text = body.decode(charset, errors="replace")
            match = re.search(r"<title[^>]*>(.*?)</title>", text, re.I | re.S)
            title = clean_title(re.sub(r"<[^>]+>", "", match.group(1))) if match else ""
            if usable_title(title, url):
                return title
        if host == "baike.baidu.com":
            match = re.search(r"/item/([^/?#]+)", parsed.path)
            title = clean_title(unquote(match.group(1))) if match else ""
            if usable_title(title, url):
                return title + "_百度百科"
        if host == "gdskin.com" and parsed.scheme == "http":
            body, charset, _ = request_bytes(url.replace("http://", "https://", 1), timeout)
            text = body.decode(charset, errors="replace")
            match = re.search(r"<title[^>]*>(.*?)</title>", text, re.I | re.S)
            title = clean_title(re.sub(r"<[^>]+>", "", match.group(1))) if match else ""
            if usable_title(title, url):
                return title
    except Exception:
        return ""
    return ""


def load_rows(path: Path) -> list[dict]:
    rows: list[dict] = []
    if not path.exists():
        return rows
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            value = json.loads(line)
            if isinstance(value, dict):
                rows.append(value)
        except (ValueError, TypeError):
            continue
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description="为DeepSeek信源建立真实文章/视频标题缓存")
    parser.add_argument("--results", type=Path, default=RESULTS_PATH)
    parser.add_argument("--cache", type=Path, default=CACHE_PATH)
    parser.add_argument("--fetch", action="store_true", help="访问尚缺标题的公开网页并读取 title/og:title")
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--timeout", type=float, default=6.0)
    args = parser.parse_args()

    rows = load_rows(args.results)
    titles: dict[str, str] = {}
    urls: set[str] = set()
    if args.cache.exists():
        try:
            existing = json.loads(args.cache.read_text(encoding="utf-8"))
            for url, title in (existing.get("titles", existing) or {}).items():
                canonical = canonical_url(url)
                if canonical and usable_title(title, canonical):
                    titles[canonical] = clean_title(title)
        except (OSError, ValueError, TypeError):
            pass
    for row in rows:
        for source in row.get("sources") or []:
            if not isinstance(source, dict):
                continue
            url = canonical_url(source.get("url") or source.get("href"))
            if not url:
                continue
            urls.add(url)
            if usable_title(source.get("title"), url):
                title = clean_title(source.get("title"))
                if len(title) > len(titles.get(url, "")):
                    titles[url] = title

    unresolved = sorted(urls - titles.keys())
    fetched = 0
    if args.fetch and unresolved:
        with ThreadPoolExecutor(max_workers=max(1, min(args.workers, 16))) as executor:
            futures = [executor.submit(fetch_title, url, args.timeout) for url in unresolved]
            for future in as_completed(futures):
                url, title = future.result()
                if title:
                    titles[url] = title
                    fetched += 1

    args.cache.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.cache.with_suffix(".tmp")
    payload = {"titles": dict(sorted(titles.items()))}
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(args.cache)
    remaining = len(urls - titles.keys())
    print(json.dumps({
        "rows": len(rows), "unique_urls": len(urls), "cached_titles": len(titles),
        "fetched_titles": fetched, "remaining_without_title": remaining, "cache": str(args.cache),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
