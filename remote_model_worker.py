"""Configure and run exactly one model collector on a remote computer."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import socket
import subprocess
import sys
from typing import Any

from monitor_core.plugins import ROOT, discover_plugins
from monitor_core.lan_result_sync import start as start_result_sync


REMOTE_MODELS = ("deepseek", "yuanbao", "wenxin")


def atomic_write(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def configure(model: str, pairing: Path) -> Path:
    value = json.loads(pairing.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict) or not value.get("receiver_url") or len(str(value.get("token") or "")) < 24:
        raise ValueError("invalid pairing file")
    target = ROOT / "runtime" / "remote_workers" / f"{model}_sync.json"
    atomic_write(target, {**value, "enabled": True, "model": model})
    return target


def validate_sync_config(model: str, path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid remote sync config: {path}") from exc
    urls = [value.get("receiver_url")]
    if isinstance(value.get("receiver_urls"), list):
        urls.extend(value["receiver_urls"])
    if not value.get("enabled") or value.get("model") != model:
        raise ValueError(f"remote sync config is not enabled for {model}")
    if len(str(value.get("token") or "")) < 24:
        raise ValueError("remote sync token is missing or too short")
    if not any(str(url or "").startswith("http://") for url in urls):
        raise ValueError("remote receiver URL is missing")
    return value


def preflight(model: str) -> dict[str, Any]:
    config = ROOT / "runtime" / "remote_workers" / f"{model}_sync.json"
    validate_sync_config(model, config)
    plugin = discover_plugins().get(model)
    if plugin is None or not plugin.ready():
        raise ValueError(f"collector files are incomplete for {model}")
    questions = plugin.load_questions()
    if not questions:
        raise ValueError(f"question list is empty for {model}")
    for module in ("requests", "selenium", "uiautomator2", "websocket"):
        __import__(module)
    return {"ok": True, "model": model, "questions": len(questions), "sync_config": str(config)}


def main() -> int:
    parser = argparse.ArgumentParser(description="Run one remote model worker and return results to the main machine")
    parser.add_argument("--model", choices=REMOTE_MODELS, required=True)
    parser.add_argument("--pairing", type=Path, help="Copy of runtime/lan_result_pairing.json from the main machine")
    parser.add_argument("--rounds", type=int, default=10)
    parser.add_argument("--question-mode", choices=("interleaved", "sequential"), default="interleaved")
    parser.add_argument("--configure-only", action="store_true")
    parser.add_argument("--preflight", action="store_true")
    args = parser.parse_args()
    if args.pairing:
        path = configure(args.model, args.pairing)
        print(f"remote sync configured: {path}")
    if args.configure_only:
        return 0
    if args.preflight:
        print(json.dumps(preflight(args.model), ensure_ascii=False))
        return 0
    guard = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        guard.bind(("127.0.0.1", 18800))
    except OSError as exc:
        guard.close()
        raise SystemExit("This computer already has a remote model collector running.") from exc
    try:
        config = ROOT / "runtime" / "remote_workers" / f"{args.model}_sync.json"
        if not config.exists():
            raise SystemExit("Run once with --pairing <lan_result_pairing.json> before collecting.")
        validate_sync_config(args.model, config)
        start_result_sync(args.model)
        plugin = discover_plugins().get(args.model)
        if plugin is None:
            raise SystemExit(f"unknown model: {args.model}")
        options: dict[str, Any] = {"rounds": max(1, args.rounds), "question_mode": args.question_mode}
        plugin.prepare(options, print)
        command, cwd = plugin.command(options)
        return subprocess.run(command, cwd=cwd, check=False).returncode
    finally:
        guard.close()


if __name__ == "__main__":
    raise SystemExit(main())
