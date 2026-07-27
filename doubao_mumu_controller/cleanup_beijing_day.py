from __future__ import annotations

import argparse
import csv
import json
import shutil
from datetime import datetime
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
MONITOR_DIR = BASE_DIR.parent
TIME_KEYS = (
    "captured_at",
    "extractedAt",
    "extracted_at",
    "timestamp",
    "received_at",
    "processed_at",
    "run_time",
)


def record_day(record: dict[str, object]) -> str:
    for key in TIME_KEYS:
        value = str(record.get(key) or "").strip()
        if len(value) >= 10:
            return value[:10]
    return ""


def backup(path: Path, backup_root: Path) -> None:
    relative = path.relative_to(MONITOR_DIR)
    destination = backup_root / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, destination)


def clean_csv(path: Path, day: str, backup_root: Path) -> int:
    if not path.exists():
        return 0
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fieldnames = list(reader.fieldnames or [])
        rows = list(reader)
    kept = [row for row in rows if record_day(row) != day]
    removed = len(rows) - len(kept)
    if not removed:
        return 0
    backup(path, backup_root)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(kept)
    temporary.replace(path)
    return removed


def clean_jsonl(path: Path, day: str, backup_root: Path) -> int:
    if not path.exists():
        return 0
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    kept: list[str] = []
    removed = 0
    for line in lines:
        try:
            value = json.loads(line)
        except Exception:
            kept.append(line)
            continue
        if isinstance(value, dict) and record_day(value) == day:
            removed += 1
        else:
            kept.append(line)
    if not removed:
        return 0
    backup(path, backup_root)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        "".join(item + "\n" for item in kept),
        encoding="utf-8",
    )
    temporary.replace(path)
    return removed


def remove_day_json(path: Path, day: str, backup_root: Path) -> bool:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return False
    if not isinstance(value, dict) or record_day(value) != day:
        return False
    backup(path, backup_root)
    path.unlink()
    return True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("day", help="Beijing date in YYYY-MM-DD format")
    args = parser.parse_args()
    datetime.strptime(args.day, "%Y-%m-%d")
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_root = (
        MONITOR_DIR
        / "data_cleanup_backups"
        / f"beijing_{args.day}_before_reset_{stamp}"
    )

    summary: dict[str, object] = {
        "day": args.day,
        "backup": str(backup_root),
        "csv_removed": {},
        "jsonl_removed": {},
        "queue_files_removed": [],
    }
    for name in (
        "doubao_answers_result.csv",
        "doubao_refs_result.csv",
        "doubao_products_result.csv",
        "doubao_capture_skips.csv",
        "doubao_capture_runtime_errors.csv",
    ):
        path = MONITOR_DIR / name
        removed = clean_csv(path, args.day, backup_root)
        if removed:
            summary["csv_removed"][name] = removed

    for path in BASE_DIR.glob("*.jsonl"):
        removed = clean_jsonl(path, args.day, backup_root)
        if removed:
            summary["jsonl_removed"][path.name] = removed

    queue_root = BASE_DIR / "lan_receiver_queue"
    if queue_root.exists():
        for path in queue_root.rglob("*.json"):
            if remove_day_json(path, args.day, backup_root):
                summary["queue_files_removed"].append(
                    str(path.relative_to(BASE_DIR))
                )

    readiness = BASE_DIR / "doubao_panel_readiness.json"
    if readiness.exists():
        backup(readiness, backup_root)
        readiness.unlink()
        summary["readiness_reset"] = True

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
