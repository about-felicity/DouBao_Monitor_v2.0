from __future__ import annotations

import argparse
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import time

import doubao_mumu_loop as mumu


BASE_DIR = Path(__file__).resolve().parent


def build_logger(log_path: Path) -> logging.Logger:
    logger = logging.getLogger("doubao-memu-watchdog")
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        handler = RotatingFileHandler(
            log_path,
            maxBytes=2 * 1024 * 1024,
            backupCount=2,
            encoding="utf-8",
        )
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(message)s")
        )
        logger.addHandler(handler)
    return logger


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Monitor running Doubao emulator instances for ANR/crash dialogs."
    )
    parser.add_argument("--serial", action="append", required=True)
    parser.add_argument("--interval", type=float, default=5.0)
    parser.add_argument(
        "--restart-missing",
        action="store_true",
        help="Restart Doubao when its process disappears (for legacy running jobs).",
    )
    parser.add_argument(
        "--log",
        type=Path,
        default=BASE_DIR / "doubao_mumu_watchdog.log",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    logger = build_logger(args.log)
    controllers = [
        mumu.AdbController(logger, serial=serial)
        for serial in dict.fromkeys(args.serial)
    ]
    logger.info("看门程序启动：%s", ", ".join(args.serial))
    cooldown_until: dict[str, float] = {}
    failure_times: dict[str, list[float]] = {}
    while True:
        for adb in controllers:
            serial = adb.requested_serial or "unknown"
            if time.monotonic() < cooldown_until.get(serial, 0):
                continue
            try:
                failure = adb.doubao_system_failure()
                if failure:
                    now = time.monotonic()
                    recent = [
                        seen
                        for seen in failure_times.get(serial, [])
                        if now - seen <= 180
                    ]
                    recent.append(now)
                    failure_times[serial] = recent
                    logger.warning(
                        "%s 检测到豆包%s，执行关闭并重启。",
                        serial,
                        failure,
                    )
                    if len(recent) >= 3:
                        adb.clear_doubao_cache_and_restart()
                        failure_times[serial] = []
                    else:
                        adb.force_stop_and_restart()
                    cooldown_until[serial] = time.monotonic() + 15
                    continue
                if args.restart_missing and not adb.doubao_pid():
                    logger.warning("%s 豆包进程消失，正在自动拉起。", serial)
                    adb.bring_doubao_foreground()
                    cooldown_until[serial] = time.monotonic() + 10
            except Exception as exc:  # Keep protecting the other instances.
                logger.warning("%s 检查失败：%s", serial, exc)
        time.sleep(max(2.0, args.interval))


if __name__ == "__main__":
    raise SystemExit(main())
