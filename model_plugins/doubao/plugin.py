from __future__ import annotations

import json
import sys
from functools import lru_cache
from pathlib import Path
from typing import Any

from monitor_core.plugins import ModelPlugin, ROOT
from monitor_core.analytics import load_doubao_runs


REFS = ROOT / "doubao_refs_result.csv"
ANSWERS = ROOT / "doubao_answers_result.csv"


def _stamp(path: Path) -> int:
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return 0


@lru_cache(maxsize=4)
def _cached_runs(refs_stamp: int, answers_stamp: int) -> list[dict[str, Any]]:
    return load_doubao_runs(REFS, ANSWERS)


class Plugin(ModelPlugin):
    id, name, short_name, tone = "doubao", "豆包", "豆", "doubao"
    supports_control = False
    execution = "remote"
    config = ROOT / "doubao_mumu_controller" / "doubao_mumu_panel_config.json"
    runner = ROOT / "doubao_mumu_controller" / "doubao_mumu_scheduled_job.py"
    readiness_path = ROOT / "doubao_mumu_controller" / "doubao_panel_readiness.json"

    def ready(self) -> bool:
        return self.config.exists() and self.runner.exists()

    def command(self, options: dict[str, Any]) -> tuple[list[str], Path]:
        return [sys.executable, str(self.runner), "--config", str(self.config)], self.runner.parent

    def _config(self) -> dict[str, Any]:
        return json.loads(self.config.read_text(encoding="utf-8-sig")) if self.config.exists() else {"version": 2}

    def load_questions(self) -> list[str]:
        return [str(item.get("text") or "").strip() for item in self._config().get("questions", []) if str(item.get("text") or "").strip()]

    def save_questions(self, questions: list[str]) -> None:
        value = self._config()
        old = {str(item.get("text") or "").strip(): max(1, int(item.get("repeat") or 1)) for item in value.get("questions", [])}
        value["questions"] = [{"text": question, "repeat": old.get(question, 1)} for question in questions]
        self.config.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")

    def account_check(self) -> dict[str, Any]:
        if not self.readiness_path.exists():
            return {"ok": False, "status": "not_checked", "message": "请先在远端豆包控制端执行账号检测"}
        value = json.loads(self.readiness_path.read_text(encoding="utf-8-sig"))
        ready = bool(value.get("ready"))
        return {"ok": ready, "status": "matched" if ready else str(value.get("state") or "not_ready"),
                "message": str(value.get("message") or ("账号一致" if ready else "账号尚未通过校验")),
                "checked_at": str(value.get("updated_at") or ""), "location": "remote"}

    def analytics_runs(self) -> list[dict[str, Any]]:
        return _cached_runs(_stamp(REFS), _stamp(ANSWERS))
