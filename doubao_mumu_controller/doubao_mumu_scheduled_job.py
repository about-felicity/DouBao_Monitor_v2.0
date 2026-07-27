from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
import logging
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
from typing import Any

import doubao_mumu_web_pipeline as pipeline


BASE_DIR = Path(__file__).resolve().parent
PIPELINE = BASE_DIR / "doubao_mumu_web_pipeline.py"
DEFAULT_CONFIG = BASE_DIR / "doubao_mumu_panel_config.json"
RUNTIME_DIR = BASE_DIR / "runtime"
JOB_LOG = BASE_DIR / "doubao_mumu_scheduled_job.log"
JOB_LOCK = BASE_DIR / ".scheduled_job.lock"
BEIJING_TZ = timezone(timedelta(hours=8))


class JobConfigError(ValueError):
    pass


class JobLock:
    def __init__(self) -> None:
        self.handle: Any = None

    def acquire(self) -> bool:
        if os.name != "nt":
            return True
        import msvcrt

        self.handle = JOB_LOCK.open("a+b")
        if JOB_LOCK.stat().st_size == 0:
            self.handle.write(b"0")
            self.handle.flush()
        self.handle.seek(0)
        try:
            msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            self.handle.close()
            self.handle = None
            return False

    def release(self) -> None:
        if self.handle is None or os.name != "nt":
            return
        import msvcrt

        try:
            self.handle.seek(0)
            msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
        finally:
            self.handle.close()
            self.handle = None


def log(message: str) -> None:
    stamp = datetime.now(BEIJING_TZ).isoformat(sep=" ", timespec="seconds")
    line = f"{stamp} {message}"
    print(line, flush=True)
    JOB_LOG.parent.mkdir(parents=True, exist_ok=True)
    with JOB_LOG.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def positive_number(
    value: Any,
    name: str,
    *,
    minimum: float = 0,
) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise JobConfigError(f"{name} 必须是数字。") from exc
    if number < minimum:
        raise JobConfigError(f"{name} 不能小于 {minimum:g}。")
    return number


def load_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise JobConfigError(f"配置文件不存在：{path}")
    try:
        config = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise JobConfigError(f"配置文件读取失败：{exc}") from exc
    if not isinstance(config, dict):
        raise JobConfigError("配置文件根节点必须是对象。")
    return config


def normalized_questions(config: dict[str, Any]) -> list[dict[str, Any]]:
    raw = config.get("questions") or []
    questions: list[dict[str, Any]] = []
    for index, item in enumerate(raw, 1):
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or item.get("question") or "").strip()
        try:
            repeat = int(item.get("repeat") or item.get("rounds") or 0)
        except (TypeError, ValueError):
            repeat = 0
        if not text:
            raise JobConfigError(f"第 {index} 个问题为空。")
        if repeat <= 0:
            raise JobConfigError(f"第 {index} 个问题的重复次数必须大于 0。")
        questions.append({"text": text, "repeat": repeat})
    if not questions:
        raise JobConfigError("至少需要一个问题。")
    return questions


def build_schedule(
    questions: list[dict[str, Any]],
    mode: str,
) -> list[str]:
    if mode == "interleaved":
        result: list[str] = []
        maximum = max(int(item["repeat"]) for item in questions)
        for repeat_index in range(maximum):
            for item in questions:
                if repeat_index < int(item["repeat"]):
                    result.append(str(item["text"]))
        return result
    result = []
    for item in questions:
        result.extend([str(item["text"])] * int(item["repeat"]))
    return result


def safe_file_component(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("_") or "job"


def write_runtime_questions(schedule: list[str]) -> Path:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(BEIJING_TZ).strftime("%Y%m%d_%H%M%S")
    path = RUNTIME_DIR / f"scheduled_questions_{safe_file_component(stamp)}.txt"
    path.write_text("\n".join(schedule) + "\n", encoding="utf-8")
    latest = RUNTIME_DIR / "latest_questions.txt"
    latest.write_text("\n".join(schedule) + "\n", encoding="utf-8")
    return path


def build_pipeline_command(
    config: dict[str, Any],
    queue_file: Path,
    total_rounds: int,
    device_index: str,
) -> list[str]:
    runtime = config.get("runtime") or {}
    min_wait = positive_number(runtime.get("min_wait", 8), "最少等待秒")
    stable = positive_number(runtime.get("stable_seconds", 5), "稳定秒数", minimum=1)
    answer_timeout = positive_number(
        runtime.get("answer_timeout", 240),
        "回答超时秒",
        minimum=1,
    )
    sync_timeout = positive_number(
        runtime.get("sync_timeout", 300),
        "网页同步超时秒",
        minimum=1,
    )
    retry_delay = positive_number(runtime.get("retry_delay", 5), "异常重试秒")
    cooldown_min = positive_number(
        runtime.get("cooldown_min", 10),
        "轮次最小冷却秒",
    )
    cooldown_max = positive_number(
        runtime.get("cooldown_max", 20),
        "轮次最大冷却秒",
    )
    if cooldown_max < cooldown_min:
        raise JobConfigError("轮次最大冷却秒不能小于最小冷却秒。")
    max_retries = int(runtime.get("max_round_retries", 0) or 0)
    if max_retries < 0:
        raise JobConfigError("最大重试次数不能小于 0。")

    command = [
        sys.executable,
        str(PIPELINE),
        "--questions-file",
        str(queue_file),
        "--rounds",
        str(total_rounds),
        "--min-wait",
        f"{min_wait:g}",
        "--stable-seconds",
        f"{stable:g}",
        "--answer-timeout",
        f"{answer_timeout:g}",
        "--sync-timeout",
        f"{sync_timeout:g}",
        "--retry-delay",
        f"{retry_delay:g}",
        "--round-delay-min",
        f"{cooldown_min:g}",
        "--round-delay-max",
        f"{cooldown_max:g}",
        "--max-round-retries",
        str(max_retries),
        "--device-index",
        device_index,
        "--browser-slot",
        device_index,
        "--log",
        str(BASE_DIR / f"doubao_mumu_web_pipeline_instance_{safe_file_component(device_index)}.log"),
        "--results",
        str(BASE_DIR / f"doubao_mumu_web_results_instance_{safe_file_component(device_index)}.jsonl"),
        "--diagnostics-dir",
        str(BASE_DIR / "doubao_mumu_web_diagnostics" / f"instance_{safe_file_component(device_index)}"),
    ]
    if bool(runtime.get("verbose")):
        command.append("--verbose")
    return command


def selected_devices(config: dict[str, Any]) -> list[dict[str, Any]]:
    logger = logging.getLogger("doubao_multi_device_discovery")
    if not logger.handlers:
        logger.addHandler(logging.NullHandler())
    devices = pipeline.discover_mumu_instances(logger, None)
    requested_text = str(config.get("device_index") or "").strip()
    if not requested_text:
        return devices
    requested = {
        value.strip()
        for value in re.split(r"[,，;；\\s]+", requested_text)
        if value.strip()
    }
    selected = [
        device for device in devices
        if str(device.get("index") or "") in requested
    ]
    missing = sorted(requested - {str(item["index"]) for item in selected})
    if missing:
        raise JobConfigError(
            "以下 MuMu 实例未启动或不存在：" + "、".join(missing)
        )
    return selected


def run(config_path: Path, dry_run: bool) -> int:
    config = load_config(config_path)
    questions = normalized_questions(config)
    mode = str(config.get("question_mode") or "interleaved").strip()
    if mode not in {"sequential", "interleaved"}:
        raise JobConfigError("问题模式必须是 sequential 或 interleaved。")
    schedule = build_schedule(questions, mode)
    skip_initial = max(
        0,
        int(os.environ.pop("DOUBAO_SKIP_INITIAL_ROUNDS", "0") or 0),
    )
    if skip_initial:
        if skip_initial >= len(schedule):
            log(
                f"RESUME 已保存 {skip_initial} 轮，当前计划没有剩余轮次。"
            )
            return 0
        schedule = schedule[skip_initial:]
        log(
            f"RESUME 已确认保存前 {skip_initial} 轮，"
            f"继续执行剩余 {len(schedule)} 轮。"
        )

    lock = JobLock()
    if not dry_run and not lock.acquire():
        log("SKIP 已有豆包 MuMu 整体任务在运行，本次定时触发不重叠执行。")
        return 0
    queue_file: Path | None = None
    try:
        queue_file = write_runtime_questions(schedule)
        devices = selected_devices(config)
        commands = [
            {
                "device": device,
                "command": build_pipeline_command(
                    config,
                    queue_file,
                    len(schedule),
                    str(device["index"]),
                ),
            }
            for device in devices
        ]
        summary = {
            "config": str(config_path),
            "question_count": len(questions),
            "total_rounds": len(schedule),
            "mode": mode,
            "queue_file": str(queue_file),
            "device_count": len(devices),
            "devices": [
                {
                    "index": item["device"]["index"],
                    "serial": item["device"]["serial"],
                    "command": item["command"],
                }
                for item in commands
            ],
        }
        if dry_run:
            print(json.dumps(summary, ensure_ascii=False, indent=2))
            return 0
        log(
            "START "
            f"{len(devices)} 个 MuMu 实例并行；每个实例 "
            f"{len(questions)} 个问题，共 {len(schedule)} 轮，"
            f"模式={'交叉' if mode == 'interleaved' else '顺序'}。"
        )
        child_env = os.environ.copy()
        child_env["PYTHONIOENCODING"] = "utf-8"
        child_env["PYTHONUTF8"] = "1"
        processes: list[tuple[str, subprocess.Popen[str]]] = []
        output_lock = threading.Lock()

        def pump_output(instance_index: str, process: subprocess.Popen[str]) -> None:
            assert process.stdout is not None
            for line in process.stdout:
                prefixed = f"[实例 {instance_index}] {line}"
                with output_lock:
                    print(prefixed, end="", flush=True)
                    with JOB_LOG.open("a", encoding="utf-8") as job_log:
                        job_log.write(prefixed)

        threads = []
        for item in commands:
            index = str(item["device"]["index"])
            process = subprocess.Popen(
                item["command"],
                cwd=str(BASE_DIR),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=child_env,
                creationflags=(
                    subprocess.CREATE_NO_WINDOW
                    if os.name == "nt" and hasattr(subprocess, "CREATE_NO_WINDOW")
                    else 0
                ),
            )
            processes.append((index, process))
            thread = threading.Thread(
                target=pump_output,
                args=(index, process),
                daemon=True,
                name=f"doubao-instance-{index}-log",
            )
            thread.start()
            threads.append(thread)
        codes = {}
        for index, process in processes:
            codes[index] = process.wait()
        for thread in threads:
            thread.join(timeout=5)
        failed = {index: code for index, code in codes.items() if code != 0}
        log(
            "END 多实例流水线完成："
            + "，".join(f"实例 {index}=退出码 {code}" for index, code in codes.items())
        )
        return max(failed.values()) if failed else 0
    finally:
        if queue_file is not None:
            queue_file.unlink(missing_ok=True)
        if not dry_run:
            lock.release()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="执行面板保存的豆包 MuMu 问题计划。")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        return run(Path(args.config), args.dry_run)
    except KeyboardInterrupt:
        log("STOP 收到人工停止请求。")
        return 130
    except Exception as exc:
        log(f"ERROR {type(exc).__name__}: {exc}")
        if args.dry_run:
            raise
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
