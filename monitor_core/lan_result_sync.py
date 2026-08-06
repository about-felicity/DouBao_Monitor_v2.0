"""Durable LAN result upload for one-model-per-machine workers."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import socket
import tempfile
import threading
import time
from typing import Any
import urllib.parse
import urllib.request


ROOT = Path(__file__).resolve().parent.parent
_AGENTS: set[str] = set()
_AGENT_LOCK = threading.Lock()


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    os.close(handle)
    temporary = Path(temporary_name)
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _config_path(model: str) -> Path:
    return ROOT / "runtime" / "remote_workers" / f"{model}_sync.json"


def _load_config(model: str) -> dict[str, Any]:
    try:
        value = json.loads(_config_path(model).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, json.JSONDecodeError):
        return {"enabled": False}
    return value if isinstance(value, dict) else {"enabled": False}


def _urls(config: dict[str, Any]) -> list[str]:
    configured = config.get("receiver_urls")
    candidates = [config.get("receiver_url")]
    if isinstance(configured, list):
        candidates.extend(configured)
    result: list[str] = []
    for candidate in candidates:
        url = str(candidate or "").strip().rstrip("/")
        if url.startswith("http://") and url not in result:
            result.append(url)
    return result


def _request_id(model: str, record: dict[str, Any], device: str) -> str:
    payload = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(f"{model}\n{device}\n{payload}".encode("utf-8")).hexdigest()


def enqueue(model: str, record: dict[str, Any]) -> dict[str, Any]:
    """Persist an upload before any network attempt; safe to call per result."""
    config = _load_config(model)
    if not config.get("enabled"):
        return {"enabled": False, "status": "disabled"}
    device = str(config.get("device_name") or socket.gethostname()).strip()
    request_id = _request_id(model, record, device)
    root = ROOT / "runtime" / "remote_workers" / model
    pending, sent = root / "outbox", root / "sent"
    path = pending / f"{request_id}.json"
    if not path.exists() and not (sent / path.name).exists():
        _atomic_json(path, {"version": 1, "model": model, "request_id": request_id,
                            "source_device": device, "sent_at": time.time(), "record": record})
    _ensure_agent(model)
    return {"enabled": True, "status": "queued_for_background_upload", "request_id": request_id}


def _post(config: dict[str, Any], envelope: dict[str, Any]) -> dict[str, Any]:
    token = str(config.get("token") or "")
    if len(token) < 24:
        raise ValueError("remote sync token is missing or too short")
    urls = _urls(config)
    if not urls:
        raise ValueError("receiver_url is not configured")
    data = json.dumps(envelope, ensure_ascii=False).encode("utf-8")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    failures = []
    for url in urls:
        request = urllib.request.Request(
            f"{url}/api/v1/models/{envelope['model']}/results", data=data, method="POST",
            headers={"Content-Type": "application/json; charset=utf-8", "Authorization": f"Bearer {token}"},
        )
        try:
            with opener.open(request, timeout=min(8, max(1, float(config.get("upload_timeout") or 3)))) as response:
                result = json.loads(response.read().decode("utf-8"))
            if result.get("ok") and result.get("request_id") == envelope["request_id"]:
                return result
            raise RuntimeError("receiver did not acknowledge queued result")
        except Exception as exc:
            failures.append(f"{url}: {type(exc).__name__}: {exc}")
    raise RuntimeError("; ".join(failures))


def flush(model: str, max_items: int = 100) -> dict[str, Any]:
    config = _load_config(model)
    root = ROOT / "runtime" / "remote_workers" / model
    pending, sent = root / "outbox", root / "sent"
    pending.mkdir(parents=True, exist_ok=True)
    sent.mkdir(parents=True, exist_ok=True)
    sent_count, error = 0, ""
    for path in sorted(pending.glob("*.json"))[:max_items]:
        try:
            envelope = json.loads(path.read_text(encoding="utf-8-sig"))
            receipt = _post(config, envelope)
            _atomic_json(sent / path.name, {"uploaded_at": time.time(), "receiver": receipt})
            path.unlink(missing_ok=True)
            sent_count += 1
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            break
    return {"enabled": bool(config.get("enabled")), "sent": sent_count,
            "pending": len(list(pending.glob("*.json"))), "last_error": error}


def _watch(model: str) -> None:
    while True:
        flush(model)
        time.sleep(5)


def _ensure_agent(model: str) -> None:
    with _AGENT_LOCK:
        if model in _AGENTS:
            return
        thread = threading.Thread(target=_watch, args=(model,), name=f"{model}-lan-sync", daemon=True)
        thread.start()
        _AGENTS.add(model)
