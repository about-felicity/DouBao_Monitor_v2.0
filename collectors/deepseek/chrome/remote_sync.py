from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import socket
import tempfile
import threading
import time
from typing import Any, Callable
import urllib.error
import urllib.request


EventWriter = Callable[[dict[str, Any]], dict[str, Any]]


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


class RemoteSync:
    """Durable background uploader using the existing LAN monitor protocol."""

    def __init__(self, data_dir: Path, event_writer: EventWriter) -> None:
        self.model = "deepseek"
        self.data_dir = data_dir
        self.root = data_dir / "remote_sync"
        self.outbox = self.root / "outbox"
        self.sent = self.root / "sent"
        self.event_writer = event_writer
        self.stop_event = threading.Event()
        self.wake_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.last_error = ""
        self.last_error_logged_at = 0.0

    def _config(self) -> dict[str, Any]:
        configured = str(os.environ.get("DEEPSEEK_REMOTE_SYNC_CONFIG") or "").strip()
        if not configured:
            return {"enabled": False}
        try:
            value = json.loads(Path(configured).read_text(encoding="utf-8-sig"))
        except (OSError, ValueError, json.JSONDecodeError):
            return {"enabled": False, "config_error": "回传配置无法读取"}
        return value if isinstance(value, dict) else {"enabled": False}

    @staticmethod
    def _urls(config: dict[str, Any]) -> list[str]:
        candidates = [config.get("receiver_url")]
        if isinstance(config.get("receiver_urls"), list):
            candidates.extend(config["receiver_urls"])
        result: list[str] = []
        for candidate in candidates:
            value = str(candidate or "").strip().rstrip("/")
            if value.startswith("http://") and value not in result:
                result.append(value)
        return result

    def enabled(self) -> bool:
        config = self._config()
        return bool(config.get("enabled") and len(str(config.get("token") or "")) >= 24 and self._urls(config))

    def status(self) -> dict[str, Any]:
        config = self._config()
        return {
            "enabled": self.enabled(),
            "model": self.model,
            "receiver_urls": self._urls(config),
            "pending": len(list(self.outbox.glob("*.json"))) if self.outbox.exists() else 0,
            "sent": len(list(self.sent.glob("*.json"))) if self.sent.exists() else 0,
            "last_error": self.last_error,
        }

    def _request_id(self, record: dict[str, Any], device: str) -> str:
        payload = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(f"{self.model}\n{device}\n{payload}".encode("utf-8")).hexdigest()

    def enqueue(self, record: dict[str, Any]) -> dict[str, Any]:
        if not self.enabled():
            self.event_writer({
                "event": "remote_sync_disabled", "level": "error", "run_id": record.get("run_id", ""),
                "round": record.get("round", 0), "prompt": record.get("prompt", ""),
                "details": {"error": "远端回传配置未启用或不完整"},
            })
            return {"enabled": False, "status": "disabled"}
        value = dict(record)
        value["collector_model"] = self.model
        device = str(os.environ.get("DEEPSEEK_REMOTE_DEVICE") or socket.gethostname()).strip()
        request_id = self._request_id(value, device)
        path = self.outbox / f"{request_id}.json"
        sent_path = self.sent / path.name
        if not path.exists() and not sent_path.exists():
            _atomic_json(path, {
                "version": 1, "model": self.model, "request_id": request_id,
                "source_device": device, "sent_at": time.time(), "record": value,
            })
            self.event_writer({
                "event": "remote_sync_queued", "level": "info", "run_id": value.get("run_id", ""),
                "round": value.get("round", 0), "prompt": value.get("prompt", ""),
                "details": {"request_id": request_id, "result_id": value.get("result_id", ""),
                            "pending": len(list(self.outbox.glob("*.json")))},
            })
        self.wake_event.set()
        return {"enabled": True, "status": "queued", "request_id": request_id}

    def _post(self, config: dict[str, Any], envelope: dict[str, Any]) -> tuple[dict[str, Any], str]:
        token = str(config.get("token") or "")
        data = json.dumps(envelope, ensure_ascii=False).encode("utf-8")
        errors: list[str] = []
        for base_url in self._urls(config):
            request = urllib.request.Request(
                f"{base_url}/api/v1/models/{self.model}/results",
                data=data,
                method="POST",
                headers={"Content-Type": "application/json; charset=utf-8", "Authorization": f"Bearer {token}"},
            )
            try:
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                with opener.open(request, timeout=min(8, max(1, float(config.get("upload_timeout") or 5)))) as response:
                    result = json.loads(response.read().decode("utf-8"))
                if result.get("ok") and result.get("request_id") == envelope.get("request_id"):
                    return result, base_url
                errors.append(f"{base_url}: 主机未确认该结果")
            except urllib.error.HTTPError as exc:
                errors.append(f"{base_url}: HTTP {exc.code}")
            except Exception as exc:  # network failures must remain queued
                errors.append(f"{base_url}: {type(exc).__name__}: {exc}")
        raise RuntimeError("；".join(errors) if errors else "没有配置可用的主机地址")

    def flush(self, max_items: int = 100) -> dict[str, Any]:
        config = self._config()
        if not self.enabled():
            return self.status()
        self.outbox.mkdir(parents=True, exist_ok=True)
        self.sent.mkdir(parents=True, exist_ok=True)
        sent_count = 0
        for path in sorted(self.outbox.glob("*.json"))[:max_items]:
            try:
                envelope = json.loads(path.read_text(encoding="utf-8-sig"))
                receipt, receiver_url = self._post(config, envelope)
                _atomic_json(self.sent / path.name, {"uploaded_at": time.time(), "receiver": receipt})
                path.unlink(missing_ok=True)
                sent_count += 1
                self.last_error = ""
                record = envelope.get("record") if isinstance(envelope.get("record"), dict) else {}
                self.event_writer({
                    "event": "remote_sync_success", "level": "info", "run_id": record.get("run_id", ""),
                    "round": record.get("round", 0), "prompt": record.get("prompt", ""),
                    "details": {"request_id": envelope.get("request_id", ""), "result_id": record.get("result_id", ""),
                                "receiver_url": receiver_url, "status": receipt.get("status", "accepted"),
                                "pending": len(list(self.outbox.glob("*.json")))},
                })
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                current_time = time.time()
                if error != self.last_error or current_time - self.last_error_logged_at >= 300:
                    try:
                        envelope = json.loads(path.read_text(encoding="utf-8-sig"))
                        record = envelope.get("record") if isinstance(envelope.get("record"), dict) else {}
                    except (OSError, ValueError, json.JSONDecodeError):
                        record = {}
                    self.event_writer({
                        "event": "remote_sync_retry", "level": "warning", "run_id": record.get("run_id", ""),
                        "round": record.get("round", 0), "prompt": record.get("prompt", ""),
                        "details": {"error": error, "pending": len(list(self.outbox.glob("*.json"))),
                                    "retry_seconds": 10},
                    })
                    self.last_error_logged_at = current_time
                self.last_error = error
                break
        value = self.status()
        value["sent_now"] = sent_count
        return value

    def probe(self) -> bool:
        config = self._config()
        failures: list[str] = []
        for base_url in self._urls(config):
            request = urllib.request.Request(f"{base_url}/api/v1/health", method="GET")
            try:
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                with opener.open(request, timeout=2) as response:
                    result = json.loads(response.read().decode("utf-8"))
                if result.get("ok"):
                    self.last_error = ""
                    self.event_writer({
                        "event": "remote_sync_host_ready", "level": "info", "run_id": "system", "round": 0,
                        "prompt": "", "details": {"receiver_url": base_url},
                    })
                    return True
                failures.append(f"{base_url}: 主机健康检查未通过")
            except urllib.error.HTTPError as exc:
                failures.append(f"{base_url}: HTTP {exc.code}")
            except Exception as exc:
                failures.append(f"{base_url}: {type(exc).__name__}: {exc}")
        self.last_error = "；".join(failures) if failures else "没有配置主机地址"
        self.event_writer({
            "event": "remote_sync_host_waiting", "level": "warning", "run_id": "system", "round": 0,
            "prompt": "", "details": {"error": self.last_error, "retry_seconds": 10},
        })
        return False

    def _watch(self) -> None:
        self.probe()
        while not self.stop_event.is_set():
            self.flush()
            self.wake_event.wait(10)
            self.wake_event.clear()

    def start(self) -> None:
        self.outbox.mkdir(parents=True, exist_ok=True)
        self.sent.mkdir(parents=True, exist_ok=True)
        state = self.status()
        self.event_writer({
            "event": "remote_sync_started" if state["enabled"] else "remote_sync_disabled",
            "level": "info" if state["enabled"] else "error", "run_id": "system", "round": 0, "prompt": "",
            "details": {"receiver_urls": state["receiver_urls"], "pending": state["pending"],
                        "error": "" if state["enabled"] else "回传配置未找到或不完整"},
        })
        if not state["enabled"]:
            return
        self.thread = threading.Thread(target=self._watch, name="deepseek-remote-sync", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        self.wake_event.set()
        if self.thread:
            self.thread.join(timeout=3)
