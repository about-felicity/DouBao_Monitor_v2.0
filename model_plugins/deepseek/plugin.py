from __future__ import annotations

import sys
import json
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

from monitor_core.plugins import ModelPlugin, ROOT
from monitor_core.scheduling import normalize_question_mode


class Plugin(ModelPlugin):
    id, name, short_name, tone = "deepseek", "DeepSeek", "D", "deepseek"
    questions = ROOT / "deepseek_monitor" / "product.txt"
    runner = ROOT / "deepseek_monitor" / "deepseek_loop.py"
    results = ROOT / "deepseek_monitor" / "deepseek_results.jsonl"
    collector_results = ROOT / "runtime" / "remote_workers" / "deepseek_collector_results.jsonl"
    collector_state = ROOT / "runtime" / "remote_workers" / "deepseek_state.json"
    dashboard = ROOT / "deepseek_monitor" / "dashboard.json"
    builder = ROOT / "deepseek_monitor" / "build_dashboard_data.py"
    execution = "remote"
    supports_control = False

    def has_device_binding(self) -> bool:
        return (ROOT / "runtime" / "deepseek_device.json").is_file()

    def device_serial(self) -> str:
        """Use a host-local binding when several MuMu instances are present."""
        config = ROOT / "runtime" / "deepseek_device.json"
        try:
            value = json.loads(config.read_text(encoding="utf-8-sig"))
            serial = str(value.get("serial") or "").strip()
            if serial:
                return serial
        except (OSError, ValueError, json.JSONDecodeError):
            pass
        return "127.0.0.1:16384"

    def ready(self) -> bool:
        return self.questions.exists() and self.runner.exists()

    def command(self, options: dict[str, Any]) -> tuple[list[str], Path]:
        rounds = max(1, min(int(options.get("rounds") or 10), 10000))
        mode = normalize_question_mode(options.get("question_mode"))
        serial = str(getattr(self, "_resolved_serial", "") or self.device_serial())
        return [sys.executable, str(self.runner), "--questions-file", str(self.questions), "--rounds-per-question", str(rounds), "--question-mode", mode, "--resume", "--min-interval", "92", "--max-interval", "118", "--serial", serial, "--state", str(self.collector_state), "--results", str(self.collector_results)], self.runner.parent

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
        from deepseek_monitor.controller import DeepSeekAppController, DeepSeekWebCollector, discover_deepseek_device, ensure_deepseek_device, ensure_deepseek_chrome
        from monitor_core.device_lock import device_session
        ensure_deepseek_chrome(9333)
        web = DeepSeekWebCollector(9333)
        last_error: Exception | None = None
        mobile: dict[str, Any] = {}
        for attempt in range(1, 4):
            try:
                preferred = self.device_serial()
                self._resolved_serial = (
                    ensure_deepseek_device(preferred, timeout=60)
                    if self.has_device_binding()
                    else discover_deepseek_device(preferred)
                )
                ensure_deepseek_device(self._resolved_serial, timeout=45)
                app = DeepSeekAppController(self._resolved_serial, connect_timeout=25)
                with device_session(self._resolved_serial, "DeepSeek 账号校验", timeout=300):
                    mobile = app.account_identity()
                break
            except Exception as exc:
                last_error = exc
                if attempt < 3:
                    time.sleep(4 * attempt)
        else:
            raise RuntimeError(f"DeepSeek MuMu 连续 3 次连接失败：{last_error}") from last_error
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
