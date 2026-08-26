from __future__ import annotations

import sys
import json
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

from monitor_core.plugins import ModelPlugin, ROOT
from monitor_core.scheduling import normalize_question_mode


class Plugin(ModelPlugin):
    id, name, short_name, tone = "yuanbao", "元宝", "元", "yuanbao"
    questions = ROOT / "yuanbao_monitor" / "product.txt"
    runner = ROOT / "yuanbao_monitor" / "yuanbao_loop.py"
    results = ROOT / "yuanbao_monitor" / "yuanbao_results.jsonl"
    collector_results = ROOT / "runtime" / "remote_workers" / "yuanbao_collector_results.jsonl"
    dashboard = ROOT / "yuanbao_monitor" / "dashboard" / "public" / "data" / "dashboard.json"
    builder = ROOT / "yuanbao_monitor" / "build_dashboard_data.py"
    execution = "remote"
    supports_control = False

    def ready(self) -> bool:
        return self.questions.exists() and self.runner.exists()

    def command(self, options: dict[str, Any]) -> tuple[list[str], Path]:
        rounds = max(1, min(int(options.get("rounds") or 10), 10000))
        mode = normalize_question_mode(options.get("question_mode"))
        runner_mode = "cross" if mode == "interleaved" else "sequential"
        command = [sys.executable, str(self.runner), "--questions-file", str(self.questions),
                "--rounds-per-question", str(rounds),
                "--mode", runner_mode, "--resume", "--collect-web", "--max-retries", "0",
                "--retry-wait", "90", "--wait", "10", "--random-wait", "20",
                "--results", str(self.collector_results)]
        if options.get("restart_completed"):
            command.append("--restart-completed")
        return command, self.runner.parent

    def prepare(self, options: dict[str, Any], progress: Callable[[str], None] | None = None) -> None:
        if progress:
            progress("正在按逍遥实例数量启动 Chrome 并校验全部账号")
        account = self.account_check()
        if not account.get("ok"):
            raise ValueError(account.get("message") or "元宝模拟器与网页账号不一致")
        if progress:
            progress("账号一致，正在启动元宝采集脚本")

    def account_check(self) -> dict[str, Any]:
        from yuanbao_monitor.bowser import ensure_yuanbao_chrome, yuanbao_web_identity
        from yuanbao_monitor.controller import YuanbaoController
        from yuanbao_monitor.yuanbao_loop import (
            BROWSER_MAP_PATH,
            discover_device_records,
            safe_serial,
        )
        from monitor_core.device_lock import device_session

        devices = discover_device_records()
        if not devices:
            return {
                "ok": False, "status": "emulator_required",
                "message": "未发现已启动的逍遥模拟器，请启动后重新检测",
                "instances": [], "location": "local",
            }
        web_sessions = []
        for order, device in enumerate(devices):
            serial = device["serial"]
            port = 9222 + order
            profile = (
                ROOT / "yuanbao_monitor" / "chrome_profile_auto"
                if len(devices) == 1
                else ROOT / "yuanbao_monitor" / "chrome_profiles" / safe_serial(serial)
            )
            ensure_yuanbao_chrome(port, user_data_dir=str(profile))
            web: dict[str, str] = {}
            web_error = ""
            # Chrome can accept CDP connections before the Yuanbao nickname is
            # rendered. Retry the identity read so a cold boot is not reported
            # as a false logout.
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                try:
                    web = yuanbao_web_identity(port)
                    web_error = ""
                    if web.get("name"):
                        break
                except Exception as exc:
                    web_error = str(exc)
                time.sleep(2)
            if not web.get("name") and not web_error:
                web_error = "元宝网页昵称等待 30 秒后仍未显示"
            web_sessions.append({
                "port": port, "profile": str(profile.resolve()),
                "web": web, "web_error": web_error,
            })

        mobile_sessions = []
        for device in devices:
            mobile: dict[str, str] = {}
            mobile_error = ""
            try:
                with device_session(device["serial"], "元宝账号校验", timeout=300):
                    mobile = YuanbaoController(device["serial"]).account_identity()
            except Exception as exc:
                mobile_error = str(exc)
            mobile_sessions.append({
                "device": device, "mobile": mobile,
                "mobile_error": mobile_error,
            })

        # Chrome windows are independent account containers. Their port order
        # does not have to equal the emulator index order, so pair by the
        # detected account name and persist that assignment for collection.
        unused_ports = {int(item["port"]) for item in web_sessions}
        assignments: dict[str, dict[str, Any]] = {}
        instances = []
        for mobile_item in mobile_sessions:
            device = mobile_item["device"]
            serial = device["serial"]
            index = device["index"]
            mobile = mobile_item["mobile"]
            mobile_error = mobile_item["mobile_error"]
            mobile_name = str(mobile.get("name") or "").strip().casefold()
            matched_session = next(
                (
                    item for item in web_sessions
                    if int(item["port"]) in unused_ports
                    and mobile_name
                    and str(item["web"].get("name") or "").strip().casefold()
                    == mobile_name
                ),
                None,
            )
            if matched_session is not None:
                unused_ports.discard(int(matched_session["port"]))
                assignments[serial] = {
                    "port": int(matched_session["port"]),
                    "profile": matched_session["profile"],
                    "account": str(mobile.get("name") or ""),
                    "instance": str(index),
                }
            fallback_session = matched_session or next(
                (item for item in web_sessions if int(item["port"]) in unused_ports),
                web_sessions[0],
            )
            web = fallback_session["web"]
            web_error = fallback_session["web_error"]
            matched = matched_session is not None
            if mobile_error or not mobile.get("name"):
                message = f"实例 {index} 的元宝 App 未登录，请登录后重新检测"
            elif not any(item["web"].get("name") for item in web_sessions):
                message = f"实例 {index} 没有可用的已登录 Chrome，请登录后重新检测"
            elif not matched:
                message = f"实例 {index} 没有找到账号 {mobile['name']} 对应的 Chrome"
            else:
                message = (
                    f"实例 {index} 已按账号自动匹配 Chrome "
                    f"{matched_session['port']}"
                )
            instances.append({
                "index": index, "serial": serial,
                "port": int(matched_session["port"]) if matched else None,
                "ok": matched, "message": message,
                "mobile": mobile, "web": web,
                "mobile_error": mobile_error, "web_error": web_error,
            })
        ok = bool(instances) and all(item["ok"] for item in instances)
        if ok:
            temporary = BROWSER_MAP_PATH.with_suffix(".tmp")
            temporary.write_text(
                json.dumps({"assignments": assignments}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            temporary.replace(BROWSER_MAP_PATH)
        pending = [item["message"] for item in instances if not item["ok"]]
        message = (
            f"全部 {len(instances)} 个逍遥实例已按账号自动匹配网页"
            if ok else "；".join(pending) + "。完成登录后请点击“打开并重新检测”"
        )
        return {
            "ok": ok, "status": "matched" if ok else "login_required",
            "message": message, "instances": instances, "location": "local",
        }

    def load_questions(self) -> list[str]:
        return [line.strip() for line in self.questions.read_text(encoding="utf-8-sig").splitlines() if line.strip() and not line.lstrip().startswith("#")]

    def save_questions(self, questions: list[str]) -> None:
        self.questions.write_text("\n".join(questions) + "\n", encoding="utf-8")

    def stats(self) -> dict[str, Any]:
        if self.builder.exists() and self.results.exists() and (not self.dashboard.exists() or self.results.stat().st_mtime > self.dashboard.stat().st_mtime):
            subprocess.run([sys.executable, str(self.builder)], cwd=str(self.runner.parent), capture_output=True,
                           timeout=120, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if not self.dashboard.exists():
            return super().stats()
        return json.loads(self.dashboard.read_text(encoding="utf-8"))
