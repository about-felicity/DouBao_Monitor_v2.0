"""DeepSeek monitoring loop with a persisted 92–118 second send cadence."""

from __future__ import annotations

import argparse
import json
import logging
import random
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from controller import DeepSeekAppController, DeepSeekWebCollector

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from monitor_core.quality import answer_quality_reason
from monitor_core.scheduling import build_question_schedule
from monitor_core.device_lock import device_session
from monitor_core.lan_result_sync import enqueue as enqueue_remote_result


BASE_DIR = Path(__file__).resolve().parent
STOP = False
MIN_SEND_INTERVAL_SECONDS = 92.0
MAX_SEND_INTERVAL_SECONDS = 118.0


def safe_interval_bounds(minimum: float, maximum: float) -> tuple[float, float]:
    """Clamp requested timing inside the account-safe 90–120 second window."""
    lower = min(MAX_SEND_INTERVAL_SECONDS, max(MIN_SEND_INTERVAL_SECONDS, float(minimum)))
    upper = min(MAX_SEND_INTERVAL_SECONDS, max(lower, float(maximum)))
    return lower, upper


def now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def load_questions(path: Path) -> list[str]:
    return [line.strip() for line in path.read_text(encoding="utf-8-sig").splitlines() if line.strip() and not line.lstrip().startswith("#")]


def append_jsonl(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    enqueue_remote_result("deepseek", record)


def load_state(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def wait_until(timestamp: float, logger: logging.Logger) -> None:
    global STOP
    remaining = max(0.0, timestamp - time.time())
    if remaining:
        logger.info("下一次提问将在约 %.1f 分钟后", remaining / 60)
    while not STOP and time.time() < timestamp:
        time.sleep(min(1.0, max(0.05, timestamp - time.time())))


def answer_from_page(body: str, question: str) -> str:
    text = str(body or "").strip()
    position = text.rfind(question)
    if position >= 0:
        text = text[position + len(question):].strip()
    for marker in ("本回答由 AI 生成", "内容由 AI 生成"):
        if marker in text:
            text = text.split(marker, 1)[0].strip()
    return text


def latest_chat_url(value: dict) -> str:
    links = value.get("links") if isinstance(value, dict) else None
    if isinstance(links, list) and links:
        return str((links[0] or {}).get("href") or "")
    return str((value or {}).get("href") or "")


def main() -> int:
    parser = argparse.ArgumentParser(description="DeepSeek 网页监控")
    parser.add_argument("--questions-file", default=str(BASE_DIR / "product.txt"))
    parser.add_argument("--rounds", type=int, default=10)
    parser.add_argument("--rounds-per-question", type=int, help="每个问题执行的轮数")
    parser.add_argument("--question-mode", choices=("interleaved", "sequential"), default="interleaved")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--min-interval", type=float, default=MIN_SEND_INTERVAL_SECONDS, help="计划发送间隔下限；强制限制在 92–118 秒")
    parser.add_argument("--max-interval", type=float, default=MAX_SEND_INTERVAL_SECONDS, help="计划发送间隔上限；强制限制在 92–118 秒")
    parser.add_argument("--retry-wait", type=float, default=60)
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--stable-seconds", type=int, default=8)
    parser.add_argument("--serial", default="127.0.0.1:16384", help="MuMu ADB 序列号")
    parser.add_argument("--chrome-port", type=int, default=9333)
    parser.add_argument("--max-web-retries", type=int, default=3)
    parser.add_argument("--results", default=str(BASE_DIR / "deepseek_results.jsonl"))
    parser.add_argument("--state", default=str(BASE_DIR / "deepseek_state.json"))
    parser.add_argument("--log", default=str(BASE_DIR / "deepseek_loop.log"))
    args = parser.parse_args()
    if args.rounds_per_question is not None and args.rounds_per_question < 1:
        raise SystemExit("--rounds-per-question 必须大于 0")
    if args.rounds < 1:
        raise SystemExit("--rounds 必须大于 0")
    args.min_interval, args.max_interval = safe_interval_bounds(args.min_interval, args.max_interval)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", handlers=[logging.StreamHandler(), logging.FileHandler(args.log, encoding="utf-8")])
    logger = logging.getLogger("deepseek_loop")
    questions = load_questions(Path(args.questions_file))
    if not questions:
        raise SystemExit("问题列表为空")
    schedule = (
        build_question_schedule(questions, args.rounds_per_question, args.question_mode)
        if args.rounds_per_question is not None
        else questions
    )
    target_rounds = len(schedule) if args.rounds_per_question is not None else args.rounds
    state_path = Path(args.state)
    state = load_state(state_path) if args.resume else {}
    index = int(state.get("next_index") or 0)
    app = None
    web = None

    def stop_handler(*_):
        global STOP
        STOP = True

    signal.signal(signal.SIGINT, stop_handler)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, stop_handler)

    completed = 0
    while not STOP and completed < target_rounds:
        question = schedule[index % len(schedule)]
        wait_until(float(state.get("next_send_at") or 0), logger)
        if STOP:
            break
        started = now()
        sent = False
        try:
            if app is None:
                app = DeepSeekAppController(args.serial)
            if web is None:
                web = DeepSeekWebCollector(args.chrome_port)
            # 发送前记住网页当前第一条会话。采集时必须等到一个新的会话出现，
            # 这样即使连续询问同一个问题，也不会误抓上一次的旧回答。
            app_completion: dict = {}
            app_error = ""
            with device_session(args.serial, "DeepSeek", timeout=args.timeout + 120,
                                on_wait=logger.info):
                baseline = web.latest_chat(timeout=min(30, args.timeout))
                previous_chat_url = latest_chat_url(baseline)
                # App 发送和等待回答必须是一个原子区间，其他模型只能排队。
                delay = random.uniform(args.min_interval, args.max_interval)
                # Reserve the safety window before clicking Send. If the UI
                # click succeeds but the automation connection drops before it
                # returns, the retry still cannot submit another question early.
                attempted_at = time.time()
                state.update({"last_send_attempt_at": attempted_at, "next_send_at": attempted_at + delay, "next_index": index})
                save_state(state_path, state)
                app.send(question)
                sent = True
                sent_at = time.time()
                state.update({"last_sent_at": sent_at, "next_send_at": sent_at + delay, "next_index": index})
                save_state(state_path, state)
                logger.info("第 %d 轮已发送；下次随机间隔 %.1f 分钟", index + 1, delay / 60)
                logger.info("第 %d 轮正在等待模拟器端回答加载完成", index + 1)
                try:
                    app_completion = app.wait_for_answer(question, args.timeout, min(5, args.stable_seconds))
                    logger.info("第 %d 轮模拟器端回答已完成，正在等待网页端同步最新会话并采集信源", index + 1)
                except Exception as exc:
                    app_error = str(exc)
                    logger.warning("第 %d 轮模拟器端回答异常，直接跳过网页采集：%s", index + 1, exc)
            result: dict = {}
            web_error = app_error
            if not app_error:
                for web_attempt in range(1, args.max_web_retries + 1):
                    try:
                        result = web.collect_latest(
                            question, args.timeout, args.stable_seconds,
                            previous_chat_url=previous_chat_url,
                        )
                        web_error = ""
                        break
                    except Exception as exc:
                        web_error = str(exc)
                        logger.warning("第 %d 轮网页抓取第 %d/%d 次失败：%s", index + 1, web_attempt, args.max_web_retries, exc)
                        if web_attempt < args.max_web_retries:
                            wait_until(time.time() + max(30.0, args.retry_wait), logger)
            answer = str(result.get("answer") or "").strip() or answer_from_page(str(result.get("body") or ""), question)
            skip_reason = web_error or answer_quality_reason(question, answer)
            if skip_reason:
                logger.warning("第 %d 轮模型回答无效，将按安全间隔重试原题：%s", index + 1, skip_reason)
                app = None
                web = None
                wait_until(max(float(state.get("next_send_at") or 0), time.time() + args.retry_wait), logger)
                continue
            append_jsonl(Path(args.results), {"status": "success", "skip_reason": "",
                "round": index + 1, "serial": args.serial, "question": question, "reply": answer,
                "web_body": result.get("body", ""), "sources": result.get("sources", []),
                "conversation_question": result.get("currentQuestion", ""),
                "page_url": result.get("conversation_url") or result.get("url", ""), "web_error": web_error,
                "app_error": app_error, "app_completion": app_completion,
                "started_at": started, "finished_at": now()})
            index += 1
            completed += 1
            state["next_index"] = index
            save_state(state_path, state)
            subprocess.run([sys.executable, str(BASE_DIR / "build_dashboard_data.py")], cwd=BASE_DIR, capture_output=True, timeout=60, check=False)
        except Exception as exc:
            logger.exception("第 %d 轮失败：%s", index + 1, exc)
            app = None
            web = None
            safe_at = float(state.get("next_send_at") or 0) if sent else 0
            wait_until(max(safe_at, time.time() + max(60.0, args.retry_wait)), logger)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
