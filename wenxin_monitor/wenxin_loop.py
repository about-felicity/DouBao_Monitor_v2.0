from __future__ import annotations

import argparse
import hashlib
import json
import logging
import random
import re
import signal
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
STOP = False


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
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--results", default=str(BASE_DIR / "wenxin_results.jsonl"))
    parser.add_argument("--state", default=str(BASE_DIR / "wenxin_state.json"))
    parser.add_argument("--log", default=str(BASE_DIR / "wenxin_loop.log"))
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    active_profile = Path(args.chrome_profile)
    logging.basicConfig(level=logging.INFO,
                        format=f"%(asctime)s [%(levelname)s] [任务 {max(1, args.task_id)}] %(message)s",
                        handlers=[logging.StreamHandler(), logging.FileHandler(args.log, mode="w", encoding="utf-8")])
    log = logging.getLogger("wenxin")
    raw = [line.strip() for line in Path(args.questions_file).read_text(encoding="utf-8-sig").splitlines()
           if line.strip() and not line.lstrip().startswith("#")]
    prompts = validate_prompt_list(raw)
    schedule = build_question_schedule(prompts, max(1, args.rounds_per_question), args.question_mode)
    web = None
    state_path = Path(args.state)
    try:
        state = json.loads(state_path.read_text(encoding="utf-8")) if args.resume else {}
    except (OSError, ValueError):
        state = {}
    index = int(state.get("next_index") or 0)
    deferred = []
    for item in state.get("deferred") or []:
        if isinstance(item, dict):
            slot = int(item.get("slot") or 0)
            prompt = str(item.get("prompt") or "").strip()
        else:
            slot = 0
            prompt = str(item or "").strip()
        if prompt:
            deferred.append({"slot": slot, "prompt": prompt})

    def save_state() -> None:
        state_path.write_text(
            json.dumps({
                "next_index": index,
                "deferred": deferred,
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

    while not STOP and (index < len(schedule) or deferred):
        from_deferred = index >= len(schedule)
        current_slot = int(deferred[0]["slot"]) if from_deferred else index
        prompt = str(deferred[0]["prompt"]) if from_deferred else schedule[index]
        attempts = 0
        started = now()
        saved_result = False
        while not STOP:
            try:
                if web is None:
                    web = WenxinWebCollector(args.chrome_port, profile=active_profile)
                log.info("第 %d 轮%s文心搜索生成：%s", current_slot + 1, "（延期补抓）" if from_deferred else "", prompt)
                result = web.collect_search(prompt, args.timeout)
                result, repeated_tab_change = replace_repeated_search_card(
                    web,
                    prompt,
                    result,
                    args.timeout,
                )
                if repeated_tab_change is not None:
                    log.warning(
                        "第 %d 轮检测到同日同问题第二次出现相同搜索卡片；已关闭旧页面 %s，"
                        "并在新页面 %s 改用文心专用入口重新生成",
                        current_slot + 1,
                        repeated_tab_change.get("old_target") or "unknown",
                        repeated_tab_change.get("new_target") or "unknown",
                    )
                answer = str(result.get("body") or "")
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
                       "capture_warning": str(result.get("capture_warning") or ""),
                       "citation_count": int(result.get("citation_count") or 0),
                       "page_navigation_id": str(result.get("page_navigation_id") or ""),
                       "capture_mode": result.get("capture_mode") or "baidu_search_ai", "page_url": result.get("url"),
                       "started_at": started, "finished_at": now()}
                row["capture_label"] = (
                    "搜索卡片" if row["capture_mode"] == "baidu_search_ai" else "文心页面兜底"
                )
                # A finished body is not a complete production observation when
                # Baidu exposes more citation cards than usable source links.
                # Retry the same question on a fresh page instead of persisting
                # a knowingly incomplete row and letting it affect analytics.
                if not row["body_capture_complete"]:
                    raise RuntimeError(
                        f"正文抓取不完整：{prompt}（正文 {len(answer)} 字）"
                    )
                if not row["source_capture_complete"]:
                    raise RuntimeError(
                        f"信源抓取不完整：{prompt}（取得 {len(row['sources'])}/"
                        f"{int(row['expected_source_count'] or 0)} 条）"
                    )
                if not reserve_unique_observation(prompt, answer):
                    raise RuntimeError(
                        "同日同问题正文与已保存轮次完全相同；该页面不是新的独立回答"
                    )
                append(Path(args.results), row)
                log.info(
                    "第 %d 轮完成：%s｜抓取方式：%s｜正文完整：%s（%d 字）｜信源完整：%s（%d/%d 条，引用图标 %d 个）",
                    current_slot + 1,
                    prompt,
                    row["capture_label"],
                    "是" if row["body_capture_complete"] else "否",
                    len(answer),
                    "是" if row["source_capture_complete"] else "否",
                    len(row["sources"]),
                    int(row["expected_source_count"] or 0),
                    int(row["citation_count"] or 0),
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
                    deferred.pop(0)
                else:
                    index += 1
                save_state()
                saved_result = True
                break
            except Exception as exc:
                attempts += 1
                reason = " ".join(str(exc).split())
                if len(reason) > 240:
                    reason = reason[:237] + "..."
                security_verification = "百度安全验证" in reason
                if security_verification and attempts >= 2:
                    base_profile = Path(args.chrome_profile)
                    active_profile = base_profile.with_name(
                        f"{base_profile.name}_rotated_{int(time.time())}"
                    )
                    log.warning("百度验证连续出现；冷却后改用新隐身档案 %s", active_profile.name)
                elif security_verification:
                    log.warning("检测到百度验证；先关闭页面并保留当前 Scrapling 身份，冷却后重开")
                log.warning(
                    "第 %d 轮第 %d 次未完成：%s；将重开页面并重试同一问题",
                    current_slot + 1,
                    attempts,
                    reason,
                )
                if args.max_retries > 0 and attempts >= args.max_retries:
                    if from_deferred:
                        deferred.append(deferred.pop(0))
                    else:
                        deferred.append({"slot": current_slot, "prompt": prompt})
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
                retry_delay = max(60, args.retry_wait) if security_verification else max(1, args.retry_wait)
                log.warning("%s 秒后重新搜索并抓取同一问题", retry_delay)
                time.sleep(retry_delay)
        if STOP:
            break
        if saved_result:
            subprocess.run([sys.executable, str(BASE_DIR / "build_dashboard_data.py")], cwd=BASE_DIR,
                           capture_output=True, timeout=120, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if not STOP and (index < len(schedule) or deferred):
            time.sleep(max(1, args.wait) + random.uniform(0, max(0, args.random_wait)))
    if web is not None:
        web.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
