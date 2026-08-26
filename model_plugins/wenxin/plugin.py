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
    runner = ROOT / "wenxin_monitor" / "wenxin_supervisor.py"
    results = ROOT / "wenxin_monitor" / "wenxin_results.jsonl"
    collector_results = ROOT / "runtime" / "remote_workers" / "wenxin_collector_results.jsonl"
    collector_state = ROOT / "runtime" / "remote_workers" / "wenxin_baidu_state.json"
    collector_log = ROOT / "runtime" / "remote_workers" / "wenxin_baidu_loop.log"
    dashboard = ROOT / "wenxin_monitor" / "dashboard.json"
    builder = ROOT / "wenxin_monitor" / "build_dashboard_data.py"
    execution = "remote"
    supports_control = False

    def ready(self) -> bool:
        return self.questions.exists() and self.runner.exists()

    def command(self, options: dict[str, Any]) -> tuple[list[str], Path]:
        rounds = max(1, min(int(options.get("rounds") or 10), 10000))
        tasks = max(1, min(int(options.get("tasks") or 1), 4))
        mode = normalize_question_mode(options.get("question_mode"))
        return [sys.executable, str(self.runner), "--questions-file", str(self.questions),
                "--rounds-per-question", str(rounds), "--question-mode", mode,
                "--tasks", str(tasks), "--wait", "8", "--random-wait", "20",
                "--retry-wait", "8", "--timeout", "45",
                "--results", str(self.collector_results)], self.runner.parent

    def prepare(self, options: dict[str, Any], progress: Callable[[str], None] | None = None) -> None:
        tasks = max(1, min(int(options.get("tasks") or 1), 4))
        from wenxin_monitor.controller import WenxinWebCollector
        for task_id in range(1, tasks + 1):
            if progress:
                progress(f"正在启动并校验任务 {task_id}/{tasks} 的 Scrapling 隐身浏览器")
            profile = ROOT / "runtime" / "remote_workers" / f"wenxin_scrapling_task_{task_id}"
            web = WenxinWebCollector(9443 + task_id, profile=profile)
            try:
                web.ensure_ready()
            finally:
                web.close()

    def load_questions(self) -> list[str]:
        raw = [line.strip() for line in self.questions.read_text(encoding="utf-8-sig").splitlines()
               if line.strip() and not line.lstrip().startswith("#")]
        return validate_prompt_list(raw)

    def save_questions(self, questions: list[str]) -> None:
        values = validate_prompt_list(questions)
        self.questions.write_text("\n".join(values) + "\n", encoding="utf-8")

    def account_check(self, collector=None) -> dict[str, Any]:
        from wenxin_monitor.controller import WenxinWebCollector
        profile = ROOT / "runtime" / "remote_workers" / "wenxin_scrapling_task_1"
        web = collector or WenxinWebCollector(9444, profile=profile)
        try:
            browser = web.account_identity()
            ready = True
            message = "百度搜索页面可用；文心采集将直接抓取搜索结果中的 AI 回答，无需模拟器或登录"
        except Exception as exc:
            browser = {}
            ready = False
            message = f"百度搜索页面检测失败：{exc}"
        finally:
            if collector is None:
                web.close()
        return {"ok": ready, "status": "logged_in" if ready else "login_required",
                "message": message, "mobile": {}, "web": browser, "latest": WenxinWebCollector.HOME,
                "conversation_sync_ready": ready, "capture_mode": "baidu_search_ai",
                "location": "local"}

    def stats(self) -> dict[str, Any]:
        if self.results.exists() and (not self.dashboard.exists() or self.results.stat().st_mtime > self.dashboard.stat().st_mtime):
            subprocess.run([sys.executable, str(self.builder)], cwd=self.runner.parent, capture_output=True,
                           timeout=120, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if not self.dashboard.exists():
            return super().stats()
        return json.loads(self.dashboard.read_text(encoding="utf-8"))
