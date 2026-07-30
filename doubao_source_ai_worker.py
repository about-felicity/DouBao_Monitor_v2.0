import csv
import json
import os
import time
from pathlib import Path
from urllib.parse import urlparse

import doubao_env_loader  # noqa: F401  loads API keys from local .env file
import save_doubao_refs as saver


BASE_DIR = Path(__file__).resolve().parent
CSV_PATH = BASE_DIR / "doubao_refs_result.csv"
AI_CACHE_PATH = BASE_DIR / "doubao_source_ai_cache.json"
LOCK_PATH = BASE_DIR / "doubao_source_ai_worker.lock"
LOG_PATH = BASE_DIR / "doubao_source_ai_worker.log"
RETRY_STATE_PATH = BASE_DIR / "doubao_source_ai_retry_state.json"


def log(message):
    try:
        with LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(saver.now_str() + " " + str(message) + "\n")
    except Exception:
        pass


def host_of(url):
    return urlparse(url or "").netloc.lower().split(":")[0]


def read_json(path):
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def write_json(path, data):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def collect_samples():
    samples = {}
    if not CSV_PATH.exists():
        return samples
    with CSV_PATH.open("r", encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            href = row.get("href") or ""
            host = host_of(href)
            if not host:
                continue
            current = samples.get(host)
            if not current or len(row.get("title") or "") > len(current.get("title") or ""):
                samples[host] = row
    return samples


def needs_ai(host, row, cache, retry_state=None, max_attempts=2):
    retry_state = retry_state or {}
    state = retry_state.get(host) or {}
    if int(state.get("attempts") or 0) >= max_attempts:
        return False
    if float(state.get("next_retry_at") or 0) > time.time():
        return False
    cached = cache.get(host)
    if not cached:
        return True
    note = str(cached.get("note") or "")
    media = str(cached.get("media") or "")
    if "AI timeout fallback" in note:
        return True
    if saver.is_weak_media_name(media, host):
        return True
    # Re-classify if body content became available after the last run.
    href = row.get("href") or ""
    content_text = saver.get_source_content_text(href, max_chars=1)
    had_content = bool(cached.get("had_content_text"))
    if content_text and not had_content:
        return True
    return False


def retry_delay(attempts):
    return (30 * 60, 6 * 60 * 60)[min(max(1, attempts), 2) - 1]


def classify_one(host, row):
    href = row.get("href") or ""
    meta = saver.get_source_meta(href)
    if row.get("title") and not meta.get("title"):
        meta["title"] = row.get("title", "")
    result = saver.call_anthropic_source_classifier(href, meta) or saver.call_openai_source_classifier(href, meta)
    if isinstance(result, dict):
        result["had_content_text"] = bool(meta.get("content_text"))
    return result


def lock_is_stale():
    if not LOCK_PATH.exists():
        return False
    try:
        pid = int(LOCK_PATH.read_text(encoding="utf-8").strip())
        return (
            not saver.process_is_running(pid)
            or time.time() - LOCK_PATH.stat().st_mtime > 30 * 60
        )
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
        if LOCK_PATH.exists():
            LOCK_PATH.unlink()
    except Exception:
        pass


def main():
    if not acquire_lock():
        log("skip: worker already running")
        return

    try:
        os.environ.setdefault("DOUBAO_AI_TIMEOUT", "12")
        os.environ.setdefault("DOUBAO_META_TIMEOUT", "3")
        if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("OPENAI_API_KEY")):
            log("skip: missing API key")
            return

        max_hosts = min(
            5,
            max(1, int(os.environ.get("DOUBAO_AI_WORKER_MAX_HOSTS", "5") or "5")),
        )
        max_attempts = max(1, int(os.environ.get("DOUBAO_SOURCE_AI_MAX_RETRIES", "2") or "2"))
        samples = collect_samples()
        cache = read_json(AI_CACHE_PATH)
        retry_state = read_json(RETRY_STATE_PATH)
        pending = [
            (host, row)
            for host, row in sorted(samples.items())
            if needs_ai(host, row, cache, retry_state, max_attempts)
        ]

        log("start: pending=%s max=%s" % (len(pending), max_hosts))
        done = 0
        failed = 0
        for host, row in pending[:max_hosts]:
            try:
                result = classify_one(host, row)
                if result:
                    cache[host] = result
                    write_json(AI_CACHE_PATH, cache)
                    retry_state.pop(host, None)
                    write_json(RETRY_STATE_PATH, retry_state)
                    done += 1
                    log("done: %s => %s | %s" % (host, result.get("source_type"), result.get("media")))
                else:
                    failed += 1
                    state = retry_state.get(host) or {}
                    attempts = int(state.get("attempts") or 0) + 1
                    retry_state[host] = {
                        "attempts": attempts,
                        "next_retry_at": time.time() + retry_delay(attempts),
                        "updated_at": saver.now_str(),
                    }
                    if attempts >= max_attempts:
                        href = row.get("href") or ""
                        meta = saver.get_source_meta(href)
                        fallback = saver.fallback_ai_source_result(href, meta)
                        fallback["note"] = "bounded deterministic fallback after model failures"
                        cache[host] = fallback
                        write_json(AI_CACHE_PATH, cache)
                    write_json(RETRY_STATE_PATH, retry_state)
                    log(
                        "failed: %s attempt=%s next_retry_minutes=%s"
                        % (host, attempts, retry_delay(attempts) // 60)
                    )
            except Exception as exc:
                failed += 1
                state = retry_state.get(host) or {}
                attempts = int(state.get("attempts") or 0) + 1
                retry_state[host] = {
                    "attempts": attempts,
                    "next_retry_at": time.time() + retry_delay(attempts),
                    "updated_at": saver.now_str(),
                }
                if attempts >= max_attempts:
                    href = row.get("href") or ""
                    meta = saver.get_source_meta(href)
                    fallback = saver.fallback_ai_source_result(href, meta)
                    fallback["note"] = "bounded deterministic fallback after worker errors"
                    cache[host] = fallback
                    write_json(AI_CACHE_PATH, cache)
                write_json(RETRY_STATE_PATH, retry_state)
                log("error: %s | %r" % (host, exc))

        log("finish: done=%s failed=%s remaining=%s" % (done, failed, max(0, len(pending) - max_hosts)))
    finally:
        release_lock()


if __name__ == "__main__":
    main()
