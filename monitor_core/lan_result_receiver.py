"""Main-machine receiver for JSONL result records from remote model workers."""

from __future__ import annotations

import argparse
import hmac
import json
import os
from pathlib import Path
import re
import socket
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse

from monitor_core.plugins import ROOT


CONFIG = ROOT / "runtime" / "lan_result_receiver.json"
PAIRING = ROOT / "runtime" / "lan_result_pairing.json"
QUEUE = ROOT / "runtime" / "lan_result_receiver"
MODELS = {"deepseek", "yuanbao", "wenxin", "afu"}
TARGETS = {
    "deepseek": ROOT / "deepseek_monitor" / "deepseek_results.jsonl",
    "yuanbao": ROOT / "yuanbao_monitor" / "yuanbao_results.jsonl",
    "wenxin": ROOT / "wenxin_monitor" / "wenxin_results.jsonl",
    "afu": ROOT / "afu_monitor" / "afu_results.jsonl",
}
def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def load_config() -> dict[str, Any]:
    if CONFIG.exists():
        value = json.loads(CONFIG.read_text(encoding="utf-8-sig"))
    else:
        value = {"host": "0.0.0.0", "port": 8791, "token": os.urandom(24).hex()}
        _atomic_json(CONFIG, value)
    value["port"] = int(value.get("port") or 8791)
    return value


def preferred_lan_ip() -> str:
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("8.8.8.8", 80))
        address = str(probe.getsockname()[0])
        return address if not address.startswith("127.") else "127.0.0.1"
    except OSError:
        return "127.0.0.1"
    finally:
        probe.close()


def write_pairing(config: dict[str, Any]) -> None:
    port = int(config["port"])
    ip = preferred_lan_ip()
    _atomic_json(PAIRING, {"version": 1, "receiver_url": f"http://{ip}:{port}",
                           "receiver_urls": [f"http://{ip}:{port}", f"http://{socket.gethostname()}:{port}"],
                           "token": str(config["token"]), "upload_timeout": 5})


def valid_request_id(value: Any) -> str:
    text = str(value or "").lower()
    if not re.fullmatch(r"[0-9a-f]{64}", text):
        raise ValueError("invalid request_id")
    return text


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], config: dict[str, Any]) -> None:
        super().__init__(address, Handler)
        self.config = config
        self.write_lock = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    server: Server

    def log_message(self, *_: Any) -> None:
        return

    def send_json(self, status: int, value: dict[str, Any]) -> None:
        raw = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def authorized(self) -> bool:
        expected = str(self.server.config.get("token") or "")
        supplied = str(self.headers.get("Authorization") or "")
        supplied = supplied[7:].strip() if supplied.lower().startswith("bearer ") else ""
        return bool(expected) and hmac.compare_digest(expected, supplied)

    def do_GET(self) -> None:
        if urlparse(self.path).path == "/api/v1/health":
            self.send_json(200, {"ok": True, "service": "lan-result-receiver", "host": socket.gethostname()})
        else:
            self.send_json(404, {"ok": False, "error": "not found"})

    def do_POST(self) -> None:
        match = re.fullmatch(r"/api/v1/models/([a-z0-9_-]+)/results", urlparse(self.path).path)
        if not match or match.group(1) not in MODELS:
            self.send_json(404, {"ok": False, "error": "unknown model"})
            return
        if not self.authorized():
            self.send_json(401, {"ok": False, "error": "unauthorized"})
            return
        try:
            size = int(self.headers.get("Content-Length") or 0)
            if not 0 < size <= 25 * 1024 * 1024:
                raise ValueError("invalid body size")
            value = json.loads(self.rfile.read(size).decode("utf-8"))
            model = match.group(1)
            request_id = valid_request_id(value.get("request_id"))
            if value.get("model") != model or not isinstance(value.get("record"), dict):
                raise ValueError("invalid result envelope")
            done = QUEUE / model / "done" / f"{request_id}.json"
            if not done.exists():
                with self.server.write_lock:
                    if not done.exists():
                        target = TARGETS[model]
                        target.parent.mkdir(parents=True, exist_ok=True)
                        record = dict(value["record"])
                        record["remote_source_device"] = str(value.get("source_device") or "")
                        record["remote_received_at"] = time.time()
                        with target.open("a", encoding="utf-8") as handle:
                            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
                        _atomic_json(done, {"request_id": request_id, "received_at": time.time()})
            self.send_json(202, {"ok": True, "request_id": request_id, "status": "processed"})
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            self.send_json(400, {"ok": False, "error": str(exc)})


def main() -> int:
    parser = argparse.ArgumentParser(description="Receive remote model results on the main machine")
    parser.add_argument("--host")
    parser.add_argument("--port", type=int)
    args = parser.parse_args()
    config = load_config()
    if args.host:
        config["host"] = args.host
    if args.port:
        config["port"] = args.port
    write_pairing(config)
    server = Server((args.host or str(config.get("host") or "0.0.0.0"), args.port or int(config["port"])), config)
    print(f"LAN result receiver listening on {server.server_address[0]}:{server.server_address[1]}")
    server.serve_forever(poll_interval=0.5)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
