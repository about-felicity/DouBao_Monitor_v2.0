"""DeepSeek 网页监控循环，默认每次实际提问随机间隔 1–10 分钟。"""

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
from monitor_core.quality import invalid_answer_reason


BASE_DIR = Path(__file__).resolve().parent
STOP = False


def now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def load_questions(path: Path) -> list[str]:
    return [line.strip() for line in path.read_text(encoding="utf-8-sig").splitlines() if line.strip() and not line.lstrip().startswith("#")]


def append_jsonl(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


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


def main() -> int:
    parser = argparse.ArgumentParser(description="DeepSeek 网页监控")
    parser.add_argument("--questions-file", default=str(BASE_DIR / "product.txt"))
    parser.add_argument("--rounds", type=int, default=10)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--min-interval", type=float, default=60, help="实际发送之间的最短秒数，最低 60")
    parser.add_argument("--max-interval", type=float, default=600, help="实际发送之间的最长秒数")
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
    if args.rounds < 1:
        raise SystemExit("--rounds 必须大于 0")
    args.min_interval = max(60.0, args.min_interval)
    args.max_interval = max(args.min_interval, args.max_interval)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", handlers=[logging.StreamHandler(), logging.FileHandler(args.log, encoding="utf-8")])
    logger = logging.getLogger("deepseek_loop")
    questions = load_questions(Path(args.questions_file))
    if not questions:
        raise SystemExit("问题列表为空")
    state_path = Path(args.state)
    state = load_state(state_path) if args.resume else {}
    index = int(state.get("next_index") or 0)
    app = DeepSeekAppController(args.serial)
    web = DeepSeekWebCollector(args.chrome_port)

    def stop_handler(*_):
        global STOP
        STOP = True

    signal.signal(signal.SIGINT, stop_handler)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, stop_handler)

    completed = 0
    while not STOP and completed < args.rounds:
        question = questions[index % len(questions)]
        wait_until(float(state.get("next_send_at") or 0), logger)
        if STOP:
            break
        started = now()
        sent = False
        try:
            # 唯一允许发送问题的入口是 MuMu DeepSeek App；网页端只读取同步结果。
            app.send(question)
            sent = True
            sent_at = time.time()
            delay = random.uniform(args.min_interval, args.max_interval)
            state.update({"last_sent_at": sent_at, "next_send_at": sent_at + delay, "next_index": index})
            save_state(state_path, state)
            logger.info("第 %d 轮已发送；下次随机间隔 %.1f 分钟", index + 1, delay / 60)
            result: dict = {}
            web_error = ""
            for web_attempt in range(1, args.max_web_retries + 1):
                try:
                    result = web.collect_latest(question, args.timeout, args.stable_seconds)
                    web_error = ""
                    break
                except Exception as exc:
                    web_error = str(exc)
                    logger.warning("第 %d 轮网页抓取第 %d/%d 次失败：%s", index + 1, web_attempt, args.max_web_retries, exc)
                    if web_attempt < args.max_web_retries:
                        wait_until(time.time() + max(30.0, args.retry_wait), logger)
            answer = answer_from_page(str(result.get("body") or ""), question)
            skip_reason = web_error or invalid_answer_reason(answer)
            append_jsonl(Path(args.results), {"status": "skipped" if skip_reason else "success", "skip_reason": skip_reason,
                "round": index + 1, "serial": args.serial, "question": question, "reply": answer,
                "web_body": result.get("body", ""), "sources": [] if skip_reason else result.get("sources", []),
                "page_url": result.get("url", ""), "web_error": web_error, "started_at": started, "finished_at": now()})
            if skip_reason:
                logger.warning("第 %d 轮模型回答无效，已直接跳过：%s", index + 1, skip_reason)
            index += 1
            completed += 1
            state["next_index"] = index
            save_state(state_path, state)
            subprocess.run([sys.executable, str(BASE_DIR / "build_dashboard_data.py")], cwd=BASE_DIR, capture_output=True, timeout=60, check=False)
        except Exception as exc:
            logger.exception("第 %d 轮失败：%s", index + 1, exc)
            append_jsonl(Path(args.results), {"status": "error", "round": index + 1, "question": question, "started_at": started, "finished_at": now(), "sent": sent, "error": str(exc)})
            # 已经实际发送的问题绝不快速重发；网页失败在上面只重试抓取，不重发 App 问题。
            if not sent:
                wait_until(time.time() + max(60.0, args.retry_wait), logger)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
