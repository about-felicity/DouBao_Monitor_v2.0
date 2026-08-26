from __future__ import annotations

from pathlib import Path
from typing import Any

from monitor_core.jsonl_dashboard import build_jsonl_dashboard
from monitor_core.plugins import ModelPlugin, ROOT


class Plugin(ModelPlugin):
    """Passive Quark monitor fed by the authenticated LAN callback API."""

    id, name, short_name, tone = "quark", "夸克", "夸", "quark"
    results = ROOT / "quark_monitor" / "quark_results.jsonl"
    dashboard = ROOT / "quark_monitor" / "dashboard.json"
    execution = "remote"
    supports_control = False
    ingest_only = True

    def ready(self) -> bool:
        return True

    def command(self, options: dict[str, Any]) -> tuple[list[str], Path]:
        raise RuntimeError("夸克由外部采集端回传，本机不启动采集任务")

    def load_questions(self) -> list[str]:
        return []

    def save_questions(self, questions: list[str]) -> None:
        raise RuntimeError("夸克问题清单由外部采集端管理")

    def account_check(self) -> dict[str, Any]:
        return {
            "ok": True,
            "status": "ingest_only",
            "message": "夸克监控已就绪，等待外部采集端回传数据",
            "location": "remote",
        }

    def stats(self) -> dict[str, Any]:
        if not self.results.exists():
            return super().stats()
        return build_jsonl_dashboard(self.id, self.results, self.dashboard)
