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

from controller import WenxinAppController, WenxinWebCollector

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from monitor_core.quality import answer_quality_reason
from monitor_core.recommendation_questions import canonical_recommendation_question, validate_prompt_list
from monitor_core.scheduling import build_question_schedule
from monitor_core.device_lock import device_session
from monitor_core.lan_result_sync import enqueue as enqueue_remote_result

BASE_DIR = Path(__file__).resolve().parent
STOP = False


def now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def append(path: Path, row: dict) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    enqueue_remote_result("wenxin", row)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--questions-file", default=str(BASE_DIR / "product.txt"))
    parser.add_argument("--rounds-per-question", type=int, default=1)
    parser.add_argument("--question-mode", choices=("interleaved", "sequential"), default="interleaved")
    parser.add_argument("--serial", default="127.0.0.1:16384")
    parser.add_argument("--chrome-port", type=int, default=9444)
    parser.add_argument("--wait", type=float, default=30)
    parser.add_argument("--random-wait", type=float, default=90)
    parser.add_argument("--retry-wait", type=float, default=60)
    parser.add_argument("--max-retries", type=int, default=0,
                        help="单题最大重试次数，0 表示持续重试直到成功")
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--results", default=str(BASE_DIR / "wenxin_results.jsonl"))
    parser.add_argument("--state", default=str(BASE_DIR / "wenxin_state.json"))
    parser.add_argument("--log", default=str(BASE_DIR / "wenxin_loop.log"))
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
                        handlers=[logging.StreamHandler(), logging.FileHandler(args.log, encoding="utf-8")])
    log = logging.getLogger("wenxin")
    raw = [line.strip() for line in Path(args.questions_file).read_text(encoding="utf-8-sig").splitlines()
           if line.strip() and not line.lstrip().startswith("#")]
    prompts = validate_prompt_list(raw)
    schedule = build_question_schedule(prompts, max(1, args.rounds_per_question), args.question_mode)
    app = None
    web = None
    state_path = Path(args.state)
    try:
        state = json.loads(state_path.read_text(encoding="utf-8")) if args.resume else {}
    except (OSError, ValueError):
        state = {}
    index = int(state.get("next_index") or 0)

    def stop(*_):
        global STOP
        STOP = True
    signal.signal(signal.SIGINT, stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, stop)

    while not STOP and index < len(schedule):
        prompt = schedule[index]
        attempts = 0
        while not STOP:
            started = now()
            try:
                if app is None:
                    app = WenxinAppController(args.serial)
                if web is None:
                    web = WenxinWebCollector(args.chrome_port)
                with device_session(args.serial, "文心", timeout=args.timeout + 120, on_wait=log.info):
                    previous = web.latest_reference()
                    app.send(prompt)
                    mobile = app.wait_for_mobile_accept(min(60, args.timeout))
                result = web.collect_latest(previous, args.timeout)
                answer = str(result.get("body") or "")
                skip = answer_quality_reason(prompt, answer)
                if skip:
                    raise RuntimeError(f"回答质量校验未通过：{skip}")
                row = {"status": "success", "skip_reason": "",
                       "round": index + 1, "serial": args.serial, "prompt": prompt,
                       "question": canonical_recommendation_question(prompt), "reply": answer,
                       "web_body": answer, "sources": result.get("sources", []),
                       "page_url": result.get("url"), "mobile": mobile,
                       "started_at": started, "finished_at": now()}
                append(Path(args.results), row)
                log.info("第 %d 轮完成：%s，正文 %d 字，信源 %d 条", index + 1, prompt,
                         len(answer), len(row["sources"]))
                break
            except Exception as exc:
                attempts += 1
                log.exception("第 %d 轮第 %d 次尝试失败，将重连后重试同一问题：%s",
                              index + 1, attempts, exc)
                if args.max_retries > 0 and attempts >= args.max_retries:
                    return 1
                app = None
                web = None
                time.sleep(max(1, args.retry_wait))
        if STOP:
            break
        index += 1
        state_path.write_text(json.dumps({"next_index": index, "updated_at": now()}, ensure_ascii=False, indent=2), encoding="utf-8")
        subprocess.run([sys.executable, str(BASE_DIR / "build_dashboard_data.py")], cwd=BASE_DIR,
                       capture_output=True, timeout=120, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if not STOP and index < len(schedule):
            time.sleep(max(1, args.wait) + random.uniform(0, max(0, args.random_wait)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
