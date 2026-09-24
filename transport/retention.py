"""Hourly cleanup for local collector archives already acknowledged by the server."""

from __future__ import annotations

import json
import time
from pathlib import Path

from transport.sync import (
    ALLOWED_MODELS,
    ROOT,
    _collector_result_paths,
    _load_config,
    prune_collector_results,
)


def main() -> int:
    reports: list[dict[str, object]] = []
    config_root = ROOT / "runtime" / "remote_workers"
    for config_path in sorted(config_root.glob("*_sync.json")):
        model = config_path.stem.removesuffix("_sync")
        if model not in ALLOWED_MODELS:
            continue
        config = _load_config(model)
        if not config.get("enabled"):
            continue
        for result_path in _collector_result_paths(model):
            if not result_path.exists():
                continue
            result = prune_collector_results(model, result_path, force=True)
            reports.append({"model": model, "path": str(result_path), **result})
    status_path = ROOT / "runtime" / "local_result_retention_status.json"
    temporary = status_path.with_suffix(".tmp")
    temporary.write_text(json.dumps({
        "checked_at": time.time(),
        "policy": "delete_only_after_server_acknowledgement",
        "reports": reports,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(status_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
