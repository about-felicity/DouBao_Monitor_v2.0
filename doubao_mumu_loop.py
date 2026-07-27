from __future__ import annotations

import argparse
from io import BytesIO
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from typing import Any, Callable, Iterable
import xml.etree.ElementTree as ET

from PIL import Image, UnidentifiedImageError
import requests


BASE_DIR = Path(__file__).resolve().parent
PACKAGE = "com.larus.nova"
DEFAULT_QUESTION = "推荐一款染发剂"
DEFAULT_APPIUM_URL = "http://127.0.0.1:4723/wd/hub"

ADB_CANDIDATES = [
    BASE_DIR / "portable_runtime" / "platform-tools" / "adb.exe",
    Path(r"C:\ProgramData\ShadowBot\support_x64\mobile\AndroidSDK\platform-tools\adb.exe"),
    Path(r"C:\Program Files\Netease\MuMu\nx_device\15.0\shell\adb.exe"),
    Path(r"C:\Program Files\Netease\MuMu\nx_main\adb.exe"),
]
APPIUM_NODE_CANDIDATES = [
    BASE_DIR / "portable_runtime" / "NodeJS" / "node.exe",
    Path(r"C:\ProgramData\ShadowBot\support_x64\mobile\NodeJS\node.exe"),
]
APPIUM_MAIN_CANDIDATES = [
    BASE_DIR
    / "portable_runtime"
    / "NodeJS"
    / "node_modules"
    / "appium"
    / "build"
    / "lib"
    / "main.js",
    Path(r"C:\ProgramData\ShadowBot\support_x64\mobile\NodeJS\node_modules\appium\build\lib\main.js"),
]
MUMU_SERIAL_CANDIDATES = [
    "127.0.0.1:5555",
    "127.0.0.1:7555",
    "127.0.0.1:16384",
]

LOGIN_MARKERS = {
    "com.larus.nova:id/send_code_message",
    "com.larus.nova:id/edit_solid",
}
CHAT_ROOT_ID = "com.larus.nova:id/chat_root"
LIST_ID = "com.larus.nova:id/conversation_list"
INPUT_ID = "com.larus.nova:id/input_text"
INPUT_TOGGLE_ID = "com.larus.nova:id/action_input"
SEND_ID = "com.larus.nova:id/action_send"
BACK_ID = "com.larus.nova:id/back_icon"
NEW_CHAT_ID = "com.larus.nova:id/right_img"
MESSAGE_LIST_ID = "com.larus.nova:id/message_list"
CREATE_NEW_CHAT_TEXT = "创建新对话"


class AutomationError(RuntimeError):
    pass


class SessionLost(AutomationError):
    pass


class ManualActionRequired(AutomationError):
    pass


def first_existing(paths: Iterable[Path]) -> Path | None:
    return next((path for path in paths if path.exists()), None)


def now_text() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def configure_logging(log_path: Path, verbose: bool) -> logging.Logger:
    logger = logging.getLogger("doubao_mumu")
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()

    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.DEBUG if verbose else logging.INFO)
    console.setFormatter(formatter)
    logger.addHandler(console)

    log_path.parent.mkdir(parents=True, exist_ok=True)
    file_handler = RotatingFileHandler(
        log_path,
        maxBytes=5 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    return logger


def run_process(
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
        )
    except subprocess.TimeoutExpired as exc:
        raise AutomationError(f"命令超时（{timeout:g} 秒）：{' '.join(command)}") from exc
    except OSError as exc:
        raise AutomationError(f"无法运行命令：{' '.join(command)}；{exc}") from exc

    if check and result.returncode != 0:
        raise AutomationError(
            "命令执行失败：%s\nstdout=%s\nstderr=%s"
            % (" ".join(command), result.stdout.strip(), result.stderr.strip())
        )
    return result


class AdbController:
    def __init__(
        self,
        logger: logging.Logger,
        adb_path: str | None = None,
        serial: str | None = None,
    ) -> None:
        self.logger = logger
        resolved = Path(adb_path) if adb_path else first_existing(ADB_CANDIDATES)
        if resolved is None:
            resolved_from_path = run_process(
                ["where.exe", "adb"],
                check=False,
                timeout=5,
            ).stdout.splitlines()
            resolved = Path(resolved_from_path[0]) if resolved_from_path else None
        if resolved is None or not resolved.exists():
            raise AutomationError("找不到 adb.exe，请安装 ADB 或启动影刀移动端组件。")
        self.adb_path = str(resolved)
        self.requested_serial = serial
        self.serial: str | None = None

    def _run(
        self,
        args: list[str],
        *,
        serial: str | None = None,
        timeout: float = 30,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        command = [self.adb_path]
        active_serial = serial if serial is not None else self.serial
        if active_serial:
            command += ["-s", active_serial]
        command += args
        return run_process(command, timeout=timeout, check=check)

    def connect(self) -> str:
        candidates = (
            [self.requested_serial]
            if self.requested_serial
            else list(MUMU_SERIAL_CANDIDATES)
        )
        errors: list[str] = []
        for serial in candidates:
            assert serial is not None
            self._run(["connect", serial], serial="", timeout=8, check=False)
            state = self._run(
                ["get-state"],
                serial=serial,
                timeout=8,
                check=False,
            )
            if state.returncode == 0 and state.stdout.strip() == "device":
                size = self._run(
                    ["shell", "wm", "size"],
                    serial=serial,
                    timeout=8,
                    check=False,
                ).stdout.strip()
                model = self._run(
                    ["shell", "getprop", "ro.product.model"],
                    serial=serial,
                    timeout=8,
                    check=False,
                ).stdout.strip()
                self.serial = serial
                self.logger.info(
                    "已连接 MuMu：serial=%s model=%s %s",
                    serial,
                    model or "?",
                    size or "",
                )
                return serial
            errors.append(
                f"{serial}: {(state.stderr or state.stdout).strip() or 'offline'}"
            )
        raise AutomationError("无法连接 MuMu ADB；" + " | ".join(errors))

    def ensure_connected(self) -> str:
        if self.serial:
            state = self._run(
                ["get-state"],
                timeout=8,
                check=False,
            )
            if state.returncode == 0 and state.stdout.strip() == "device":
                return self.serial
        return self.connect()

    def shell(
        self,
        *args: str,
        timeout: float = 30,
        check: bool = True,
    ) -> str:
        self.ensure_connected()
        result = self._run(
            ["shell", *args],
            timeout=timeout,
            check=check,
        )
        return result.stdout.strip()

    def bring_doubao_foreground(self) -> None:
        self.shell(
            "monkey",
            "-p",
            PACKAGE,
            "-c",
            "android.intent.category.LAUNCHER",
            "1",
            timeout=20,
            check=False,
        )
        time.sleep(1.5)

    def press_back(self) -> None:
        self.shell("input", "keyevent", "4", timeout=10, check=False)
        time.sleep(0.5)

    def force_stop_and_restart(self) -> None:
        self.logger.warning("执行豆包前台恢复")
        self.shell("am", "force-stop", PACKAGE, timeout=10, check=False)
        time.sleep(0.8)
        self.bring_doubao_foreground()

    def screenshot_bytes(self) -> bytes:
        self.ensure_connected()
        assert self.serial
        try:
            result = subprocess.run(
                [
                    self.adb_path,
                    "-s",
                    self.serial,
                    "exec-out",
                    "screencap",
                    "-p",
                ],
                capture_output=True,
                timeout=20,
            )
        except (OSError, subprocess.TimeoutExpired):
            return b""
        return result.stdout if result.returncode == 0 else b""


class AppiumClient:
    def __init__(
        self,
        logger: logging.Logger,
        adb: AdbController,
        base_url: str,
        system_port_start: int = 8201,
    ) -> None:
        self.logger = logger
        self.adb = adb
        self.base_url = base_url.rstrip("/")
        self.system_port_start = system_port_start
        self.http = requests.Session()
        self.session_id: str | None = None
        self.started_process: subprocess.Popen[Any] | None = None

    def _url(self, path: str) -> str:
        return f"{self.base_url}/{path.lstrip('/')}"

    def _json_request(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        timeout: float = 30,
        allow_error: bool = False,
    ) -> dict[str, Any]:
        try:
            response = self.http.request(
                method,
                self._url(path),
                json=payload,
                timeout=timeout,
            )
        except requests.RequestException as exc:
            raise AutomationError(f"Appium 请求失败：{exc}") from exc

        try:
            data = response.json()
        except ValueError as exc:
            raise AutomationError(
                f"Appium 返回了非 JSON 内容：HTTP {response.status_code}"
            ) from exc

        value = data.get("value")
        error_text = ""
        if isinstance(value, dict):
            error_text = " ".join(
                str(value.get(key) or "")
                for key in ("error", "message")
            ).strip()
        if (
            response.status_code >= 400
            or data.get("status") not in (None, 0)
            or error_text
        ):
            message = error_text or json.dumps(data, ensure_ascii=False)
            if "invalid session" in message.lower():
                self.session_id = None
                raise SessionLost(message)
            if allow_error:
                return {"_error": message, "_status": response.status_code}
            raise AutomationError(
                f"Appium 操作失败（HTTP {response.status_code}）：{message}"
            )
        return data

    def server_ready(self) -> bool:
        try:
            data = self._json_request("GET", "status", timeout=5)
            return isinstance(data.get("value"), dict)
        except AutomationError:
            return False

    def _start_server(self) -> None:
        node = first_existing(APPIUM_NODE_CANDIDATES)
        appium_main = first_existing(APPIUM_MAIN_CANDIDATES)
        if node is not None and appium_main is not None:
            command = [
                str(node),
                str(appium_main),
                "-p",
                "4723",
                "--log-level",
                "info",
            ]
        else:
            global_appium = run_process(
                ["where.exe", "appium.cmd"],
                check=False,
                timeout=5,
            ).stdout.splitlines()
            if not global_appium:
                raise AutomationError(
                    "Appium 未运行，且找不到影刀自带或全局安装的 Appium。"
                )
            command = [
                "cmd.exe",
                "/d",
                "/c",
                global_appium[0],
                "-p",
                "4723",
                "--base-path",
                "/wd/hub",
                "--log-level",
                "info",
            ]
        log_path = BASE_DIR / "doubao_mumu_appium.log"
        log_handle = open(log_path, "a", encoding="utf-8")
        creation_flags = (
            subprocess.CREATE_NO_WINDOW
            if os.name == "nt" and hasattr(subprocess, "CREATE_NO_WINDOW")
            else 0
        )
        self.started_process = subprocess.Popen(
            command,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            cwd=str(BASE_DIR),
            creationflags=creation_flags,
        )
        self.logger.warning("Appium 未运行，已自动启动，日志：%s", log_path)

    def ensure_server(self, timeout: float = 35) -> None:
        if self.server_ready():
            return
        self._start_server()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.server_ready():
                return
            time.sleep(1)
        raise AutomationError("自动启动 Appium 后仍无法连接。")

    def _existing_session(self) -> str | None:
        data = self._json_request("GET", "sessions", timeout=10)
        sessions = data.get("value") or []
        for entry in sessions:
            capabilities = entry.get("capabilities") or {}
            desired = capabilities.get("desired") or {}
            udid = capabilities.get("udid") or desired.get("udid")
            if udid == self.adb.serial:
                return entry.get("id") or entry.get("sessionId")
        return None

    def _create_session(self) -> str:
        assert self.adb.serial
        errors: list[str] = []
        for system_port in range(
            self.system_port_start,
            self.system_port_start + 10,
        ):
            desired = {
                "platformName": "Android",
                "automationName": "UiAutomator2",
                "udid": self.adb.serial,
                "deviceName": "MuMu",
                "systemPort": system_port,
                "newCommandTimeout": 0,
                "noReset": True,
            }
            try:
                data = self._json_request(
                    "POST",
                    "session",
                    payload={
                        "desiredCapabilities": desired,
                        "capabilities": {
                            "alwaysMatch": {
                                f"appium:{key}": value
                                for key, value in desired.items()
                                if key != "platformName"
                            }
                            | {"platformName": "Android"}
                        },
                    },
                    timeout=45,
                )
                session_id = data.get("sessionId")
                if not session_id and isinstance(data.get("value"), dict):
                    session_id = data["value"].get("sessionId")
                if session_id:
                    self.logger.info(
                        "已创建 Appium 会话：%s systemPort=%s",
                        session_id,
                        system_port,
                    )
                    return str(session_id)
            except AutomationError as exc:
                errors.append(f"{system_port}: {exc}")
                self.logger.debug("systemPort %s 创建失败：%s", system_port, exc)
        raise AutomationError("创建 Appium 会话失败；" + " | ".join(errors[-3:]))

    def ensure_session(self, force_new: bool = False) -> str:
        self.adb.ensure_connected()
        self.ensure_server()
        if self.session_id and not force_new:
            try:
                self._json_request(
                    "GET",
                    f"session/{self.session_id}/source",
                    timeout=20,
                )
                return self.session_id
            except AutomationError:
                self.session_id = None

        if not force_new:
            existing = self._existing_session()
            if existing:
                self.session_id = existing
                try:
                    self._json_request(
                        "GET",
                        f"session/{existing}/source",
                        timeout=20,
                    )
                    self.logger.info("复用 Appium 会话：%s", existing)
                    return existing
                except AutomationError:
                    self.session_id = None

        self.session_id = self._create_session()
        return self.session_id

    def invalidate_session(self) -> None:
        self.session_id = None

    def _session_request(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        timeout: float = 30,
        allow_error: bool = False,
    ) -> dict[str, Any]:
        self.ensure_session()
        assert self.session_id
        try:
            return self._json_request(
                method,
                f"session/{self.session_id}/{path.lstrip('/')}",
                payload=payload,
                timeout=timeout,
                allow_error=allow_error,
            )
        except SessionLost:
            self.logger.warning("Appium 会话失效，正在重建")
            self.ensure_session(force_new=True)
            assert self.session_id
            return self._json_request(
                method,
                f"session/{self.session_id}/{path.lstrip('/')}",
                payload=payload,
                timeout=timeout,
                allow_error=allow_error,
            )

    def source(self) -> str:
        data = self._session_request("GET", "source", timeout=25)
        value = data.get("value")
        if not isinstance(value, str) or "<hierarchy" not in value:
            raise AutomationError("Appium 没有返回有效的页面 XML。")
        return value

    def find_element(
        self,
        using: str,
        value: str,
        *,
        timeout: float = 12,
    ) -> str:
        deadline = time.monotonic() + timeout
        last_error = ""
        while time.monotonic() < deadline:
            data = self._session_request(
                "POST",
                "element",
                payload={"using": using, "value": value},
                timeout=15,
                allow_error=True,
            )
            if "_error" not in data:
                item = data.get("value") or {}
                element_id = (
                    item.get("ELEMENT")
                    or item.get("element-6066-11e4-a52e-4f735466cecf")
                )
                if element_id:
                    return str(element_id)
            last_error = str(data.get("_error") or "控件不存在")
            time.sleep(0.5)
        raise AutomationError(
            f"等待控件超时：using={using} value={value}；{last_error}"
        )

    def click(self, element_id: str) -> None:
        self._session_request(
            "POST",
            f"element/{element_id}/click",
            payload={},
            timeout=15,
        )

    def click_id(self, resource_id: str, timeout: float = 12) -> None:
        self.click(self.find_element("id", resource_id, timeout=timeout))

    def click_text(self, text: str, timeout: float = 12) -> None:
        escaped = text.replace('"', '\\"')
        self.click(
            self.find_element(
                "xpath",
                f'//*[@text="{escaped}"]',
                timeout=timeout,
            )
        )

    def clear(self, element_id: str) -> None:
        self._session_request(
            "POST",
            f"element/{element_id}/clear",
            payload={},
            timeout=15,
        )

    def send_keys(self, element_id: str, text: str) -> None:
        self._session_request(
            "POST",
            f"element/{element_id}/value",
            payload={"text": text, "value": list(text)},
            timeout=30,
        )

    def back(self) -> None:
        self._session_request("POST", "back", payload={}, timeout=15)
        time.sleep(0.5)


def parse_xml(source: str) -> ET.Element:
    try:
        return ET.fromstring(source)
    except ET.ParseError as exc:
        raise AutomationError(f"页面 XML 解析失败：{exc}") from exc


def has_id(root: ET.Element, resource_id: str) -> bool:
    return any(
        node.attrib.get("resource-id") == resource_id
        for node in root.iter()
    )


def find_node_by_id(root: ET.Element, resource_id: str) -> ET.Element | None:
    return next(
        (
            node
            for node in root.iter()
            if node.attrib.get("resource-id") == resource_id
        ),
        None,
    )


def all_texts(root: ET.Element) -> list[str]:
    return [
        text.strip()
        for node in root.iter()
        if (text := (node.attrib.get("text") or "").strip())
    ]


def message_texts(root: ET.Element) -> list[str]:
    message_list = find_node_by_id(root, MESSAGE_LIST_ID)
    if message_list is None:
        return []
    return [
        text.strip()
        for node in message_list.iter()
        if (text := (node.attrib.get("text") or "").strip())
    ]


def node_bounds(node: ET.Element | None) -> tuple[int, int, int, int] | None:
    if node is None:
        return None
    numbers = [
        int(value)
        for value in re.findall(r"\d+", node.attrib.get("bounds") or "")
    ]
    if len(numbers) != 4:
        return None
    left, top, right, bottom = numbers
    if right <= left or bottom <= top:
        return None
    return left, top, right, bottom


def visual_fingerprint(
    screenshot: bytes,
    crop: tuple[int, int, int, int] | None,
) -> bytes:
    if not screenshot:
        return b""
    try:
        with Image.open(BytesIO(screenshot)) as image:
            image = image.convert("L")
            if crop is not None:
                image = image.crop(crop)
            image = image.resize((64, 64), Image.Resampling.BILINEAR)
            return image.tobytes()
    except (OSError, UnidentifiedImageError):
        return b""


def fingerprint_difference(previous: bytes, current: bytes) -> float:
    if not previous or not current or len(previous) != len(current):
        return float("inf")
    return sum(
        abs(left - right)
        for left, right in zip(previous, current)
    ) / len(current)


def page_name(root: ET.Element) -> str:
    if any(has_id(root, marker) for marker in LOGIN_MARKERS):
        return "login"
    if has_id(root, CHAT_ROOT_ID):
        return "chat"
    if has_id(root, LIST_ID):
        return "list"
    if CREATE_NEW_CHAT_TEXT in all_texts(root):
        return "new_chat_menu"
    return "unknown"


class DoubaoAutomation:
    def __init__(
        self,
        logger: logging.Logger,
        adb: AdbController,
        appium: AppiumClient,
        diagnostics_dir: Path,
    ) -> None:
        self.logger = logger
        self.adb = adb
        self.appium = appium
        self.diagnostics_dir = diagnostics_dir

    def source_root(self) -> tuple[str, ET.Element]:
        source = self.appium.source()
        return source, parse_xml(source)

    def wait_until(
        self,
        predicate: Callable[[ET.Element], bool],
        *,
        timeout: float,
        description: str,
    ) -> tuple[str, ET.Element]:
        deadline = time.monotonic() + timeout
        last_page = "unknown"
        while time.monotonic() < deadline:
            source, root = self.source_root()
            last_page = page_name(root)
            if predicate(root):
                return source, root
            time.sleep(0.5)
        raise AutomationError(
            f"等待{description}超时（当前页面：{last_page}）"
        )

    def ensure_doubao_ready(self) -> None:
        self.adb.ensure_connected()
        self.adb.bring_doubao_foreground()
        self.appium.ensure_session()
        _, root = self.wait_until(
            lambda item: page_name(item) in {
                "chat",
                "list",
                "new_chat_menu",
                "login",
            },
            timeout=18,
            description="豆包页面",
        )
        if page_name(root) == "login":
            raise ManualActionRequired(
                "MuMu 中的豆包停在登录/验证码页面，请手动完成一次登录；脚本会持续重试。"
            )

    def create_new_chat(self) -> None:
        self.logger.info("准备新建对话")
        for recovery_round in range(1, 7):
            source, root = self.source_root()
            state = page_name(root)
            self.logger.debug(
                "新建对话导航：recovery=%s page=%s",
                recovery_round,
                state,
            )
            if state == "login":
                raise ManualActionRequired(
                    "豆包要求登录/验证，请手动处理；脚本不会退出。"
                )
            if state == "new_chat_menu":
                try:
                    self.appium.click_text(CREATE_NEW_CHAT_TEXT, timeout=5)
                    self.wait_until(
                        lambda item: (
                            page_name(item) == "chat"
                            and has_id(item, INPUT_ID)
                        ),
                        timeout=12,
                        description="新对话聊天页",
                    )
                    return
                except AutomationError as exc:
                    self.logger.warning("点击“创建新对话”失败：%s", exc)
                    self.appium.back()
                    continue
            if state == "chat":
                try:
                    self.appium.click_id(BACK_ID, timeout=5)
                except AutomationError:
                    self.appium.back()
                time.sleep(0.8)
                continue
            if state == "list":
                self.appium.click_id(NEW_CHAT_ID, timeout=8)
                self.wait_until(
                    lambda item: page_name(item) in {
                        "new_chat_menu",
                        "chat",
                    },
                    timeout=8,
                    description="新对话菜单",
                )
                continue

            self.logger.warning("页面不可识别，执行恢复：%s", state)
            self.adb.press_back()
            self.adb.bring_doubao_foreground()
            time.sleep(1)

        self.adb.force_stop_and_restart()
        self.wait_until(
            lambda item: page_name(item) in {"chat", "list"},
            timeout=18,
            description="豆包恢复页面",
        )
        raise AutomationError("多次恢复后仍无法创建新对话。")

    def ensure_text_input(self) -> None:
        source, root = self.source_root()
        if page_name(root) != "chat":
            raise AutomationError(
                f"准备输入时不在聊天页，而在 {page_name(root)}"
            )
        if has_id(root, INPUT_ID):
            return
        if has_id(root, INPUT_TOGGLE_ID):
            self.appium.click_id(INPUT_TOGGLE_ID, timeout=5)
            self.wait_until(
                lambda item: has_id(item, INPUT_ID),
                timeout=8,
                description="文字输入框",
            )
            return
        raise AutomationError("聊天页中找不到文字输入框。")

    def fill_and_send(self, question: str) -> None:
        self.ensure_text_input()
        input_element = self.appium.find_element(
            "id",
            INPUT_ID,
            timeout=8,
        )
        self.appium.click(input_element)
        try:
            self.appium.clear(input_element)
        except AutomationError as exc:
            self.logger.warning("清空输入框失败，将继续覆盖输入：%s", exc)
        self.appium.send_keys(input_element, question)

        _, root = self.wait_until(
            lambda item: (
                (node := find_node_by_id(item, INPUT_ID)) is not None
                and question in (node.attrib.get("text") or "")
            ),
            timeout=10,
            description="输入文字校验",
        )
        input_node = find_node_by_id(root, INPUT_ID)
        if input_node is None or question not in (
            input_node.attrib.get("text") or ""
        ):
            raise AutomationError("输入文字校验失败。")

        self.appium.click_id(SEND_ID, timeout=8)
        self.wait_until(
            lambda item: question in message_texts(item),
            timeout=15,
            description="已发送消息",
        )
        self.logger.info("消息已发送：%s", question)

    def _answer_after_question(
        self,
        root: ET.Element,
        question: str,
    ) -> str:
        texts = message_texts(root)
        question_index = -1
        for index, text in enumerate(texts):
            if text == question or question in text:
                question_index = index
        if question_index < 0:
            return ""
        answer_parts = texts[question_index + 1 :]
        return "\n".join(answer_parts).strip()

    def wait_for_answer(
        self,
        question: str,
        *,
        min_wait: float,
        stable_seconds: float,
        timeout: float,
    ) -> tuple[str, str]:
        started = time.monotonic()
        last_answer = ""
        last_visual = b""
        stable_since: float | None = None
        latest_nonempty_answer = ""
        grace_deadline: float | None = None

        while True:
            loop_now = time.monotonic()
            elapsed_before_poll = loop_now - started
            if elapsed_before_poll >= timeout and grace_deadline is None:
                if latest_nonempty_answer:
                    grace_seconds = max(5.0, stable_seconds + 2.0)
                    grace_deadline = loop_now + grace_seconds
                    self.logger.warning(
                        "回答在超时边缘已有文本，追加 %.1f 秒稳定确认宽限期。",
                        grace_seconds,
                    )
                else:
                    break
            if grace_deadline is not None and loop_now >= grace_deadline:
                break

            source, root = self.source_root()
            state = page_name(root)
            if state == "login":
                raise ManualActionRequired(
                    "等待回答时豆包跳到了登录/验证页面。"
                )
            if state != "chat":
                raise AutomationError(
                    f"等待回答时离开了聊天页：{state}"
                )

            answer = self._answer_after_question(root, question)
            if answer:
                latest_nonempty_answer = answer

            elapsed = time.monotonic() - started
            text_unchanged = bool(answer) and answer == last_answer
            # UiAutomator may truncate very long TextView content. For short
            # answers, text stability is the most reliable and fastest signal.
            # For a possibly truncated answer, also compare a downsampled crop
            # of the message area so streaming below the XML limit is observed.
            possibly_truncated = len(answer) >= 900 or answer.endswith("⚫")
            visual_unchanged = True
            current_visual = b""
            visual_delta = 0.0
            if possibly_truncated:
                message_bounds = node_bounds(
                    find_node_by_id(root, MESSAGE_LIST_ID)
                )
                current_visual = visual_fingerprint(
                    self.adb.screenshot_bytes(),
                    message_bounds,
                )
                visual_delta = fingerprint_difference(
                    last_visual,
                    current_visual,
                )
                visual_unchanged = visual_delta <= 1.25

            if (
                elapsed >= min_wait
                and text_unchanged
                and visual_unchanged
            ):
                if stable_since is None:
                    stable_since = time.monotonic()
                stable_for = time.monotonic() - stable_since
                self.logger.debug(
                    "回答稳定 %.1f/%.1f 秒，长度=%s，视觉差=%.3f",
                    stable_for,
                    stable_seconds,
                    len(answer),
                    visual_delta,
                )
                if stable_for >= stable_seconds:
                    self.logger.info(
                        "回答完成：耗时 %.1f 秒，文本长度 %s",
                        elapsed,
                        len(answer),
                    )
                    return answer, "stable"
            else:
                stable_since = None

            last_answer = answer
            if current_visual:
                last_visual = current_visual
            time.sleep(1)

        if latest_nonempty_answer:
            self.logger.warning(
                "回答完成判定超时，但已有回答文本；记录后继续下一轮。"
            )
            return latest_nonempty_answer, "timeout_with_answer"
        raise AutomationError(
            f"等待回答 {timeout:g} 秒后仍未读取到回答。"
        )

    def ask_once(
        self,
        question: str,
        *,
        min_wait: float,
        stable_seconds: float,
        timeout: float,
    ) -> tuple[str, str]:
        self.ensure_doubao_ready()
        self.create_new_chat()
        self.fill_and_send(question)
        return self.wait_for_answer(
            question,
            min_wait=min_wait,
            stable_seconds=stable_seconds,
            timeout=timeout,
        )

    def save_diagnostics(
        self,
        round_number: int,
        attempt: int,
        error: BaseException,
    ) -> None:
        self.diagnostics_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        prefix = (
            self.diagnostics_dir
            / f"round_{round_number:04d}_attempt_{attempt:03d}_{stamp}"
        )
        try:
            source = self.appium.source()
            prefix.with_suffix(".xml").write_text(
                source,
                encoding="utf-8",
            )
        except Exception as exc:
            self.logger.debug("保存错误 XML 失败：%s", exc)
        try:
            screenshot = self.adb.screenshot_bytes()
            if screenshot:
                prefix.with_suffix(".png").write_bytes(screenshot)
        except Exception as exc:
            self.logger.debug("保存错误截图失败：%s", exc)
        try:
            prefix.with_suffix(".error.txt").write_text(
                f"{now_text()}\n{type(error).__name__}: {error}\n",
                encoding="utf-8",
            )
        except Exception:
            pass


def append_jsonl(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        handle.flush()


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(path)


def load_questions(args: argparse.Namespace) -> list[str]:
    if args.questions_file:
        path = Path(args.questions_file)
        lines = [
            line.strip()
            for line in path.read_text(encoding="utf-8-sig").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        if not lines:
            raise ValueError(f"问题文件为空：{path}")
        return lines
    question = (args.question or "").strip()
    if not question:
        raise ValueError("问题不能为空。")
    return [question]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "稳定控制 MuMu 中的豆包：每轮新建对话、发送问题、等待回答，"
            "异常时自动恢复并重试。"
        )
    )
    parser.add_argument("--question", default=DEFAULT_QUESTION)
    parser.add_argument(
        "--questions-file",
        help="UTF-8 文本文件，一行一个问题；设置后忽略 --question。",
    )
    parser.add_argument(
        "--rounds",
        type=int,
        default=10,
        help="成功完成的轮数，默认 10。",
    )
    parser.add_argument(
        "--forever",
        action="store_true",
        help="无限循环；忽略 --rounds。",
    )
    parser.add_argument(
        "--serial",
        help="MuMu ADB 地址；默认自动尝试 127.0.0.1:5555/7555/16384。",
    )
    parser.add_argument("--adb", help="adb.exe 的完整路径。")
    parser.add_argument(
        "--appium-url",
        default=DEFAULT_APPIUM_URL,
    )
    parser.add_argument(
        "--system-port",
        type=int,
        default=8201,
        help="UiAutomator2 起始端口，冲突时自动向后尝试。",
    )
    parser.add_argument(
        "--min-wait",
        type=float,
        default=8,
        help="发送后至少等待的秒数。",
    )
    parser.add_argument(
        "--stable-seconds",
        type=float,
        default=5,
        help="回答文字和画面都不再变化的持续秒数。",
    )
    parser.add_argument(
        "--answer-timeout",
        type=float,
        default=180,
        help="单轮回答最长等待秒数。",
    )
    parser.add_argument(
        "--retry-delay",
        type=float,
        default=5,
        help="异常后的重试间隔秒数。",
    )
    parser.add_argument(
        "--max-round-retries",
        type=int,
        default=0,
        help="每轮最大重试次数；0 表示无限重试，默认 0。",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="从状态文件记录的已完成轮数继续。",
    )
    parser.add_argument(
        "--log",
        default=str(BASE_DIR / "doubao_mumu_loop.log"),
    )
    parser.add_argument(
        "--results",
        default=str(BASE_DIR / "doubao_mumu_results.jsonl"),
    )
    parser.add_argument(
        "--state",
        default=str(BASE_DIR / "doubao_mumu_state.json"),
    )
    parser.add_argument(
        "--diagnostics-dir",
        default=str(BASE_DIR / "doubao_mumu_diagnostics"),
    )
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args()


def validate_args(args: argparse.Namespace) -> None:
    if args.rounds <= 0 and not args.forever:
        raise ValueError("--rounds 必须大于 0。")
    if args.min_wait < 0:
        raise ValueError("--min-wait 不能小于 0。")
    if args.stable_seconds <= 0:
        raise ValueError("--stable-seconds 必须大于 0。")
    if args.answer_timeout <= 0:
        raise ValueError("--answer-timeout 必须大于 0。")
    if args.retry_delay < 0:
        raise ValueError("--retry-delay 不能小于 0。")
    if args.max_round_retries < 0:
        raise ValueError("--max-round-retries 不能小于 0。")


def main() -> int:
    args = parse_args()
    validate_args(args)
    questions = load_questions(args)
    logger = configure_logging(Path(args.log), args.verbose)

    results_path = Path(args.results)
    state_path = Path(args.state)
    diagnostics_dir = Path(args.diagnostics_dir)
    completed = 0
    if args.resume and state_path.exists():
        try:
            previous_state = json.loads(
                state_path.read_text(encoding="utf-8")
            )
            completed = max(
                0,
                int(previous_state.get("completed_rounds") or 0),
            )
            logger.info("从状态文件继续：已完成 %s 轮", completed)
        except Exception as exc:
            logger.warning("状态文件读取失败，将从第 1 轮开始：%s", exc)
            completed = 0

    logger.info(
        "任务开始：目标=%s 问题数=%s 无限循环=%s",
        "∞" if args.forever else args.rounds,
        len(questions),
        args.forever,
    )

    adb: AdbController | None = None
    appium: AppiumClient | None = None
    automation: DoubaoAutomation | None = None
    round_number = completed + 1

    try:
        while args.forever or completed < args.rounds:
            question = questions[(round_number - 1) % len(questions)]
            attempt = 0

            while True:
                attempt += 1
                started = time.monotonic()
                logger.info(
                    "第 %s 轮，尝试 %s：%s",
                    round_number,
                    attempt,
                    question,
                )
                try:
                    if adb is None:
                        adb = AdbController(
                            logger,
                            adb_path=args.adb,
                            serial=args.serial,
                        )
                    if appium is None:
                        appium = AppiumClient(
                            logger,
                            adb,
                            args.appium_url,
                            system_port_start=args.system_port,
                        )
                    if automation is None:
                        automation = DoubaoAutomation(
                            logger,
                            adb,
                            appium,
                            diagnostics_dir,
                        )

                    answer, completion = automation.ask_once(
                        question,
                        min_wait=args.min_wait,
                        stable_seconds=args.stable_seconds,
                        timeout=args.answer_timeout,
                    )
                    elapsed = round(time.monotonic() - started, 2)
                    record = {
                        "ok": True,
                        "timestamp": now_text(),
                        "round": round_number,
                        "attempt": attempt,
                        "question": question,
                        "answer": answer,
                        "completion": completion,
                        "elapsed_seconds": elapsed,
                        "serial": adb.serial,
                        "appium_session": appium.session_id,
                    }
                    append_jsonl(results_path, record)
                    completed += 1
                    save_state(
                        state_path,
                        {
                            "updated_at": now_text(),
                            "completed_rounds": completed,
                            "last_round": round_number,
                            "last_question": question,
                            "last_completion": completion,
                            "last_error": None,
                        },
                    )
                    logger.info(
                        "第 %s 轮成功（尝试 %s，%.2f 秒）",
                        round_number,
                        attempt,
                        elapsed,
                    )
                    round_number += 1
                    break

                except Exception as exc:
                    elapsed = round(time.monotonic() - started, 2)
                    logger.exception(
                        "第 %s 轮尝试 %s 失败：%s",
                        round_number,
                        attempt,
                        exc,
                    )
                    if automation is not None:
                        automation.save_diagnostics(
                            round_number,
                            attempt,
                            exc,
                        )
                    append_jsonl(
                        results_path,
                        {
                            "ok": False,
                            "timestamp": now_text(),
                            "round": round_number,
                            "attempt": attempt,
                            "question": question,
                            "error_type": type(exc).__name__,
                            "error": str(exc),
                            "elapsed_seconds": elapsed,
                            "serial": adb.serial if adb else None,
                            "appium_session": (
                                appium.session_id if appium else None
                            ),
                        },
                    )
                    save_state(
                        state_path,
                        {
                            "updated_at": now_text(),
                            "completed_rounds": completed,
                            "last_round": round_number,
                            "last_question": question,
                            "last_error": str(exc),
                        },
                    )
                    if appium is not None:
                        appium.invalidate_session()
                    if adb is not None:
                        adb.serial = None

                    retry_limit = args.max_round_retries
                    if retry_limit and attempt >= retry_limit:
                        logger.error(
                            "第 %s 轮达到最大重试次数 %s，任务结束。",
                            round_number,
                            retry_limit,
                        )
                        return 2

                    logger.warning(
                        "%.1f 秒后自动恢复并重试；脚本不会因本次异常退出。",
                        args.retry_delay,
                    )
                    time.sleep(args.retry_delay)

    except KeyboardInterrupt:
        logger.warning("收到人工停止请求，已保存进度并安全退出。")
        save_state(
            state_path,
            {
                "updated_at": now_text(),
                "completed_rounds": completed,
                "last_round": round_number,
                "stopped_by_user": True,
            },
        )
        return 130

    logger.info("全部完成：成功 %s 轮", completed)
    print(
        json.dumps(
            {
                "ok": True,
                "completed_rounds": completed,
                "results": str(results_path),
                "state": str(state_path),
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
