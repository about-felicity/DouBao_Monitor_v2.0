from __future__ import annotations

import sys
import json
import subprocess
from pathlib import Path
from typing import Any, Callable

from monitor_core.plugins import ModelPlugin, ROOT
from monitor_core.scheduling import normalize_question_mode


class Plugin(ModelPlugin):
    id, name, short_name, tone = "deepseek", "DeepSeek", "D", "deepseek"
    questions = ROOT / "deepseek_monitor" / "product.txt"
    runner = ROOT / "deepseek_monitor" / "deepseek_loop.py"
    results = ROOT / "deepseek_monitor" / "deepseek_results.jsonl"
    dashboard = ROOT / "deepseek_monitor" / "dashboard.json"
    builder = ROOT / "deepseek_monitor" / "build_dashboard_data.py"

    def ready(self) -> bool:
        return self.questions.exists() and self.runner.exists()

    def command(self, options: dict[str, Any]) -> tuple[list[str], Path]:
        rounds = max(1, min(int(options.get("rounds") or 10), 10000))
        mode = normalize_question_mode(options.get("question_mode"))
        return [sys.executable, str(self.runner), "--questions-file", str(self.questions), "--rounds-per-question", str(rounds), "--question-mode", mode, "--resume", "--min-interval", "60", "--max-interval", "600"], self.runner.parent

    def prepare(self, options: dict[str, Any], progress: Callable[[str], None] | None = None) -> None:
        from deepseek_monitor.controller import ensure_deepseek_chrome
        if progress:
            progress("正在启动或唤醒 DeepSeek 专用 Chrome")
        ensure_deepseek_chrome(9333)
        if progress:
            progress("正在校验模拟器与网页账号")
        account = self.account_check()
        if not account.get("ok"):
            raise ValueError(account.get("message") or "模拟器与网页账号不一致，已阻止启动")
        if progress:
            progress("账号一致，正在启动采集脚本")

    def load_questions(self) -> list[str]:
        return [line.strip() for line in self.questions.read_text(encoding="utf-8-sig").splitlines() if line.strip() and not line.lstrip().startswith("#")]

    def save_questions(self, questions: list[str]) -> None:
        self.questions.write_text("\n".join(questions) + "\n", encoding="utf-8")

    def account_check(self) -> dict[str, Any]:
        from deepseek_monitor.controller import DeepSeekAppController, DeepSeekWebCollector, ensure_deepseek_chrome
        ensure_deepseek_chrome(9333)
        app = DeepSeekAppController("127.0.0.1:16384")
        web = DeepSeekWebCollector(9333)
        mobile = app.account_identity()
        browser = web.account_identity()
        matched = bool(mobile.get("name") and browser.get("name") and mobile["name"].casefold() == browser["name"].casefold())
        return {"ok": matched, "status": "matched" if matched else "mismatch", "message": "模拟器与网页账号一致" if matched else "模拟器与网页账号不一致",
                "mobile": mobile, "web": browser, "location": "local"}

    def stats(self) -> dict[str, Any]:
        if self.builder.exists() and (not self.dashboard.exists() or (self.results.exists() and self.results.stat().st_mtime > self.dashboard.stat().st_mtime)):
            subprocess.run([sys.executable, str(self.builder)], cwd=str(self.runner.parent), capture_output=True,
                           timeout=120, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if not self.dashboard.exists():
            return super().stats()
        return json.loads(self.dashboard.read_text(encoding="utf-8"))
