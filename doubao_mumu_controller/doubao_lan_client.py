from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
from typing import Any
import urllib.error
import urllib.parse
import urllib.request


BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "doubao_remote_sync_config.json"
OUTBOX_DIR = BASE_DIR / "lan_upload_outbox"
SENT_DIR = BASE_DIR / "lan_upload_sent"
OUTBOX_LOCK = BASE_DIR / ".lan_upload_outbox.lock"
SYNC_AGENT_LOG = BASE_DIR / "doubao_lan_sync_agent.log"
SYNC_AGENT_GUARD_PORT = 18790
logger = logging.getLogger("doubao_lan_client")
BEIJING_TZ = timezone(timedelta(hours=8))


def beijing_now() -> str:
    """Program-side UTC+8 timestamp; never reads MuMu/Android device time."""
    return datetime.now(BEIJING_TZ).isoformat(timespec="seconds")


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


def load_config() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        return {"enabled": False}
    try:
        value = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
    except Exception:
        return {"enabled": False}
    return value if isinstance(value, dict) else {"enabled": False}


def candidate_receiver_urls(config: dict[str, Any]) -> list[str]:
    values = [config.get("receiver_url")]
    configured = config.get("receiver_urls")
    if isinstance(configured, list):
        values.extend(configured)
    host = str(config.get("receiver_host") or "").strip()
    if host:
        first = str(config.get("receiver_url") or "")
        try:
            port = urllib.parse.urlparse(first).port or 8790
        except ValueError:
            port = 8790
        values.append(f"http://{host}:{port}")
    result: list[str] = []
    for value in values:
        url = str(value or "").rstrip("/")
        if url.startswith("http://") and url not in result:
            result.append(url)
    return result


def request_id_for(payload: dict[str, Any], source_device: str) -> str:
    parts = [
        source_device,
        str(payload.get("url") or ""),
        str(payload.get("question") or ""),
        str(payload.get("extractedAt") or ""),
        str(payload.get("account_uid") or payload.get("account_uid_masked") or ""),
        str(payload.get("mumu_instance") or ""),
        str(payload.get("answerText") or payload.get("answer_text") or ""),
    ]
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


def enqueue(payload: dict[str, Any]) -> tuple[Path | None, dict[str, Any]]:
    config = load_config()
    if not config.get("enabled"):
        return None, {"enabled": False, "status": "disabled"}
    source_device = str(
        config.get("device_name") or socket.gethostname()
    ).strip()
    request_id = request_id_for(payload, source_device)
    envelope = {
        "version": 1,
        "request_id": request_id,
        "source_device": source_device,
        "sent_at": beijing_now(),
        "payload": payload,
    }
    path = OUTBOX_DIR / f"{request_id}.json"
    if not path.exists() and not (SENT_DIR / path.name).exists():
        atomic_json(path, envelope)
    return path, {
        "enabled": True,
        "status": "queued",
        "request_id": request_id,
    }


def post_envelope(
    config: dict[str, Any],
    envelope: dict[str, Any],
) -> dict[str, Any]:
    token = str(config.get("token") or "")
    receiver_urls = candidate_receiver_urls(config)
    if not receiver_urls:
        raise ValueError("receiver_url 必须是同一局域网内的 http:// 地址。")
    if len(token) < 24:
        raise ValueError("远端同步密钥无效。")
    data = json.dumps(envelope, ensure_ascii=False).encode("utf-8")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    # Uploading happens in a background agent. Keep each endpoint attempt
    # short so a broken network cannot stall the retry queue for long.
    timeout = min(5.0, max(1.0, float(config.get("upload_timeout") or 3)))
    failures: list[str] = []
    for receiver_url in receiver_urls:
        request = urllib.request.Request(
            receiver_url + "/api/v1/captures",
            data=data,
            method="POST",
            headers={
                "Content-Type": "application/json; charset=utf-8",
                "Authorization": "Bearer " + token,
                "X-Doubao-Device": str(
                    envelope.get("source_device") or ""
                ),
            },
        )
        try:
            with opener.open(request, timeout=timeout) as response:
                value = json.loads(response.read().decode("utf-8"))
            if not isinstance(value, dict) or not value.get("ok"):
                raise RuntimeError("主机接收接口未确认数据。")
            if str(value.get("request_id") or "") != str(
                envelope.get("request_id") or ""
            ):
                raise RuntimeError("主机确认的 request_id 与待传数据不一致。")
            if value.get("status") not in {"queued", "processed"}:
                raise RuntimeError("主机没有确认数据已进入可靠队列。")
            return {**value, "receiver_url": receiver_url}
        except Exception as exc:
            failures.append(f"{receiver_url}: {type(exc).__name__}: {exc}")
    raise RuntimeError("；".join(failures))


def flush_outbox(
    target_logger: logging.Logger | None = None,
    *,
    max_items: int = 100,
) -> dict[str, Any]:
    active_logger = target_logger or logger
    config = load_config()
    if not config.get("enabled"):
        return {"enabled": False, "sent": 0, "pending": 0}
    OUTBOX_DIR.mkdir(parents=True, exist_ok=True)
    SENT_DIR.mkdir(parents=True, exist_ok=True)
    lock_fd = None
    try:
        lock_fd = os.open(
            OUTBOX_LOCK,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY,
        )
        os.write(lock_fd, str(os.getpid()).encode("ascii"))
    except FileExistsError:
        try:
            if time.time() - OUTBOX_LOCK.stat().st_mtime > 300:
                OUTBOX_LOCK.unlink()
                return flush_outbox(active_logger, max_items=max_items)
        except FileNotFoundError:
            return flush_outbox(active_logger, max_items=max_items)
        return {
            "enabled": True,
            "sent": 0,
            "pending": len(list(OUTBOX_DIR.glob("*.json"))),
            "failures": 0,
            "last_error": "同步代理正在上传，当前调用立即返回",
        }
    sent = 0
    failures = 0
    last_error = ""
    try:
        for path in sorted(OUTBOX_DIR.glob("*.json"))[:max_items]:
            try:
                envelope = json.loads(path.read_text(encoding="utf-8-sig"))
                response = post_envelope(config, envelope)
                receipt = {
                    "request_id": envelope.get("request_id"),
                    "uploaded_at": beijing_now(),
                    "receiver": response,
                }
                atomic_json(SENT_DIR / path.name, receipt)
                path.unlink(missing_ok=True)
                sent += 1
                active_logger.info(
                    "远端数据已上传主机：request_id=%s status=%s",
                    str(envelope.get("request_id") or "")[:12],
                    response.get("status"),
                )
            except Exception as exc:
                failures += 1
                last_error = f"{type(exc).__name__}: {exc}"
                active_logger.warning(
                    "主机暂不可达，数据保留在离线队列：%s",
                    last_error,
                )
                break
        pending = len(list(OUTBOX_DIR.glob("*.json")))
        return {
            "enabled": True,
            "sent": sent,
            "pending": pending,
            "failures": failures,
            "last_error": last_error,
        }
    finally:
        try:
            os.close(lock_fd)
        except Exception:
            pass
        try:
            OUTBOX_LOCK.unlink()
        except FileNotFoundError:
            pass


def sync_agent_running() -> bool:
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.bind(("127.0.0.1", SYNC_AGENT_GUARD_PORT))
        return False
    except OSError:
        return True
    finally:
        probe.close()


def ensure_sync_agent_running() -> bool:
    config = load_config()
    if not config.get("enabled") or sync_agent_running():
        return False
    log_handle = SYNC_AGENT_LOG.open("ab")
    try:
        subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "--watch", "--interval", "5"],
            cwd=str(BASE_DIR),
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            creationflags=(
                subprocess.CREATE_NO_WINDOW
                if os.name == "nt" and hasattr(subprocess, "CREATE_NO_WINDOW")
                else 0
            ),
        )
    finally:
        log_handle.close()
    return True


def enqueue_for_background_upload(
    payload: dict[str, Any],
    target_logger: logging.Logger | None = None,
) -> dict[str, Any]:
    path, queued = enqueue(payload)
    if path is None:
        return queued
    ensure_sync_agent_running()
    return {
        **queued,
        "status": "queued_for_background_upload",
    }


def enqueue_and_flush(
    payload: dict[str, Any],
    target_logger: logging.Logger | None = None,
) -> dict[str, Any]:
    """Backward-compatible alias; network I/O is intentionally asynchronous."""
    return enqueue_for_background_upload(payload, target_logger)


def watch_outbox(interval: float = 5.0) -> int:
    guard = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        guard.bind(("127.0.0.1", SYNC_AGENT_GUARD_PORT))
    except OSError:
        logger.info("豆包回传同步代理已经运行，本进程退出。")
        guard.close()
        return 0
    logger.info("豆包回传同步代理已启动，检查间隔 %.1f 秒。", interval)
    try:
        while True:
            result = flush_outbox(max_items=100)
            if result.get("sent") or result.get("failures"):
                logger.info(
                    "回传检查：sent=%s pending=%s failures=%s error=%s",
                    result.get("sent", 0),
                    result.get("pending", 0),
                    result.get("failures", 0),
                    result.get("last_error", ""),
                )
            time.sleep(interval)
    except KeyboardInterrupt:
        return 0
    finally:
        guard.close()


def health_check() -> dict[str, Any]:
    config = load_config()
    receiver_urls = candidate_receiver_urls(config)
    if not receiver_urls:
        return {"ok": False, "error": "receiver_url 未配置"}
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    failures: list[str] = []
    for receiver_url in receiver_urls:
        try:
            with opener.open(
                receiver_url + "/api/v1/health",
                timeout=float(config.get("upload_timeout") or 10),
            ) as response:
                value = json.loads(response.read().decode("utf-8"))
            return {**value, "receiver_url": receiver_url}
        except (
            OSError,
            urllib.error.URLError,
            json.JSONDecodeError,
        ) as exc:
            failures.append(f"{receiver_url}: {exc}")
    return {"ok": False, "error": "；".join(failures)}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="上传远端豆包抓取离线队列。")
    parser.add_argument("--flush", action="store_true")
    parser.add_argument("--health", action="store_true")
    parser.add_argument("--watch", action="store_true")
    parser.add_argument("--interval", type=float, default=5.0)
    return parser.parse_args()


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    args = parse_args()
    if args.watch:
        return watch_outbox(min(300.0, max(1.0, args.interval)))
    if args.health:
        value = health_check()
        print(json.dumps(value, ensure_ascii=False, indent=2))
        return 0 if value.get("ok") else 2
    value = flush_outbox()
    print(json.dumps(value, ensure_ascii=False, indent=2))
    return 0 if not value.get("failures") else 2


if __name__ == "__main__":
    raise SystemExit(main())
