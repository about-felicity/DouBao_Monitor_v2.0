"""Backfill complete historical remote response records into PostgreSQL."""

from __future__ import annotations

import json

from monitor_core.database import close_pool, store_ingest_events
from monitor_core.lan_result_receiver import TARGETS


def main() -> int:
    stored = 0
    events = []
    for model_id, path in TARGETS.items():
        if not path.exists():
            continue
        with path.open("r", encoding="utf-8-sig") as handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except (ValueError, json.JSONDecodeError):
                    continue
                request_id = str(record.get("remote_request_id") or "").strip()
                if not request_id:
                    continue
                events.append((model_id, request_id, record, {}))
                stored += 1
                if len(events) >= 500:
                    store_ingest_events(events)
                    events.clear()
    store_ingest_events(events)
    close_pool()
    print(json.dumps({"ok": True, "stored": stored}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
