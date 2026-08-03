from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import urllib.request
import webbrowser


BASE_DIR = Path(__file__).resolve().parent
MONITOR_DIR = BASE_DIR.parent
DASHBOARD_SCRIPT = MONITOR_DIR / "doubao_dashboard_server.py"
JOB_RUNNER = BASE_DIR / "doubao_mumu_scheduled_job.py"
PANEL_CONFIG = BASE_DIR / "doubao_mumu_panel_config.json"
CONTROL_PANEL = BASE_DIR / "doubao_mumu_control_panel.py"
STARTUP_LOG = BASE_DIR / "doubao_remote_startup.log"
CREATE_NO_WINDOW = (
    subprocess.CREATE_NO_WINDOW
    if os.name == "nt" and hasattr(subprocess, "CREATE_NO_WINDOW")
    else 0
)
BEIJING_TZ = timezone(timedelta(hours=8))


def log(message: str) -> None:
    stamp = datetime.now(BEIJING_TZ).isoformat(sep=" ", timespec="seconds")
    line = f"{stamp} {message}"
    print(line, flush=True)
    with STARTUP_LOG.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


def require_file(path: Path, label: str) -> None:
    if not path.exists():
        raise RuntimeError(f"缺少{label}：{path}")


def environment_check() -> dict[str, object]:
    require_file(DASHBOARD_SCRIPT, "实时面板程序")
    require_file(JOB_RUNNER, "任务执行器")
    require_file(PANEL_CONFIG, "问题配置")
    require_file(
        MONITOR_DIR / "run_doubao_latest_grab.py",
        "网页抓取程序",
    )
    require_file(
        MONITOR_DIR / "doubao_ref_extension" / "manifest.json",
        "内置网页抓取器",
    )
    import PIL  # noqa: F401
    import lxml  # noqa: F401
    import openpyxl  # noqa: F401
    import requests  # noqa: F401
    import scrapling  # noqa: F401
    import websocket  # noqa: F401

    from doubao_mumu_web_pipeline import (
        resolve_adb,
        resolve_chrome,
        resolve_mumu_manager,
    )
    from doubao_mumu_loop import (
        APPIUM_MAIN_CANDIDATES,
        APPIUM_NODE_CANDIDATES,
        android_sdk_root_for_adb,
        resolve_global_appium,
        resolve_java_home,
    )

    mumu_manager = resolve_mumu_manager()
    chrome = resolve_chrome()
    try:
        adb = resolve_adb()
    except Exception:
        adb = None
    appium_node = next(
        (path for path in APPIUM_NODE_CANDIDATES if path.is_file()),
        None,
    )
    appium_main = next(
        (path for path in APPIUM_MAIN_CANDIDATES if path.is_file()),
        None,
    )
    if mumu_manager is None:
        raise RuntimeError("找不到 MuMuManager.exe，请先安装并启动 MuMu。")
    if chrome is None:
        raise RuntimeError("找不到 Google Chrome。")
    if adb is None:
        raise RuntimeError("找不到 ADB；请确认 MuMu 安装完整。")
    if appium_node is None or appium_main is None:
        global_appium = resolve_global_appium()
        if global_appium is None:
            raise RuntimeError(
                "找不到便携 Appium、影刀 Appium 或全局 appium.cmd。"
            )
    else:
        global_appium = None
    android_sdk = android_sdk_root_for_adb(adb)
    java_home = resolve_java_home()
    return {
        "python": sys.version.split()[0],
        "mumu_manager": str(mumu_manager),
        "chrome": str(chrome),
        "adb": str(adb),
        "portable_appium": bool(appium_node and appium_main),
        "global_appium": str(global_appium or ""),
        "android_sdk": str(android_sdk),
        "java_home": str(java_home),
    }


def start_dashboard() -> None:
    if port_open(8765):
        log("本地实时检测面板已经运行。")
        return
    dashboard_log = (BASE_DIR / "doubao_remote_dashboard.log").open("ab")
    subprocess.Popen(
        [sys.executable, str(DASHBOARD_SCRIPT)],
        cwd=str(MONITOR_DIR),
        stdout=dashboard_log,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        creationflags=CREATE_NO_WINDOW,
    )
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if port_open(8765):
            log("本地实时检测面板已启动：http://127.0.0.1:8765/")
            return
        time.sleep(0.5)
    raise RuntimeError("实时检测面板启动超时。")


def check_main_receiver() -> dict[str, object]:
    try:
        import doubao_lan_client
        config = doubao_lan_client.load_config()
        if not config.get("enabled"):
            return {"ok": True, "disabled": True, "mode": "standalone"}
        return doubao_lan_client.health_check()
    except Exception as exc:
        return {"ok": False, "disabled": False, "error": str(exc)}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="远端电脑豆包采集端一键启动。")
    parser.add_argument("--check-only", action="store_true")
    parser.add_argument("--no-open-dashboard", action="store_true")
    parser.add_argument("--panel-only", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    log("开始环境自检。")
    environment = environment_check()
    log("环境自检通过：" + json.dumps(environment, ensure_ascii=False))
    receiver = check_main_receiver()
    if receiver.get("disabled"):
        log("当前为本机模式，结果直接写入本机实时面板。")
    elif receiver.get("ok"):
        log("主机接收接口连通。")
    else:
        log(
            "主机接收接口当前不可达，抓取数据将保存在离线队列，"
            "恢复网络后自动续传：" + str(receiver.get("error") or "")
        )
    if args.check_only:
        print(
            json.dumps(
                {"ok": True, "environment": environment, "receiver": receiver},
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    start_dashboard()
    if not args.no_open_dashboard:
        webbrowser.open("http://127.0.0.1:8765/")
    if args.panel_only:
        panel_python = Path(sys.executable).with_name("pythonw.exe")
        if not panel_python.exists():
            panel_python = Path(sys.executable)
        subprocess.Popen(
            [str(panel_python), str(CONTROL_PANEL)],
            cwd=str(BASE_DIR),
            stdin=subprocess.DEVNULL,
            creationflags=CREATE_NO_WINDOW,
        )
        log(
            "操作面板已启动；程序会按 MuMu 数量打开独立 Chrome。"
            "请分别登录后点击“重新检测账号”，仅在全部 UID 一致时"
            "启用绿色开始按钮。"
        )
        return 0
    log(
        "开始整批任务。程序将自动识别 MuMu 账号并启动调试 Chrome，"
        "无需手动安装插件，抓取器会自动注入网页；"
        "若网页未登录同一账号，请在自动打开的 Chrome 中完成登录。"
    )
    child_env = os.environ.copy()
    child_env["PYTHONIOENCODING"] = "utf-8"
    child_env["PYTHONUTF8"] = "1"
    process = subprocess.Popen(
        [
            sys.executable,
            str(JOB_RUNNER),
            "--config",
            str(PANEL_CONFIG),
        ],
        cwd=str(BASE_DIR),
        env=child_env,
    )
    code = process.wait()
    log(f"整批任务结束：退出码={code}。")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
