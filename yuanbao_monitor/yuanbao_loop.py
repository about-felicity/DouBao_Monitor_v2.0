"""腾讯元宝 MuMu 多设备可靠循环。

提供问题队列、每题重复、交叉/顺序模式、多设备并行、断点续跑、无限重试、
JSONL 结果和失败诊断。控制面板也通过这个入口启动任务。
"""

from __future__ import annotations

import argparse
import json
import logging
import random
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from controller import YuanbaoController


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_ADB = Path(r"C:\Program Files\Netease\MuMu\nx_device\15.0\shell\adb.exe")
WRITE_LOCK = threading.Lock()
STOP_EVENT = threading.Event()


def now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def configure_logging(path: Path, verbose: bool = False) -> logging.Logger:
    logger = logging.getLogger("yuanbao_loop")
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    logger.handlers.clear()
    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    logger.addHandler(console)
    path.parent.mkdir(parents=True, exist_ok=True)
    file_handler = logging.FileHandler(path, encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    return logger


def discover_devices(adb: Path) -> list[str]:
    if not adb.exists():
        raise FileNotFoundError(f"找不到 MuMu ADB：{adb}")
    # 先尝试连接常见 MuMu 端口
    for port in [16384, 7555, 21503, 21513, 21453, 21553]:
        subprocess.run(
            [str(adb), "connect", f"127.0.0.1:{port}"],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=5, check=False,
        )
    proc = subprocess.run(
        [str(adb), "devices"], capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=10, check=False,
    )
    devices = []
    seen_ips = set()
    for line in proc.stdout.splitlines()[1:]:
        fields = line.split()
        if len(fields) >= 2 and fields[1] == "device":
            serial = fields[0]
            ip = serial.split(":")[0] if ":" in serial else serial
            if ip not in seen_ips:
                seen_ips.add(ip)
                devices.append(serial)
    return devices


def load_plan(path: Path) -> dict[str, Any]:
    if path.suffix.lower() == ".json":
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        items = data.get("questions", data if isinstance(data, list) else [])
        questions = []
        for item in items:
            if isinstance(item, str):
                questions.append({"text": item.strip(), "repeat": 1})
            elif isinstance(item, dict) and str(item.get("text", "")).strip():
                questions.append({
                    "text": str(item["text"]).strip(),
                    "repeat": max(1, int(item.get("repeat", 1))),
                })
        return {"questions": questions, "mode": data.get("mode", "cross")}
    questions = [
        {"text": line.strip(), "repeat": 1}
        for line in path.read_text(encoding="utf-8-sig").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    return {"questions": questions, "mode": "cross"}


def build_schedule(questions: list[dict[str, Any]], mode: str) -> list[str]:
    if mode == "sequential":
        return [q["text"] for q in questions for _ in range(q["repeat"])]
    # 交叉：先把每题问一遍，再进入下一重复层，避免连续重复同一问题。
    return [
        q["text"]
        for layer in range(max((q["repeat"] for q in questions), default=0))
        for q in questions if layer < q["repeat"]
    ]


def append_jsonl(path: Path, record: dict[str, Any]):
    line = json.dumps(record, ensure_ascii=False) + "\n"
    with WRITE_LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line)


def save_state(path: Path, state: dict[str, Any]):
    with WRITE_LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(path.suffix + ".tmp")
        temp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(path)


def refresh_dashboard(logger: logging.Logger):
    """采集成功后刷新 React 面板数据；失败不影响主任务。"""
    try:
        subprocess.run(
            [sys.executable, str(BASE_DIR / "build_dashboard_data.py")],
            cwd=str(BASE_DIR),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=180,
            check=True,
        )
    except Exception as exc:
        logger.warning("刷新 React 面板数据失败：%s", exc)


def safe_serial(serial: str) -> str:
    return serial.replace(":", "_").replace(".", "_")


def worker(
    serial: str,
    schedule: list[str],
    args: argparse.Namespace,
    logger: logging.Logger,
):
    tag = safe_serial(serial)
    state_path = Path(args.state_dir) / f"state_{tag}.json"
    diagnostic_dir = Path(args.diagnostics) / tag
    start_index = 0
    if args.resume and state_path.exists():
        try:
            start_index = int(json.loads(state_path.read_text(encoding="utf-8")).get("next_index", 0))
        except Exception:
            logger.warning("[%s] 断点文件损坏，将从头开始", serial)

    controller: YuanbaoController | None = None
    collector = None
    index = start_index
    while not STOP_EVENT.is_set() and (args.forever or index < len(schedule)):
        position = index % len(schedule)
        question = schedule[position]
        attempt = 0
        while not STOP_EVENT.is_set():
            attempt += 1
            started = now()
            try:
                if controller is None:
                    controller = YuanbaoController(serial=serial, connect_timeout=args.connect_timeout)
                xml_path = diagnostic_dir / f"reply_{index + 1:06d}.xml"
                xml_path.parent.mkdir(parents=True, exist_ok=True)
                logger.info("[%s] 第 %d 轮，第 %d 次尝试：%s", serial, index + 1, attempt, question)
                xml = controller.ask(question, save_xml_path=str(xml_path))
                reply = controller.extract_visible_reply(xml, question)
                web_result: dict[str, Any] = {}
                if args.collect_web:
                    # 手机端一旦发送成功，网页抓取失败只重试网页，绝不重发问题。
                    from collector import YuanbaoSourceCollector
                    web_attempt = 0
                    while not STOP_EVENT.is_set():
                        web_attempt += 1
                        try:
                            if collector is None:
                                device_number = args.device_order.get(serial, 0)
                                port = args.chrome_port + device_number
                                profile = (
                                    BASE_DIR / "chrome_profile_auto"
                                    if len(args.device_order) == 1
                                    else BASE_DIR / "chrome_profiles" / tag
                                )
                                collector = YuanbaoSourceCollector(
                                    debug_port=port, user_data_dir=str(profile)
                                )
                            web_path = BASE_DIR / "web_results" / f"result_{tag}_{index + 1:06d}.json"
                            web_path.parent.mkdir(parents=True, exist_ok=True)
                            web_result = collector.collect(
                                question, output_path=str(web_path),
                                wait_reply_timeout=args.web_timeout,
                                extra={"serial": serial, "round": index + 1},
                            )
                            if not web_result.get("error"):
                                break
                            raise RuntimeError(str(web_result["error"]))
                        except Exception as web_exc:
                            logger.warning("[%s] 网页抓取第 %d 次失败：%s", serial, web_attempt, web_exc)
                            collector = None
                            if args.max_retries and web_attempt >= args.max_retries:
                                web_result = {"error": str(web_exc)}
                                break
                            STOP_EVENT.wait(args.retry_wait)
                record = {
                    "status": "success", "serial": serial, "round": index + 1,
                    "schedule_index": position, "question": question, "reply": reply,
                    "reply_length": len(reply), "attempt": attempt,
                    "started_at": started, "finished_at": now(), "xml": str(xml_path),
                    "web_body": web_result.get("body", ""),
                    "sources": web_result.get("sources", []),
                    "web_error": web_result.get("error"),
                }
                append_jsonl(Path(args.results), record)
                refresh_dashboard(logger)
                index += 1
                save_state(state_path, {"serial": serial, "next_index": index, "updated_at": now()})
                logger.info("[%s] 第 %d 轮完成，提取可见文本 %d 字", serial, index, len(reply))
                break
            except KeyboardInterrupt:
                STOP_EVENT.set()
                break
            except Exception as exc:
                logger.exception("[%s] 第 %d 轮失败：%s", serial, index + 1, exc)
                prefix = f"round_{index + 1:06d}_attempt_{attempt:03d}"
                if controller is not None:
                    controller.save_diagnostics(diagnostic_dir, prefix, str(exc))
                append_jsonl(Path(args.results), {
                    "status": "error", "serial": serial, "round": index + 1,
                    "question": question, "attempt": attempt, "started_at": started,
                    "finished_at": now(), "error": str(exc),
                })
                controller = None
                if args.max_retries and attempt >= args.max_retries:
                    logger.error("[%s] 已达到最大重试次数，跳过本轮", serial)
                    index += 1
                    save_state(state_path, {"serial": serial, "next_index": index, "updated_at": now()})
                    break
                STOP_EVENT.wait(args.retry_wait)

        if not STOP_EVENT.is_set():
            delay = args.wait + (random.uniform(0, args.random_wait) if args.random_wait else 0)
            STOP_EVENT.wait(delay)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="腾讯元宝 MuMu 多设备可靠循环")
    parser.add_argument("--questions-file", default=str(BASE_DIR / "product.txt"))
    parser.add_argument("--mode", choices=("cross", "sequential"), help="覆盖计划文件中的模式")
    parser.add_argument("--serial", action="append", help="指定设备序列号，可重复；默认全部在线设备")
    parser.add_argument("--adb", default=str(DEFAULT_ADB))
    parser.add_argument("--forever", action="store_true")
    parser.add_argument("--rounds", type=int, help="精确运行轮数；问题不足时循环使用")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--wait", type=float, default=3, help="成功轮次间固定等待秒数")
    parser.add_argument("--random-wait", type=float, default=0, help="额外随机等待上限")
    parser.add_argument("--retry-wait", type=float, default=5)
    parser.add_argument("--max-retries", type=int, default=0, help="0 表示无限重试")
    parser.add_argument("--connect-timeout", type=int, default=15)
    parser.add_argument("--collect-web", action="store_true", help="同步抓取元宝网页正文和信源")
    parser.add_argument("--chrome-port", type=int, default=9222)
    parser.add_argument("--web-timeout", type=int, default=120)
    parser.add_argument("--results", default=str(BASE_DIR / "yuanbao_results.jsonl"))
    parser.add_argument("--state-dir", default=str(BASE_DIR / "yuanbao_state"))
    parser.add_argument("--diagnostics", default=str(BASE_DIR / "yuanbao_diagnostics"))
    parser.add_argument("--log", default=str(BASE_DIR / "yuanbao_loop.log"))
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    logger = configure_logging(Path(args.log), args.verbose)
    plan = load_plan(Path(args.questions_file))
    schedule = build_schedule(plan["questions"], args.mode or plan.get("mode", "cross"))
    if not schedule:
        raise SystemExit("问题列表为空")
    if args.rounds is not None:
        if args.rounds < 1:
            raise SystemExit("--rounds 必须大于 0")
        schedule = [schedule[index % len(schedule)] for index in range(args.rounds)]
    devices = args.serial or discover_devices(Path(args.adb))
    if not devices:
        raise SystemExit("没有发现在线 MuMu 设备")
    logger.info("发现 %d 台设备，共 %d 个计划轮次", len(devices), len(schedule))
    args.device_order = {serial: index for index, serial in enumerate(devices)}
    threads = [
        threading.Thread(target=worker, args=(serial, schedule, args, logger), daemon=True)
        for serial in devices
    ]
    for thread in threads:
        thread.start()
    try:
        for thread in threads:
            while thread.is_alive():
                thread.join(timeout=0.5)
    except KeyboardInterrupt:
        logger.info("收到停止信号，正在保存进度")
        STOP_EVENT.set()
        for thread in threads:
            thread.join(timeout=10)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
