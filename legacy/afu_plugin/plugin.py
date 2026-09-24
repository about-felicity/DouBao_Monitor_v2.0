from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

from monitor_core.plugins import ModelPlugin, ROOT
from monitor_core.recommendation_questions import validate_prompt_list
from monitor_core.scheduling import normalize_question_mode


class Plugin(ModelPlugin):
    id, name, short_name, tone = "afu", "蚂蚁阿福", "福", "afu"
    questions = ROOT / "afu_monitor" / "product.txt"
    runner = ROOT / "afu_monitor" / "afu_loop.py"
    results = ROOT / "afu_monitor" / "afu_results.jsonl"
    dashboard = ROOT / "afu_monitor" / "dashboard.json"
    builder = ROOT / "afu_monitor" / "build_dashboard_data.py"
    execution = "remote"
    supports_control = False

    @staticmethod
    def _serial() -> str:
        return os.getenv("AFU_MUMU_SERIAL", "127.0.0.1:16384").strip() or "127.0.0.1:16384"

    @staticmethod
    def _chrome_port() -> int:
        try:
            return int(os.getenv("AFU_CHROME_PORT", "9555"))
        except ValueError:
            return 9555

    def ready(self) -> bool:
        return self.questions.exists() and self.runner.exists()

    def command(self, options: dict[str, Any]) -> tuple[list[str], Path]:
        rounds = max(1, min(int(options.get("rounds") or 10), 10000))
        mode = normalize_question_mode(options.get("question_mode"))
        return [sys.executable, str(self.runner), "--questions-file", str(self.questions),
                "--rounds-per-question", str(rounds), "--question-mode", mode,
                "--serial", self._serial(), "--chrome-port", str(self._chrome_port()),
                "--resume", "--wait", "30", "--random-wait", "90"], self.runner.parent

    def prepare(self, options: dict[str, Any], progress: Callable[[str], None] | None = None) -> None:
        if progress:
            progress("正在启动蚂蚁阿福专用 Chrome")
        from afu_monitor.controller import AfuWebCollector
        web = AfuWebCollector(self._chrome_port())
        if progress:
            progress("正在校验蚂蚁阿福 App 与网页登录状态")
        check = self.account_check(web)
        if not check.get("ok"):
            raise ValueError(check.get("message") or "蚂蚁阿福 App 与网页尚未同步")

    def load_questions(self) -> list[str]:
        raw = [line.strip() for line in self.questions.read_text(encoding="utf-8-sig").splitlines()
               if line.strip() and not line.lstrip().startswith("#")]
        return validate_prompt_list(raw)

    def save_questions(self, questions: list[str]) -> None:
        values = validate_prompt_list(questions)
        self.questions.write_text("\n".join(values) + "\n", encoding="utf-8")

    def account_check(self, collector=None) -> dict[str, Any]:
        from afu_monitor.controller import AfuAppController, AfuWebCollector
        from monitor_core.device_lock import device_session
        serial = self._serial()
        app = AfuAppController(serial)
        web = collector or AfuWebCollector(self._chrome_port())
        with device_session(serial, "蚂蚁阿福账号校验", timeout=300):
            mobile = app.account_identity()
        browser = web.account_identity()
        try:
            latest = web.latest_reference()
        except Exception:
            latest = ""
        matched = bool(mobile.get("name") and browser.get("name") and latest and latest != web.HOME)
        return {"ok": matched, "status": "matched" if matched else "mismatch",
                "message": "蚂蚁阿福 App 与网页会话已同步" if matched else "请在蚂蚁阿福专用 Chrome 登录与 MuMu App 相同账号，并确认网页出现历史会话",
                "mobile": mobile, "web": browser, "latest": latest, "location": "local"}

    def stats(self) -> dict[str, Any]:
        if self.results.exists() and (not self.dashboard.exists() or self.results.stat().st_mtime > self.dashboard.stat().st_mtime):
            subprocess.run([sys.executable, str(self.builder)], cwd=self.runner.parent, capture_output=True,
                           timeout=120, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if not self.dashboard.exists():
            return super().stats()
        return json.loads(self.dashboard.read_text(encoding="utf-8"))
