from __future__ import annotations

import argparse
import hashlib
import json
import logging
import random
import re
import signal
import shutil
import sqlite3
import subprocess
import sys
import time
import unicodedata
from datetime import datetime
from pathlib import Path

try:
    from .controller import WenxinWebCollector
except ImportError:  # Direct script execution on remote collection hosts.
    from controller import WenxinWebCollector

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from monitor_core.quality import answer_quality_reason
from monitor_core.recommendation_questions import canonical_recommendation_question, validate_prompt_list
from monitor_core.scheduling import build_question_schedule
from monitor_core.lan_result_sync import enqueue as enqueue_remote_result

BASE_DIR = Path(__file__).resolve().parent
CARD_HISTORY_PATH = BASE_DIR.parent / "runtime" / "remote_workers" / "wenxin_card_history.sqlite3"
BAIDU_CARD_DAILY_ROUNDS = 5
STOP = False


def security_backoff_seconds(streak: int, retry_wait: float) -> int:
    """Return a bounded cooldown for consecutive Baidu verification pages."""
    base = max(180, int(retry_wait))
    return min(900, base * (2 ** max(0, min(int(streak) - 1, 3))))


def should_retry_baidu_sources(
    source_capture_complete: bool,
    attempt_number: int,
    retry_limit: int,
) -> bool:
    """Retry a source-less Baidu card variant, but preserve its body at the limit."""
    return not bool(source_capture_complete) and max(1, int(attempt_number)) < max(
        1, int(retry_limit)
    )


def interruptible_sleep(seconds: float) -> bool:
    """Sleep in short steps so panel stop requests remain responsive."""
    deadline = time.monotonic() + max(0, float(seconds))
    while not STOP and time.monotonic() < deadline:
        time.sleep(min(1, max(0, deadline - time.monotonic())))
    return not STOP


def resume_schedule_window(
    index: int,
    schedule_size: int,
    restart_completed: bool,
    state: dict | None = None,
) -> tuple[int, int]:
    """Restore the active batch even when unfinished deferred work exists."""
    size = max(1, int(schedule_size))
    current = max(0, int(index))
    saved = state or {}
    saved_origin = max(0, int(saved.get("schedule_origin") or 0))
    saved_end = max(0, int(saved.get("target_end_index") or 0))
    if saved_end - saved_origin == size and saved_origin <= current < saved_end:
        return saved_origin, saved_end
    if restart_completed and current >= size:
        origin = (current // size) * size
        return origin, origin + size
    return 0, size


def deferred_due_index(deferred: list[dict], index: int) -> int | None:
    """Return one due deferred item without letting it monopolize the queue."""
    current = max(0, int(index))
    for position, item in enumerate(deferred):
        if max(0, int(item.get("retry_after_index") or 0)) <= current:
            return position
    return None


def prune_rotated_profiles(
    base_profile: Path,
    *,
    keep: int = 2,
    protected: tuple[Path, ...] = (),
) -> list[Path]:
    """Remove stale browser-profile rotations while preserving active profiles."""
    base_profile = Path(base_profile)
    protected_paths = {Path(path).resolve() for path in protected}
    candidates = sorted(
        (
            path
            for path in base_profile.parent.glob(f"{base_profile.name}_rotated_*")
            if path.is_dir() and path.resolve() not in protected_paths
        ),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    removed: list[Path] = []
    for path in candidates[max(0, keep):]:
        try:
            shutil.rmtree(path)
        except OSError:
            continue
        removed.append(path)
    return removed


def now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def append(path: Path, row: dict) -> None:
    row = {**row, "collector_model": "wenxin"}
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    enqueue_remote_result("wenxin", row)


def answer_fingerprint(question: str, answer: str) -> str:
    """Return a diagnostic content fingerprint, never an observation identity."""
    compact = re.sub(
        r"[\u200b-\u200f\u202a-\u202e\u2060\ufeff\s]+", "",
        f"{question}\0{answer}",
    )
    return hashlib.sha256(compact.encode("utf-8")).hexdigest()


def _normalized_card_fingerprint(answer: str) -> str:
    normalized = unicodedata.normalize("NFKC", str(answer or ""))
    compact = re.sub(r"[\u200b-\u200f\u202a-\u202e\u2060\ufeff\s]+", "", normalized)
    return hashlib.sha256(compact.encode("utf-8")).hexdigest()


def repeated_search_card(
    question: str,
    answer: str,
    *,
    history_path: Path = CARD_HISTORY_PATH,
    natural_day: str | None = None,
) -> bool:
    """Atomically record a Baidu search card and report whether it repeats.

    The SQLite file is shared by every Wenxin worker process, so the second of
    up to four parallel tasks sees the first task's card immediately.  History
    is scoped to a natural day: each question may use a newly observed card
    once per day, but every subsequent identical card is sent to Wenxin.
    """
    day = natural_day or datetime.now().astimezone().date().isoformat()
    canonical_question = canonical_recommendation_question(question) or str(question).strip()
    fingerprint = _normalized_card_fingerprint(answer)
    history_path = Path(history_path)
    history_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(history_path, timeout=30, isolation_level=None)
    try:
        connection.execute("PRAGMA busy_timeout=30000")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS search_card_history (
                natural_day TEXT NOT NULL,
                question TEXT NOT NULL,
                body_fingerprint TEXT NOT NULL,
                seen_count INTEGER NOT NULL DEFAULT 1,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                PRIMARY KEY (natural_day, question)
            )
            """
        )
        timestamp = now()
        connection.execute("BEGIN IMMEDIATE")
        existing = connection.execute(
            "SELECT body_fingerprint FROM search_card_history WHERE natural_day = ? AND question = ?",
            (day, canonical_question),
        ).fetchone()
        if existing is None:
            connection.execute(
                """
                INSERT INTO search_card_history
                    (natural_day, question, body_fingerprint, seen_count, first_seen_at, last_seen_at)
                VALUES (?, ?, ?, 1, ?, ?)
                """,
                (day, canonical_question, fingerprint, timestamp, timestamp),
            )
            repeated = False
        elif str(existing[0]) == fingerprint:
            connection.execute(
                """
                UPDATE search_card_history
                SET seen_count = seen_count + 1, last_seen_at = ?
                WHERE natural_day = ? AND question = ?
                """,
                (timestamp, day, canonical_question),
            )
            repeated = True
        else:
            connection.execute(
                """
                UPDATE search_card_history
                SET body_fingerprint = ?, seen_count = 1, first_seen_at = ?, last_seen_at = ?
                WHERE natural_day = ? AND question = ?
                """,
                (fingerprint, timestamp, timestamp, day, canonical_question),
            )
            repeated = False
        connection.execute("COMMIT")
        return repeated
    except Exception:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
    finally:
        connection.close()


def replace_repeated_search_card(
    web: WenxinWebCollector,
    prompt: str,
    result: dict,
    timeout: int,
    *,
    history_path: Path = CARD_HISTORY_PATH,
    natural_day: str | None = None,
) -> tuple[dict, dict | None]:
    """Replace a repeated Baidu AI card with a fresh Wenxin-page answer."""
    if str(result.get("capture_mode") or "") != "baidu_search_ai":
        return result, None
    if not repeated_search_card(
        prompt,
        str(result.get("body") or ""),
        history_path=history_path,
        natural_day=natural_day,
    ):
        return result, None
    tab_change = web.reset_after_round()
    fallback = web.collect_wenxin_search(prompt, timeout=max(45, int(timeout)))
    return fallback, tab_change


def reserve_unique_observation(
    question: str,
    answer: str,
    *,
    history_path: Path = CARD_HISTORY_PATH,
    natural_day: str | None = None,
) -> bool:
    """Reserve one exact answer per question/day across all capture modes.

    Search-card fallback can itself return the same cached Wenxin result. Such
    a page is valid evidence once, but it is not a new independent observation
    on subsequent rounds and must never inflate rates or sample counts.
    """
    day = natural_day or datetime.now().astimezone().date().isoformat()
    canonical_question = canonical_recommendation_question(question) or str(question).strip()
    fingerprint = _normalized_card_fingerprint(answer)
    history_path = Path(history_path)
    history_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(history_path, timeout=30, isolation_level=None)
    try:
        connection.execute("PRAGMA busy_timeout=30000")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS final_observation_history (
                natural_day TEXT NOT NULL,
                question TEXT NOT NULL,
                body_fingerprint TEXT NOT NULL,
                reserved_at TEXT NOT NULL,
                PRIMARY KEY (natural_day, question, body_fingerprint)
            )
            """
        )
        cursor = connection.execute(
            """
            INSERT OR IGNORE INTO final_observation_history
                (natural_day, question, body_fingerprint, reserved_at)
            VALUES (?, ?, ?, ?)
            """,
            (day, canonical_question, fingerprint, now()),
        )
        return cursor.rowcount == 1
    finally:
        connection.close()


def _ensure_baidu_attempt_table(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS baidu_card_daily_attempts (
            natural_day TEXT NOT NULL,
            question TEXT NOT NULL,
            attempt_no INTEGER NOT NULL,
            outcome TEXT NOT NULL,
            body_fingerprint TEXT NOT NULL DEFAULT '',
            recorded_at TEXT NOT NULL,
            PRIMARY KEY (natural_day, question, attempt_no)
        )
        """
    )


def baidu_card_attempt_count(
    question: str,
    *,
    history_path: Path = CARD_HISTORY_PATH,
    natural_day: str | None = None,
) -> int:
    """Return completed Baidu-card probes for one question and natural day."""
    day = natural_day or datetime.now().astimezone().date().isoformat()
    canonical_question = canonical_recommendation_question(question) or str(question).strip()
    history_path = Path(history_path)
    history_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(history_path, timeout=30)
    try:
        _ensure_baidu_attempt_table(connection)
        row = connection.execute(
            "SELECT COUNT(*) FROM baidu_card_daily_attempts WHERE natural_day=? AND question=?",
            (day, canonical_question),
        ).fetchone()
        return int(row[0] or 0)
    finally:
        connection.close()


def record_baidu_card_attempt(
    question: str,
    outcome: str,
    answer: str = "",
    *,
    history_path: Path = CARD_HISTORY_PATH,
    natural_day: str | None = None,
    maximum: int = BAIDU_CARD_DAILY_ROUNDS,
) -> int:
    """Atomically record one completed daily probe and return its attempt number."""
    day = natural_day or datetime.now().astimezone().date().isoformat()
    canonical_question = canonical_recommendation_question(question) or str(question).strip()
    history_path = Path(history_path)
    history_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(history_path, timeout=30, isolation_level=None)
    try:
        connection.execute("PRAGMA busy_timeout=30000")
        _ensure_baidu_attempt_table(connection)
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT COUNT(*) FROM baidu_card_daily_attempts WHERE natural_day=? AND question=?",
            (day, canonical_question),
        ).fetchone()
        completed = int(row[0] or 0)
        if completed >= max(1, int(maximum)):
            connection.execute("COMMIT")
            return completed
        attempt_no = completed + 1
        connection.execute(
            """
            INSERT INTO baidu_card_daily_attempts
                (natural_day,question,attempt_no,outcome,body_fingerprint,recorded_at)
            VALUES(?,?,?,?,?,?)
            """,
            (
                day,
                canonical_question,
                attempt_no,
                str(outcome or "unknown"),
                _normalized_card_fingerprint(answer) if answer else "",
                now(),
            ),
        )
        connection.execute("COMMIT")
        return attempt_no
    except Exception:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
    finally:
        connection.close()


def seed_baidu_card_attempts(
    results_path: Path,
    *,
    history_path: Path = CARD_HISTORY_PATH,
    natural_day: str | None = None,
    maximum: int = BAIDU_CARD_DAILY_ROUNDS,
) -> None:
    """Recover today's quota from durable JSONL after upgrades or state loss."""
    day = natural_day or datetime.now().astimezone().date().isoformat()
    path = Path(results_path)
    if not path.exists():
        return
    recovered: dict[str, list[tuple[str, str]]] = {}
    with path.open("r", encoding="utf-8-sig", errors="replace") as handle:
        for line in handle:
            try:
                row = json.loads(line)
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            row_day = str(row.get("day") or row.get("finished_at") or row.get("started_at") or "")[:10]
            if row_day != day or str(row.get("capture_mode") or "") != "baidu_search_ai":
                continue
            question = canonical_recommendation_question(row.get("question") or row.get("prompt"))
            if not question:
                continue
            # Before the 2026-09-15 collector fix, a complete Baidu-card body
            # was discarded as ``capture_failed`` whenever Baidu changed only
            # its citation markup.  Do not resurrect those obsolete quota rows
            # after an upgrade; the question must receive fresh probes under
            # the body/source split-completeness policy.
            legacy_warning = str(row.get("capture_warning") or row.get("skip_reason") or "")
            if str(row.get("status") or "") == "capture_failed" and (
                "百度 AI 卡片未加载信源" in legacy_warning
                or "百度 AI 卡片信源不完整" in legacy_warning
                or "信源抓取不完整" in legacy_warning
            ):
                continue
            recovered.setdefault(question, []).append((
                str(row.get("status") or "success"),
                str(row.get("web_body") or row.get("reply") or ""),
            ))
    for question, observations in recovered.items():
        existing = baidu_card_attempt_count(question, history_path=history_path, natural_day=day)
        for outcome, answer in observations[existing:max(1, int(maximum))]:
            record_baidu_card_attempt(
                question, outcome, answer,
                history_path=history_path, natural_day=day, maximum=maximum,
            )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--questions-file", default=str(BASE_DIR / "product.txt"))
    parser.add_argument("--rounds-per-question", type=int, default=1)
    parser.add_argument("--question-mode", choices=("interleaved", "sequential"), default="interleaved")
    parser.add_argument("--chrome-port", type=int, default=9444)
    parser.add_argument("--chrome-profile", default=str(BASE_DIR / "chrome_profile"))
    parser.add_argument("--startup-delay", type=float, default=0)
    parser.add_argument("--task-id", type=int, default=1)
    parser.add_argument("--wait", type=float, default=30)
    parser.add_argument("--random-wait", type=float, default=90)
    parser.add_argument("--retry-wait", type=float, default=60)
    parser.add_argument("--max-retries", type=int, default=3,
                        help="单题连续失败后延期的次数；0 表示持续重试同一题")
    parser.add_argument("--baidu-capture-retries", type=int, default=5,
                        help="百度卡片单轮抓取失败时最多尝试次数")
    parser.add_argument("--max-deferred-cycles", type=int, default=3,
                        help="延期题在同一批次最多重试周期，防止单题永久阻塞")
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--baidu-card-daily-rounds", type=int, default=BAIDU_CARD_DAILY_ROUNDS,
                        help="每个问题每天独立采集百度搜索 AI 卡片的完成轮数")
    parser.add_argument("--results", default=str(BASE_DIR / "wenxin_results.jsonl"))
    parser.add_argument("--state", default=str(BASE_DIR / "wenxin_state.json"))
    parser.add_argument("--log", default=str(BASE_DIR / "wenxin_loop.log"))
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--restart-completed", action="store_true",
                        help="断点中的有限批次已完成时，保留全局轮次并开启下一批")
    args = parser.parse_args()
    active_profile = Path(args.chrome_profile)
    logging.basicConfig(level=logging.INFO,
                        format=f"%(asctime)s [%(levelname)s] [任务 {max(1, args.task_id)}] %(message)s",
                        handlers=[logging.StreamHandler(), logging.FileHandler(args.log, mode="w", encoding="utf-8")],
                        force=True)
    log = logging.getLogger("wenxin")
    removed_profiles = prune_rotated_profiles(active_profile)
    if removed_profiles:
        log.info("已清理 %d 个过期 Scrapling 轮换档案", len(removed_profiles))
    raw = [line.strip() for line in Path(args.questions_file).read_text(encoding="utf-8-sig").splitlines()
           if line.strip() and not line.lstrip().startswith("#")]
    prompts = validate_prompt_list(raw)
    schedule = build_question_schedule(prompts, max(1, args.rounds_per_question), args.question_mode)
    baidu_daily_rounds = max(1, min(int(args.baidu_card_daily_rounds), 20))
    baidu_capture_retries = max(1, min(int(args.baidu_capture_retries), 10))
    seed_baidu_card_attempts(Path(args.results), maximum=baidu_daily_rounds)
    web = None
    state_path = Path(args.state)
    try:
        state = json.loads(state_path.read_text(encoding="utf-8")) if args.resume else {}
    except (OSError, ValueError):
        state = {}
    index = int(state.get("next_index") or 0)
    security_streak = max(0, int(state.get("security_streak") or 0))
    cooldown_until = max(0.0, float(state.get("cooldown_until") or 0))
    last_error = str(state.get("last_error") or "")
    deferred = []
    failed_deferred = list(state.get("failed_deferred") or [])[-100:]
    for item in state.get("deferred") or []:
        if isinstance(item, dict):
            slot = int(item.get("slot") or 0)
            prompt = str(item.get("prompt") or "").strip()
            retry_after_index = (
                max(0, int(item.get("retry_after_index")))
                if item.get("retry_after_index") is not None
                else index + len(prompts)
            )
            deferred_cycles = max(0, int(item.get("deferred_cycles") or 0))
        else:
            slot = 0
            prompt = str(item or "").strip()
            retry_after_index = index + len(prompts)
            deferred_cycles = 0
        if prompt:
            deferred.append({
                "slot": slot, "prompt": prompt,
                "retry_after_index": retry_after_index,
                "deferred_cycles": deferred_cycles,
            })
    schedule_origin, target_end_index = resume_schedule_window(
        index, len(schedule), args.restart_completed, state,
    )
    if schedule_origin:
        log.info(
            "已恢复持续采集批次：第 %d-%d 轮；%d 个延期题将在后续问题间轮转补抓",
            schedule_origin + 1, target_end_index, len(deferred),
        )

    def save_state() -> None:
        state_path.write_text(
            json.dumps({
                "next_index": index,
                "deferred": deferred,
                "failed_deferred": failed_deferred[-100:],
                "schedule_origin": schedule_origin,
                "target_end_index": target_end_index,
                "security_streak": security_streak,
                "cooldown_until": cooldown_until,
                "last_error": last_error,
                "updated_at": now(),
            },
            ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def stop(*_):
        global STOP
        STOP = True
    signal.signal(signal.SIGINT, stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, stop)

    if args.startup_delay > 0:
        log.info("双任务错峰启动，等待 %.1f 秒", args.startup_delay)
        time.sleep(args.startup_delay)

    while not STOP and (index < target_end_index or deferred):
        remaining_cooldown = int(max(0, cooldown_until - time.time()))
        if remaining_cooldown:
            log.warning("百度安全冷却中，剩余约 %d 秒；冷却结束后自动续跑", remaining_cooldown)
            if not interruptible_sleep(remaining_cooldown):
                break
        due_position = deferred_due_index(deferred, index)
        if args.restart_completed and index >= target_end_index and due_position is None:
            schedule_origin = target_end_index
            target_end_index = schedule_origin + len(schedule)
            log.info(
                "上一批主体已完成，延期题尚在退避；自动开启新批次：第 %d-%d 轮",
                schedule_origin + 1, target_end_index,
            )
            save_state()
        due_position = deferred_due_index(deferred, index)
        from_deferred = due_position is not None or index >= target_end_index
        deferred_position = due_position if due_position is not None else 0
        current_slot = int(deferred[deferred_position]["slot"]) if from_deferred else index
        prompt = str(deferred[deferred_position]["prompt"]) if from_deferred else schedule[(index - schedule_origin) % len(schedule)]
        attempts = 0
        started = now()
        saved_result = False
        while not STOP:
            baidu_completed = baidu_card_attempt_count(prompt)
            capture_mode = (
                "baidu_search_ai" if baidu_completed < baidu_daily_rounds
                else "baidu_wenxin_search"
            )
            capture_label = "百度卡片" if capture_mode == "baidu_search_ai" else "文心卡片"
            try:
                if web is None:
                    web = WenxinWebCollector(args.chrome_port, profile=active_profile)
                log.info(
                    "第 %d 轮%s%s生成：%s%s",
                    current_slot + 1,
                    "（延期补抓）" if from_deferred else "",
                    capture_label,
                    prompt,
                    f"（今日 {baidu_completed + 1}/{baidu_daily_rounds}）" if capture_mode == "baidu_search_ai" else "",
                )
                result = (
                    web.collect_baidu_search_card(prompt, args.timeout)
                    if capture_mode == "baidu_search_ai"
                    else web.collect_wenxin_search(prompt, timeout=max(45, args.timeout))
                )
                answer = str(result.get("body") or "")
                if capture_mode == "baidu_search_ai" and result.get("card_available") is False:
                    attempt_no = record_baidu_card_attempt(
                        prompt, "not_available", maximum=baidu_daily_rounds,
                    )
                    row = {
                        "status": "not_available", "skip_reason": "百度搜索未出现 AI 卡片",
                        "task_id": max(1, args.task_id), "round": current_slot + 1,
                        "serial": f"baidu-search-task-{max(1, args.task_id)}",
                        "prompt": prompt, "question": canonical_recommendation_question(prompt),
                        "reply": "", "web_body": "", "body_capture_complete": False,
                        "sources": [], "expected_source_count": 0,
                        "source_capture_complete": False, "citation_count": 0,
                        "capture_warning": str(result.get("capture_warning") or "百度搜索未出现 AI 卡片"),
                        "capture_mode": "baidu_search_ai", "capture_label": "百度卡片",
                        "card_available": False, "surface_attempt": attempt_no,
                        "daily_surface_target": baidu_daily_rounds,
                        "page_url": result.get("url"), "started_at": started, "finished_at": now(),
                    }
                    append(Path(args.results), row)
                    log.info(
                        "第 %d 轮完成百度卡片探测：%s｜今日 %d/%d｜未出现 AI 卡片，已记录供面板说明",
                        current_slot + 1, prompt, attempt_no, baidu_daily_rounds,
                    )
                else:
                    skip = answer_quality_reason(prompt, answer)
                    if skip:
                        raise RuntimeError(f"回答质量校验未通过：{skip}")
                    row = {"status": "success", "skip_reason": "", "task_id": max(1, args.task_id),
                           "round": current_slot + 1, "serial": f"baidu-search-task-{max(1, args.task_id)}", "prompt": prompt,
                           "question": canonical_recommendation_question(prompt), "reply": answer,
                           "web_body": answer,
                           "body_capture_complete": bool(result.get("body_capture_complete")),
                           "sources": result.get("sources", []),
                           "expected_source_count": result.get("expected_source_count", len(result.get("sources", []))),
                           "source_capture_complete": bool(result.get("source_capture_complete")),
                           "source_list_present": result.get("source_list_present"),
                           "capture_warning": str(result.get("capture_warning") or ""),
                           "citation_count": int(result.get("citation_count") or 0),
                           "page_navigation_id": str(result.get("page_navigation_id") or ""),
                           "capture_mode": capture_mode, "page_url": result.get("url"),
                           "started_at": started, "finished_at": now()}
                    row["capture_label"] = capture_label
                    if not row["body_capture_complete"]:
                        raise RuntimeError(
                            f"正文抓取不完整：{prompt}（正文 {len(answer)} 字）"
                        )
                    if capture_mode == "baidu_search_ai" and should_retry_baidu_sources(
                        row["source_capture_complete"],
                        attempts + 1,
                        baidu_capture_retries,
                    ):
                        raise RuntimeError(
                            f"百度卡片本次未暴露完整信源：{prompt}（取得 {len(row['sources'])}/"
                            f"{int(row['expected_source_count'] or 0)} 条）；换新页面重抓"
                        )
                    if (
                        capture_mode == "baidu_search_ai"
                        and not row["source_capture_complete"]
                        and attempts + 1 >= baidu_capture_retries
                    ):
                        suffix = f"已换页重抓 {baidu_capture_retries} 次，百度仍未暴露完整信源"
                        row["capture_warning"] = "；".join(
                            item for item in (row["capture_warning"], suffix) if item
                        )
                    if not row["source_capture_complete"] and capture_mode != "baidu_search_ai":
                        raise RuntimeError(
                            f"信源抓取不完整：{prompt}（取得 {len(row['sources'])}/"
                            f"{int(row['expected_source_count'] or 0)} 条）"
                        )
                    if not row["sources"] and capture_mode != "baidu_search_ai":
                        raise RuntimeError(f"信源抓取为空：{prompt}；拒绝保存零信源回答")
                    if capture_mode == "baidu_wenxin_search" and not reserve_unique_observation(prompt, answer):
                        raise RuntimeError(
                            "同日同问题文心卡片正文与已保存轮次完全相同；不是新的独立回答"
                        )
                    if capture_mode == "baidu_search_ai":
                        attempt_no = baidu_completed + 1
                        row.update({
                            "card_available": True,
                            "surface_attempt": attempt_no,
                            "daily_surface_target": baidu_daily_rounds,
                            "answer_fingerprint": answer_fingerprint(prompt, answer),
                        })
                    append(Path(args.results), row)
                    if capture_mode == "baidu_search_ai":
                        row["surface_attempt"] = record_baidu_card_attempt(
                            prompt, "success", answer, maximum=baidu_daily_rounds,
                        )
                    log.info(
                        "第 %d 轮完成：%s｜抓取方式：%s｜正文完整：%s（%d 字）｜信源完整：%s（%d/%d 条，引用图标 %d 个）%s",
                        current_slot + 1, prompt, capture_label,
                        "是" if row["body_capture_complete"] else "否", len(answer),
                        "是" if row["source_capture_complete"] else "否", len(row["sources"]),
                        int(row["expected_source_count"] or 0), int(row["citation_count"] or 0),
                        "｜重复正文保留，分析时合并" if capture_mode == "baidu_search_ai" else "",
                    )
                try:
                    tab_change = web.reset_after_round()
                    log.info(
                        "第 %d 轮已入队；已关闭旧页面 %s，并新建百度页面 %s",
                        current_slot + 1,
                        tab_change.get("old_target") or "unknown",
                        tab_change.get("new_target") or "unknown",
                    )
                except Exception as reset_exc:
                    # The result is already durably persisted and queued.  Do not retry the
                    # completed round or create a duplicate; rebuild the browser next round.
                    log.warning("第 %d 轮已入队，但页面轮换失败，下轮将重建浏览器：%s", current_slot + 1, reset_exc)
                    web.close()
                    web = None
                if from_deferred:
                    deferred.pop(deferred_position)
                else:
                    index += 1
                security_streak = 0
                cooldown_until = 0
                last_error = ""
                save_state()
                saved_result = True
                break
            except Exception as exc:
                attempts += 1
                reason = " ".join(str(exc).split())
                if len(reason) > 240:
                    reason = reason[:237] + "..."
                security_verification = "百度安全验证" in reason
                last_error = reason
                if security_verification:
                    security_streak += 1
                    retry_delay = security_backoff_seconds(security_streak, args.retry_wait)
                    cooldown_until = time.time() + retry_delay
                    log.warning(
                        "检测到百度验证；保留稳定浏览器档案并冷却 %d 秒（连续 %d 次）",
                        retry_delay,
                        security_streak,
                    )
                else:
                    retry_delay = max(1, int(args.retry_wait))
                    cooldown_until = 0
                save_state()
                log.warning(
                    "第 %d 轮第 %d 次未完成：%s；将重开页面并重试同一问题",
                    current_slot + 1,
                    attempts,
                    reason,
                )
                retry_limit = baidu_capture_retries if capture_mode == "baidu_search_ai" else args.max_retries
                if retry_limit > 0 and attempts >= retry_limit:
                    if capture_mode == "baidu_search_ai":
                        attempt_no = record_baidu_card_attempt(
                            prompt, "capture_failed", maximum=baidu_daily_rounds,
                        )
                        append(Path(args.results), {
                            "status": "capture_failed",
                            "skip_reason": reason,
                            "task_id": max(1, args.task_id),
                            "round": current_slot + 1,
                            "serial": f"baidu-search-task-{max(1, args.task_id)}",
                            "prompt": prompt,
                            "question": canonical_recommendation_question(prompt),
                            "reply": "",
                            "web_body": "",
                            "body_capture_complete": False,
                            "sources": [],
                            "expected_source_count": 0,
                            "source_capture_complete": False,
                            "citation_count": 0,
                            "capture_warning": reason,
                            "capture_mode": "baidu_search_ai",
                            "capture_label": "百度卡片",
                            "card_available": True,
                            "surface_attempt": attempt_no,
                            "daily_surface_target": baidu_daily_rounds,
                            "started_at": started,
                            "finished_at": now(),
                        })
                        log.error(
                            "第 %d 轮百度卡片抓取失败已记为今日第 %d/%d 次探测；达到 5 次后将继续采集文心卡片",
                            current_slot + 1, attempt_no, baidu_daily_rounds,
                        )
                    if from_deferred:
                        item = deferred.pop(deferred_position)
                        item["deferred_cycles"] = max(0, int(item.get("deferred_cycles") or 0)) + 1
                        if item["deferred_cycles"] >= max(1, int(args.max_deferred_cycles)):
                            failed_deferred.append({
                                "slot": current_slot, "prompt": prompt,
                                "reason": reason, "failed_at": now(),
                            })
                            log.error(
                                "第 %d 轮延期补抓已达 %d 个周期，本批次停止重试该题；后续批次仍会按正常题目再次采集",
                                current_slot + 1, item["deferred_cycles"],
                            )
                        else:
                            item["retry_after_index"] = index + len(prompts)
                            deferred.append(item)
                    else:
                        deferred.append({
                            "slot": current_slot, "prompt": prompt,
                            "retry_after_index": index + len(prompts),
                            "deferred_cycles": 0,
                        })
                        index += 1
                    save_state()
                    log.error(
                        "第 %d 轮连续失败 %d 次，已延期补抓；先继续后续问题，避免阻塞整条生产队列",
                        current_slot + 1,
                        attempts,
                    )
                    break
                try:
                    if web is not None:
                        tab_change = web.reset_after_round()
                        log.warning(
                            "第 %d 轮已关闭无 AI 回答的旧页面 %s，并新建百度页面 %s",
                            current_slot + 1,
                            tab_change.get("old_target") or "unknown",
                            tab_change.get("new_target") or "unknown",
                        )
                except Exception as reset_exc:
                    log.warning("关闭旧页面失败，下次尝试将重建浏览器连接：%s", reset_exc)
                if web is not None:
                    web.close()
                web = None
                log.warning("%s 秒后重新搜索并抓取同一问题", retry_delay)
                sleep_seconds = max(0, cooldown_until - time.time()) if security_verification else retry_delay
                interruptible_sleep(sleep_seconds)
        if STOP:
            break
        if saved_result:
            subprocess.run([sys.executable, str(BASE_DIR / "build_dashboard_data.py")], cwd=BASE_DIR,
                           capture_output=True, timeout=120, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if not STOP and (index < target_end_index or deferred):
            time.sleep(max(1, args.wait) + random.uniform(0, max(0, args.random_wait)))
    if web is not None:
        web.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
