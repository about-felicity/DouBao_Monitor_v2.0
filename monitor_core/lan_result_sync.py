"""Durable LAN result upload for one-model-per-machine workers."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from pathlib import Path
import select
import socket
import secrets
import tempfile
import threading
import time
from typing import Any
import urllib.parse
import urllib.request


ROOT = Path(__file__).resolve().parent.parent
_AGENTS: set[str] = set()
_AGENT_LOCK = threading.Lock()
DISCOVERY_PORT = 8792
DISCOVERY_SERVICE = "monitor-lan-result-v1"
ALLOWED_MODELS = frozenset({"deepseek", "yuanbao", "wenxin", "afu", "quark"})


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
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme == "http" and parsed.hostname and parsed.port == 8791:
            fallback = f"http://{parsed.hostname}:8765"
            if fallback not in result:
                result.append(fallback)
    return result


def _request_id(model: str, record: dict[str, Any], device: str) -> str:
    payload = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(f"{model}\n{device}\n{payload}".encode("utf-8")).hexdigest()


def stamp_record_model(model: str, record: dict[str, Any]) -> dict[str, Any]:
    if model not in ALLOWED_MODELS:
        raise ValueError(f"unsupported collector model: {model}")
    if not isinstance(record, dict):
        raise TypeError("collector result must be a JSON object")
    value = dict(record)
    declared = str(value.get("collector_model") or "").strip()
    if declared and declared != model:
        raise ValueError(f"collector model mismatch: expected {model}, got {declared}")
    value["collector_model"] = model
    return value


def _token_fingerprint(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:24]


def _discovery_signature(token: str, nonce: str, receiver_url: str) -> str:
    return hmac.new(token.encode("utf-8"), f"{nonce}\n{receiver_url}".encode("utf-8"), hashlib.sha256).hexdigest()


def _validated_discovery_url(value: dict[str, Any], token: str, nonce: str) -> str:
    url = str(value.get("receiver_url") or "").rstrip("/")
    signature = str(value.get("signature") or "")
    if value.get("service") != DISCOVERY_SERVICE or value.get("nonce") != nonce:
        return ""
    if not url.startswith("http://") or not hmac.compare_digest(_discovery_signature(token, nonce, url), signature):
        return ""
    return url


def _discover(config: dict[str, Any]) -> list[str]:
    token = str(config.get("token") or "")
    if len(token) < 24:
        return []
    nonce = secrets.token_hex(16)
    request = json.dumps({"service": DISCOVERY_SERVICE, "nonce": nonce,
                          "fingerprint": _token_fingerprint(token)}).encode("utf-8")
    port = int(config.get("discovery_port") or DISCOVERY_PORT)
    timeout = min(5.0, max(0.5, float(config.get("discovery_timeout") or 2)))
    addresses = [""]
    try:
        for item in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET, socket.SOCK_DGRAM):
            address = str(item[4][0])
            if not address.startswith(("127.", "169.254.")) and address not in addresses:
                addresses.append(address)
    except OSError:
        pass
    clients: list[socket.socket] = []
    found: list[str] = []
    try:
        for address in addresses:
            client = None
            try:
                client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                client.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                client.bind((address, 0))
                client.setblocking(False)
                client.sendto(request, ("255.255.255.255", port))
                clients.append(client)
            except OSError:
                if client is not None:
                    client.close()
        deadline = time.monotonic() + timeout
        while clients and time.monotonic() < deadline:
            readable, _, _ = select.select(clients, [], [], min(0.25, max(0, deadline - time.monotonic())))
            for client in readable:
                try:
                    raw, _address = client.recvfrom(4096)
                    value = json.loads(raw.decode("utf-8"))
                    url = _validated_discovery_url(value, token, nonce)
                    if url and url not in found:
                        found.append(url)
                except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
                    continue
    finally:
        for client in clients:
            client.close()
    return found


def _remember_receiver(model: str, config: dict[str, Any], receiver_url: str) -> None:
    updated = dict(config)
    updated["receiver_url"] = receiver_url
    updated["receiver_urls"] = [receiver_url] + [url for url in _urls(config) if url != receiver_url]
    updated["receiver_discovered_at"] = time.time()
    _atomic_json(_config_path(model), updated)
    config.clear()
    config.update(updated)


def enqueue(model: str, record: dict[str, Any]) -> dict[str, Any]:
    """Persist an upload before any network attempt; safe to call per result."""
    record = stamp_record_model(model, record)
    config = _load_config(model)
    if not config.get("enabled"):
        return {"enabled": False, "status": "disabled"}
    configured_model = str(config.get("model") or model).strip()
    if configured_model != model:
        raise ValueError(f"sync config model mismatch: expected {model}, got {configured_model}")
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


def _post_one(config: dict[str, Any], envelope: dict[str, Any], url: str) -> dict[str, Any]:
    token = str(config.get("token") or "")
    data = json.dumps(envelope, ensure_ascii=False).encode("utf-8")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    request = urllib.request.Request(
        f"{url}/api/v1/models/{envelope['model']}/results", data=data, method="POST",
        headers={"Content-Type": "application/json; charset=utf-8", "Authorization": f"Bearer {token}"},
    )
    with opener.open(request, timeout=min(8, max(1, float(config.get("upload_timeout") or 3)))) as response:
        result = json.loads(response.read().decode("utf-8"))
    if result.get("ok") and result.get("request_id") == envelope["request_id"]:
        return result
    raise RuntimeError("receiver did not acknowledge queued result")


def _post(config: dict[str, Any], envelope: dict[str, Any]) -> dict[str, Any]:
    token = str(config.get("token") or "")
    if len(token) < 24:
        raise ValueError("remote sync token is missing or too short")
    failures = []
    urls = _urls(config)
    for url in urls:
        try:
            return _post_one(config, envelope, url)
        except Exception as exc:
            failures.append(f"{url}: {type(exc).__name__}: {exc}")
    for url in _discover(config):
        if url in urls:
            continue
        try:
            result = _post_one(config, envelope, url)
            _remember_receiver(str(envelope["model"]), config, url)
            return result
        except Exception as exc:
            failures.append(f"{url}: {type(exc).__name__}: {exc}")
    if not failures:
        failures.append("receiver_url is not configured and LAN discovery found no receiver")
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
            if envelope.get("model") != model:
                raise ValueError(f"outbox model mismatch: expected {model}, got {envelope.get('model')}")
            original_record = envelope.get("record")
            record = stamp_record_model(model, original_record)
            device = str(envelope.get("source_device") or "").strip()
            valid_ids = {_request_id(model, record, device)}
            if isinstance(original_record, dict):
                valid_ids.add(_request_id(model, original_record, device))
            if envelope.get("request_id") not in valid_ids or path.stem != envelope.get("request_id"):
                raise ValueError("outbox request identity mismatch")
            receipt = _post(config, envelope)
            _atomic_json(sent / path.name, {"uploaded_at": time.time(), "receiver": receipt})
            path.unlink(missing_ok=True)
            sent_count += 1
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            break
    return {"enabled": bool(config.get("enabled")), "sent": sent_count,
            "pending": len(list(pending.glob("*.json"))), "last_error": error}


def _collector_result_paths(model: str) -> tuple[Path, ...]:
    """Return the local archives a collector may be writing for this model."""
    worker_root = ROOT / "runtime" / "remote_workers"
    candidates = (
        ROOT / "runtime" / "remote_workers" / f"{model}_collector_results.jsonl",
        ROOT / f"{model}_monitor" / f"{model}_results.jsonl",
        *sorted(worker_root.glob(f"{model}_collector_results*.jsonl")),
    )
    return tuple(dict.fromkeys(candidates))


def prune_collector_results(
    model: str,
    path: Path,
    *,
    retention_days: int | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Remove only records whose successful upload receipt is over the limit.

    The outbox is authoritative for unsent work. A JSONL line without a valid
    acknowledgement is always retained, regardless of its age. The source file
    is replaced only when it stayed unchanged for the whole compaction, so an
    active collector cannot lose a concurrently appended line.
    """
    path = Path(path)
    config = _load_config(model)
    days = max(1, min(365, int(
        retention_days if retention_days is not None
        else config.get("local_retention_days") or 1
    )))
    root = ROOT / "runtime" / "remote_workers" / model
    state_path = root / f"local_retention_{path.name}.json"
    now = time.time()
    if not force:
        try:
            state = json.loads(state_path.read_text(encoding="utf-8-sig"))
            if now - float(state.get("last_checked_at") or 0) < 3600:
                return {"checked": False, "removed": 0, "bytes_freed": 0, "retention_days": days}
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            pass
    if not path.is_file():
        _atomic_json(state_path, {"last_checked_at": now, "retention_days": days})
        return {"checked": True, "removed": 0, "bytes_freed": 0, "retention_days": days}

    sent = root / "sent"
    sent.mkdir(parents=True, exist_ok=True)
    cutoff = now - days * 86400
    before = path.stat()
    handle, temporary_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".retention.tmp", dir=path.parent)
    os.close(handle)
    temporary = Path(temporary_name)
    removed = 0
    dropped_receipts: list[Path] = []
    device = str(config.get("device_name") or socket.gethostname()).strip()
    try:
        with path.open("r", encoding="utf-8-sig", errors="replace") as source, temporary.open(
            "w", encoding="utf-8", newline=""
        ) as target:
            for line in source:
                keep = True
                try:
                    record = json.loads(line)
                    stamped = stamp_record_model(model, record)
                    receipt_path = sent / f"{_request_id(model, stamped, device)}.json"
                    if receipt_path.is_file():
                        receipt = json.loads(receipt_path.read_text(encoding="utf-8-sig"))
                        if float(receipt.get("uploaded_at") or 0) <= cutoff:
                            keep = False
                            dropped_receipts.append(receipt_path)
                except (OSError, ValueError, TypeError, json.JSONDecodeError):
                    # Malformed or unprovable records are evidence, not trash.
                    keep = True
                if keep:
                    target.write(line if line.endswith("\n") else line + "\n")
                else:
                    removed += 1
        after = path.stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            return {"checked": False, "removed": 0, "bytes_freed": 0,
                    "retention_days": days, "reason": "collector_file_changed"}
        if removed:
            os.replace(temporary, path)
            for receipt_path in dropped_receipts:
                receipt_path.unlink(missing_ok=True)
        bytes_freed = max(0, before.st_size - path.stat().st_size)
        _atomic_json(state_path, {
            "last_checked_at": now,
            "retention_days": days,
            "last_removed": removed,
            "last_bytes_freed": bytes_freed,
            "path": str(path),
        })
        return {"checked": True, "removed": removed, "bytes_freed": bytes_freed,
                "retention_days": days}
    finally:
        temporary.unlink(missing_ok=True)


def _watch(model: str) -> None:
    while True:
        flush(model)
        for result_path in _collector_result_paths(model):
            try:
                prune_collector_results(model, result_path)
            except Exception:
                # Retention must never interrupt durable upload retries.
                pass
        time.sleep(5)


def _ensure_agent(model: str) -> None:
    with _AGENT_LOCK:
        if model in _AGENTS:
            return
        thread = threading.Thread(target=_watch, args=(model,), name=f"{model}-lan-sync", daemon=True)
        thread.start()
        _AGENTS.add(model)


def start(model: str) -> None:
    """Start retrying an existing outbox before the next collector result arrives."""
    if _load_config(model).get("enabled"):
        _ensure_agent(model)
