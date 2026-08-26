"""腾讯元宝逍遥/MuMu 多设备可靠循环。

提供问题队列、每题重复、交叉/顺序模式、多设备并行、断点续跑、无限重试、
JSONL 结果和失败诊断。控制面板也通过这个入口启动任务。
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import logging
import os
import random
import re
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any

try:
    from .controller import (
        YuanbaoController, YuanbaoGenerationError, is_generation_failure_text,
    )
except ImportError:  # Direct script execution from yuanbao_monitor/.
    from controller import (
        YuanbaoController, YuanbaoGenerationError, is_generation_failure_text,
    )

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from monitor_core.quality import answer_quality_reason
from monitor_core.scheduling import build_question_schedule
from monitor_core.device_lock import device_session
from monitor_core.lan_result_sync import enqueue as enqueue_remote_result


BASE_DIR = Path(__file__).resolve().parent
ADB_CANDIDATES = (
    Path(r"C:\Program Files\Microvirt\MEmu\adb.exe"),
    Path(r"C:\Program Files (x86)\Microvirt\MEmu\adb.exe"),
    Path(r"C:\Program Files\Netease\MuMu\nx_device\15.0\shell\adb.exe"),
    Path(r"C:\Program Files\Netease\MuMu\nx_main\adb.exe"),
)
MEMUC_CANDIDATES = (
    Path(r"C:\Program Files\Microvirt\MEmu\memuc.exe"),
    Path(r"C:\Program Files (x86)\Microvirt\MEmu\memuc.exe"),
)
BROWSER_MAP_PATH = BASE_DIR / "yuanbao_browser_assignments.json"
WRITE_LOCK = threading.Lock()
DASHBOARD_LOCK = threading.Lock()
DASHBOARD_REFRESH_COUNT = 0
STOP_EVENT = threading.Event()


class YuanbaoReaskRequired(RuntimeError):
    """The current mobile/web conversation is invalid and must be asked again."""


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


def resolve_adb(configured: str | Path | None = None) -> Path:
    if configured:
        path = Path(configured)
        if path.is_file():
            return path
        raise FileNotFoundError(f"找不到指定的 ADB：{path}")
    for path in ADB_CANDIDATES:
        if path.is_file():
            return path
    located = shutil.which("adb")
    if located:
        return Path(located)
    raise FileNotFoundError("找不到逍遥或 MuMu 的 ADB")


def resolve_memuc() -> Path | None:
    configured = str(os.environ.get("MEMUC_PATH") or "").strip()
    candidates = ((Path(configured),) if configured else ()) + MEMUC_CANDIDATES
    return next((path for path in candidates if path.is_file()), None)


def discover_device_records(adb: Path | None = None) -> list[dict[str, str]]:
    """Return every running emulator instance with a stable instance index."""
    memuc = resolve_memuc()
    if memuc is not None:
        listed = subprocess.run(
            [str(memuc), "listvms", "--running"], capture_output=True,
            text=True, encoding="mbcs" if sys.platform == "win32" else "utf-8",
            errors="replace", timeout=15, check=False,
        )
        records: list[dict[str, str]] = []
        for row in csv.reader(listed.stdout.splitlines()):
            if len(row) < 4 or not row[0].strip().isdigit() or row[3].strip() != "1":
                continue
            index = row[0].strip()
            serial_result = subprocess.run(
                [str(memuc), "adb", "-i", index, "get-serialno"],
                capture_output=True, text=True,
                encoding="mbcs" if sys.platform == "win32" else "utf-8",
                errors="replace", timeout=15, check=False,
            )
            matches = re.findall(
                r"(?:127\.0\.0\.1|localhost):(\d+)", serial_result.stdout
            )
            if serial_result.returncode == 0 and matches:
                records.append({
                    "index": index,
                    "name": row[1].strip() or f"逍遥模拟器-{index}",
                    "serial": f"127.0.0.1:{matches[-1]}",
                    "emulator": "memu",
                })
        if records:
            return sorted(records, key=lambda item: int(item["index"]))

    adb = adb or resolve_adb()
    if not adb.exists():
        raise FileNotFoundError(f"找不到模拟器 ADB：{adb}")
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
    records = []
    for line in proc.stdout.splitlines()[1:]:
        fields = line.split()
        if len(fields) >= 2 and fields[1] == "device":
            serial = fields[0]
            port_match = re.search(r":(\d+)$", serial)
            port = int(port_match.group(1)) if port_match else 0
            if port >= 16384 and (port - 16384) % 32 == 0:
                index = str((port - 16384) // 32)
            else:
                index = str(len(records))
            records.append({
                "index": index, "name": f"模拟器-{index}",
                "serial": serial, "emulator": "adb",
            })
    return records


def discover_devices(adb: Path | None = None) -> list[str]:
    return [item["serial"] for item in discover_device_records(adb)]


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
    record = {**record, "collector_model": "yuanbao"}
    line = json.dumps(record, ensure_ascii=False) + "\n"
    with WRITE_LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line)
    enqueue_remote_result("yuanbao", record)


def save_state(path: Path, state: dict[str, Any]):
    with WRITE_LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(path.suffix + ".tmp")
        temp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(path)


def refresh_dashboard(logger: logging.Logger):
    """刷新面板；DeepSeek 按批次运行，并阻止多设备重复分析同一条结果。"""
    global DASHBOARD_REFRESH_COUNT
    with DASHBOARD_LOCK:
        DASHBOARD_REFRESH_COUNT += 1
        batch_interval = max(1, int(os.getenv("YUANBAO_AI_BATCH_INTERVAL", "8") or 8))
        env = os.environ.copy()
        if DASHBOARD_REFRESH_COUNT % batch_interval:
            # 面板仍会立即刷新；只把昂贵的 AI 分析留到凑满一批时执行。
            env["YUANBAO_AI_MAX_NEW_PER_BUILD"] = "0"
        try:
            subprocess.run(
                [sys.executable, str(BASE_DIR / "build_dashboard_data.py")],
                cwd=str(BASE_DIR),
                env=env,
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


def load_browser_assignments() -> dict[str, dict[str, Any]]:
    try:
        value = json.loads(BROWSER_MAP_PATH.read_text(encoding="utf-8-sig"))
        assignments = value.get("assignments", value)
        return assignments if isinstance(assignments, dict) else {}
    except (OSError, ValueError, json.JSONDecodeError):
        return {}


def worker(
    serial: str,
    schedule: list[str],
    plan_signature: str,
    args: argparse.Namespace,
    logger: logging.Logger,
):
    tag = safe_serial(serial)
    state_path = Path(args.state_dir) / f"state_{tag}.json"
    pending_path = Path(args.state_dir) / f"pending_web_{tag}.json"
    diagnostic_dir = Path(args.diagnostics) / tag
    start_index = 0
    schedule_origin = 0
    target_end_index = len(schedule)
    if args.resume and state_path.exists():
        try:
            saved_state = json.loads(state_path.read_text(encoding="utf-8"))
            saved_signature = str(saved_state.get("plan_signature") or "")
            if saved_signature and saved_signature != plan_signature:
                if args.forever:
                    # 持续采集时换题库也要沿用全局轮次，避免覆盖旧网页结果；
                    # 新题库则从第一题开始轮换。
                    start_index = int(saved_state.get("next_index", 0))
                    schedule_origin = start_index
                    logger.info(
                        "[%s] 检测到新题库，从总第 %d 轮继续并从新题库第 1 题开始",
                        serial,
                        start_index + 1,
                    )
                else:
                    logger.info("[%s] 检测到任务清单已变化，忽略旧断点并从第 1 轮开始", serial)
            else:
                start_index = int(saved_state.get("next_index", 0))
                schedule_origin = int(saved_state.get("schedule_origin", 0))
                target_end_index = int(saved_state.get("target_end_index", len(schedule)))
        except Exception:
            logger.warning("[%s] 断点文件损坏，将从头开始", serial)

    if args.restart_completed and start_index >= target_end_index:
        # An explicit Start click after a completed finite plan means a new
        # batch. Keep round numbers monotonic so prior web/diagnostic files are
        # never overwritten, while incomplete batches still resume normally.
        schedule_origin = start_index
        target_end_index = start_index + len(schedule)
        pending_path.unlink(missing_ok=True)
        logger.info(
            "[%s] 上一批已完成（%d 轮），自动开启新批次：第 %d-%d 轮",
            serial,
            start_index,
            start_index + 1,
            target_end_index,
        )

    controller: YuanbaoController | None = None
    collector = None

    def collect_web_until_success(
        target_question: str,
        round_index: int,
        previous_reference: str,
        cancel_event: threading.Event,
    ) -> dict[str, Any]:
        """抓取对应网页；失效会话立即交回外层重新提问。"""
        nonlocal collector
        from collector import YuanbaoSourceCollector
        from selenium.common.exceptions import WebDriverException

        web_attempt = 0
        while not STOP_EVENT.is_set() and not cancel_event.is_set():
            web_attempt += 1
            try:
                if collector is None:
                    device_number = args.device_order.get(serial, 0)
                    assignment = args.browser_assignments.get(serial) or {}
                    port = int(assignment.get("port") or (args.chrome_port + device_number))
                    profile = Path(str(assignment.get("profile") or ""))
                    if not str(assignment.get("profile") or ""):
                        profile = (
                            BASE_DIR / "chrome_profile_auto"
                            if len(args.device_order) == 1
                            else BASE_DIR / "chrome_profiles" / tag
                        )
                    collector = YuanbaoSourceCollector(debug_port=port, user_data_dir=str(profile))
                web_path = BASE_DIR / "web_results" / f"result_{tag}_{round_index + 1:06d}.json"
                web_path.parent.mkdir(parents=True, exist_ok=True)
                result = collector.collect(
                    target_question,
                    output_path=str(web_path),
                    wait_reply_timeout=args.web_timeout,
                    extra={"serial": serial, "round": round_index + 1},
                    previous_conversation=previous_reference,
                )
                quality_reason = ""
                if not result.get("error"):
                    quality_reason = answer_quality_reason(
                        target_question,
                        str(result.get("body") or ""),
                    )
                if not result.get("error") and not quality_reason:
                    return result
                failure_text = "\n".join((
                    str(result.get("error") or ""),
                    str(result.get("body") or ""),
                ))
                if quality_reason or is_generation_failure_text(failure_text):
                    raise YuanbaoReaskRequired(
                        "网页会话无有效对应回答，必须重新向元宝发送本题："
                        + str(result.get("error") or quality_reason)
                    )
                raise RuntimeError(str(result.get("error") or quality_reason))
            except Exception as web_exc:
                logger.warning("[%s] 网页抓取第 %d 次失败：%s", serial, web_attempt, web_exc)
                if isinstance(web_exc, YuanbaoReaskRequired):
                    raise
                if (
                    isinstance(web_exc, WebDriverException)
                    or "session" in str(web_exc).lower()
                    or "no such" in str(web_exc).lower()
                ):
                    logger.info("[%s] driver 会话失效，下次将重建 collector", serial)
                    collector = None
                if args.max_retries and web_attempt >= args.max_retries:
                    raise RuntimeError(f"网页抓取达到最大重试次数：{web_exc}") from web_exc
                logger.info("[%s] %.0f 秒后只重试网页抓取", serial, args.retry_wait)
                if cancel_event.wait(args.retry_wait) or STOP_EVENT.is_set():
                    break
        return {}

    index = start_index
    # The generated schedule is the complete target plan. A resumed run must
    # finish at its original length, not add another full plan after the
    # checkpoint (for example 3 + 1300 rounds).
    end_index = target_end_index
    if not args.forever and index >= end_index:
        logger.info(
            "[%s] 断点已完成（%d/%d），没有待续任务；持续采集请使用 --forever。",
            serial,
            index,
            end_index,
        )
    while not STOP_EVENT.is_set() and (args.forever or index < end_index):
        position = (index - schedule_origin) % len(schedule)
        question = schedule[position]
        attempt = 0
        while not STOP_EVENT.is_set():
            attempt += 1
            started = now()
            web_cancel = threading.Event()
            web_executor: ThreadPoolExecutor | None = None
            web_future: Future | None = None
            try:
                xml_path = diagnostic_dir / f"reply_{index + 1:06d}.xml"
                xml_path.parent.mkdir(parents=True, exist_ok=True)
                logger.info("[%s] 第 %d 轮，第 %d 次尝试：%s", serial, index + 1, attempt, question)
                previous_conversation = ""
                pending: dict[str, Any] = {}
                try:
                    pending = json.loads(pending_path.read_text(encoding="utf-8-sig"))
                except (OSError, ValueError, json.JSONDecodeError):
                    pending = {}
                resume_web = bool(
                    args.collect_web
                    and int(pending.get("round") or 0) == index + 1
                    and str(pending.get("question") or "") == question
                    and str(pending.get("status") or "") in {"mobile_sent", "reply_ready"}
                )
                pending_age = 0.0
                try:
                    pending_age = max(0.0, time.time() - pending_path.stat().st_mtime)
                except OSError:
                    pass
                if (
                    resume_web
                    and str(pending.get("status") or "") == "mobile_sent"
                    and not str(pending.get("reply") or "").strip()
                    and pending_age >= max(600, args.web_timeout * 3)
                ):
                    logger.warning(
                        "[%s] 第 %d 轮只有已发送断点且已停滞 %.0f 分钟，清除断点并重新新建对话提问",
                        serial,
                        index + 1,
                        pending_age / 60,
                    )
                    pending_path.unlink(missing_ok=True)
                    pending = {}
                    resume_web = False
                if resume_web and str(pending.get("status") or "") == "mobile_sent":
                    if controller is None:
                        controller = YuanbaoController(
                            serial=serial, connect_timeout=args.connect_timeout
                        )
                    try:
                        visible_xml = controller.d.dump_hierarchy(compressed=False)
                    except Exception:
                        visible_xml = ""
                    if is_generation_failure_text(visible_xml):
                        logger.warning(
                            "[%s] 第 %d 轮检测到元宝生成失败卡片，立即清除断点并重新提问",
                            serial,
                            index + 1,
                        )
                        pending_path.unlink(missing_ok=True)
                        pending = {}
                        resume_web = False
                if resume_web:
                    reply = str(pending.get("reply") or "")
                    previous_conversation = str(pending.get("previous_conversation") or "")
                    started = str(pending.get("started_at") or started)
                    saved_xml = str(pending.get("xml") or "")
                    if saved_xml:
                        xml_path = Path(saved_xml)
                    logger.info(
                        "[%s] 第 %d 轮手机端已发送，按持久断点只恢复网页抓取",
                        serial,
                        index + 1,
                    )
                else:
                    if controller is None:
                        controller = YuanbaoController(serial=serial, connect_timeout=args.connect_timeout)
                    with device_session(serial, "元宝", timeout=args.web_timeout + 240, on_wait=logger.info):
                        if args.collect_web:
                            from collector import YuanbaoSourceCollector
                            if collector is None:
                                device_number = args.device_order.get(serial, 0)
                                assignment = args.browser_assignments.get(serial) or {}
                                port = int(assignment.get("port") or (args.chrome_port + device_number))
                                profile = Path(str(assignment.get("profile") or ""))
                                if not str(assignment.get("profile") or ""):
                                    profile = BASE_DIR / "chrome_profile_auto" if len(args.device_order) == 1 else BASE_DIR / "chrome_profiles" / tag
                                collector = YuanbaoSourceCollector(debug_port=port, user_data_dir=str(profile))
                            previous_conversation = collector.latest_conversation_reference(refresh=True)
                        def remember_mobile_sent() -> None:
                            save_state(
                                pending_path,
                                {
                                    "status": "mobile_sent",
                                    "serial": serial,
                                    "round": index + 1,
                                    "question": question,
                                    "previous_conversation": previous_conversation,
                                    "reply": "",
                                    "xml": str(xml_path),
                                    "started_at": started,
                                    "saved_at": now(),
                                },
                            )

                        def start_web_immediately() -> None:
                            nonlocal web_executor, web_future
                            if web_future is not None:
                                return
                            logger.info(
                                "[%s] 检测到模拟器停止方块变回＋，并发启动第 %d 轮网页抓取",
                                serial,
                                index + 1,
                            )
                            web_executor = ThreadPoolExecutor(
                                max_workers=1,
                                thread_name_prefix=f"yuanbao-web-{tag}",
                            )
                            web_future = web_executor.submit(
                                collect_web_until_success,
                                question,
                                index,
                                previous_conversation,
                                web_cancel,
                            )

                        xml = controller.ask(
                            question,
                            save_xml_path=str(xml_path),
                            on_sent=remember_mobile_sent if args.collect_web else None,
                            on_generation_complete=(
                                start_web_immediately if args.collect_web else None
                            ),
                        )
                    reply = controller.extract_visible_reply(xml, question)
                    skip_reason = answer_quality_reason(question, reply)
                    if skip_reason:
                        raise RuntimeError(f"回答质量校验未通过：{skip_reason}")
                    if args.collect_web:
                        save_state(
                            pending_path,
                            {
                                "status": "reply_ready",
                                "serial": serial,
                                "round": index + 1,
                                "question": question,
                                "previous_conversation": previous_conversation,
                                "reply": reply,
                                "xml": str(xml_path),
                                "started_at": started,
                                "saved_at": now(),
                            },
                        )
                web_result: dict[str, Any] = {}
                if args.collect_web:
                    # 进程中断或旧版误判后，优先复用已经完整落盘且质量合格的
                    # 本轮网页结果，避免永远围绕同一个 mobile_sent 断点重复抓取。
                    cached_web_path = (
                        BASE_DIR / "web_results" /
                        f"result_{tag}_{index + 1:06d}.json"
                    )
                    if resume_web and cached_web_path.exists():
                        try:
                            cached = json.loads(cached_web_path.read_text(encoding="utf-8-sig"))
                            cached_reason = str(cached.get("error") or "")
                            if not cached_reason:
                                cached_reason = answer_quality_reason(
                                    question,
                                    str(cached.get("body") or ""),
                                )
                            if (
                                str(cached.get("question") or "") == question
                                and not cached_reason
                            ):
                                web_result = cached
                                logger.info(
                                    "[%s] 第 %d 轮复用已落盘且校验通过的网页结果",
                                    serial,
                                    index + 1,
                                )
                        except (OSError, ValueError, json.JSONDecodeError):
                            pass
                    # 正常流程在模拟器“停止方块→＋”时已经启动；断点恢复或
                    # 极快回复未捕获到状态变化时，才在这里补启动。
                    if not web_result and web_future is None:
                        logger.info("[%s] 补启动第 %d 轮网页抓取", serial, index + 1)
                        web_executor = ThreadPoolExecutor(
                            max_workers=1,
                            thread_name_prefix=f"yuanbao-web-{tag}",
                        )
                        web_future = web_executor.submit(
                            collect_web_until_success,
                            question,
                            index,
                            previous_conversation,
                            web_cancel,
                        )
                    if not web_result:
                        web_result = web_future.result()
                    if web_executor is not None:
                        web_executor.shutdown(wait=True)
                        web_executor = None
                if STOP_EVENT.is_set():
                    logger.info(
                        "[%s] 已停止；第 %d 轮保留为待网页抓取，下次不会重发",
                        serial,
                        index + 1,
                    )
                    return
                web_skip_reason = str(web_result.get("error") or "") if args.collect_web else ""
                if args.collect_web and not web_skip_reason:
                    web_skip_reason = answer_quality_reason(question, str(web_result.get("body") or ""))
                if web_skip_reason:
                    raise RuntimeError(f"网页回答质量校验未通过：{web_skip_reason}")
                if not reply:
                    reply = str(web_result.get("body") or "")
                record = {
                    "status": "success", "skip_reason": "",
                    "serial": serial, "round": index + 1,
                    "schedule_index": position, "question": question, "reply": reply,
                    "reply_length": len(reply), "attempt": attempt,
                    "started_at": started, "finished_at": now(), "xml": str(xml_path),
                    "web_body": web_result.get("body", ""),
                    "sources": web_result.get("sources", []),
                    "expected_source_count": web_result.get("expected_source_count", len(web_result.get("sources", []))),
                    "source_capture_complete": bool(web_result.get("source_capture_complete")),
                    "web_error": web_result.get("error"),
                }
                append_jsonl(Path(args.results), record)
                refresh_dashboard(logger)
                index += 1
                save_state(state_path, {
                    "serial": serial,
                    "next_index": index,
                    "plan_signature": plan_signature,
                    "schedule_origin": schedule_origin,
                    "target_end_index": end_index,
                    "updated_at": now(),
                })
                pending_path.unlink(missing_ok=True)
                logger.info("[%s] 第 %d 轮完成，提取可见文本 %d 字", serial, index, len(reply))
                break
            except KeyboardInterrupt:
                web_cancel.set()
                if web_executor is not None:
                    web_executor.shutdown(wait=False, cancel_futures=True)
                STOP_EVENT.set()
                break
            except Exception as exc:
                web_cancel.set()
                if web_executor is not None:
                    web_executor.shutdown(wait=False, cancel_futures=True)
                logger.exception("[%s] 第 %d 轮失败：%s", serial, index + 1, exc)
                immediate_reask = isinstance(
                    exc, (YuanbaoGenerationError, YuanbaoReaskRequired)
                )
                if immediate_reask:
                    pending_path.unlink(missing_ok=True)
                    logger.warning(
                        "[%s] 已识别无效回答/失效会话，清除错误断点；立即新建对话并重新发送本题",
                        serial,
                    )
                prefix = f"round_{index + 1:06d}_attempt_{attempt:03d}"
                if controller is not None:
                    controller.save_diagnostics(diagnostic_dir, prefix, str(exc))
                controller = None
                if args.max_retries and attempt >= args.max_retries:
                    logger.error("[%s] 已达到最大重试次数，停止当前设备且不跳过本轮", serial)
                    return
                STOP_EVENT.wait(min(2.0, args.retry_wait) if immediate_reask else args.retry_wait)

        if not STOP_EVENT.is_set():
            delay = args.wait + (random.uniform(0, args.random_wait) if args.random_wait else 0)
            STOP_EVENT.wait(delay)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="腾讯元宝逍遥/MuMu 多设备可靠循环")
    parser.add_argument("--questions-file", default=str(BASE_DIR / "product.txt"))
    parser.add_argument("--mode", choices=("cross", "sequential"), help="覆盖计划文件中的模式")
    parser.add_argument("--serial", action="append", help="指定设备序列号，可重复；默认全部在线设备")
    parser.add_argument("--adb", default="", help="ADB 路径；默认优先自动识别逍遥")
    parser.add_argument("--forever", action="store_true")
    parser.add_argument("--rounds", type=int, help="精确运行轮数；问题不足时循环使用")
    parser.add_argument("--rounds-per-question", type=int, help="每个问题执行的轮数")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument(
        "--restart-completed",
        action="store_true",
        help="已完成相同计划时开启下一批，并延续全局轮次编号",
    )
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
    if args.rounds_per_question is not None:
        if args.rounds_per_question < 1:
            raise SystemExit("--rounds-per-question 必须大于 0")
        schedule = build_question_schedule(
            [str(item["text"]) for item in plan["questions"]],
            args.rounds_per_question,
            "sequential" if (args.mode or plan.get("mode")) == "sequential" else "interleaved",
        )
    elif args.rounds is not None:
        if args.rounds < 1:
            raise SystemExit("--rounds 必须大于 0")
        schedule = [schedule[index % len(schedule)] for index in range(args.rounds)]
    devices = args.serial or discover_devices(resolve_adb(args.adb or None))
    if not devices:
        raise SystemExit("没有发现在线逍遥/MuMu 设备")
    logger.info("发现 %d 台设备，共 %d 个计划轮次", len(devices), len(schedule))
    plan_signature = hashlib.sha256(
        json.dumps(schedule, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    args.device_order = {serial: index for index, serial in enumerate(devices)}
    args.browser_assignments = load_browser_assignments()
    threads = [
        threading.Thread(
            target=worker,
            args=(serial, schedule, plan_signature, args, logger),
            daemon=True,
        )
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
