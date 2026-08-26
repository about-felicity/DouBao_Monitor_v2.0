"""Main-machine receiver for JSONL result records from remote model workers."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import socket
import tempfile
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse

import sys

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from monitor_core.plugins import ROOT
from monitor_core.database import store_ingested_run
from monitor_core.ingestion import normalize_remote_record
from monitor_core.recommendation_questions import canonical_recommendation_question


CONFIG = ROOT / "runtime" / "lan_result_receiver.json"
PAIRING = ROOT / "runtime" / "lan_result_pairing.json"
QUEUE = ROOT / "runtime" / "lan_result_receiver"
DISCOVERY_PORT = 8792
DISCOVERY_SERVICE = "monitor-lan-result-v1"
MODELS = {"deepseek", "yuanbao", "wenxin", "afu", "quark"}
TARGETS = {
    "deepseek": ROOT / "deepseek_monitor" / "deepseek_results.jsonl",
    "yuanbao": ROOT / "yuanbao_monitor" / "yuanbao_results.jsonl",
    "wenxin": ROOT / "wenxin_monitor" / "wenxin_results.jsonl",
    "afu": ROOT / "afu_monitor" / "afu_results.jsonl",
    "quark": ROOT / "quark_monitor" / "quark_results.jsonl",
}
def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        last_error: OSError | None = None
        for attempt in range(6):
            try:
                os.replace(temporary, path)
                last_error = None
                break
            except PermissionError as exc:
                last_error = exc
                time.sleep(0.05 * (attempt + 1))
        if last_error is not None:
            raise last_error
    finally:
        temporary.unlink(missing_ok=True)


def _unlink_best_effort(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        # Queue processing is already durable at this point. A transient
        # Windows file lock may leave a harmless stale receipt for a later pass.
        pass


def load_config() -> dict[str, Any]:
    if CONFIG.exists():
        value = json.loads(CONFIG.read_text(encoding="utf-8-sig"))
    else:
        value = {"host": "0.0.0.0", "port": 8791, "token": os.urandom(24).hex()}
        _atomic_json(CONFIG, value)
    value["port"] = int(value.get("port") or 8791)
    value["discovery_port"] = int(value.get("discovery_port") or DISCOVERY_PORT)
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
    ip = str(config.get("advertised_ip") or preferred_lan_ip()).strip()
    token = str(config["token"])
    fallback_port = 8765
    _atomic_json(PAIRING, {"version": 3, "receiver_url": f"http://{ip}:{port}",
                           "receiver_urls": [f"http://{ip}:{port}", f"http://{ip}:{fallback_port}",
                                             f"http://{socket.gethostname()}:{port}", f"http://{socket.gethostname()}:{fallback_port}"],
                           "token": token, "upload_timeout": 5,
                           "discovery_port": int(config.get("discovery_port") or DISCOVERY_PORT),
                           "discovery_fingerprint": token_fingerprint(token)})


def token_fingerprint(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:24]


def discovery_signature(token: str, nonce: str, receiver_url: str) -> str:
    return hmac.new(token.encode("utf-8"), f"{nonce}\n{receiver_url}".encode("utf-8"), hashlib.sha256).hexdigest()


def discovery_response(request: dict[str, Any], config: dict[str, Any], local_ip: str) -> dict[str, Any] | None:
    if not isinstance(request, dict):
        return None
    token = str(config.get("token") or "")
    nonce = str(request.get("nonce") or "")
    if request.get("service") != DISCOVERY_SERVICE or not re.fullmatch(r"[0-9a-f]{32}", nonce):
        return None
    supplied = str(request.get("fingerprint") or "")
    if not token or not hmac.compare_digest(token_fingerprint(token), supplied):
        return None
    receiver_url = f"http://{local_ip}:{int(config['port'])}"
    return {"service": DISCOVERY_SERVICE, "nonce": nonce, "receiver_url": receiver_url,
            "signature": discovery_signature(token, nonce, receiver_url)}


def local_ip_for_peer(peer_ip: str) -> str:
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect((peer_ip, 9))
        address = str(probe.getsockname()[0])
        return address if not address.startswith("127.") else preferred_lan_ip()
    except OSError:
        return preferred_lan_ip()
    finally:
        probe.close()


def discovery_server(config: dict[str, Any]) -> None:
    server = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("0.0.0.0", int(config.get("discovery_port") or DISCOVERY_PORT)))
    while True:
        try:
            raw, address = server.recvfrom(4096)
            request = json.loads(raw.decode("utf-8"))
            response = discovery_response(request, config, local_ip_for_peer(address[0]))
            if response:
                server.sendto(json.dumps(response).encode("utf-8"), address)
                write_pairing(config)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
            continue


def valid_request_id(value: Any) -> str:
    text = str(value or "").lower()
    if not re.fullmatch(r"[0-9a-f]{64}", text):
        raise ValueError("invalid request_id")
    return text


def validate_result_envelope(model: str, value: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    if value.get("model") != model or not isinstance(value.get("record"), dict):
        raise ValueError("invalid result envelope")
    request_id = valid_request_id(value.get("request_id"))
    record = dict(value["record"])
    declared = str(record.get("collector_model") or "").strip()
    if declared and declared != model:
        raise ValueError(f"collector model mismatch: expected {model}, got {declared}")
    device = str(value.get("source_device") or "").strip()
    raw = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    valid_ids = {hashlib.sha256(f"{model}\n{device}\n{raw}".encode("utf-8")).hexdigest()}
    if declared == model:
        legacy_record = dict(record)
        legacy_record.pop("collector_model", None)
        legacy_raw = json.dumps(legacy_record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        valid_ids.add(hashlib.sha256(f"{model}\n{device}\n{legacy_raw}".encode("utf-8")).hexdigest())
    if request_id not in valid_ids:
        raise ValueError("request identity mismatch")
    record["collector_model"] = model
    value["record"] = record
    return request_id, record


def result_receipt(model: str, request_id: str, value: dict[str, Any], record: dict[str, Any]) -> dict[str, Any]:
    sources = record.get("sources") if isinstance(record.get("sources"), list) else []
    compact_sources = []
    for item in sources:
        if not isinstance(item, dict):
            continue
        compact_sources.append({
            "title": str(item.get("title") or "").strip(),
            "href": str(item.get("href") or item.get("url") or "").strip(),
        })
    question = str(record.get("question") or record.get("prompt") or "").strip()
    answer = str(record.get("web_body") or record.get("reply") or record.get("answer") or "").strip()
    products = record.get("products") if isinstance(record.get("products"), list) else []
    review_status = str(record.get("product_review_status") or "")
    successful = str(record.get("status") or "success").casefold() == "success"
    expected_source_count = max(len(compact_sources), int(record.get("expected_source_count") or 0))
    capture_complete = bool(record.get("source_capture_complete", len(compact_sources) >= expected_source_count))
    if model == "wenxin" and not compact_sources:
        # A 0/0 Wenxin round is a citation-selector/capture failure, not a valid
        # source-less answer.  Quarantine it even when an outdated collector sent
        # source_capture_complete=true.
        capture_complete = False
    return {
        "request_id": request_id,
        "model": model,
        "source_device": str(value.get("source_device") or ""),
        "received_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "question": question,
        "account_uid_masked": str(record.get("account_uid_masked") or ""),
        "rows_written": len(compact_sources),
        "analysis": {
            "question_present": bool(question),
            "answer_present": bool(answer),
            "answer_length": len(answer),
            "task_id": int(record.get("task_id") or 1),
            "capture_mode": str(record.get("capture_mode") or ""),
            "capture_label": str(record.get("capture_label") or ""),
            "body_capture_complete": bool(record.get("body_capture_complete", bool(answer))),
            "source_count": len(compact_sources),
            "expected_source_count": expected_source_count,
            "source_capture_complete": successful and capture_complete and len(compact_sources) >= expected_source_count,
            "missing_source_links": sum(not item["href"] for item in compact_sources),
            "missing_source_titles": sum(not item["title"] for item in compact_sources),
            "recommendation_question": bool(canonical_recommendation_question(question)),
            "product_count": len(products),
            "product_review_status": review_status or ("not_required" if not canonical_recommendation_question(question) else "pending"),
            "product_extraction_method": str(record.get("product_extraction_method") or ""),
            "product_analysis_model": str(record.get("product_analysis_model") or ""),
            "product_parse_complete": (not canonical_recommendation_question(question)) or review_status == "ai_verified",
            "sources": compact_sources,
        },
    }


def analyze_record_products(record: dict[str, Any]) -> None:
    question = str(record.get("question") or record.get("prompt") or "").strip()
    if not canonical_recommendation_question(question):
        record["products"] = []
        record["product_review_status"] = "not_required"
        record["product_extraction_method"] = "not_required"
        record["product_analysis_model"] = ""
        return
    answer = str(record.get("web_body") or record.get("reply") or record.get("answer") or "").strip()
    if not answer:
        record["products"] = []
        record["product_review_status"] = "no_answer"
        record["product_extraction_method"] = "none"
        record["product_analysis_model"] = ""
        return
    # Preserve products only when the remote side already completed the same
    # verified review contract. Otherwise queue enrichment without making an
    # external model call on the ingestion thread. This keeps answer/source
    # durability independent from billing, rate limits, and network latency.
    review_status = str(record.get("product_review_status") or "")
    products = record.get("products") if isinstance(record.get("products"), list) else []
    if review_status != "ai_verified":
        products = []
        review_status = "ai_pending"
        record["product_extraction_method"] = "pending"
        record["product_analysis_model"] = ""
    record["products"] = products
    record["brands"] = sorted({
        str(item.get("brand_name") or "").strip()
        for item in products if isinstance(item, dict) and str(item.get("brand_name") or "").strip()
    })
    record["product_review_status"] = review_status


class ResultWorker(threading.Thread):
    def __init__(self, server: "Server", models: tuple[str, ...]) -> None:
        super().__init__(name=f"lan-result-analysis-{'-'.join(models)}", daemon=True)
        self.server = server
        self.models = models
        self.stop_event = threading.Event()

    def run(self) -> None:
        while not self.stop_event.is_set():
            processed = False
            for model in self.models:
                inbox = QUEUE / model / "inbox"
                for path in sorted(inbox.glob("*.json")) if inbox.exists() else []:
                    processed = True
                    try:
                        self.process(model, path)
                    except Exception:
                        # One corrupt or temporarily locked queue item must not
                        # terminate ingestion for the entire model.
                        self.stop_event.wait(0.5)
                    if self.stop_event.is_set():
                        break
            self.stop_event.wait(0.25 if processed else 1.0)

    def process(self, model: str, path: Path) -> None:
        value: dict[str, Any] = {}
        record: dict[str, Any] = {}
        try:
            value = json.loads(path.read_text(encoding="utf-8-sig"))
            request_id, record = validate_result_envelope(model, value)
            analyze_record_products(record)
            sources = record.get("sources") if isinstance(record.get("sources"), list) else []
            record["expected_source_count"] = max(int(record.get("expected_source_count") or 0), len(sources))
            record["model_id"] = model
            record["remote_request_id"] = request_id
            record["remote_source_device"] = str(value.get("source_device") or "")
            record["remote_received_at"] = time.time()
            error_path = QUEUE / model / "errors" / f"{request_id}.json"
            with self.server.write_lock:
                # A response is not acknowledged as complete until its full raw
                # event and query-ready children commit in the same PostgreSQL
                # transaction. There is deliberately no CSV/JSONL write here.
                stored = store_ingested_run(
                    model, request_id, record, value,
                    normalize_remote_record(model, record),
                )
                receipt = result_receipt(model, request_id, value, record)
                receipt["stored_run_id"] = str(stored.get("run_id") or "")
                receipt["analysis"]["duplicate_answer"] = bool(stored.get("deduplicated"))
                receipt["analysis"]["duplicate_reason"] = str(stored.get("duplicate_reason") or "")
                receipt["analysis"]["quarantined"] = bool(stored.get("quarantined"))
                receipt["analysis"]["quarantine_reason"] = str(stored.get("quarantine_reason") or "")
                _atomic_json(QUEUE / model / "done" / f"{request_id}.json", receipt)
                _unlink_best_effort(path)
                _unlink_best_effort(error_path)
        except Exception as exc:
            error = {
                "request_id": path.stem,
                "last_error": f"{type(exc).__name__}: {exc}",
                "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }
            if record:
                receipt = result_receipt(model, path.stem, value, record)
                error.update({
                    "source_device": receipt["source_device"],
                    "question": receipt["question"],
                    "received_at": receipt["received_at"],
                    "rows_written": receipt["rows_written"],
                    "analysis": receipt["analysis"],
                })
            try:
                _atomic_json(QUEUE / model / "errors" / f"{path.stem}.json", error)
            except OSError:
                pass
            self.stop_event.wait(2.0)


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], config: dict[str, Any]) -> None:
        super().__init__(address, Handler)
        self.config = config
        self.write_lock = threading.Lock()
        self.workers = [ResultWorker(self, (model,)) for model in sorted(MODELS)]
        for worker in self.workers:
            worker.start()

    def server_close(self) -> None:
        for worker in self.workers:
            worker.stop_event.set()
        for worker in self.workers:
            worker.join(timeout=3)
        super().server_close()


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
            request_id, _record = validate_result_envelope(model, value)
            done = QUEUE / model / "done" / f"{request_id}.json"
            inbox = QUEUE / model / "inbox" / f"{request_id}.json"
            if not done.exists() and not inbox.exists():
                with self.server.write_lock:
                    if not done.exists() and not inbox.exists():
                        _atomic_json(inbox, value)
            self.send_json(202, {"ok": True, "request_id": request_id,
                                 "status": "processed" if done.exists() else "queued"})
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
    threading.Thread(target=discovery_server, args=(config,), name="lan-result-discovery", daemon=True).start()
    server = Server((args.host or str(config.get("host") or "0.0.0.0"), args.port or int(config["port"])), config)
    print(f"LAN result receiver listening on {server.server_address[0]}:{server.server_address[1]}; "
          f"discovery UDP {config['discovery_port']}")
    server.serve_forever(poll_interval=0.5)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
