from __future__ import annotations

import importlib.util
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


ROOT = Path(__file__).resolve().parent.parent
PLUGINS_ROOT = ROOT / "model_plugins"


class ModelPlugin:
    id = ""
    name = ""
    short_name = ""
    tone = ""
    supports_control = True
    execution = "local"

    @property
    def stats_endpoint(self) -> str:
        return f"/api/models/{self.id}/stats"

    def metadata(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "short_name": self.short_name,
                "tone": self.tone, "stats_endpoint": self.stats_endpoint,
                "supports_control": self.supports_control,
                "execution": self.execution}

    def ready(self) -> bool:
        raise NotImplementedError

    def command(self, options: dict[str, Any]) -> tuple[list[str], Path]:
        raise NotImplementedError

    def prepare(self, options: dict[str, Any], progress: Callable[[str], None] | None = None) -> None:
        if progress:
            progress("正在准备运行环境")

    def load_questions(self) -> list[str]:
        raise NotImplementedError

    def save_questions(self, questions: list[str]) -> None:
        raise NotImplementedError

    def load_question_mode(self) -> str:
        from monitor_core.scheduling import normalize_question_mode
        path = ROOT / "runtime" / "unified_control" / f"{self.id}_question_mode.txt"
        try:
            return normalize_question_mode(path.read_text(encoding="utf-8"))
        except OSError:
            return "interleaved"

    def save_question_mode(self, mode: str) -> None:
        from monitor_core.scheduling import normalize_question_mode
        path = ROOT / "runtime" / "unified_control" / f"{self.id}_question_mode.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(normalize_question_mode(mode) + "\n", encoding="utf-8")

    def account_check(self) -> dict[str, Any]:
        return {"ok": False, "status": "unsupported", "message": "该模型尚未实现统一账号校验"}

    def stats(self) -> dict[str, Any]:
        return {"generated_at": "", "runs": [], "daily": [], "total_runs": 0,
                "successful_runs": 0, "total_sources": 0, "questions": [], "devices": []}

    def analytics_runs(self) -> list[dict[str, Any]]:
        from monitor_core.analytics import load_generic_runs
        return load_generic_runs(self.id, self.stats())

    def activity(self, limit: int = 40) -> dict[str, Any]:
        if self.execution != "remote":
            return {"ok": True, "model": self.id,
                    "queue": {"queued": 0, "processed": 0, "errors": 0}, "events": []}
        limit = max(1, min(int(limit or 40), 100))
        root = ROOT / "runtime" / "lan_result_receiver" / self.id
        folders = {"queued": root / "inbox", "processed": root / "done", "error": root / "errors"}
        counts = {"queued": 0, "processed": 0, "errors": 0}
        events: list[dict[str, Any]] = []
        for status, folder in folders.items():
            paths = list(folder.glob("*.json")) if folder.exists() else []
            counts["errors" if status == "error" else status] = len(paths)
            for path in sorted(paths, key=lambda item: item.stat().st_mtime_ns, reverse=True)[:limit]:
                try:
                    value = json.loads(path.read_text(encoding="utf-8-sig"))
                except (OSError, ValueError, json.JSONDecodeError):
                    continue
                analysis = value.get("analysis") if isinstance(value.get("analysis"), dict) else {}
                event = {
                    "request_id": str(value.get("request_id") or path.stem),
                    "status": status,
                    "source_device": str(value.get("source_device") or "远端设备"),
                    "question": str(value.get("question") or ""),
                    "received_at": _activity_time(value.get("received_at"), path),
                    "processed_at": _activity_time(value.get("processed_at") or value.get("received_at"), path),
                    "account_uid_masked": str(value.get("account_uid_masked") or ""),
                    "rows_written": int(value.get("rows_written") or analysis.get("source_count") or 0),
                    "message": str(value.get("last_error") or value.get("message") or ""),
                }
                event.update(_generic_analysis_event(status, analysis))
                events.append(event)
        events.sort(key=lambda item: item["processed_at"] or item["received_at"], reverse=True)
        return {"ok": True, "model": self.id, "queue": counts, "events": events[:limit]}


def _activity_time(value: Any, path: Path) -> str:
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, timezone.utc).isoformat(timespec="seconds")
    text = str(value or "").strip()
    if text:
        return text
    return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(timespec="seconds")


def _generic_analysis_event(status: str, analysis: dict[str, Any]) -> dict[str, Any]:
    sources = analysis.get("sources") if isinstance(analysis.get("sources"), list) else []
    source_count = int(analysis.get("source_count") or len(sources) or 0)
    missing_links = int(analysis.get("missing_source_links") or 0)
    missing_titles = int(analysis.get("missing_source_titles") or 0)
    missing_fields = []
    if analysis and not analysis.get("question_present", True):
        missing_fields.append("问题")
    if analysis and not analysis.get("answer_present", True):
        missing_fields.append("正文")
    if missing_links:
        missing_fields.append(f"{missing_links}个信源链接")
    if missing_titles:
        missing_fields.append(f"{missing_titles}个信源标题")
    content_total = 0
    content_analyzed = 0
    content_failed = 0
    owned_marked = 0
    owned_detected = 0
    try:
        from monitor_core.analytics import canonical_url, content_index
        entries = content_index()
    except Exception:
        entries = {}
    for source in sources:
        if not isinstance(source, dict):
            continue
        href = str(source.get("href") or source.get("url") or "").strip()
        if not href:
            continue
        content_total += 1
        entry = (entries.get(href) or entries.get(canonical_url(href))) if isinstance(entries, dict) else None
        if not isinstance(entry, dict):
            continue
        entry_status = str(entry.get("status") or "")
        if entry_status == "ok":
            content_analyzed += 1
        elif entry_status:
            content_failed += 1
        mentions = entry.get("own_product_mentions")
        if entry_status == "ok" and isinstance(mentions, list):
            owned_marked += 1
            if mentions:
                owned_detected += 1
    if status == "queued":
        analysis_status = "pending"
    elif status == "error":
        analysis_status = "failed"
    elif missing_fields or content_failed:
        analysis_status = "warning"
    elif analysis and content_total > content_analyzed:
        analysis_status = "pending"
    else:
        analysis_status = "success"
    return {
        "analysis_status": analysis_status,
        "source_count": source_count,
        "expected_source_count": int(analysis.get("expected_source_count") or source_count),
        "source_capture_complete": bool(analysis.get("source_capture_complete")),
        "missing_source_links": missing_links,
        "missing_source_titles": missing_titles,
        "answer_present": bool(analysis.get("answer_present")) if analysis else None,
        "answer_length": int(analysis.get("answer_length") or 0),
        "recommendation_question": bool(analysis.get("recommendation_question")),
        "product_count": int(analysis.get("product_count") or 0),
        "product_review_status": str(analysis.get("product_review_status") or ""),
        "product_parse_complete": bool(analysis.get("product_parse_complete")),
        "source_content_total": content_total,
        "source_content_analyzed": content_analyzed,
        "source_content_failed": content_failed,
        "owned_product_links_marked": owned_marked,
        "owned_product_links_detected": owned_detected,
        "missing_fields": missing_fields,
    }


def _load_plugin(path: Path) -> ModelPlugin:
    module_name = "monitor_model_" + re.sub(r"[^a-z0-9_]", "_", path.parent.name.lower())
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载模型插件：{path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    plugin = module.Plugin()
    if not isinstance(plugin, ModelPlugin) or not re.fullmatch(r"[a-z0-9_-]+", plugin.id):
        raise RuntimeError(f"模型插件契约无效：{path}")
    return plugin


def discover_plugins() -> dict[str, ModelPlugin]:
    plugins: dict[str, ModelPlugin] = {}
    if not PLUGINS_ROOT.exists():
        return plugins
    for path in sorted(PLUGINS_ROOT.glob("*/plugin.py")):
        plugin = _load_plugin(path)
        if plugin.id in plugins:
            raise RuntimeError(f"重复模型 ID：{plugin.id}")
        plugins[plugin.id] = plugin
    return plugins
