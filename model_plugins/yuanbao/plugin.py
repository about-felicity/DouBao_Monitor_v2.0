from __future__ import annotations

import sys
import json
import subprocess
from pathlib import Path
from typing import Any

from monitor_core.plugins import ModelPlugin, ROOT
from monitor_core.scheduling import normalize_question_mode


class Plugin(ModelPlugin):
    id, name, short_name, tone = "yuanbao", "元宝", "元", "yuanbao"
    questions = ROOT / "yuanbao_monitor" / "product.txt"
    runner = ROOT / "yuanbao_monitor" / "yuanbao_loop.py"
    results = ROOT / "yuanbao_monitor" / "yuanbao_results.jsonl"
    dashboard = ROOT / "yuanbao_monitor" / "dashboard" / "public" / "data" / "dashboard.json"
    builder = ROOT / "yuanbao_monitor" / "build_dashboard_data.py"

    def ready(self) -> bool:
        return self.questions.exists() and self.runner.exists()

    def command(self, options: dict[str, Any]) -> tuple[list[str], Path]:
        rounds = max(1, min(int(options.get("rounds") or 10), 10000))
        mode = normalize_question_mode(options.get("question_mode"))
        runner_mode = "cross" if mode == "interleaved" else "sequential"
        return [sys.executable, str(self.runner), "--questions-file", str(self.questions), "--rounds-per-question", str(rounds), "--mode", runner_mode, "--resume", "--collect-web", "--max-retries", "3"], self.runner.parent

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
