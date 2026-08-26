from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import importlib.util
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse


BASE_DIR = Path(__file__).resolve().parent
MONITOR_DIR = BASE_DIR.parent
CONFIG_PATH = BASE_DIR / "doubao_lan_receiver_config.json"
PAIRING_PATH = BASE_DIR / "doubao_lan_pairing.json"
QUEUE_DIR = BASE_DIR / "lan_receiver_queue"
INBOX_DIR = QUEUE_DIR / "inbox"
DONE_DIR = QUEUE_DIR / "done"
ERROR_DIR = QUEUE_DIR / "errors"
LOG_PATH = BASE_DIR / "doubao_lan_receiver.log"
DASHBOARD_SCRIPT = MONITOR_DIR / "doubao_dashboard_server.py"
MAX_BODY_BYTES = 25 * 1024 * 1024
logger = logging.getLogger("doubao_lan_receiver")
BEIJING_TZ = timezone(timedelta(hours=8))


def beijing_now() -> str:
    """Receiver-side UTC+8 timestamp; never reads MuMu/Android device time."""
    return datetime.now(BEIJING_TZ).isoformat(timespec="seconds")


def configure_logging() -> None:
    logger.handlers.clear()
    logger.setLevel(logging.INFO)
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S+08:00",
    )
    formatter.converter = lambda timestamp: time.gmtime(timestamp + 8 * 3600)
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    logger.addHandler(console)
    file_handler = RotatingFileHandler(
        LOG_PATH,
        maxBytes=8 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=path.name + ".",
        suffix=".tmp",
        dir=path.parent,
    )
    os.close(fd)
    temporary = Path(temporary_name)
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def preferred_lan_ip() -> str:
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("8.8.8.8", 80))
        value = str(probe.getsockname()[0])
        if value and not value.startswith("127."):
            return value
    except OSError:
        pass
    finally:
        probe.close()
    for value in socket.gethostbyname_ex(socket.gethostname())[2]:
        if value.startswith(("192.168.", "10.")):
            return value
        if value.startswith("172."):
            try:
                second = int(value.split(".")[1])
                if 16 <= second <= 31:
                    return value
            except (IndexError, ValueError):
                pass
    return "127.0.0.1"


def load_or_create_config() -> dict[str, Any]:
    if CONFIG_PATH.exists():
        config = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
    else:
        config = {
            "version": 1,
            "host": "0.0.0.0",
            "port": 8790,
            "token": secrets.token_urlsafe(32),
            "start_dashboard": True,
            "dashboard_port": 8765,
        }
        atomic_json(CONFIG_PATH, config)
    token = str(config.get("token") or "").strip()
    if len(token) < 24:
        config["token"] = secrets.token_urlsafe(32)
        atomic_json(CONFIG_PATH, config)
    config["port"] = int(config.get("port") or 8790)
    return config


def write_pairing(config: dict[str, Any]) -> dict[str, Any]:
    ip = str(config.get("advertised_ip") or preferred_lan_ip()).strip()
    port = int(config["port"])
    hostname = socket.gethostname()
    pairing = {
        "version": 1,
        "enabled": True,
        "receiver_url": f"http://{ip}:{port}",
        "receiver_urls": [
            f"http://{ip}:{port}",
            f"http://{ip}:8765",
            f"http://{hostname}:{port}",
            f"http://{hostname}:8765",
        ],
        "receiver_host": hostname,
        "token": str(config["token"]),
        "device_name": "",
        "upload_timeout": 20,
    }
    atomic_json(PAIRING_PATH, pairing)
    return pairing


def import_grabber() -> Any:
    path = MONITOR_DIR / "run_doubao_latest_grab.py"
    monitor_path = str(MONITOR_DIR)
    if monitor_path not in sys.path:
        sys.path.insert(0, monitor_path)
    spec = importlib.util.spec_from_file_location("doubao_lan_grabber", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载保存程序：{path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


def ensure_dashboard(config: dict[str, Any]) -> None:
    if not config.get("start_dashboard"):
        return
    port = int(config.get("dashboard_port") or 8765)
    if port_open(port):
        logger.info("实时检测面板已运行：http://127.0.0.1:%s", port)
        return
    if not DASHBOARD_SCRIPT.exists():
        logger.warning("找不到实时检测面板程序：%s", DASHBOARD_SCRIPT)
        return
    log_handle = (BASE_DIR / "doubao_dashboard_service.log").open("ab")
    subprocess.Popen(
        [sys.executable, str(DASHBOARD_SCRIPT)],
        cwd=str(MONITOR_DIR),
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        creationflags=(
            subprocess.CREATE_NO_WINDOW
            if os.name == "nt" and hasattr(subprocess, "CREATE_NO_WINDOW")
            else 0
        ),
    )
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if port_open(port):
            logger.info("已启动实时检测面板：http://127.0.0.1:%s", port)
            return
        time.sleep(0.5)
    logger.warning("实时检测面板启动后 15 秒仍未监听端口 %s。", port)


def safe_request_id(value: str) -> str:
    value = str(value or "").strip().lower()
    if len(value) == 64 and all(char in "0123456789abcdef" for char in value):
        return value
    raise ValueError("request_id 必须是 64 位 SHA-256 十六进制字符串。")


def validate_envelope(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("请求体必须是 JSON 对象。")
    request_id = safe_request_id(value.get("request_id"))
    payload = value.get("payload")
    if not isinstance(payload, dict):
        raise ValueError("payload 必须是对象。")
    url = str(payload.get("url") or "").strip()
    if not url.startswith("https://www.doubao.com/chat/"):
        raise ValueError("payload.url 不是豆包会话地址。")
    question = str(payload.get("question") or "").strip()
    answer = str(payload.get("answerText") or payload.get("answer_text") or "").strip()
    if not question:
        raise ValueError("payload.question 为空。")
    if not answer:
        raise ValueError("payload.answerText 为空。")
    normalized = dict(value)
    normalized["request_id"] = request_id
    normalized["received_at"] = beijing_now()
    normalized["payload"] = payload
    return normalized


def queue_envelope(envelope: dict[str, Any]) -> tuple[str, bool]:
    request_id = envelope["request_id"]
    done_path = DONE_DIR / f"{request_id}.json"
    inbox_path = INBOX_DIR / f"{request_id}.json"
    if done_path.exists():
        return "processed", True
    if inbox_path.exists():
        return "queued", True
    atomic_json(inbox_path, envelope)
    return "queued", False


def queue_counts() -> dict[str, int]:
    return {
        "queued": len(list(INBOX_DIR.glob("*.json"))) if INBOX_DIR.exists() else 0,
        "processed": len(list(DONE_DIR.glob("*.json"))) if DONE_DIR.exists() else 0,
        "errors": len(list(ERROR_DIR.glob("*.json"))) if ERROR_DIR.exists() else 0,
    }


def parse_save_analysis(save_result: dict[str, Any]) -> dict[str, Any]:
    raw: Any = save_result.get("output")
    if isinstance(raw, str):
        for candidate in reversed(raw.splitlines()):
            try:
                decoded = json.loads(candidate.strip())
            except (ValueError, json.JSONDecodeError):
                continue
            if isinstance(decoded, dict):
                raw = decoded
                break
    if not isinstance(raw, dict):
        return {}
    analysis = raw.get("analysis")
    return analysis if isinstance(analysis, dict) else {}


class CaptureWorker(threading.Thread):
    def __init__(self) -> None:
        super().__init__(name="doubao-lan-capture-worker", daemon=True)
        self.stop_event = threading.Event()
        monitor_path = str(MONITOR_DIR)
        if monitor_path not in sys.path:
            sys.path.insert(0, monitor_path)
        import save_doubao_refs
        from monitor_core.database import store_ingested_run
        from monitor_core.ingestion import normalize_doubao_payload
        self.saver = save_doubao_refs
        self.store_ingested_run = store_ingested_run
        self.normalize_doubao_payload = normalize_doubao_payload

    def run(self) -> None:
        while not self.stop_event.is_set():
            processed_any = False
            for path in sorted(INBOX_DIR.glob("*.json")):
                if self.stop_event.is_set():
                    break
                processed_any = True
                self.process(path)
            self.stop_event.wait(0.5 if processed_any else 2.0)

    def process(self, path: Path) -> None:
        try:
            envelope = json.loads(path.read_text(encoding="utf-8-sig"))
            request_id = safe_request_id(envelope.get("request_id"))
            payload = dict(envelope["payload"])
            payload.setdefault(
                "source_device",
                str(envelope.get("source_device") or ""),
            )
            payload.setdefault(
                "source_uploaded_at",
                str(envelope.get("sent_at") or ""),
            )
            payload["receiver_received_at"] = str(
                envelope.get("received_at") or ""
            )
            answer = str(payload.get("answerText") or payload.get("answer_text") or "")
            question = str(payload.get("question") or "")
            products, review_status, method, analysis_model = self.saver.review_products_with_ai(
                answer, question
            )
            run = self.normalize_doubao_payload(
                payload, products, review_status, analysis_model, method
            )
            stored = self.store_ingested_run("doubao", request_id, payload, envelope, run)
            analysis = {
                "run_no": stored["sequence"],
                "source_count": stored["sources"],
                "expected_source_count": int(payload.get("expectedCount") or payload.get("count") or 0),
                "source_capture_complete": bool(payload.get("complete")),
                "answer_present": bool(answer.strip()),
                "answer_length": len(answer.strip()),
                "product_count": stored["products"],
                "product_review_status": review_status,
                "product_extraction_method": method,
                "product_analysis_model": analysis_model,
                "product_parse_complete": review_status == "ai_verified",
            }
            save_result = {
                "deferred": review_status != "ai_verified",
                "storage": "postgresql",
                "run_id": stored["run_id"],
                "run_no": stored["sequence"],
                "rows_written": stored["sources"],
            }
            receipt = {
                "request_id": request_id,
                "source_device": envelope.get("source_device") or "",
                "received_at": envelope.get("received_at") or "",
                "processed_at": beijing_now(),
                "question": payload.get("question") or "",
                "account_uid_masked": payload.get("account_uid_masked") or "",
                "url": payload.get("url") or "",
                "save": save_result,
                "analysis": analysis,
            }
            atomic_json(DONE_DIR / f"{request_id}.json", receipt)
            path.unlink(missing_ok=True)
            (ERROR_DIR / f"{request_id}.json").unlink(missing_ok=True)
            logger.info(
                "远端数据已处理：device=%s question=%s deferred=%s",
                envelope.get("source_device") or "unknown",
                payload.get("question") or "",
                bool(save_result.get("deferred")),
            )
        except Exception as exc:
            request_id = path.stem
            error_path = ERROR_DIR / f"{request_id}.json"
            attempts = 0
            if error_path.exists():
                try:
                    attempts = int(
                        json.loads(error_path.read_text(encoding="utf-8")).get(
                            "attempts",
                            0,
                        )
                    )
                except Exception:
                    attempts = 0
            atomic_json(
                error_path,
                {
                    "request_id": request_id,
                    "attempts": attempts + 1,
                    "last_error": f"{type(exc).__name__}: {exc}",
                    "updated_at": beijing_now(),
                },
            )
            logger.exception("处理远端数据失败，将保留队列自动重试：%s", exc)
            time.sleep(min(30, max(2, attempts + 1)))


class ReceiverServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        address: tuple[str, int],
        handler: type[BaseHTTPRequestHandler],
        config: dict[str, Any],
    ) -> None:
        super().__init__(address, handler)
        self.config = config


class ReceiverHandler(BaseHTTPRequestHandler):
    server: ReceiverServer

    def log_message(self, format: str, *args: Any) -> None:
        logger.debug(format, *args)

    def send_json(self, status: int, value: Any) -> None:
        data = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def authorized(self) -> bool:
        expected = str(self.server.config.get("token") or "")
        authorization = str(self.headers.get("Authorization") or "")
        supplied = (
            authorization[7:].strip()
            if authorization.lower().startswith("bearer ")
            else str(self.headers.get("X-Doubao-Token") or "").strip()
        )
        return bool(expected) and hmac.compare_digest(expected, supplied)

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/v1/health":
            self.send_json(
                200,
                {
                    "ok": True,
                    "service": "doubao-lan-receiver",
                    "version": 1,
                    "host": socket.gethostname(),
                    "queue": queue_counts(),
                    "dashboard": "http://%s:%s"
                    % (
                        preferred_lan_ip(),
                        int(self.server.config.get("dashboard_port") or 8765),
                    ),
                },
            )
            return
        if path.startswith("/api/v1/status/"):
            if not self.authorized():
                self.send_json(401, {"ok": False, "error": "unauthorized"})
                return
            try:
                request_id = safe_request_id(path.rsplit("/", 1)[-1])
            except ValueError as exc:
                self.send_json(400, {"ok": False, "error": str(exc)})
                return
            done = DONE_DIR / f"{request_id}.json"
            inbox = INBOX_DIR / f"{request_id}.json"
            error = ERROR_DIR / f"{request_id}.json"
            if done.exists():
                self.send_json(
                    200,
                    {
                        "ok": True,
                        "status": "processed",
                        "receipt": json.loads(done.read_text(encoding="utf-8")),
                    },
                )
            elif inbox.exists():
                self.send_json(
                    200,
                    {
                        "ok": True,
                        "status": "queued",
                        "error": (
                            json.loads(error.read_text(encoding="utf-8"))
                            if error.exists()
                            else None
                        ),
                    },
                )
            else:
                self.send_json(404, {"ok": False, "status": "unknown"})
            return
        self.send_json(404, {"ok": False, "error": "not found"})

    def do_POST(self) -> None:
        if urlparse(self.path).path != "/api/v1/captures":
            self.send_json(404, {"ok": False, "error": "not found"})
            return
        if not self.authorized():
            self.send_json(401, {"ok": False, "error": "unauthorized"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_BODY_BYTES:
            self.send_json(413, {"ok": False, "error": "invalid body size"})
            return
        try:
            raw = self.rfile.read(length)
            envelope = validate_envelope(json.loads(raw.decode("utf-8")))
            status, duplicate = queue_envelope(envelope)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            self.send_json(400, {"ok": False, "error": str(exc)})
            return
        self.send_json(
            202,
            {
                "ok": True,
                "request_id": envelope["request_id"],
                "status": status,
                "duplicate": duplicate,
            },
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="接收同一局域网内远端豆包抓取数据。")
    parser.add_argument("--host")
    parser.add_argument("--port", type=int)
    parser.add_argument("--no-dashboard", action="store_true")
    return parser.parse_args()


def main() -> int:
    configure_logging()
    args = parse_args()
    config = load_or_create_config()
    if args.host:
        config["host"] = args.host
    if args.port:
        config["port"] = args.port
    pairing = write_pairing(config)
    for directory in (INBOX_DIR, DONE_DIR, ERROR_DIR):
        directory.mkdir(parents=True, exist_ok=True)
    if not args.no_dashboard:
        ensure_dashboard(config)
    worker = CaptureWorker()
    worker.start()
    server = ReceiverServer(
        (str(config.get("host") or "0.0.0.0"), int(config["port"])),
        ReceiverHandler,
        config,
    )
    logger.info(
        "局域网接收接口已启动：%s/api/v1/captures",
        pairing["receiver_url"],
    )
    logger.info("配对文件：%s", PAIRING_PATH)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        logger.info("收到停止请求。")
    finally:
        worker.stop_event.set()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
