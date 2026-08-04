from __future__ import annotations

import json
import sys
import threading
from functools import lru_cache
from pathlib import Path
from typing import Any

from monitor_core.plugins import ModelPlugin, ROOT
from monitor_core.analytics import load_doubao_runs


REFS = ROOT / "doubao_refs_result.csv"
ANSWERS = ROOT / "doubao_answers_result.csv"
PRODUCTS = ROOT / "doubao_products_result.csv"
RUNS_CACHE_LOCK = threading.Lock()
RECEIVER_QUEUE = ROOT / "doubao_mumu_controller" / "lan_receiver_queue"


def _stamp(path: Path) -> int:
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return 0


@lru_cache(maxsize=4)
def _cached_runs(refs_stamp: int, answers_stamp: int, products_stamp: int) -> list[dict[str, Any]]:
    return load_doubao_runs(REFS, ANSWERS, PRODUCTS)


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

    def load_question_mode(self) -> str:
        from monitor_core.scheduling import normalize_question_mode
        return normalize_question_mode(self._config().get("question_mode"))

    def save_question_mode(self, mode: str) -> None:
        from monitor_core.scheduling import normalize_question_mode
        value = self._config()
        value["question_mode"] = normalize_question_mode(mode)
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
        with RUNS_CACHE_LOCK:
            return _cached_runs(_stamp(REFS), _stamp(ANSWERS), _stamp(PRODUCTS))

    def activity(self, limit: int = 40) -> dict[str, Any]:
        limit = max(1, min(int(limit or 40), 100))
        folders = {
            "queued": RECEIVER_QUEUE / "inbox",
            "processed": RECEIVER_QUEUE / "done",
            "error": RECEIVER_QUEUE / "errors",
        }
        events: list[dict[str, Any]] = []
        counts = {"queued": 0, "processed": 0, "errors": 0}
        for status, folder in folders.items():
            paths = list(folder.glob("*.json")) if folder.exists() else []
            counts["errors" if status == "error" else status] = len(paths)
            for path in sorted(paths, key=lambda item: item.stat().st_mtime_ns, reverse=True)[:limit]:
                try:
                    value = json.loads(path.read_text(encoding="utf-8-sig"))
                except (OSError, ValueError, json.JSONDecodeError):
                    continue
                payload = value.get("payload") if isinstance(value.get("payload"), dict) else {}
                save = value.get("save") if isinstance(value.get("save"), dict) else {}
                events.append({
                    "request_id": str(value.get("request_id") or path.stem),
                    "status": status,
                    "source_device": str(value.get("source_device") or payload.get("source_device") or "远端设备"),
                    "question": str(value.get("question") or payload.get("question") or ""),
                    "received_at": str(value.get("received_at") or ""),
                    "processed_at": str(value.get("processed_at") or ""),
                    "account_uid_masked": str(value.get("account_uid_masked") or payload.get("account_uid_masked") or ""),
                    "rows_written": _saved_rows(save),
                    "message": str(value.get("last_error") or ""),
                })
        events.sort(key=lambda item: item["processed_at"] or item["received_at"], reverse=True)
        return {"ok": True, "model": self.id, "queue": counts, "events": events[:limit]}


def _saved_rows(save: dict[str, Any]) -> int:
    raw = save.get("output")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, json.JSONDecodeError):
            raw = {}
    if not isinstance(raw, dict):
        return 0
    return int(raw.get("rows_written") or raw.get("count") or 0)
