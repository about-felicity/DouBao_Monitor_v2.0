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
SOURCE_CONTENT_INDEX = ROOT / "doubao_source_content_index.json"


def _stamp(path: Path) -> int:
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return 0


@lru_cache(maxsize=4)
def _cached_runs(refs_stamp: int, answers_stamp: int, products_stamp: int) -> list[dict[str, Any]]:
    return load_doubao_runs(REFS, ANSWERS, PRODUCTS)


@lru_cache(maxsize=2)
def _cached_source_entries(index_stamp: int) -> dict[str, Any]:
    if not index_stamp or not SOURCE_CONTENT_INDEX.exists():
        return {}
    try:
        value = json.loads(SOURCE_CONTENT_INDEX.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, json.JSONDecodeError):
        return {}
    entries = value.get("entries") if isinstance(value, dict) else None
    return entries if isinstance(entries, dict) else {}


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
                saved = _saved_payload(save)
                analysis = value.get("analysis") if isinstance(value.get("analysis"), dict) else {}
                if not analysis and isinstance(saved.get("analysis"), dict):
                    analysis = saved["analysis"]
                event = {
                    "request_id": str(value.get("request_id") or path.stem),
                    "status": status,
                    "source_device": str(value.get("source_device") or payload.get("source_device") or "远端设备"),
                    "question": str(value.get("question") or payload.get("question") or ""),
                    "received_at": str(value.get("received_at") or ""),
                    "processed_at": str(value.get("processed_at") or ""),
                    "account_uid_masked": str(value.get("account_uid_masked") or payload.get("account_uid_masked") or ""),
                    "rows_written": int(saved.get("rows_written") or saved.get("count") or 0),
                    "message": str(value.get("last_error") or ""),
                }
                event.update(_analysis_event(status, analysis, payload))
                events.append(event)
        events.sort(key=lambda item: item["processed_at"] or item["received_at"], reverse=True)
        return {"ok": True, "model": self.id, "queue": counts, "events": events[:limit]}


def _saved_payload(save: dict[str, Any]) -> dict[str, Any]:
    raw = save.get("output")
    if isinstance(raw, str):
        candidates = [raw, *reversed(raw.splitlines())]
        raw = {}
        for candidate in candidates:
            candidate = candidate.strip()
            if not candidate:
                continue
            try:
                decoded = json.loads(candidate)
            except (ValueError, json.JSONDecodeError):
                continue
            if isinstance(decoded, dict):
                raw = decoded
                break
    if not isinstance(raw, dict):
        return {}
    return raw


def _saved_rows(save: dict[str, Any]) -> int:
    raw = _saved_payload(save)
    return int(raw.get("rows_written") or raw.get("count") or 0)


def _analysis_event(status: str, analysis: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    sources = analysis.get("sources") if isinstance(analysis.get("sources"), list) else []
    if not sources and isinstance(payload.get("items"), list):
        sources = payload["items"]
    source_count = int(analysis.get("source_count") or len(sources) or 0)
    missing_links = int(analysis.get("missing_source_links") or sum(
        not str(item.get("href") or "").strip() for item in sources if isinstance(item, dict)
    ))
    missing_titles = int(analysis.get("missing_source_titles") or sum(
        not str(item.get("title") or "").strip() for item in sources if isinstance(item, dict)
    ))
    entries = _cached_source_entries(_stamp(SOURCE_CONTENT_INDEX))
    content_analyzed = 0
    content_failed = 0
    owned_marked = 0
    owned_detected = 0
    for source in sources:
        if not isinstance(source, dict):
            continue
        href = str(source.get("href") or "").strip()
        title_matches = source.get("owned_products_in_title")
        source_owned = bool(isinstance(title_matches, list) and title_matches)
        entry = entries.get(href) if href else None
        if not isinstance(entry, dict) and href:
            try:
                from monitor_core.analytics import canonical_url
                entry = entries.get(canonical_url(href))
            except Exception:
                entry = None
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
                source_owned = True
        if source_owned:
            owned_detected += 1
    recommendation = bool(analysis.get("recommendation_question"))
    review_status = str(analysis.get("product_review_status") or "")
    missing_fields = []
    if analysis and not analysis.get("question_present", True):
        missing_fields.append("问题")
    if analysis and not analysis.get("answer_present", True):
        missing_fields.append("正文")
    if missing_links:
        missing_fields.append(f"{missing_links}个信源链接")
    if missing_titles:
        missing_fields.append(f"{missing_titles}个信源标题")
    if status == "queued":
        analysis_status = "pending"
    elif status == "error":
        analysis_status = "failed"
    elif missing_fields or (recommendation and review_status != "ai_verified") or content_failed:
        analysis_status = "warning"
    elif sources and content_analyzed < len([item for item in sources if isinstance(item, dict) and str(item.get("href") or "").strip()]):
        analysis_status = "pending"
    else:
        analysis_status = "success"
    return {
        "analysis_status": analysis_status,
        "run_no": analysis.get("run_no"),
        "source_count": source_count,
        "expected_source_count": int(analysis.get("expected_source_count") or source_count),
        "source_capture_complete": bool(analysis.get("source_capture_complete")),
        "missing_source_links": missing_links,
        "missing_source_titles": missing_titles,
        "answer_present": bool(analysis.get("answer_present")) if analysis else None,
        "answer_length": int(analysis.get("answer_length") or 0),
        "recommendation_question": recommendation,
        "product_count": int(analysis.get("product_count") or 0),
        "product_review_status": review_status,
        "product_parse_complete": bool(analysis.get("product_parse_complete")),
        "source_content_total": len([item for item in sources if isinstance(item, dict) and str(item.get("href") or "").strip()]),
        "source_content_analyzed": content_analyzed,
        "source_content_failed": content_failed,
        "owned_product_links_marked": owned_marked,
        "owned_product_links_detected": owned_detected,
        "missing_fields": missing_fields,
    }
