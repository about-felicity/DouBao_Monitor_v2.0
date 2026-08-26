"""Run up to four fully independent Baidu/Wenxin collection tasks."""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
ROOT = BASE_DIR.parent
RUNTIME = ROOT / "runtime" / "remote_workers"


def task_paths(task_id: int) -> dict[str, Path]:
    suffix = "" if task_id == 1 else f"_task_{task_id}"
    return {
        "state": RUNTIME / f"wenxin_baidu{suffix}_state.json",
        "log": RUNTIME / f"wenxin_baidu{suffix}_loop.log",
        "results": RUNTIME / f"wenxin_collector_results{suffix}.jsonl",
        "profile": RUNTIME / f"wenxin_scrapling_task_{task_id}",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--questions-file", required=True)
    parser.add_argument("--rounds-per-question", type=int, required=True)
    parser.add_argument("--question-mode", choices=("interleaved", "sequential"), required=True)
    parser.add_argument("--tasks", type=int, default=1)
    parser.add_argument("--results", default=str(RUNTIME / "wenxin_collector_results.jsonl"))
    parser.add_argument("--wait", type=float, default=30)
    parser.add_argument("--random-wait", type=float, default=90)
    parser.add_argument("--retry-wait", type=float, default=15)
    parser.add_argument("--max-retries", type=int, default=3)
    parser.add_argument("--timeout", type=int, default=45)
    args = parser.parse_args()

    task_count = max(1, min(args.tasks, 4))
    children: list[tuple[int, subprocess.Popen]] = []
    for task_id in range(1, task_count + 1):
        paths = task_paths(task_id)
        if task_id == 1:
            paths["results"] = Path(args.results)
        command = [
            sys.executable,
            str(BASE_DIR / "wenxin_loop.py"),
            "--questions-file", args.questions_file,
            "--rounds-per-question", str(max(1, args.rounds_per_question)),
            "--question-mode", args.question_mode,
            "--task-id", str(task_id),
            "--chrome-port", str(9443 + task_id),
            "--chrome-profile", str(paths["profile"]),
            "--startup-delay", str(max(0, task_id - 1) * 25),
            "--resume",
            "--wait", str(args.wait),
            "--random-wait", str(args.random_wait),
            "--retry-wait", str(args.retry_wait),
            "--max-retries", str(max(0, args.max_retries)),
            "--timeout", str(args.timeout),
            "--results", str(paths["results"]),
            "--state", str(paths["state"]),
            "--log", str(paths["log"]),
        ]
        print(f"[任务 {task_id}] 已启动独立 Scrapling 隐身浏览器", flush=True)
        children.append((task_id, subprocess.Popen(command, cwd=BASE_DIR)))

    return_code = 0
    try:
        while children:
            active: list[tuple[int, subprocess.Popen]] = []
            for task_id, child in children:
                code = child.poll()
                if code is None:
                    active.append((task_id, child))
                elif code != 0:
                    print(f"[任务 {task_id}] 异常退出：{code}", flush=True)
                    return_code = code
                else:
                    print(f"[任务 {task_id}] 已完成全部问题和轮数", flush=True)
            children = active
            if children:
                time.sleep(1)
    except KeyboardInterrupt:
        return_code = 130
    finally:
        for _, child in children:
            if child.poll() is None:
                child.terminate()
        for _, child in children:
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
    return return_code


if __name__ == "__main__":
    raise SystemExit(main())
