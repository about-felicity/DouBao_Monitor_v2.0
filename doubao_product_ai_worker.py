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
RETRY_STATE_PATH = BASE_DIR / "doubao_product_ai_retry_state.json"


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


def read_retry_state():
    try:
        data = json.loads(RETRY_STATE_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def write_retry_state(state):
    tmp = RETRY_STATE_PATH.with_suffix(RETRY_STATE_PATH.suffix + ".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(RETRY_STATE_PATH)


def pending_rows():
    if not os.path.exists(saver.OUT_ANSWERS_CSV):
        return []
    try:
        with open(saver.OUT_ANSWERS_CSV, "r", encoding="utf-8-sig", newline="") as f:
            rows = [
                {
                    key: (value.replace("\x00", "") if isinstance(value, str) else value)
                    for key, value in row.items()
                }
                for row in csv.DictReader(f)
                if row.get("review_status") == "ai_pending"
            ]
    except Exception:
        return []
    deduped = {}
    for row in rows:
        key = str(row.get("answer_hash") or row.get("run_no") or "")
        deduped[key] = row

    def run_number(row):
        try:
            return int(str(row.get("run_no") or "0"))
        except (TypeError, ValueError):
            return 0

    return sorted(
        deduped.values(),
        key=run_number,
    )


def retry_key(row):
    return str(row.get("answer_hash") or row.get("run_no") or "")


def eligible_pending_rows(state, max_attempts):
    now = time.time()
    result = []
    for row in pending_rows():
        item = state.get(retry_key(row)) or {}
        if int(item.get("attempts") or 0) >= max_attempts:
            continue
        if float(item.get("next_retry_at") or 0) > now:
            continue
        result.append(row)
    return result


def current_review_status(row):
    run_no = str(row.get("run_no") or "")
    answer_hash = str(row.get("answer_hash") or "")
    if not os.path.exists(saver.OUT_ANSWERS_CSV):
        return ""
    try:
        with open(saver.OUT_ANSWERS_CSV, "r", encoding="utf-8-sig", newline="") as f:
            for current in csv.DictReader(f):
                if (
                    str(current.get("run_no") or "") == run_no
                    and str(current.get("answer_hash") or "") == answer_hash
                ):
                    return str(current.get("review_status") or "")
    except Exception:
        return ""
    return ""


def retry_delay(attempts):
    schedule = (15 * 60, 2 * 60 * 60, 12 * 60 * 60)
    return schedule[min(max(1, attempts), len(schedule)) - 1]


def pending_payload_archived(payload):
    """The capture is safe once its answer body is in the durable answer CSV.

    Product review may still be ai_pending; that is handled by this worker's
    normal review queue and must not keep the capture-save file blocked.
    """
    page_url = str(payload.get("url") or "").rstrip("/")
    extracted_at = str(payload.get("extractedAt") or "").strip()
    if not page_url or not extracted_at or not os.path.exists(saver.OUT_ANSWERS_CSV):
        return False
    # Saved rows normalize ISO timestamps such as
    # ``2026-07-28T20:57:52+08:00`` to a space-separated Beijing timestamp.
    # Compare the normalized value so a successfully archived capture is not
    # retried forever merely because the pending payload kept its ISO spelling.
    normalized_extracted_at = saver.beijing_time_str(extracted_at)
    try:
        with open(saver.OUT_ANSWERS_CSV, "r", encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                if (
                    str(row.get("page_url") or "").rstrip("/") == page_url
                    and saver.beijing_time_str(
                        str(row.get("extracted_at") or "").strip()
                    ) == normalized_extracted_at
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
            if pending_payload_archived(payload):
                finish_pending_save(
                    pending_path,
                    payload,
                    "answer already archived; skipped duplicate model call",
                )
                completed_count += 1
                continue
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
        batch_size = min(
            10,
            max(1, int(os.environ.get("DOUBAO_PRODUCT_AI_RETRY_BATCH", "10") or "10")),
        )
        retry_interval = max(30, int(os.environ.get("DOUBAO_PRODUCT_AI_RETRY_INTERVAL", "60") or "60"))
        max_attempts = max(1, int(os.environ.get("DOUBAO_PRODUCT_AI_MAX_RETRIES", "2") or "2"))
        env = os.environ.copy()
        env.setdefault("DOUBAO_PRODUCT_AI_MODEL", "deepseek-v4-flash")
        env.setdefault("DOUBAO_AI_PRODUCT_TIMEOUT", "20")
        # One API request per archived answer. Backoff is managed by this
        # worker, so the model function itself must never multiply retries.
        env["DOUBAO_PRODUCT_AI_ATTEMPTS"] = "1"
        log("watch start: batch=%s max_retries=%s model=%s" % (
            batch_size, max_attempts, env["DOUBAO_PRODUCT_AI_MODEL"]
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
            rows = pending_rows()
            count = len(rows)
            if count <= 0:
                if last_state != "idle":
                    log("watch idle: no pending product reviews")
                    last_state = "idle"
                time.sleep(retry_interval)
                continue

            state = read_retry_state()
            eligible = eligible_pending_rows(state, max_attempts)
            if not eligible:
                if last_state != "backoff":
                    log(
                        "watch backoff: pending=%s; no answer is due for retry"
                        % count
                    )
                    last_state = "backoff"
                time.sleep(retry_interval)
                continue

            last_state = "working"
            log("retry start: pending=%s eligible=%s batch=%s" % (
                count, len(eligible), batch_size
            ))
            outputs = []
            for row in eligible[:batch_size]:
                heartbeat_lock()
                key = retry_key(row)
                run_no = str(row.get("run_no") or "")
                try:
                    result = subprocess.run(
                        [
                            sys.executable,
                            str(REBUILD_SCRIPT),
                            "--pending-only",
                            "--run-no",
                            run_no,
                        ],
                        cwd=str(BASE_DIR),
                        env=env,
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        encoding="utf-8",
                        errors="ignore",
                        timeout=50,
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    )
                    status = current_review_status(row)
                    if result.returncode == 0 and status == "ai_verified":
                        state.pop(key, None)
                        outputs.append("run=%s verified" % run_no)
                        continue
                    item = state.get(key) or {}
                    attempts = int(item.get("attempts") or 0) + 1
                    if attempts >= max_attempts:
                        fallback_env = env.copy()
                        fallback_env["DOUBAO_PRODUCT_AI_MODE"] = "off"
                        subprocess.run(
                            [sys.executable, str(REBUILD_SCRIPT), "--run-no", run_no],
                            cwd=str(BASE_DIR),
                            env=fallback_env,
                            stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT,
                            timeout=30,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                        )
                        state.pop(key, None)
                        outputs.append(
                            "run=%s model_failed=%s finalized=rule_unverified"
                            % (run_no, attempts)
                        )
                    else:
                        state[key] = {
                            "attempts": attempts,
                            "next_retry_at": time.time() + retry_delay(attempts),
                            "run_no": run_no,
                            "updated_at": saver.now_str(),
                        }
                        outputs.append(
                            "run=%s failed=%s next_retry_minutes=%s"
                            % (run_no, attempts, retry_delay(attempts) // 60)
                        )
                except subprocess.TimeoutExpired:
                    item = state.get(key) or {}
                    attempts = int(item.get("attempts") or 0) + 1
                    if attempts >= max_attempts:
                        fallback_env = env.copy()
                        fallback_env["DOUBAO_PRODUCT_AI_MODE"] = "off"
                        subprocess.run(
                            [sys.executable, str(REBUILD_SCRIPT), "--run-no", run_no],
                            cwd=str(BASE_DIR),
                            env=fallback_env,
                            stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT,
                            timeout=30,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                        )
                        state.pop(key, None)
                        outputs.append(
                            "run=%s timeout=%s finalized=rule_unverified"
                            % (run_no, attempts)
                        )
                    else:
                        state[key] = {
                            "attempts": attempts,
                            "next_retry_at": time.time() + retry_delay(attempts),
                            "run_no": run_no,
                            "updated_at": saver.now_str(),
                        }
                        outputs.append("run=%s timeout=%s" % (run_no, attempts))
                write_retry_state(state)
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
