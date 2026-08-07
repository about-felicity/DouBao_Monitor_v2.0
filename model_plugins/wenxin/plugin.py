from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

from monitor_core.plugins import ModelPlugin, ROOT
from monitor_core.recommendation_questions import validate_prompt_list
from monitor_core.scheduling import normalize_question_mode


class Plugin(ModelPlugin):
    id, name, short_name, tone = "wenxin", "文心", "文", "wenxin"
    questions = ROOT / "wenxin_monitor" / "product.txt"
    runner = ROOT / "wenxin_monitor" / "wenxin_loop.py"
    results = ROOT / "wenxin_monitor" / "wenxin_results.jsonl"
    dashboard = ROOT / "wenxin_monitor" / "dashboard.json"
    builder = ROOT / "wenxin_monitor" / "build_dashboard_data.py"
    execution = "remote"
    supports_control = False

    def ready(self) -> bool:
        return self.questions.exists() and self.runner.exists()

    def command(self, options: dict[str, Any]) -> tuple[list[str], Path]:
        rounds = max(1, min(int(options.get("rounds") or 10), 10000))
        mode = normalize_question_mode(options.get("question_mode"))
        return [sys.executable, str(self.runner), "--questions-file", str(self.questions),
                "--rounds-per-question", str(rounds), "--question-mode", mode,
                "--resume", "--wait", "30", "--random-wait", "90", "--retry-wait", "15"], self.runner.parent

    def prepare(self, options: dict[str, Any], progress: Callable[[str], None] | None = None) -> None:
        if progress:
            progress("正在启动文心专用 Chrome")
        from wenxin_monitor.controller import WenxinWebCollector
        web = WenxinWebCollector(9444)
        if progress:
            progress("正在校验文心 App 与网页会话同步")
        check = self.account_check(web)
        if not check.get("ok"):
            raise ValueError(check.get("message") or "文心 App 与网页未同步")

    def load_questions(self) -> list[str]:
        raw = [line.strip() for line in self.questions.read_text(encoding="utf-8-sig").splitlines()
               if line.strip() and not line.lstrip().startswith("#")]
        return validate_prompt_list(raw)

    def save_questions(self, questions: list[str]) -> None:
        values = validate_prompt_list(questions)
        self.questions.write_text("\n".join(values) + "\n", encoding="utf-8")

    def account_check(self, collector=None) -> dict[str, Any]:
        from wenxin_monitor.controller import WenxinAppController, WenxinWebCollector
        from monitor_core.device_lock import device_session
        app = WenxinAppController("127.0.0.1:16384")
        web = collector or WenxinWebCollector(9444)
        with device_session("127.0.0.1:16384", "文心账号校验", timeout=300):
            mobile = app.account_identity()
        browser = web.account_identity()
        try:
            latest = web.latest_reference()
        except Exception:
            latest = ""
        mobile_logged_in = bool(mobile.get("name"))
        browser_logged_in = bool(browser.get("name"))
        ready = mobile_logged_in and browser_logged_in
        if not mobile_logged_in:
            message = "文心模拟器 App 未登录或未进入可提问页面"
        elif not browser_logged_in:
            message = "文心专用 Chrome 未登录；请在打开的 Chrome 中完成登录"
        else:
            message = "文心 App 与专用 Chrome 均已登录；采集首轮会用新会话再次确认同步关系"
        return {"ok": ready, "status": "logged_in" if ready else "login_required",
                "message": message, "mobile": mobile, "web": browser, "latest": latest,
                "conversation_sync_ready": "/search/" in latest, "location": "local"}

    def stats(self) -> dict[str, Any]:
        if self.results.exists() and (not self.dashboard.exists() or self.results.stat().st_mtime > self.dashboard.stat().st_mtime):
            subprocess.run([sys.executable, str(self.builder)], cwd=self.runner.parent, capture_output=True,
                           timeout=120, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if not self.dashboard.exists():
            return super().stats()
        return json.loads(self.dashboard.read_text(encoding="utf-8"))
