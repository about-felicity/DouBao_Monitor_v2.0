from __future__ import annotations

import argparse
import json
import os
import threading
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from remote_sync import RemoteSync


ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("DEEPSEEK_MONITOR_DATA_DIR", ROOT / "data")).resolve()
RESULTS_PATH = DATA_DIR / "deepseek_results.jsonl"
EVENTS_PATH = DATA_DIR / "deepseek_monitor_events.jsonl"
HUMAN_LOG_PATH = DATA_DIR / "deepseek_monitor.log"
RESULTS_DIR = DATA_DIR / "results"
TITLE_CACHE_PATH = DATA_DIR / "source_title_cache.json"
LOCK = threading.Lock()
SEEN_IDS: set[str] = set()
TITLE_CACHE: dict[str, str] = {}
TITLE_CACHE_MTIME = 0.0
MAX_BODY = 20 * 1024 * 1024
REMOTE_SYNC: RemoteSync | None = None


def now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def load_seen_ids() -> None:
    SEEN_IDS.clear()
    if not RESULTS_PATH.exists():
        return
    with RESULTS_PATH.open("r", encoding="utf-8") as handle:
        for line in handle:
            try:
                result_id = str(json.loads(line).get("result_id") or "")
            except (ValueError, TypeError):
                continue
            if result_id:
                SEEN_IDS.add(result_id)


def canonical_source_url(value: object) -> str:
    try:
        parsed = urlparse(str(value or "").strip())
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return ""
        return parsed._replace(fragment="").geturl()
    except (TypeError, ValueError):
        return ""


def source_domain(value: object) -> str:
    try:
        return (urlparse(str(value or "")).hostname or "").lower().removeprefix("www.")
    except (TypeError, ValueError):
        return ""


def placeholder_title(value: object, url: object = "") -> bool:
    title = re.sub(r"\s+", " ", str(value or "")).strip()
    if not title or title in {"标题未获取", "未命名信源", "未知信源", "网页链接"}:
        return True
    compact = re.sub(r"^https?://", "", title.lower()).removeprefix("www.").rstrip("/")
    domain = source_domain(url)
    if domain and compact == domain:
        return True
    if re.match(r"^https?://", title, re.I):
        return True
    return bool(re.fullmatch(r"(?:www\.)?[a-z0-9-]+(?:\.[a-z0-9-]+)+(?:/.*)?", title, re.I))


def refresh_title_cache() -> None:
    global TITLE_CACHE_MTIME
    try:
        mtime = TITLE_CACHE_PATH.stat().st_mtime
        if mtime <= TITLE_CACHE_MTIME:
            return
        value = json.loads(TITLE_CACHE_PATH.read_text(encoding="utf-8"))
        entries = value.get("titles", value) if isinstance(value, dict) else {}
        if isinstance(entries, dict):
            for url, title in entries.items():
                canonical = canonical_source_url(url)
                if canonical and not placeholder_title(title, canonical):
                    TITLE_CACHE[canonical] = re.sub(r"\s+", " ", str(title)).strip()[:500]
        TITLE_CACHE_MTIME = mtime
    except (OSError, ValueError, TypeError):
        return


def persist_title_cache() -> None:
    global TITLE_CACHE_MTIME
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    temporary = TITLE_CACHE_PATH.with_suffix(".tmp")
    payload = {"updated_at": now(), "titles": dict(sorted(TITLE_CACHE.items()))}
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(TITLE_CACHE_PATH)
    TITLE_CACHE_MTIME = TITLE_CACHE_PATH.stat().st_mtime


def normalize_source_titles(value: dict, update_cache: bool = False) -> dict:
    refresh_title_cache()
    row = dict(value)
    raw_sources = value.get("sources") or []
    sources = [dict(item) for item in raw_sources if isinstance(item, dict)]
    changed_cache = False
    for source in sources:
        url = canonical_source_url(source.get("url") or source.get("href"))
        if not url:
            continue
        source["url"] = url
        title = re.sub(r"\s+", " ", str(source.get("title") or "")).strip()[:500]
        if not placeholder_title(title, url):
            if update_cache and TITLE_CACHE.get(url) != title:
                TITLE_CACHE[url] = title
                changed_cache = True
        elif TITLE_CACHE.get(url):
            source["title"] = TITLE_CACHE[url]
            source["title_via"] = "local_title_cache"
        else:
            source["title"] = "标题未获取"
        source.setdefault("domain", source_domain(url))
    row["sources"] = sources
    if update_cache and changed_cache:
        persist_title_cache()
    return row


def resolve_missing_titles(value: dict) -> dict:
    row = normalize_source_titles(value)
    missing = {
        canonical_source_url(source.get("url"))
        for source in row.get("sources") or []
        if placeholder_title(source.get("title"), source.get("url"))
    }
    missing.discard("")
    if not missing:
        return row
    try:
        from tools.build_source_title_cache import fetch_title
        with ThreadPoolExecutor(max_workers=min(10, len(missing))) as executor:
            futures = [executor.submit(fetch_title, url, 8.0) for url in missing]
            for future in as_completed(futures):
                url, title = future.result()
                if title and not placeholder_title(title, url):
                    TITLE_CACHE[url] = title
        persist_title_cache()
    except Exception:
        pass
    return normalize_source_titles(row)


def validate_result(value: object) -> dict:
    if not isinstance(value, dict):
        raise ValueError("请求体必须是 JSON 对象")
    failed = str(value.get("status") or "success").casefold() != "success"
    required = ("result_id", "prompt", "finished_at") if failed else ("result_id", "prompt", "reply", "finished_at")
    missing = [key for key in required if not str(value.get(key) or "").strip()]
    if missing:
        raise ValueError("缺少字段：" + ", ".join(missing))
    if failed and not str(value.get("skip_reason") or "").strip():
        raise ValueError("失败结果必须包含 skip_reason")
    sources = value.get("sources", [])
    if not isinstance(sources, list):
        raise ValueError("sources 必须是数组")
    row = normalize_source_titles(dict(value), update_cache=True)
    row = resolve_missing_titles(row)
    row["received_at"] = now()
    row["collector_model"] = "deepseek"
    return row


def safe_name(value: object, fallback: str = "result") -> str:
    text = re.sub(r"[\\/:*?\"<>|\s]+", "_", str(value or "").strip()).strip("._")
    return (text[:60] or fallback)


def artifact_paths(row: dict) -> tuple[Path, Path]:
    run_dir = RESULTS_DIR / safe_name(row.get("run_id"), "unknown_run")
    prefix = f"{int(row.get('round') or 0):04d}_{safe_name(row.get('prompt'), 'question')}_{str(row.get('result_id'))[:8]}"
    return run_dir / f"{prefix}.json", run_dir / f"{prefix}.txt"


def write_result_artifacts(row: dict) -> tuple[Path, Path]:
    json_path, text_path = artifact_paths(row)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(row, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lines = [
        f"状态：{row.get('status', 'success')}",
        f"失败原因：{row.get('skip_reason', '')}",
        f"问题：{row.get('prompt', '')}",
        f"轮次：{row.get('round', '')}（本题第 {row.get('question_round', '')} 轮）",
        f"模型：{row.get('detected_model', '')}",
        f"页面：{row.get('page_url', '')}",
        f"开始：{row.get('started_at', '')}",
        f"完成：{row.get('finished_at', '')}",
        f"信源：{len(row.get('sources') or [])}/{row.get('expected_source_count', 0)}，完整={bool(row.get('source_capture_complete'))}",
        "", "========== 模型回答正文 ==========", "", str(row.get("reply") or ""),
        "", "========== 信源链接 ==========", "",
    ]
    for index, source in enumerate(row.get("sources") or [], 1):
        lines.extend([f"{index}. {source.get('title') or source.get('domain') or '未命名信源'}", f"   {source.get('url') or ''}"])
    text_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return json_path, text_path


def append_event(value: object) -> dict:
    if not isinstance(value, dict):
        raise ValueError("事件必须是 JSON 对象")
    event = dict(value)
    event.setdefault("timestamp", now())
    event.setdefault("level", "info")
    event.setdefault("event", "event")
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    detail_text = json.dumps(event.get("details") or {}, ensure_ascii=False, separators=(",", ":"))
    human = (f"{event['timestamp']} [{str(event['level']).upper()}] event={event['event']} "
             f"run={event.get('run_id', '')} round={event.get('round', '')} "
             f"prompt={json.dumps(event.get('prompt', ''), ensure_ascii=False)} details={detail_text}\n")
    with LOCK:
        with EVENTS_PATH.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")
        with HUMAN_LOG_PATH.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(human)
    return event


def append_result(value: object) -> tuple[dict, bool]:
    row = validate_result(value)
    result_id = str(row["result_id"])
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    created = False
    with LOCK:
        if result_id in SEEN_IDS:
            return row, False
        with RESULTS_PATH.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        SEEN_IDS.add(result_id)
        created = True
    json_path, text_path = write_result_artifacts(row)
    append_event({
        "timestamp": now(), "level": "info", "event": "result_saved",
        "run_id": row.get("run_id", ""), "round": row.get("round", ""), "prompt": row.get("prompt", ""),
        "details": {
            "result_id": result_id, "reply_chars": len(str(row.get("reply") or "")),
            "status": row.get("status", "success"), "skip_reason": row.get("skip_reason", ""),
            "source_count": len(row.get("sources") or []), "expected_source_count": row.get("expected_source_count", 0),
            "source_capture_complete": bool(row.get("source_capture_complete")),
            "json_path": str(json_path), "text_path": str(text_path),
        }
    })
    if REMOTE_SYNC is not None:
        REMOTE_SYNC.enqueue(row)
    return row, created


def read_results(limit: int = 100) -> list[dict]:
    if not RESULTS_PATH.exists():
        return []
    values: list[dict] = []
    with LOCK, RESULTS_PATH.open("r", encoding="utf-8") as handle:
        for line in handle:
            try:
                values.append(json.loads(line))
            except (ValueError, TypeError):
                continue
    return [normalize_source_titles(item) for item in values[-max(1, min(limit, 5000)):]]


def stats() -> dict:
    values = read_results(5000)
    remote = REMOTE_SYNC.status() if REMOTE_SYNC is not None else {"enabled": False, "pending": 0, "sent": 0, "last_error": ""}
    return {
        "ok": True,
        "result_count": len(SEEN_IDS),
        "loaded_count": len(values),
        "source_count": sum(len(item.get("sources") or []) for item in values),
        "incomplete_count": sum(not bool(item.get("source_capture_complete")) for item in values),
        "results_path": str(RESULTS_PATH),
        "events_path": str(EVENTS_PATH),
        "log_path": str(HUMAN_LOG_PATH),
        "result_files_path": str(RESULTS_DIR),
        "source_title_cache_path": str(TITLE_CACHE_PATH),
        "source_title_cache_count": len(TITLE_CACHE),
        "remote_sync": remote,
        "time": now(),
    }


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int]) -> None:
        super().__init__(address, Handler)
        self.remote_sync = RemoteSync(DATA_DIR, append_event)
        self.remote_sync.start()

    def server_close(self) -> None:
        self.remote_sync.stop()
        super().server_close()


class Handler(BaseHTTPRequestHandler):
    server_version = "DeepSeekLocalReceiver/0.1"

    def log_message(self, format_: str, *args: object) -> None:
        print(f"{self.log_date_time_string()} {self.client_address[0]} {format_ % args}", flush=True)

    def cors(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Cache-Control", "no-store")

    def json_response(self, status: int, value: object) -> None:
        body = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.cors()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def file_response(self, path: Path, content_type: str) -> None:
        if not path.exists():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        body = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.cors()
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(HTTPStatus.NO_CONTENT)
        self.cors()
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self.json_response(HTTPStatus.OK, stats())
        elif parsed.path == "/api/health":
            self.json_response(HTTPStatus.OK, stats())
        elif parsed.path == "/api/results":
            query = parse_qs(parsed.query)
            try:
                limit = int((query.get("limit") or [100])[0])
            except ValueError:
                limit = 100
            self.json_response(HTTPStatus.OK, {"ok": True, "results": read_results(limit)})
        elif parsed.path == "/api/events":
            values: list[dict] = []
            if EVENTS_PATH.exists():
                with LOCK, EVENTS_PATH.open("r", encoding="utf-8") as handle:
                    for line in handle:
                        try:
                            values.append(json.loads(line))
                        except (ValueError, TypeError):
                            continue
            self.json_response(HTTPStatus.OK, {"ok": True, "events": values[-500:]})
        elif parsed.path == "/api/export.jsonl":
            self.file_response(RESULTS_PATH, "application/x-ndjson; charset=utf-8")
        else:
            self.json_response(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not_found"})

    def do_POST(self) -> None:  # noqa: N802
        request_path = urlparse(self.path).path
        if request_path not in {"/api/results", "/api/events"}:
            self.json_response(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not_found"})
            return
        try:
            length = int(self.headers.get("Content-Length") or "0")
            if length <= 0 or length > MAX_BODY:
                raise ValueError("请求体为空或超过 20MB")
            value = json.loads(self.rfile.read(length).decode("utf-8"))
            if request_path == "/api/events":
                event = append_event(value)
                self.json_response(HTTPStatus.CREATED, {"ok": True, "event": event.get("event"), "path": str(EVENTS_PATH)})
                return
            row, created = append_result(value)
            self.json_response(HTTPStatus.CREATED if created else HTTPStatus.OK, {
                "ok": True, "created": created, "result_id": row["result_id"], "path": str(RESULTS_PATH)
            })
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            self.json_response(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
        except OSError as exc:
            self.json_response(HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "error": str(exc)})


def make_server(host: str = "127.0.0.1", port: int = 8766) -> Server:
    global REMOTE_SYNC
    if host not in {"127.0.0.1", "localhost"}:
        raise ValueError("本地版只允许监听 127.0.0.1/localhost")
    load_seen_ids()
    refresh_title_cache()
    server = Server((host, port))
    REMOTE_SYNC = server.remote_sync
    return server


def main() -> int:
    parser = argparse.ArgumentParser(description="DeepSeek监控本地 JSONL 接收器")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8766)
    args = parser.parse_args()
    server = make_server(args.host, args.port)
    print(f"DeepSeek监控本地接收器：http://{args.host}:{server.server_port}", flush=True)
    print(f"结果文件：{RESULTS_PATH}", flush=True)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
