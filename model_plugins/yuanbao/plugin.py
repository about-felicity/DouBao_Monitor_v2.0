from __future__ import annotations

import sys
import json
import subprocess
from pathlib import Path
from typing import Any, Callable

from monitor_core.plugins import ModelPlugin, ROOT
from monitor_core.scheduling import normalize_question_mode


class Plugin(ModelPlugin):
    id, name, short_name, tone = "yuanbao", "元宝", "元", "yuanbao"
    questions = ROOT / "yuanbao_monitor" / "product.txt"
    runner = ROOT / "yuanbao_monitor" / "yuanbao_loop.py"
    results = ROOT / "yuanbao_monitor" / "yuanbao_results.jsonl"
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
        return [sys.executable, str(self.runner), "--questions-file", str(self.questions), "--rounds-per-question", str(rounds), "--mode", runner_mode, "--resume", "--collect-web", "--max-retries", "0", "--retry-wait", "90", "--wait", "30", "--random-wait", "90"], self.runner.parent

    def prepare(self, options: dict[str, Any], progress: Callable[[str], None] | None = None) -> None:
        from yuanbao_monitor.bowser import ensure_yuanbao_chrome
        if progress:
            progress("正在启动或唤醒元宝专用 Chrome")
        ensure_yuanbao_chrome(9222, user_data_dir=str(ROOT / "yuanbao_monitor" / "chrome_profile_auto"))
        if progress:
            progress("正在校验元宝模拟器与网页账号")
        account = self.account_check()
        if not account.get("ok"):
            raise ValueError(account.get("message") or "元宝模拟器与网页账号不一致")
        if progress:
            progress("账号一致，正在启动元宝采集脚本")

    def account_check(self) -> dict[str, Any]:
        from yuanbao_monitor.bowser import ensure_yuanbao_chrome, yuanbao_web_identity
        from yuanbao_monitor.controller import YuanbaoController
        from monitor_core.device_lock import device_session
        ensure_yuanbao_chrome(9222, user_data_dir=str(ROOT / "yuanbao_monitor" / "chrome_profile_auto"))
        with device_session("127.0.0.1:16384", "元宝账号校验", timeout=300):
            mobile = YuanbaoController("127.0.0.1:16384").account_identity()
        web = yuanbao_web_identity(9222)
        matched = bool(mobile.get("name") and web.get("name") and mobile["name"].casefold() == web["name"].casefold())
        if not mobile.get("name"):
            message = "元宝模拟器 App 未登录或无法读取账号，请登录后重新检测"
        elif not web.get("name"):
            message = "元宝专用 Chrome 未登录，请在打开的 Chrome 中完成登录"
        elif not matched:
            message = "元宝模拟器与专用 Chrome 登录的不是同一个账号"
        else:
            message = "元宝模拟器与网页账号一致"
        return {"ok": matched, "status": "matched" if matched else "login_required",
                "message": message,
                "mobile": mobile, "web": web, "location": "local"}

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
