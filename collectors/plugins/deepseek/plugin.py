from __future__ import annotations

from pathlib import Path
from typing import Any

from monitor_core.jsonl_dashboard import build_jsonl_dashboard
from monitor_core.plugins import ModelPlugin, ROOT


class Plugin(ModelPlugin):
    """Passive DeepSeek monitor fed by the Chrome extension callback API."""

    id, name, short_name, tone = "deepseek", "DeepSeek", "D", "deepseek"
    results = ROOT / "collectors" / "deepseek" / "chrome" / "data" / "deepseek_results.jsonl"
    dashboard = ROOT / "collectors" / "deepseek" / "chrome" / "data" / "dashboard.json"
    execution = "remote"
    supports_control = False
    ingest_only = True

    def ready(self) -> bool:
        return True

    def command(self, options: dict[str, Any]) -> tuple[list[str], Path]:
        raise RuntimeError("DeepSeek 由 Chrome 扩展采集并回传，本机面板不直接启动采集任务")

    def load_questions(self) -> list[str]:
        questions = ROOT / "collectors" / "deepseek" / "chrome" / "questions.txt"
        if not questions.exists():
            return []
        return [
            line.strip() for line in questions.read_text(encoding="utf-8-sig").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]

    def save_questions(self, questions: list[str]) -> None:
        raise RuntimeError("DeepSeek 问题清单由 Chrome 扩展管理")

    def account_check(self) -> dict[str, Any]:
        return {
            "ok": True,
            "status": "ingest_only",
            "message": "DeepSeek 回传与分析链路已就绪，等待 Chrome 扩展数据",
            "location": "remote",
        }

    def stats(self) -> dict[str, Any]:
        if not self.results.exists():
            return super().stats()
        return build_jsonl_dashboard(self.id, self.results, self.dashboard)
