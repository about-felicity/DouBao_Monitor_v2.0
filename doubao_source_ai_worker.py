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


def needs_ai(host, row, cache):
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
        return time.time() - LOCK_PATH.stat().st_mtime > 30 * 60
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

        max_hosts = int(os.environ.get("DOUBAO_AI_WORKER_MAX_HOSTS", "20") or "20")
        samples = collect_samples()
        cache = read_json(AI_CACHE_PATH)
        pending = [(host, row) for host, row in sorted(samples.items()) if needs_ai(host, row, cache)]

        log("start: pending=%s max=%s" % (len(pending), max_hosts))
        done = 0
        failed = 0
        for host, row in pending[:max_hosts]:
            try:
                result = classify_one(host, row)
                if result:
                    cache[host] = result
                    write_json(AI_CACHE_PATH, cache)
                    done += 1
                    log("done: %s => %s | %s" % (host, result.get("source_type"), result.get("media")))
                else:
                    failed += 1
                    log("failed: " + host)
            except Exception as exc:
                failed += 1
                log("error: %s | %r" % (host, exc))

        log("finish: done=%s failed=%s remaining=%s" % (done, failed, max(0, len(pending) - max_hosts)))
    finally:
        release_lock()


if __name__ == "__main__":
    main()
