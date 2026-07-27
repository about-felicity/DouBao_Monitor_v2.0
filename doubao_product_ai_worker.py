"""Retry pending Doubao product reviews outside the foreground capture path."""

import csv
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import doubao_env_loader  # noqa: F401  loads API keys from local .env file
import save_doubao_refs as saver


BASE_DIR = Path(__file__).resolve().parent
LOCK_PATH = BASE_DIR / "doubao_product_ai_worker.lock"
LOG_PATH = BASE_DIR / "doubao_product_ai_worker.log"
REBUILD_SCRIPT = BASE_DIR / "rebuild_doubao_products_from_answers.py"
SAVE_SCRIPT = BASE_DIR / "save_doubao_refs.py"
PENDING_SAVE_DIR = BASE_DIR / "doubao_pending_saves"


def log(message):
    try:
        with LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(saver.now_str() + " " + str(message) + "\n")
    except Exception:
        pass


def lock_is_stale():
    try:
        if not LOCK_PATH.exists():
            return False
        if time.time() - LOCK_PATH.stat().st_mtime > 30 * 60:
            return True
        pid = int(LOCK_PATH.read_text(encoding="utf-8").strip())
        # os.kill(pid, 0) is not a reliable existence check on every bundled
        # Windows Python. Use the same native process check as the CSV writer.
        return not saver.process_is_running(pid)
    except Exception:
        return True


def acquire_lock():
    if LOCK_PATH.exists() and not lock_is_stale():
        return False
    try:
        LOCK_PATH.write_text(str(os.getpid()), encoding="utf-8")
        return True
    except Exception:
        return False


def release_lock():
    try:
        # ShadowBot's bundled Python can be older than 3.8, where
        # Path.unlink(missing_ok=...) is unsupported.  The old call silently
        # failed here and left a lock behind after every completed batch.
        if LOCK_PATH.exists():
            LOCK_PATH.unlink()
    except Exception:
        log("warning: could not remove worker lock")


def heartbeat_lock():
    try:
        LOCK_PATH.write_text(str(os.getpid()), encoding="utf-8")
    except Exception:
        pass


def dashboard_is_running():
    host = os.environ.get("DOUBAO_DASHBOARD_HOST", "127.0.0.1")
    port = int(os.environ.get("DOUBAO_DASHBOARD_PORT", "8765") or "8765")
    try:
        sock = socket.create_connection((host, port), timeout=2)
        sock.close()
        return True
    except Exception:
        return False


def pending_count():
    if not os.path.exists(saver.OUT_ANSWERS_CSV):
        return 0
    try:
        with open(saver.OUT_ANSWERS_CSV, "r", encoding="utf-8-sig", newline="") as f:
            return sum(1 for row in csv.DictReader(f) if row.get("review_status") == "ai_pending")
    except Exception:
        return 0


def pending_payload_archived(payload):
    """The capture is safe once its answer body is in the durable answer CSV.

    Product review may still be ai_pending; that is handled by this worker's
    normal review queue and must not keep the capture-save file blocked.
    """
    page_url = str(payload.get("url") or "").rstrip("/")
    extracted_at = str(payload.get("extractedAt") or "").strip()
    if not page_url or not extracted_at or not os.path.exists(saver.OUT_ANSWERS_CSV):
        return False
    try:
        with open(saver.OUT_ANSWERS_CSV, "r", encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                if (
                    str(row.get("page_url") or "").rstrip("/") == page_url
                    and str(row.get("extracted_at") or "").strip() == extracted_at
                    and str(row.get("answer_text") or "").strip()
                ):
                    return True
    except Exception:
        return False
    return False


def finish_pending_save(pending_path, payload, message):
    try:
        pending_path.unlink()
    except FileNotFoundError:
        pass
    try:
        import run_doubao_latest_grab as grabber
        grabber.resolve_capture_skip(payload.get("url"))
    except Exception as exc:
        log("pending save skip resolve warning: " + repr(exc))
    log("pending save recovered: file=%s %s" % (pending_path.name, message))


def drain_pending_saves(env, limit, timeout_seconds=45):
    """Retry captures whose foreground CSV save was blocked by a writer."""
    if not PENDING_SAVE_DIR.exists():
        return 0
    completed_count = 0
    try:
        pending_files = sorted(PENDING_SAVE_DIR.glob("*.json"), key=lambda path: path.stat().st_mtime)
    except Exception as exc:
        log("pending save scan failed: " + repr(exc))
        return 0

    for pending_path in pending_files[:max(1, limit)]:
        try:
            raw_payload = pending_path.read_text(encoding="utf-8")
            payload = json.loads(raw_payload)
            save_env = env.copy()
            # Archive the exact source payload first.  One short model attempt
            # is enough here: on timeout the answer remains ai_pending and the
            # normal review queue will finish it later.
            save_env["DOUBAO_PRODUCT_AI_ATTEMPTS"] = "1"
            save_env["DOUBAO_AI_PRODUCT_TIMEOUT"] = "20"
            result = subprocess.run(
                [sys.executable, str(SAVE_SCRIPT), raw_payload],
                cwd=str(BASE_DIR), env=save_env, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                encoding="utf-8", errors="ignore", timeout=timeout_seconds,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if result.returncode != 0:
                if pending_payload_archived(payload):
                    finish_pending_save(pending_path, payload, "answer archived; product review queued")
                    completed_count += 1
                    continue
                log(
                    "pending save retry failed: file=%s return=%s output=%s"
                    % (pending_path.name, result.returncode, result.stdout.strip()[-300:])
                )
                continue

            finish_pending_save(pending_path, payload, "save process completed")
            completed_count += 1
        except subprocess.TimeoutExpired:
            if pending_payload_archived(payload):
                finish_pending_save(pending_path, payload, "answer archived before model timeout; review queued")
                completed_count += 1
            else:
                log("pending save retry timed out: file=%s" % pending_path.name)
        except Exception as exc:
            log("pending save retry error: file=%s error=%r" % (pending_path.name, exc))
    return completed_count


def main():
    if not acquire_lock():
        log("skip: worker already running")
        return
    try:
        if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("OPENAI_API_KEY")):
            log("skip: missing API key")
            return
        batch_size = max(1, int(os.environ.get("DOUBAO_PRODUCT_AI_RETRY_BATCH", "5") or "5"))
        worker_count = max(1, int(os.environ.get("DOUBAO_PRODUCT_AI_WORKERS", "3") or "3"))
        retry_interval = max(10, int(os.environ.get("DOUBAO_PRODUCT_AI_RETRY_INTERVAL", "30") or "30"))
        env = os.environ.copy()
        env.setdefault("DOUBAO_PRODUCT_AI_MODEL", "deepseek-v4-flash")
        env.setdefault("DOUBAO_AI_PRODUCT_TIMEOUT", "20")
        # Background retries can make several clean attempts without slowing
        # down the foreground ShadowBot capture.
        env.setdefault("DOUBAO_PRODUCT_AI_ATTEMPTS", "3")
        log("watch start: workers=%s batch_per_worker=%s model=%s" % (
            worker_count, batch_size, env["DOUBAO_PRODUCT_AI_MODEL"]
        ))
        last_state = None
        while dashboard_is_running():
            heartbeat_lock()
            # A deferred capture already contains the irreplaceable answer and
            # source links.  Persist one on every pass even while fresh product
            # reviews arrive, otherwise continuous ShadowBot runs can starve
            # the capture recovery queue indefinitely.
            recovered = drain_pending_saves(env, 1, timeout_seconds=45)
            if recovered:
                log("pending source save prioritized: count=%s" % recovered)
            count = pending_count()
            if count <= 0:
                if last_state != "idle":
                    log("watch idle: no pending product reviews")
                    last_state = "idle"
                time.sleep(retry_interval)
                continue

            last_state = "working"
            log("retry start: pending=%s workers=%s batch_per_worker=%s" % (
                count, worker_count, batch_size
            ))
            attempts = max(1, int(env.get("DOUBAO_PRODUCT_AI_ATTEMPTS", "3") or "3"))
            timeout = max(180, batch_size * attempts * 45)
            processes = []
            for shard_index in range(worker_count):
                command = [
                    sys.executable, str(REBUILD_SCRIPT),
                    "--pending-only", "--limit", str(batch_size),
                    "--shard-count", str(worker_count),
                    "--shard-index", str(shard_index),
                ]
                processes.append((shard_index, subprocess.Popen(
                    command, cwd=str(BASE_DIR), env=env, stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )))

            deadline = time.time() + timeout
            while any(process.poll() is None for _, process in processes) and time.time() < deadline:
                heartbeat_lock()
                time.sleep(0.5)

            outputs = []
            for shard_index, process in processes:
                if process.poll() is None:
                    process.kill()
                    outputs.append("shard=%s timeout" % shard_index)
                    continue
                raw_output = process.communicate()[0] or b""
                if not isinstance(raw_output, str):
                    raw_output = raw_output.decode("utf-8", errors="ignore")
                outputs.append(
                    "shard=%s return=%s output=%s"
                    % (shard_index, process.returncode, raw_output.strip()[-300:])
                )
            remaining = pending_count()
            log("retry finish: remaining=%s %s" % (remaining, " | ".join(outputs)))
            time.sleep(retry_interval)
        log("watch stop: dashboard service is not running")
    except Exception as exc:
        log("error: " + repr(exc))
    finally:
        release_lock()


if __name__ == "__main__":
    main()
