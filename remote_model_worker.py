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


def main() -> int:
    parser = argparse.ArgumentParser(description="Run one remote model worker and return results to the main machine")
    parser.add_argument("--model", choices=REMOTE_MODELS, required=True)
    parser.add_argument("--pairing", type=Path, help="Copy of runtime/lan_result_pairing.json from the main machine")
    parser.add_argument("--rounds", type=int, default=10)
    parser.add_argument("--question-mode", choices=("interleaved", "sequential"), default="interleaved")
    parser.add_argument("--configure-only", action="store_true")
    args = parser.parse_args()
    if args.pairing:
        path = configure(args.model, args.pairing)
        print(f"remote sync configured: {path}")
    if args.configure_only:
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
