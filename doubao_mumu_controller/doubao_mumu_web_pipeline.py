from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import random
import re
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from typing import Any
import urllib.request

import doubao_mumu_loop as mumu
try:
    import doubao_lan_client
except ImportError:
    doubao_lan_client = None


BASE_DIR = Path(__file__).resolve().parent
MONITOR_DIR = BASE_DIR.parent
CREATE_NO_WINDOW = (
    subprocess.CREATE_NO_WINDOW
    if os.name == "nt" and hasattr(subprocess, "CREATE_NO_WINDOW")
    else 0
)
MUMU_MANAGER_CANDIDATES = [
    Path(os.environ.get("MUMU_MANAGER_PATH", "")),
    Path(r"C:\Program Files\Netease\MuMu\nx_main\MuMuManager.exe"),
    Path(r"C:\Program Files\Netease\MuMuPlayer-12.0\shell\MuMuManager.exe"),
    Path(os.environ.get("PROGRAMFILES", r"C:\Program Files"))
    / "Netease/MuMu/nx_main/MuMuManager.exe",
    Path(os.environ.get("PROGRAMFILES", r"C:\Program Files"))
    / "Netease/MuMuPlayer-12.0/shell/MuMuManager.exe",
    Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"))
    / "Netease/MuMu/nx_main/MuMuManager.exe",
    Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"))
    / "Netease/MuMuPlayer-12.0/shell/MuMuManager.exe",
]
CHROME_CANDIDATES = [
    Path(os.environ.get("PROGRAMFILES", r"C:\Program Files"))
    / "Google/Chrome/Application/chrome.exe",
    Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"))
    / "Google/Chrome/Application/chrome.exe",
    Path(os.environ.get("LOCALAPPDATA", ""))
    / "Google/Chrome/Application/chrome.exe",
]
ACCOUNT_DB = "/data/user/0/com.larus.nova/databases/account_db"
CDP_SCAN_PORTS = list(range(9222, 9251)) + list(range(9300, 9400))
BROWSER_SLOT_MAP_PATH = BASE_DIR / "doubao_browser_slots.json"
BEIJING_TZ = timezone(timedelta(hours=8))

HISTORY_JS = r"""
(() => {
  const clean = (value) => String(value || "").replace(/\s+/g, " ").trim();
  const seen = new Set();
  return Array.from(document.querySelectorAll('a[href]')).map((a) => {
    const href = String(a.href || "");
    const match = href.match(/^https:\/\/www\.doubao\.com\/chat\/(\d+)/);
    if (!match || seen.has(match[0])) return null;
    seen.add(match[0]);
    return {href: match[0], text: clean(a.innerText || a.textContent || "")};
  }).filter(Boolean);
})()
"""

WEB_ID_JS = r"""
(() => ({
  uid: String(localStorage.getItem("flow_tea_user_id") || ""),
  loggedIn: String(localStorage.getItem("flow_web_has_login") || "") === "true",
  url: location.href,
  title: document.title,
}))()
"""


class PipelineError(RuntimeError):
    pass


class InvalidModelAnswer(PipelineError):
    """The model answered with an empty/error response; this round must be skipped."""


def configure_logging(path: Path, verbose: bool) -> logging.Logger:
    logger = logging.getLogger("doubao_mumu_web_pipeline")
    logger.handlers.clear()
    logger.setLevel(logging.DEBUG)
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S+08:00",
    )
    formatter.converter = lambda timestamp: time.gmtime(timestamp + 8 * 3600)
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.DEBUG if verbose else logging.INFO)
    console.setFormatter(formatter)
    logger.addHandler(console)
    path.parent.mkdir(parents=True, exist_ok=True)
    file_handler = RotatingFileHandler(
        path,
        maxBytes=8 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    return logger


def mask_uid(uid: str) -> str:
    uid = str(uid or "").strip()
    if len(uid) <= 8:
        return uid or "未知"
    return f"{uid[:4]}…{uid[-4:]}"


def uid_key(uid: str) -> str:
    return hashlib.sha256(uid.encode("utf-8")).hexdigest()[:16]


def beijing_now() -> str:
    """Program-side UTC+8 timestamp; never reads MuMu/Android device time."""
    return datetime.now(BEIJING_TZ).isoformat(timespec="seconds")


def first_existing(paths: list[Path]) -> Path | None:
    return next((path for path in paths if path and path.is_file()), None)


def run_text(
    command: list[str],
    *,
    timeout: float = 30,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            creationflags=CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PipelineError(f"命令执行失败或超时：{' '.join(command)}；{exc}") from exc
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise PipelineError(
            f"命令返回 {result.returncode}：{' '.join(command)}；{detail}"
        )
    return result


def _registry_app_path(executable: str) -> Path | None:
    if os.name != "nt":
        return None
    try:
        import winreg
    except ImportError:
        return None
    key_name = (
        r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\\"
        + executable
    )
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(hive, key_name) as key:
                value = str(winreg.QueryValue(key, None) or "").strip().strip('"')
                path = Path(value)
                if path.is_file():
                    return path
        except OSError:
            continue
    return None


def _registry_mumu_manager() -> Path | None:
    if os.name != "nt":
        return None
    try:
        import winreg
    except ImportError:
        return None
    uninstall_keys = (
        r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
        r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall",
    )
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for key_name in uninstall_keys:
            try:
                parent = winreg.OpenKey(hive, key_name)
            except OSError:
                continue
            with parent:
                for index in range(winreg.QueryInfoKey(parent)[0]):
                    try:
                        child_name = winreg.EnumKey(parent, index)
                        with winreg.OpenKey(parent, child_name) as child:
                            display_name = str(
                                winreg.QueryValueEx(child, "DisplayName")[0]
                            )
                            if "mumu" not in display_name.casefold():
                                continue
                            values: list[Path] = []
                            for value_name in ("InstallLocation", "DisplayIcon"):
                                try:
                                    raw = str(
                                        winreg.QueryValueEx(child, value_name)[0]
                                    ).strip().strip('"').split(",", 1)[0]
                                except OSError:
                                    continue
                                value = Path(raw)
                                values.append(
                                    value.parent
                                    if value.name.casefold().endswith(".exe")
                                    else value
                                )
                            for base in values:
                                for root in (base, base.parent):
                                    for relative in (
                                        "MuMuManager.exe",
                                        "nx_main/MuMuManager.exe",
                                        "shell/MuMuManager.exe",
                                    ):
                                        candidate = root / relative
                                        if candidate.is_file():
                                            return candidate
                    except OSError:
                        continue
    return None


def _where_executable(name: str) -> Path | None:
    result = run_text(["where.exe", name], timeout=5, check=False)
    for line in result.stdout.splitlines():
        path = Path(line.strip().strip('"'))
        if path.is_file():
            return path
    return None


def resolve_mumu_manager() -> Path | None:
    return (
        first_existing(MUMU_MANAGER_CANDIDATES)
        or _registry_app_path("MuMuManager.exe")
        or _registry_mumu_manager()
        or _where_executable("MuMuManager.exe")
    )


def resolve_chrome() -> Path | None:
    return (
        first_existing(CHROME_CANDIDATES)
        or _registry_app_path("chrome.exe")
        or _where_executable("chrome.exe")
    )


def parse_mumu_adb_devices(
    output: str,
    requested_index: str | None,
) -> list[dict[str, Any]]:
    """Build MuMu instance records from adb when MuMuManager is unavailable."""
    instances: list[dict[str, Any]] = []
    for line in output.splitlines():
        fields = line.strip().split()
        if len(fields) < 2 or fields[1] != "device":
            continue
        match = re.fullmatch(r"127\.0\.0\.1:(\d+)", fields[0])
        if not match:
            continue
        port = int(match.group(1))
        offset = port - 16384
        if offset < 0 or offset % 32:
            continue
        index = str(offset // 32)
        if requested_index is not None and index != str(requested_index):
            continue
        instances.append(
            {
                "index": index,
                "name": "MuMu安卓设备" + (f"-{index}" if index != "0" else ""),
                "serial": fields[0],
                "pid": None,
            }
        )
    instances.sort(key=lambda item: int(item["index"]))
    return instances


def discover_mumu_instances_via_adb(
    logger: logging.Logger,
    requested_index: str | None,
    reason: Exception | str,
) -> list[dict[str, Any]]:
    adb = resolve_adb()
    result = run_text([str(adb), "devices"], timeout=10, check=False)
    instances = parse_mumu_adb_devices(result.stdout, requested_index)
    if not instances:
        raise PipelineError(
            f"MuMuManager 不可用，ADB 也没有发现在线 MuMu 实例：{reason}"
        )
    logger.warning(
        "MuMuManager 设备查询异常，已自动改用 ADB 在线设备继续：%s",
        reason,
    )
    return instances


def discover_mumu_instances(
    logger: logging.Logger,
    requested_index: str | None,
) -> list[dict[str, Any]]:
    manager = resolve_mumu_manager()
    if manager is None:
        return discover_mumu_instances_via_adb(
            logger,
            requested_index,
            "找不到 MuMuManager.exe",
        )
    try:
        result = run_text([str(manager), "info", "-v", "all"], timeout=15)
    except Exception as exc:
        return discover_mumu_instances_via_adb(logger, requested_index, exc)
    try:
        raw = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        return discover_mumu_instances_via_adb(logger, requested_index, exc)
    values = list(raw.values()) if isinstance(raw, dict) else list(raw)
    instances: list[dict[str, Any]] = []
    for item in values:
        if not isinstance(item, dict) or not item.get("is_process_started"):
            continue
        index = str(item.get("index") or "")
        if requested_index is not None and index != str(requested_index):
            continue
        host = str(item.get("adb_host_ip") or "127.0.0.1")
        port = int(item.get("adb_port") or 0)
        if not port:
            continue
        instances.append(
            {
                "index": index,
                "name": str(item.get("name") or f"MuMu-{index}"),
                "serial": f"{host}:{port}",
                "pid": item.get("pid"),
            }
        )
    if not instances:
        return discover_mumu_instances_via_adb(
            logger,
            requested_index,
            "MuMuManager 没有返回已启动实例",
        )
    instances.sort(key=lambda item: int(item["index"] or 0))
    logger.info(
        "发现 %s 台已启动的 MuMu：%s",
        len(instances),
        "，".join(
            f"{item['name']}[实例 {item['index']}, {item['serial']}]"
            for item in instances
        ),
    )
    return instances


def resolve_adb() -> Path:
    configured = Path(str(os.environ.get("ADB_PATH") or "").strip())
    if str(configured) and configured.is_file():
        return configured
    adb = first_existing(mumu.ADB_CANDIDATES)
    if adb:
        return adb
    manager = resolve_mumu_manager()
    if manager is not None:
        install_root = manager.parent
        dynamic_candidates = [
            install_root / "adb.exe",
            install_root / "shell" / "adb.exe",
            install_root.parent / "adb.exe",
            install_root.parent / "shell" / "adb.exe",
            install_root.parent / "nx_main" / "adb.exe",
        ]
        nx_device = install_root.parent / "nx_device"
        if nx_device.is_dir():
            dynamic_candidates.extend(nx_device.glob("*/shell/adb.exe"))
        adb = first_existing(dynamic_candidates)
        if adb:
            return adb
    located = run_text(["where.exe", "adb"], timeout=5, check=False)
    for line in located.stdout.splitlines():
        path = Path(line.strip())
        if path.exists():
            return path
    raise PipelineError("找不到 adb.exe。")


def adb_command(
    adb: Path,
    serial: str,
    args: list[str],
    *,
    timeout: float = 30,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    return run_text(
        [str(adb), "-s", serial, *args],
        timeout=timeout,
        check=check,
    )


ADB_SHELL_READY_MARKER = "__doubao_adb_shell_ready__"


def adb_shell_ready(adb: Path, serial: str, timeout: float = 5) -> bool:
    """Reject MuMu transports that say device but cannot execute shell calls."""
    try:
        result = adb_command(
            adb,
            serial,
            ["shell", "echo", ADB_SHELL_READY_MARKER],
            timeout=timeout,
            check=False,
        )
    except Exception:
        return False
    return (
        result.returncode == 0
        and ADB_SHELL_READY_MARKER in str(result.stdout or "")
    )


def restart_adb_connections(
    logger: logging.Logger,
    adb: Path,
    serials: list[str],
    timeout: float = 45,
) -> None:
    """Restart the local ADB server and restore every selected MuMu alias."""
    unique_serials = list(dict.fromkeys(str(item) for item in serials if item))
    logger.warning(
        "检测到 MuMu ADB 假在线，正在自动重启 ADB 并重连 %d 个实例。",
        len(unique_serials),
    )
    run_text([str(adb), "kill-server"], timeout=12, check=False)
    run_text([str(adb), "start-server"], timeout=20, check=True)
    deadline = time.monotonic() + max(10.0, timeout)
    pending = set(unique_serials)
    while pending and time.monotonic() < deadline:
        for serial in list(pending):
            run_text([str(adb), "connect", serial], timeout=8, check=False)
            if adb_shell_ready(adb, serial, timeout=5):
                pending.discard(serial)
        if pending:
            time.sleep(1)
    if pending:
        raise PipelineError(
            "ADB 自动恢复后仍无法执行 shell：" + "、".join(sorted(pending))
        )
    logger.info("ADB 自动恢复完成，%d 个 MuMu 实例均可执行 shell。", len(unique_serials))


def ensure_adb_shells_ready(
    logger: logging.Logger,
    adb: Path,
    devices: list[dict[str, Any]],
) -> bool:
    """Repair all selected transports once when any instance is falsely online."""
    serials = [str(item.get("serial") or "") for item in devices]
    unhealthy = [
        serial for serial in serials
        if serial and not adb_shell_ready(adb, serial)
    ]
    if not unhealthy:
        return False
    logger.warning("以下实例 ADB shell 无响应：%s", "、".join(unhealthy))
    restart_adb_connections(logger, adb, serials)
    return True


def wait_adb(adb: Path, serial: str, timeout: float = 30) -> None:
    deadline = time.monotonic() + timeout
    last_reconnect = 0.0
    consecutive_ready = 0
    last_error = ""
    while time.monotonic() < deadline:
        try:
            run_text([str(adb), "connect", serial], timeout=8, check=False)
            state = adb_command(
                adb,
                serial,
                ["get-state"],
                timeout=5,
                check=False,
            )
            state_text = f"{state.stdout} {state.stderr}".lower()
        except PipelineError as exc:
            last_error = str(exc)
            state = None
            state_text = last_error.lower()
        if state is not None and state.returncode == 0 and state.stdout.strip() == "device":
            consecutive_ready += 1
            # adb root/unroot can briefly report "device" before adbd restarts
            # again. Require a stable window so Appium is not started in that
            # false-ready gap.
            if consecutive_ready >= 3:
                return
            time.sleep(0.6)
            continue
        consecutive_ready = 0
        now = time.monotonic()
        if "offline" in state_text and now - last_reconnect >= 2:
            # MuMu can leave its localhost ADB alias offline while the same
            # emulator remains healthy. Recreate that TCP transport so all
            # later Appium and account calls can keep the manager serial.
            try:
                run_text(
                    [str(adb), "disconnect", serial],
                    timeout=8,
                    check=False,
                )
            except PipelineError as exc:
                last_error = str(exc)
            time.sleep(0.4)
            last_reconnect = now
        time.sleep(1)
    detail = f"；最后错误：{last_error}" if last_error else ""
    raise PipelineError(f"ADB 无法连接 MuMu：{serial}{detail}")


def read_mobile_account(
    logger: logging.Logger,
    adb: Path,
    serial: str,
) -> dict[str, str]:
    wait_adb(adb, serial)
    rooted = False
    try:
        root_result = adb_command(
            adb,
            serial,
            ["root"],
            timeout=15,
            check=False,
        )
        root_text = f"{root_result.stdout} {root_result.stderr}".lower()
        if root_result.returncode != 0 or "cannot run as root" in root_text:
            raise PipelineError(
                "当前 MuMu 没有开放 ADB root，无法可靠读取豆包账号 ID。"
            )
        rooted = True
        wait_adb(adb, serial)
        binary = None
        read_deadline = time.monotonic() + 20
        while time.monotonic() < read_deadline:
            wait_adb(adb, serial, timeout=10)
            binary = subprocess.run(
                [str(adb), "-s", serial, "exec-out", "cat", ACCOUNT_DB],
                capture_output=True,
                timeout=20,
                creationflags=CREATE_NO_WINDOW,
            )
            if binary.returncode == 0 and binary.stdout.startswith(b"SQLite format 3"):
                break
            time.sleep(1)
        assert binary is not None
        if binary.returncode != 0 or not binary.stdout.startswith(b"SQLite format 3"):
            detail = binary.stderr.decode("utf-8", errors="replace").strip()
            raise PipelineError(f"读取豆包账号数据库失败：{detail or '不是 SQLite 文件'}")
        fd, temporary_name = tempfile.mkstemp(prefix="doubao_account_", suffix=".db")
        os.close(fd)
        temporary = Path(temporary_name)
        try:
            temporary.write_bytes(binary.stdout)
            connection = sqlite3.connect(str(temporary))
            try:
                row = connection.execute(
                    """
                    SELECT uid, screen_name, type, time
                    FROM login_info
                    WHERE COALESCE(uid, '') <> ''
                    ORDER BY CAST(time AS INTEGER) DESC, rowid DESC
                    LIMIT 1
                    """
                ).fetchone()
            finally:
                connection.close()
        finally:
            temporary.unlink(missing_ok=True)
        if not row or not str(row[0]).strip():
            raise PipelineError("MuMu 豆包尚未登录，账号数据库中没有有效 UID。")
        account = {
            "uid": str(row[0]).strip(),
            "screen_name": str(row[1] or "").strip(),
            "login_type": str(row[2] or "").strip(),
        }
        logger.info(
            "已识别 MuMu 账号：UID=%s，昵称=%s",
            mask_uid(account["uid"]),
            account["screen_name"] or "未设置",
        )
        return account
    finally:
        if rooted:
            adb_command(
                adb,
                serial,
                ["unroot"],
                timeout=15,
                check=False,
            )
            try:
                # MuMu performs a second delayed adbd restart a few seconds
                # after `adb unroot`. Waiting here prevents the account probe
                # from handing Appium a transport that is about to go offline.
                time.sleep(4)
                wait_adb(adb, serial)
            except Exception as exc:
                logger.warning("ADB 从 root 恢复后重连失败，将稍后自动重试：%s", exc)


def import_grabber() -> Any:
    path = MONITOR_DIR / "run_doubao_latest_grab.py"
    if not path.exists():
        raise PipelineError(f"找不到原有网页抓取脚本：{path}")
    # The panel and scheduled task are commonly launched with
    # doubao_mumu_controller as the working directory.  The grabber imports
    # sibling modules from monitor (for example doubao_env_loader), so make
    # that source root importable regardless of the caller's current folder.
    monitor_path = str(MONITOR_DIR)
    if monitor_path not in sys.path:
        sys.path.insert(0, monitor_path)
    spec = importlib.util.spec_from_file_location("doubao_latest_grabber", path)
    if spec is None or spec.loader is None:
        raise PipelineError("无法加载原有网页抓取脚本。")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def cdp_json(port: int, path: str = "/json", timeout: float = 2) -> Any:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(f"http://127.0.0.1:{port}{path}", timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def port_is_listening(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.08):
            return True
    except OSError:
        return False


def doubao_page_for_port(port: int) -> dict[str, Any] | None:
    try:
        pages = cdp_json(port)
    except Exception:
        return None
    candidates = [
        page
        for page in pages
        if page.get("type") == "page"
        and "doubao.com" in f"{page.get('title', '')} {page.get('url', '')}".lower()
    ]
    chats = [
        page
        for page in candidates
        if "https://www.doubao.com/chat" in str(page.get("url") or "")
    ]
    return (chats or candidates or [None])[0]


def web_identity(
    grabber: Any,
    port: int,
    *,
    attempts: int = 3,
) -> dict[str, Any]:
    """Read login/capture state while tolerating Doubao SPA navigation."""
    last_page: dict[str, Any] | None = None
    last_error = ""
    for attempt in range(max(1, attempts)):
        page = doubao_page_for_port(port)
        if not page or not page.get("webSocketDebuggerUrl"):
            last_error = "未找到豆包网页"
        else:
            last_page = page
            ws_url = str(page["webSocketDebuggerUrl"])
            capture_ready = False
            capture_error = ""
            try:
                capture_state = grabber.ensure_page_grabber(ws_url)
                capture_ready = bool(
                    capture_state.get("hasGrab")
                    and capture_state.get("hasLatestGrab")
                )
            except Exception as exc:
                capture_error = str(exc)
            try:
                value = grabber.evaluate_js(
                    ws_url,
                    WEB_ID_JS,
                    timeout=8,
                )
                if not isinstance(value, dict):
                    raise PipelineError("网页账号检测没有返回有效数据")
                return {
                    "uid": str(value.get("uid") or "").strip(),
                    "loggedIn": bool(value.get("loggedIn")),
                    "captureReady": capture_ready,
                    "captureError": capture_error,
                    "detectionError": "",
                    "port": port,
                    "page": page,
                }
            except Exception as exc:
                last_error = str(exc)
        if attempt + 1 < max(1, attempts):
            time.sleep(0.6)
    return {
        "uid": "",
        "loggedIn": False,
        "captureReady": False,
        "captureError": last_error,
        "detectionError": last_error,
        "port": port,
        "page": last_page,
    }


def browser_port_for_slot(slot: str | int | None) -> int | None:
    text = str(slot if slot is not None else "").strip()
    if not text:
        return None
    mapping: dict[str, Any] = {}
    try:
        loaded = json.loads(
            BROWSER_SLOT_MAP_PATH.read_text(encoding="utf-8")
        )
        if isinstance(loaded, dict):
            mapping = loaded
        mapped = int(mapping.get(text) or 0)
        if 9300 <= mapped <= 9399:
            return mapped
    except Exception:
        pass
    try:
        value = int(text)
    except ValueError:
        value = sum(ord(char) for char in text)
    default_port = 9300 + (value % 80)
    for mapped_slot, mapped_port in mapping.items():
        try:
            if str(mapped_slot) != text and int(mapped_port) == default_port:
                return None
        except (TypeError, ValueError):
            continue
    return default_port


def remember_browser_port(slot: str | int | None, port: int) -> None:
    text = str(slot if slot is not None else "").strip()
    if not text:
        return
    try:
        mapping = json.loads(
            BROWSER_SLOT_MAP_PATH.read_text(encoding="utf-8")
        )
        if not isinstance(mapping, dict):
            mapping = {}
    except Exception:
        mapping = {}
    mapping = {
        str(key): int(value)
        for key, value in mapping.items()
        if str(value).isdigit()
        and (str(key) == text or int(value) != int(port))
    }
    mapping[text] = int(port)
    temporary = BROWSER_SLOT_MAP_PATH.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(mapping, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(BROWSER_SLOT_MAP_PATH)


def browser_candidate_ports(preferred_port: int | None = None) -> list[int]:
    """Return browser CDP ports in account-matching priority order."""
    ports: list[int] = []

    def add(value: Any) -> None:
        try:
            port = int(value)
        except (TypeError, ValueError):
            return
        if 1 <= port <= 65535 and port not in ports:
            ports.append(port)

    add(preferred_port)
    try:
        mapping = json.loads(
            BROWSER_SLOT_MAP_PATH.read_text(encoding="utf-8")
        )
        if isinstance(mapping, dict):
            for value in mapping.values():
                add(value)
    except Exception:
        pass
    for port in CDP_SCAN_PORTS:
        add(port)
    return ports


def doubao_debug_port_ready(port: int) -> bool:
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/json/list",
            timeout=1.2,
        ) as response:
            pages = json.loads(response.read().decode("utf-8", errors="replace"))
        return any(
            "doubao.com" in str(item.get("url") or "").lower()
            for item in pages
            if isinstance(item, dict)
        )
    except Exception:
        return False


def find_matching_browser(
    grabber: Any,
    uid: str,
    *,
    preferred_port: int | None = None,
    require_capture_ready: bool = True,
    excluded_ports: set[int] | None = None,
) -> dict[str, Any] | None:
    # Login order is user-controlled. The slot's original port is only a hint;
    # scan all known browser windows so swapped logins can be remapped by UID.
    for port in browser_candidate_ports(preferred_port):
        if excluded_ports and port in excluded_ports:
            continue
        if not port_is_listening(port):
            continue
        try:
            identity = web_identity(grabber, port)
        except Exception:
            continue
        if (
            identity.get("loggedIn")
            and identity.get("uid") == uid
            and (
                identity.get("captureReady")
                or not require_capture_ready
            )
        ):
            return identity
    return None


def choose_free_port(start: int = 9300, end: int = 9399) -> int:
    for port in range(start, end + 1):
        if not port_is_listening(port):
            return port
    raise PipelineError(f"{start}-{end} 没有可用的 Chrome 调试端口。")


def launch_account_browser(
    logger: logging.Logger,
    uid: str,
    *,
    browser_slot: str | int | None = None,
    preferred_port: int | None = None,
) -> tuple[int, subprocess.Popen[Any]]:
    chrome = resolve_chrome()
    if chrome is None:
        raise PipelineError("找不到 Google Chrome，无法启动豆包网页版。")
    extension = MONITOR_DIR / "doubao_ref_extension"
    if not extension.exists():
        raise PipelineError(f"找不到内置豆包网页抓取器：{extension}")
    port = (
        int(preferred_port)
        if preferred_port is not None and not port_is_listening(preferred_port)
        else choose_free_port()
    )
    remember_browser_port(browser_slot, port)
    slot_key = uid_key(str(browser_slot)) if str(browser_slot or "").strip() else "default"
    profiles_root = (
        Path(os.environ.get("LOCALAPPDATA") or BASE_DIR)
        / "DoubaoMuMuBridge"
        / "profiles"
    )
    # A browser belongs to a MuMu slot, not to the account currently signed in.
    # Reuse the newest profile previously created for that slot so startup can
    # open every browser immediately, before reading or validating mobile UIDs.
    existing_profiles = sorted(
        (
            item for item in profiles_root.glob(f"*_mumu_{slot_key}")
            if item.is_dir()
        ),
        key=lambda item: item.stat().st_mtime,
        reverse=True,
    )
    profile_root = (
        existing_profiles[0]
        if existing_profiles
        else profiles_root / f"{uid_key(uid)}_mumu_{slot_key}"
    )
    profile_root.mkdir(parents=True, exist_ok=True)
    log_handle = (BASE_DIR / f"chrome_{uid_key(uid)}.log").open("ab")
    process = subprocess.Popen(
        [
            str(chrome),
            f"--remote-debugging-port={port}",
            "--remote-allow-origins=*",
            f"--user-data-dir={profile_root}",
            f"--load-extension={extension}",
            "--no-first-run",
            "--no-default-browser-check",
            "https://www.doubao.com/chat/",
        ],
        cwd=str(BASE_DIR),
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        creationflags=CREATE_NO_WINDOW,
    )
    logger.info(
        "已为账号 %s / MuMu 实例 %s 启动独立网页会话（调试端口 %s）。",
        mask_uid(uid),
        str(browser_slot if browser_slot is not None else "默认"),
        port,
    )
    return port, process


def ensure_matching_browser(
    logger: logging.Logger,
    grabber: Any,
    uid: str,
    login_wait_seconds: int,
    *,
    browser_slot: str | int | None = None,
) -> dict[str, Any]:
    preferred_port = browser_port_for_slot(browser_slot)
    match = find_matching_browser(
        grabber,
        uid,
        preferred_port=preferred_port,
        require_capture_ready=False,
    )
    if match:
        matched_port = int(match.get("port") or 0)
        if matched_port:
            remember_browser_port(browser_slot, matched_port)
            preferred_port = matched_port
        if match.get("captureReady"):
            logger.info(
                "网页账号校验通过：UID=%s，CDP=%s",
                mask_uid(uid),
                match["port"],
            )
            return match
    process: subprocess.Popen[Any] | None = None
    existing_identity = match
    if (
        existing_identity is None
        and preferred_port is not None
        and port_is_listening(preferred_port)
    ):
        existing_identity = web_identity(grabber, preferred_port)
    if existing_identity and existing_identity.get("page"):
        port = preferred_port
        logger.info(
            "继续使用 MuMu 实例 %s 已打开的调试 Chrome（CDP=%s）。",
            str(browser_slot if browser_slot is not None else "默认"),
            port,
        )
    else:
        port, process = launch_account_browser(
            logger,
            uid,
            browser_slot=browser_slot,
            preferred_port=preferred_port,
        )
    started = time.monotonic()
    next_notice = 0.0
    last_notice_state = ""
    while True:
        if process is not None and process.poll() is not None:
            logger.warning("Chrome 已退出，正在重新启动同账号网页会话。")
            port, process = launch_account_browser(
                logger,
                uid,
                browser_slot=browser_slot,
                preferred_port=preferred_port,
            )
        try:
            identity = web_identity(grabber, port)
        except Exception:
            identity = {"uid": "", "loggedIn": False}
        if (
            identity.get("loggedIn")
            and identity.get("uid") == uid
            and identity.get("captureReady")
        ):
            logger.info("网页登录完成并与 MuMu 账号一致：UID=%s", mask_uid(uid))
            return identity
        now = time.monotonic()
        if not identity.get("loggedIn") or not identity.get("uid"):
            state = "not_logged_in"
        elif identity.get("uid") == uid and not identity.get("captureReady"):
            state = "capture_not_ready"
        else:
            state = "wrong_account"
        if now >= next_notice or state != last_notice_state:
            if state == "not_logged_in":
                logger.warning(
                    "网页端尚未登录；请在已打开的调试 Chrome 登录 "
                    "MuMu 账号 %s。校验通过前不会发送问题。",
                    mask_uid(uid),
                )
            elif state == "capture_not_ready":
                logger.warning(
                    "网页账号已匹配，但抓取器尚未就绪；程序正在自动注入并重试：%s",
                    identity.get("captureError") or "未知错误",
                )
            else:
                logger.warning(
                    "账号不一致：网页 UID=%s，MuMu UID=%s。"
                    "请在调试 Chrome 切换账号；一致前不会发送问题。",
                    mask_uid(str(identity["uid"])),
                    mask_uid(uid),
                )
            next_notice = now + 30
            last_notice_state = state
        if login_wait_seconds and now - started >= login_wait_seconds:
            raise PipelineError(
                f"等待网页登录超过 {login_wait_seconds} 秒，仍未匹配 MuMu 账号。"
            )
        time.sleep(2)


def stale_appium_sessions_for_cleanup(
    sessions: list[dict[str, Any]],
    serial: str,
    system_port: int,
    *,
    limit: int = 12,
) -> tuple[list[dict[str, Any]], int]:
    """Select only sessions that can conflict with this UiAutomator2 port.

    Appium can retain hundreds of dead sessions in its in-memory session list.
    Deleting every historical session for the same emulator made one device
    appear frozen for several minutes at task startup.  A session on another
    systemPort cannot conflict with this pipeline, so leave it alone.  Process
    newest entries first because the current port owner is normally the most
    recent one.
    """
    conflicts: list[dict[str, Any]] = []
    for entry in sessions:
        capabilities = entry.get("capabilities") or {}
        desired = capabilities.get("desired") or {}
        session_serial = str(
            capabilities.get("udid")
            or desired.get("udid")
            or capabilities.get("deviceUDID")
            or ""
        )
        session_port = capabilities.get("systemPort")
        if session_port is None:
            session_port = desired.get("systemPort")
        try:
            same_port = int(session_port) == int(system_port)
        except (TypeError, ValueError):
            same_port = False
        missing_port_for_same_device = session_port in (None, "") and session_serial == serial
        if not same_port and not missing_port_for_same_device:
            continue
        session_id = entry.get("id") or entry.get("sessionId")
        if not session_id:
            continue
        conflicts.append(entry)
    selected = list(reversed(conflicts))[: max(1, int(limit))]
    return selected, max(0, len(conflicts) - len(selected))


def cleanup_stale_appium_sessions(
    logger: logging.Logger,
    appium_url: str,
    serial: str,
    system_port: int,
) -> None:
    base_url = appium_url.rstrip("/")
    try:
        response = mumu.requests.get(f"{base_url}/sessions", timeout=8)
        response.raise_for_status()
        sessions = response.json().get("value") or []
    except Exception as exc:
        logger.debug("读取 Appium 旧会话失败，将由控制器自行恢复：%s", exc)
        return
    cleanup_limit = max(
        1,
        int(os.environ.get("DOUBAO_APPIUM_CLEANUP_LIMIT", "12") or "12"),
    )
    selected, skipped = stale_appium_sessions_for_cleanup(
        sessions,
        serial,
        system_port,
        limit=cleanup_limit,
    )
    removed = 0
    for entry in selected:
        session_id = entry.get("id") or entry.get("sessionId")
        try:
            mumu.requests.delete(
                f"{base_url}/session/{session_id}",
                timeout=3,
            )
            removed += 1
        except Exception as exc:
            logger.debug("清理 Appium 会话 %s 失败：%s", session_id, exc)
    if removed:
        logger.info("已清理 %s 个本设备/本端口的旧 Appium 会话。", removed)
    if skipped:
        logger.warning(
            "Appium 端口 %s 仍登记 %s 个更早的历史会话；"
            "已跳过逐个清理，避免设备启动长时间无响应。",
            system_port,
            skipped,
        )


def normalize_text(value: str) -> str:
    return re.sub(r"[\W_]+", "", str(value or ""), flags=re.UNICODE).lower()


def title_matches_question(title: str, question: str) -> bool:
    title_key = normalize_text(re.sub(r"\s+\d+$", "", title))
    question_key = normalize_text(question)
    if not title_key or not question_key:
        return False
    return (
        title_key in question_key
        or question_key in title_key
        or (
            len(title_key) >= 8
            and question_key.startswith(title_key[: min(20, len(title_key))])
        )
    )


def browser_page(grabber: Any, port: int, prefer_url: str = "") -> dict[str, Any]:
    grabber.CDP_HOST = f"http://127.0.0.1:{port}"
    return grabber.find_doubao_page(prefer_url)


def history_snapshot(grabber: Any, port: int) -> list[dict[str, str]]:
    page = browser_page(grabber, port)
    ws_url = page.get("webSocketDebuggerUrl")
    if not ws_url:
        raise PipelineError("豆包网页缺少 Chrome DevTools WebSocket 地址。")
    value = grabber.evaluate_js(ws_url, HISTORY_JS, timeout=10)
    return value if isinstance(value, list) else []


def extract_page_question(
    grabber: Any,
    ws_url: str,
    expected: str = "",
) -> str:
    value = grabber.evaluate_js(
        ws_url,
        r"""
(() => Array.from(document.querySelectorAll("[data-message-id]"))
  .filter((element) => String(element.className || "").includes("justify-end"))
  .map((element) => String(element.innerText || element.textContent || "").trim())
  .filter(Boolean))()
""",
        timeout=12,
    )
    questions = value if isinstance(value, list) else []
    if expected:
        expected_key = normalize_text(expected)
        exact = next(
            (
                str(question).strip()
                for question in reversed(questions)
                if normalize_text(str(question)) == expected_key
            ),
            "",
        )
        if exact:
            return exact
    if questions:
        return str(questions[-1]).strip()
    return ""


def navigate_to_chat_for_question(
    grabber: Any,
    port: int,
    ws_url: str,
    href: str,
    question: str,
    timeout: float = 35,
) -> tuple[str, str]:
    try:
        grabber.cdp_call(
            ws_url,
            "Page.navigate",
            {"url": href},
            timeout=10,
        )
    except Exception as exc:
        if not grabber.is_target_navigated_error(exc):
            raise
    deadline = time.monotonic() + timeout
    last_question = ""
    while time.monotonic() < deadline:
        time.sleep(1)
        try:
            page = browser_page(grabber, port, href)
            ws_url = page.get("webSocketDebuggerUrl") or ws_url
            current_url = str(page.get("url") or "").rstrip("/")
            if current_url != href.rstrip("/"):
                continue
            last_question = extract_page_question(grabber, ws_url, question)
            if normalize_text(last_question) == normalize_text(question):
                return ws_url, last_question
        except Exception:
            continue
    raise PipelineError(
        f"网页已发现新会话但页面问题校验失败：期望={question}，实际={last_question}"
    )


def wait_for_synced_chat(
    logger: logging.Logger,
    grabber: Any,
    port: int,
    question: str,
    baseline_hrefs: set[str],
    timeout: float,
) -> tuple[str, str]:
    started = time.monotonic()
    last_reload = 0.0
    next_notice = 0.0
    while True:
        identity = web_identity(grabber, port)
        if not identity.get("loggedIn"):
            elapsed = time.monotonic() - started
            if timeout and elapsed >= timeout:
                raise PipelineError("豆包网页长时间未恢复登录状态。")
            if elapsed >= next_notice:
                logger.warning("豆包网页正在重载或等待重新登录，脚本继续等待。")
                next_notice = elapsed + 30
            time.sleep(2)
            continue
        page = identity.get("page") or browser_page(grabber, port)
        ws_url = page.get("webSocketDebuggerUrl")
        if not ws_url:
            raise PipelineError("豆包网页调试连接已失效。")
        try:
            history = grabber.evaluate_js(ws_url, HISTORY_JS, timeout=10)
        except Exception:
            history = []
        if not isinstance(history, list):
            history = []
        new_items = [
            item
            for item in history
            if isinstance(item, dict)
            and item.get("href")
            and str(item["href"]).rstrip("/") not in baseline_hrefs
        ]
        new_items.sort(
            key=lambda item: not title_matches_question(
                str(item.get("text") or ""),
                question,
            )
        )
        for item in new_items:
            href = str(item["href"]).rstrip("/")
            title = str(item.get("text") or "").strip()
            try:
                ws_url, actual = navigate_to_chat_for_question(
                    grabber,
                    port,
                    ws_url,
                    href,
                    question,
                )
                logger.info("网页已同步到本轮新会话：%s", href)
                return href, ws_url
            except Exception as exc:
                logger.debug(
                    "检查网页候选会话失败（标题=%s）：%s",
                    title,
                    exc,
                )
        elapsed = time.monotonic() - started
        if timeout and elapsed >= timeout:
            raise PipelineError(
                f"等待网页同步新会话超过 {timeout:g} 秒：{question}"
            )
        if elapsed - last_reload >= 12:
            try:
                grabber.cdp_call(
                    ws_url,
                    "Page.reload",
                    {"ignoreCache": False},
                    timeout=8,
                )
            except Exception:
                pass
            last_reload = elapsed
        if elapsed >= next_notice:
            logger.info("仍在等待网页同步本轮新消息（已等待 %.0f 秒）", elapsed)
            next_notice = elapsed + 30
        time.sleep(2)


def read_web_answer_state(
    grabber: Any,
    ws_url: str,
    question: str,
) -> dict[str, Any]:
    clean_answer_js = """
(() => {
  const expected = %s;
  const clean = (value) => String(value || "").replace(/\\s+/g, "").trim();
  const items = Array.from(document.querySelectorAll("[data-message-id]"))
    .map((element) => ({
      text: String(element.innerText || element.textContent || "").trim(),
      isUser: String(element.className || "").includes("justify-end"),
    }))
    .filter((item) => item.text);
  let questionIndex = -1;
  for (let index = 0; index < items.length; index += 1) {
    if (items[index].isUser && clean(items[index].text) === clean(expected)) {
      questionIndex = index;
    }
  }
  const controls = Array.from(
    document.querySelectorAll("button,[role='button']")
  );
  const generating = controls.some((element) => {
    const label = [
      element.innerText,
      element.textContent,
      element.getAttribute("aria-label"),
      element.getAttribute("title"),
    ].join(" ");
    return /停止生成|停止回答|stop generating|stop response/i.test(label);
  });
  if (questionIndex < 0) {
    return {ok: false, answer: "", generating};
  }
  const parts = [];
  for (let index = questionIndex + 1; index < items.length; index += 1) {
    if (items[index].isUser) break;
    parts.push(items[index].text);
  }
  return {
    ok: parts.length > 0,
    answer: parts.join("\\n").trim(),
    generating,
  };
})()
""" % json.dumps(question, ensure_ascii=False)
    clean_value = grabber.evaluate_js(ws_url, clean_answer_js, timeout=12)
    if not isinstance(clean_value, dict):
        return {"answer": "", "generating": False}
    return {
        "answer": str(clean_value.get("answer") or "").strip(),
        "generating": bool(clean_value.get("generating")),
    }


def read_web_answer(grabber: Any, ws_url: str, question: str) -> str:
    return str(
        read_web_answer_state(grabber, ws_url, question).get("answer") or ""
    ).strip()


def is_doubao_error_answer(answer: str) -> bool:
    text = re.sub(r"\s+", "", str(answer or ""))
    return text in {
        "出了点问题，请稍后重试。",
        "出了点问题请稍后重试",
        "网络异常，请稍后重试。",
        "网络异常请稍后重试",
        "服务异常，请稍后重试。",
        "服务异常请稍后重试",
    }


def wait_for_web_answer_stable(
    logger: logging.Logger,
    grabber: Any,
    ws_url: str,
    question: str,
    *,
    min_wait: float,
    stable_seconds: float,
    timeout: float,
) -> str:
    started = time.monotonic()
    last_answer = ""
    stable_since: float | None = None
    next_notice = 0.0
    last_reload = 0.0
    empty_reload_after = max(8.0, min(12.0, min_wait + 2.0))
    while True:
        try:
            answer_state = read_web_answer_state(grabber, ws_url, question)
        except Exception:
            answer_state = {"answer": "", "generating": False}
        answer = str(answer_state.get("answer") or "").strip()
        generating = bool(answer_state.get("generating"))
        elapsed = time.monotonic() - started
        if is_doubao_error_answer(answer):
            raise InvalidModelAnswer(f"豆包返回系统异常：{answer}")
        effective_min_wait = min(4.0, max(1.5, min_wait * 0.4))
        dynamic_stable_seconds = min(
            stable_seconds,
            2.5 if len(answer) >= 120 else 3.5,
        )
        if (
            answer
            and answer == last_answer
            and elapsed >= effective_min_wait
            and not generating
        ):
            if stable_since is None:
                stable_since = time.monotonic()
            stable_for = time.monotonic() - stable_since
            if stable_for >= dynamic_stable_seconds:
                logger.info(
                    "网页回答动态稳定判定完成：长度=%s，等待=%.1f 秒。",
                    len(answer),
                    elapsed,
                )
                return answer
        else:
            stable_since = None
        if (
            not answer
            and elapsed - last_reload >= empty_reload_after
        ):
            try:
                grabber.cdp_call(
                    ws_url,
                    "Page.reload",
                    {"ignoreCache": True},
                    timeout=8,
                )
                logger.warning(
                    "网页连续 %.0f 秒没有回答正文，已自动刷新并继续等待。",
                    elapsed - last_reload,
                )
            except Exception as exc:
                logger.warning("网页空白且刷新失败，继续自动恢复：%s", exc)
            last_reload = elapsed
            stable_since = None
            time.sleep(1.5)
            continue
        if elapsed >= timeout:
            if answer:
                logger.warning(
                    "网页回答稳定判定超时，但已有 %s 字，继续执行抓取。",
                    len(answer),
                )
                return answer
            raise InvalidModelAnswer(f"等待网页回答超过 {timeout:g} 秒仍没有正文。")
        if elapsed >= next_notice:
            logger.info(
                "等待网页回答生成（已等待 %.0f 秒，当前 %s 字）",
                elapsed,
                len(answer),
            )
            next_notice = elapsed + 30
        answer_changed = answer != last_answer
        last_answer = answer
        time.sleep(0.7 if not answer or answer_changed else 1.0)


def grab_and_save(
    logger: logging.Logger,
    grabber: Any,
    ws_url: str,
    href: str,
    question: str,
    device: dict[str, Any],
    account: dict[str, str],
    question_sent_at: str,
    answer_completed_at: str,
    known_answer: str = "",
) -> dict[str, Any]:
    uid = account["uid"]
    try:
        clean_answer = read_web_answer(grabber, ws_url, question) or known_answer
    except Exception as exc:
        logger.debug("读取网页回答气泡失败，将使用原抓取结果：%s", exc)
        clean_answer = known_answer

    old_threshold = os.environ.get("DOUBAO_NO_REFERENCE_ANSWER_MIN_LENGTH")
    old_max_attempts = os.environ.get("DOUBAO_GRAB_MAX_ATTEMPTS")
    old_eval_timeout = os.environ.get("DOUBAO_GRAB_EVAL_TIMEOUT")
    old_reload_retry = os.environ.get("DOUBAO_GRAB_RELOAD_RETRY")
    # A freshly reloaded Doubao shell can expose a short placeholder answer
    # before its reference block hydrates. Do not classify that transient state
    # as a legitimate zero-reference response.
    os.environ["DOUBAO_NO_REFERENCE_ANSWER_MIN_LENGTH"] = "120"
    os.environ["DOUBAO_GRAB_MAX_ATTEMPTS"] = "5"
    os.environ["DOUBAO_GRAB_EVAL_TIMEOUT"] = "25"
    os.environ["DOUBAO_GRAB_RELOAD_RETRY"] = "2"
    try:
        grab_attempt = 0
        while True:
            grab_attempt += 1
            try:
                payload = grabber.grab_with_retry(ws_url, href)
                payload_answer = str(
                    payload.get("answerText")
                    or payload.get("answer_text")
                    or ""
                ).strip()
                if (
                    (payload_answer or clean_answer)
                    and not is_doubao_error_answer(payload_answer or clean_answer)
                ):
                    break
                raise PipelineError("插件返回的回答正文为空。")
            except Exception as exc:
                logger.warning(
                    "网页抓取第 %s 次未取得有效数据，将刷新后继续：%s",
                    grab_attempt,
                    exc,
                )
                try:
                    grabber.cdp_call(
                        ws_url,
                        "Page.reload",
                        {"ignoreCache": True},
                        timeout=8,
                    )
                except Exception as reload_exc:
                    logger.debug("抓取恢复刷新失败：%s", reload_exc)
                time.sleep(min(8.0, 1.5 + grab_attempt))
                try:
                    clean_answer = (
                        read_web_answer(grabber, ws_url, question)
                        or clean_answer
                        or known_answer
                    )
                except Exception:
                    pass
    finally:
        if old_threshold is None:
            os.environ.pop("DOUBAO_NO_REFERENCE_ANSWER_MIN_LENGTH", None)
        else:
            os.environ["DOUBAO_NO_REFERENCE_ANSWER_MIN_LENGTH"] = old_threshold
        if old_max_attempts is None:
            os.environ.pop("DOUBAO_GRAB_MAX_ATTEMPTS", None)
        else:
            os.environ["DOUBAO_GRAB_MAX_ATTEMPTS"] = old_max_attempts
        if old_eval_timeout is None:
            os.environ.pop("DOUBAO_GRAB_EVAL_TIMEOUT", None)
        else:
            os.environ["DOUBAO_GRAB_EVAL_TIMEOUT"] = old_eval_timeout
        if old_reload_retry is None:
            os.environ.pop("DOUBAO_GRAB_RELOAD_RETRY", None)
        else:
            os.environ["DOUBAO_GRAB_RELOAD_RETRY"] = old_reload_retry
    payload["question"] = question
    payload["chatTitle"] = question
    if clean_answer:
        payload["answerText"] = clean_answer
    payload["mumu_instance"] = device["index"]
    payload["mumu_serial"] = device["serial"]
    payload["account_uid"] = uid
    payload["account_uid_masked"] = mask_uid(uid)
    payload["account_nickname"] = account.get("screen_name") or ""
    payload["web_account_uid"] = uid
    payload["source_device"] = socket.gethostname()
    payload["question_sent_at"] = question_sent_at
    payload["answer_completed_at"] = answer_completed_at
    captured_at = beijing_now()
    payload["captured_at"] = captured_at
    payload["extractedAt"] = captured_at
    save = grabber.save_payload(payload)
    if not save.get("deferred"):
        grabber.resolve_capture_skip(payload.get("url") or href)
    source_worker = grabber.start_source_ai_worker()
    product_worker = grabber.start_product_ai_worker()
    logger.info(
        "网页抓取并保存完成：回答长度=%s，引用数=%s，延迟保存=%s，来源分析=%s，产品分析=%s",
        len(str(payload.get("answerText") or "")),
        payload.get("count") or 0,
        bool(save.get("deferred")),
        source_worker,
        product_worker,
    )
    sync: dict[str, Any] = {"enabled": False, "status": "standalone"}
    if doubao_lan_client is not None:
        try:
            sync = doubao_lan_client.enqueue_for_background_upload(payload, logger)
        except Exception as exc:
            # 本地保存已经成功；主机暂不可达时由客户端离线队列保障，不能丢轮次。
            sync = {"enabled": True, "status": "queued_offline", "error": str(exc)}
            logger.warning("远端主面板暂不可达，本轮已保留在离线队列：%s", exc)
    return {"payload": payload, "save": save, "sync": sync}


class DeviceLock:
    def __init__(self, logger: logging.Logger, serial: str) -> None:
        self.logger = logger
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", serial)
        self.path = BASE_DIR / f".device_{safe}.lock"
        self.handle: Any = None

    def acquire(self) -> None:
        if os.name != "nt":
            return
        import msvcrt
        self.handle = self.path.open("a+b")
        self.handle.seek(0)
        if self.path.stat().st_size == 0:
            self.handle.write(b"0")
            self.handle.flush()
        next_notice = 0.0
        while True:
            try:
                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
                return
            except OSError:
                now = time.monotonic()
                if now >= next_notice:
                    self.logger.warning(
                        "该 MuMu 已被另一个任务控制，继续等待设备锁：%s",
                        self.path.name,
                    )
                    next_notice = now + 20
                time.sleep(2)

    def release(self) -> None:
        if self.handle is None or os.name != "nt":
            return
        import msvcrt
        try:
            self.handle.seek(0)
            msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
        finally:
            self.handle.close()
            self.handle = None


def append_result(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        handle.flush()


def load_questions(args: argparse.Namespace) -> list[str]:
    if args.question:
        return [args.question.strip()]
    path = Path(args.questions_file)
    questions = [
        line.strip()
        for line in path.read_text(encoding="utf-8-sig").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    if not questions:
        raise PipelineError(f"问题文件为空：{path}")
    return questions


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="MuMu 豆包发送 + 同账号网页同步抓取的一键流水线。"
    )
    parser.add_argument("--question", help="只运行一个问题。")
    parser.add_argument(
        "--questions-file",
        default=str(BASE_DIR / "questions.txt"),
        help="UTF-8 问题文件，一行一个问题。",
    )
    parser.add_argument("--rounds", type=int, default=0, help="轮数；0 表示问题列表各一次。")
    parser.add_argument("--forever", action="store_true", help="循环运行问题列表。")
    parser.add_argument("--device-index", help="只控制指定 MuMu 实例编号。")
    parser.add_argument(
        "--browser-slot",
        help="独立 Chrome 会话槽位；多实例运行时使用 MuMu 实例编号。",
    )
    parser.add_argument("--adb", help="adb.exe 路径。")
    parser.add_argument("--appium-url", default=mumu.DEFAULT_APPIUM_URL)
    parser.add_argument("--system-port", type=int, default=8201)
    parser.add_argument("--min-wait", type=float, default=8)
    parser.add_argument("--stable-seconds", type=float, default=5)
    parser.add_argument("--answer-timeout", type=float, default=240)
    parser.add_argument(
        "--sync-timeout",
        type=float,
        default=300,
        help="单次等待网页同步秒数；超时后刷新并继续重试。",
    )
    parser.add_argument(
        "--login-wait-seconds",
        type=int,
        default=0,
        help="首次网页登录等待秒数；0 表示一直等待。",
    )
    parser.add_argument("--retry-delay", type=float, default=5)
    parser.add_argument(
        "--round-delay-min",
        type=float,
        default=0,
        help="成功轮次之间的最小冷却秒数。",
    )
    parser.add_argument(
        "--round-delay-max",
        type=float,
        default=0,
        help="成功轮次之间的最大冷却秒数。",
    )
    parser.add_argument(
        "--max-round-retries",
        type=int,
        default=0,
        help="单轮最大重试；0 表示持续恢复，不因临时异常退出。",
    )
    parser.add_argument(
        "--log",
        default=str(BASE_DIR / "doubao_mumu_web_pipeline.log"),
    )
    parser.add_argument(
        "--results",
        default=str(BASE_DIR / "doubao_mumu_web_results.jsonl"),
    )
    parser.add_argument(
        "--diagnostics-dir",
        default=str(BASE_DIR / "doubao_mumu_web_diagnostics"),
    )
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    logger = configure_logging(Path(args.log), args.verbose)
    questions = load_questions(args)
    target_rounds = args.rounds if args.rounds > 0 else len(questions)
    devices = discover_mumu_instances(logger, args.device_index)
    if len(devices) > 1 and args.device_index is None:
        logger.warning(
            "检测到多台 MuMu；当前按实例顺序选择第一台。"
            "可用 --device-index 指定其他实例，或分别启动多个任务。"
        )
    device = devices[0]
    lock = DeviceLock(logger, device["serial"])
    lock.acquire()
    logger.info("已取得设备独占锁：%s", device["serial"])
    try:
        adb = Path(args.adb) if args.adb else resolve_adb()
        account = read_mobile_account(logger, adb, device["serial"])
        uid = account["uid"]
        logger.info("正在加载网页抓取模块并检查现有 Chrome 调试会话。")
        grabber = import_grabber()
        browser_slot = args.browser_slot or device["index"]
        browser = ensure_matching_browser(
            logger,
            grabber,
            uid,
            args.login_wait_seconds,
            browser_slot=browser_slot,
        )
        cdp_port = int(browser["port"])
        assigned_system_port = (
            args.system_port + int(device["index"] or 0) * 20
        )
        cleanup_stale_appium_sessions(
            logger,
            args.appium_url,
            device["serial"],
            assigned_system_port,
        )
        mobile_adb = mumu.AdbController(
            logger,
            adb_path=str(adb),
            serial=device["serial"],
        )
        mobile_appium = mumu.AppiumClient(
            logger,
            mobile_adb,
            args.appium_url,
            system_port_start=assigned_system_port,
        )
        automation = mumu.DoubaoAutomation(
            logger,
            mobile_adb,
            mobile_appium,
            Path(args.diagnostics_dir),
        )
        completed = 0
        while args.forever or completed < target_rounds:
            question = questions[completed % len(questions)]
            attempt = 0
            sent = False
            baseline_hrefs: set[str] = set()
            mobile_answer = ""
            question_sent_at = ""
            answer_completed_at = ""
            started = time.monotonic()
            while True:
                attempt += 1
                try:
                    current_web = web_identity(grabber, cdp_port)
                    if (
                        not current_web.get("loggedIn")
                        or current_web.get("uid") != uid
                    ):
                        browser = ensure_matching_browser(
                            logger,
                            grabber,
                            uid,
                            args.login_wait_seconds,
                            browser_slot=browser_slot,
                        )
                        cdp_port = int(browser["port"])
                    if not sent:
                        baseline = history_snapshot(grabber, cdp_port)
                        baseline_hrefs = {
                            str(item.get("href") or "").rstrip("/")
                            for item in baseline
                            if isinstance(item, dict) and item.get("href")
                        }
                        logger.info(
                            "第 %s 轮开始：%s（网页基线 %s 个会话）",
                            completed + 1,
                            question,
                            len(baseline_hrefs),
                        )
                        try:
                            automation.ensure_doubao_ready()
                            automation.create_new_chat()
                            automation.fill_and_send(question)
                            sent = True
                            question_sent_at = beijing_now()
                            logger.info("MuMu 发送和消息气泡校验完成。")
                        except Exception:
                            try:
                                _source, root = automation.source_root()
                                sent = question in mumu.message_texts(root)
                                if sent and not question_sent_at:
                                    question_sent_at = beijing_now()
                            except Exception:
                                sent = False
                            raise
                    href, ws_url = wait_for_synced_chat(
                        logger,
                        grabber,
                        cdp_port,
                        question,
                        baseline_hrefs,
                        args.sync_timeout,
                    )
                    mobile_answer = wait_for_web_answer_stable(
                        logger,
                        grabber,
                        ws_url,
                        question,
                        min_wait=args.min_wait,
                        stable_seconds=args.stable_seconds,
                        timeout=args.answer_timeout,
                    )
                    answer_completed_at = beijing_now()
                    saved = grab_and_save(
                        logger,
                        grabber,
                        ws_url,
                        href,
                        question,
                        device,
                        account,
                        question_sent_at,
                        answer_completed_at,
                        known_answer=mobile_answer,
                    )
                    completed += 1
                    record = {
                        "ok": True,
                        "timestamp": beijing_now(),
                        "round": completed,
                        "attempt": attempt,
                        "question": question,
                        "mobile_answer_length": len(mobile_answer),
                        "chat_url": href,
                        "answer_length": len(
                            str(saved["payload"].get("answerText") or "")
                        ),
                        "reference_count": saved["payload"].get("count") or 0,
                        "save_deferred": bool(saved["save"].get("deferred")),
                        "remote_sync": saved.get("sync") or {},
                        "device_index": device["index"],
                        "serial": device["serial"],
                        "account_uid": uid,
                        "account_uid_masked": mask_uid(uid),
                        "account_nickname": account.get("screen_name") or "",
                        "web_account_uid": uid,
                        "question_sent_at": question_sent_at,
                        "answer_completed_at": answer_completed_at,
                        "captured_at": saved["payload"].get("captured_at") or "",
                        "elapsed_seconds": round(time.monotonic() - started, 2),
                    }
                    append_result(Path(args.results), record)
                    logger.info(
                        "第 %s 轮全链路成功，共耗时 %.1f 秒。",
                        completed,
                        record["elapsed_seconds"],
                    )
                    has_next = args.forever or completed < target_rounds
                    if has_next and args.round_delay_max > 0:
                        delay_low = max(0.0, args.round_delay_min)
                        delay_high = max(delay_low, args.round_delay_max)
                        delay = random.uniform(delay_low, delay_high)
                        logger.info("下一轮前冷却 %.1f 秒。", delay)
                        time.sleep(delay)
                    break
                except InvalidModelAnswer as exc:
                    completed += 1
                    append_result(
                        Path(args.results),
                        {"ok": False, "skipped": True, "timestamp": beijing_now(),
                         "round": completed, "attempt": attempt, "question": question,
                         "sent": sent, "skip_reason": str(exc), "device_index": device["index"],
                         "serial": device["serial"], "account_uid_masked": mask_uid(uid),
                         "question_sent_at": question_sent_at},
                    )
                    logger.warning("第 %s 轮回答无效，已直接跳过：%s", completed, exc)
                    break
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    logger.exception(
                        "第 %s 轮尝试 %s 发生异常（已发送=%s）：%s",
                        completed + 1,
                        attempt,
                        sent,
                        exc,
                    )
                    automation.save_diagnostics(completed + 1, attempt, exc)
                    append_result(
                        Path(args.results),
                        {
                            "ok": False,
                            "timestamp": beijing_now(),
                            "round": completed + 1,
                            "attempt": attempt,
                            "question": question,
                            "sent": sent,
                            "error_type": type(exc).__name__,
                            "error": str(exc),
                            "device_index": device["index"],
                            "serial": device["serial"],
                            "account_uid": uid,
                            "account_uid_masked": mask_uid(uid),
                            "account_nickname": account.get("screen_name") or "",
                            "question_sent_at": question_sent_at,
                        },
                    )
                    mobile_appium.invalidate_session()
                    mobile_adb.serial = None
                    if args.max_round_retries and attempt >= args.max_round_retries:
                        logger.error("本轮达到最大重试次数，安全结束任务。")
                        return 2
                    logger.warning(
                        "%.1f 秒后自动恢复；已确认发送的问题不会重复发送。",
                        args.retry_delay,
                    )
                    time.sleep(args.retry_delay)
        logger.info("全部完成：%s 轮。", completed)
        return 0
    except KeyboardInterrupt:
        logger.warning("收到人工停止请求，已安全释放设备。")
        return 130
    finally:
        lock.release()


if __name__ == "__main__":
    while True:
        try:
            raise SystemExit(main())
        except KeyboardInterrupt:
            raise SystemExit(130)
        except SystemExit:
            raise
        except Exception as exc:
            retry_args = parse_args()
            retry_logger = configure_logging(
                Path(retry_args.log),
                retry_args.verbose,
            )
            retry_logger.exception(
                "启动或设备识别阶段发生异常：%s；%.1f 秒后自动重新识别。",
                exc,
                retry_args.retry_delay,
            )
            time.sleep(max(1.0, retry_args.retry_delay))
