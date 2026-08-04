"""通过专用 Chrome 调试端口操作 DeepSeek 网页。"""

from __future__ import annotations

import json
import hashlib
import ctypes
import os
import re
import socket
import subprocess
import time
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from pathlib import Path
from typing import Any

import uiautomator2 as u2


BASE_DIR = Path(__file__).resolve().parent


def _chrome_executable() -> Path:
    candidates = (
        Path(os.environ.get("PROGRAMFILES", "")) / "Google" / "Chrome" / "Application" / "chrome.exe",
        Path(os.environ.get("PROGRAMFILES(X86)", "")) / "Google" / "Chrome" / "Application" / "chrome.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Google" / "Chrome" / "Application" / "chrome.exe",
    )
    for path in candidates:
        if path.is_file():
            return path
    raise RuntimeError("找不到 Google Chrome，请先安装 Chrome")


def _port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


def _restore_process_window(process_id: int) -> bool:
    if os.name != "nt":
        return False
    user32 = ctypes.windll.user32
    handles: list[int] = []
    callback_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    def collect(hwnd, _lparam):
        owner = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == process_id and user32.IsWindow(hwnd):
            class_name = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(hwnd, class_name, len(class_name))
            if class_name.value == "Chrome_WidgetWin_1":
                handles.append(int(hwnd))
        return True

    user32.EnumWindows(callback_type(collect), 0)
    if not handles:
        return False
    hwnd = handles[0]
    user32.ShowWindowAsync(hwnd, 9)
    user32.BringWindowToTop(hwnd)
    user32.SetForegroundWindow(hwnd)
    return True


def ensure_deepseek_chrome(port: int = 9333) -> dict[str, Any]:
    """Launch the dedicated browser when absent and always restore its visible window."""
    launched = False
    chrome = _chrome_executable()

    def launch_window() -> None:
        chrome = _chrome_executable()
        subprocess.Popen(
            [str(chrome), f"--remote-debugging-port={port}", "--remote-allow-origins=*",
             "--no-first-run", "--no-default-browser-check", f"--user-data-dir={BASE_DIR / 'chrome_profile'}",
             "--new-window", "https://chat.deepseek.com/"],
            cwd=str(BASE_DIR), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )

    if not _port_open(port):
        launch_window()
        launched = True
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and not _port_open(port):
            time.sleep(0.3)
        if not _port_open(port):
            raise RuntimeError("DeepSeek 专用 Chrome 启动超时")
    collector = DeepSeekWebCollector(port)
    last_error: Exception | None = None
    for attempt in range(2):
        try:
            collector._call("Page.bringToFront", timeout=10)
            processes = collector.browser_call("SystemInfo.getProcessInfo", timeout=10).get("processInfo") or []
            browser_pid = next((int(item.get("id")) for item in processes if item.get("type") == "browser"), 0)
            if not browser_pid or not _restore_process_window(browser_pid):
                raise RuntimeError("没有找到 DeepSeek Chrome 可见窗口")
            collector._call("Page.bringToFront", timeout=10)
            return {"launched": launched, "port": port}
        except Exception as exc:
            last_error = exc
            if attempt == 0:
                launch_window()
                launched = True
                time.sleep(2)
                collector = DeepSeekWebCollector(port)
    raise RuntimeError(f"DeepSeek Chrome 已启动，但无法唤醒窗口：{last_error}") from last_error


SNAPSHOT_JS = r"""
(() => {
  const clean=(s)=>String(s||"").replace(/\s+/g," ").trim();
  const body=clean(document.body?.innerText||"");
  const links=[...document.querySelectorAll("a[href]")].filter(a=>/^https?:\/\//.test(a.href)&&!a.href.includes("chat.deepseek.com/a/chat/s/"));
  const busy=!!document.querySelector('[aria-label*="停止"],[data-testid*="stop"],.ds-icon-button--rotating');
  return {url:location.href,title:document.title,body,linkCount:new Set(links.map(a=>a.href)).size,busy,hasTextarea:!!document.querySelector("textarea"),readyState:document.readyState};
})()
"""

COLLECT_JS = r"""
(async () => {
  const sleep=(ms)=>new Promise(r=>setTimeout(r,ms)); const clean=(s)=>String(s||"").replace(/\s+/g," ").trim(); const seen=new Map();
  const titleOf=(a)=>{const lines=String(a.innerText||a.textContent||a.title||a.getAttribute("aria-label")||"").split(/\n+/).map(clean).filter(Boolean);const n=lines.findIndex((line,i)=>/^\d+$/.test(line)&&i+1<lines.length);return (n>=0?lines[n+1]:clean(lines.join(" ")))||"DeepSeek citation";};
  const add=()=>{for(const a of document.querySelectorAll("a[href]")){const url=a.href||"";if(!/^https?:\/\//.test(url)||url.includes("chat.deepseek.com/a/chat/s/"))continue;const item={title:titleOf(a),url};const old=seen.get(url);if(!old||item.title.length>old.title.length)seen.set(url,item);}};
  const sourceSummary=[...document.querySelectorAll("span")].find(el=>/^\s*\d+\s*个网页\s*$/.test(el.textContent||""));
  if(sourceSummary){sourceSummary.click();await sleep(1000);}
  window.scrollTo(0,0); await sleep(250); add();
  for(let i=0;i<18;i++){window.scrollBy(0,Math.max(450,Math.floor(innerHeight*.7)));await sleep(180);add();}
  window.scrollTo(0,document.body.scrollHeight);await sleep(300);add();
  const body=clean(document.body?.innerText||"");
  return {ok:true,url:location.href,title:document.title,body,sources:[...seen.values()]};
})()
"""

LATEST_CHAT_JS = r"""
(() => {
  const clean=(s)=>String(s||"").replace(/\s+/g," ").trim();
  const links=[...document.querySelectorAll('a[href*="/a/chat/s/"]')].map(a=>({href:a.href,text:clean(a.innerText||a.textContent),top:a.getBoundingClientRect().top,left:a.getBoundingClientRect().left}));
  links.sort((a,b)=>a.top-b.top||a.left-b.left);
  if(links.length) return {ok:true,...links[0]};
  if(location.href.includes('/a/chat/s/')) return {ok:true,href:location.href,text:document.title};
  return {ok:false,url:location.href,title:document.title};
})()
"""


def _new_chat_point(xml: str, width: int, height: int, description: str = "开启新对话") -> tuple[int, int] | None:
    """Locate only the top-right new-chat plus, never the upload plus near the composer."""
    root = ET.fromstring(xml)
    for node in root.iter():
        if str(node.attrib.get("content-desc") or "").strip() != description:
            continue
        bounds = [int(value) for value in re.findall(r"\d+", node.attrib.get("bounds") or "")]
        if len(bounds) != 4:
            continue
        center_x = (bounds[0] + bounds[2]) // 2
        center_y = (bounds[1] + bounds[3]) // 2
        if center_x >= int(width * 0.75) and center_y <= int(height * 0.20):
            return center_x, center_y
    return None


class DeepSeekAppController:
    """只在 MuMu DeepSeek App 中新建会话和发送问题。"""

    PACKAGE = "com.deepseek.chat"
    NEW_CHAT = "开启新对话"
    SEND = "发送"
    INPUT_HINTS = ("发消息或按住说话", "发消息")
    BUSY_HINTS = ("停止生成", "停止回答", "正在生成", "思考中", "搜索中")

    def __init__(self, serial: str = "127.0.0.1:16384", connect_timeout: int = 15):
        self.serial = serial
        try:
            with ThreadPoolExecutor(max_workers=1) as executor:
                self.d = executor.submit(u2.connect, serial).result(timeout=connect_timeout)
        except FutureTimeoutError as exc:
            raise RuntimeError(f"连接 MuMu {serial} 超时") from exc
        try:
            self.d.set_input_ime(True)
        except Exception:
            self.d.set_fastinput_ime(True)

    def ensure_ready(self) -> None:
        self.d.app_start(self.PACKAGE, stop=False)
        time.sleep(1)
        if not any(self.d(text=hint).exists for hint in self.INPUT_HINTS) and not self.d(className="android.widget.EditText").exists:
            raise RuntimeError("MuMu DeepSeek App 未显示输入区域，请检查登录态和页面")

    def _ensure_smart_search(self) -> None:
        """监控信源需要智能搜索；只在明确识别为关闭时点击。"""
        deadline = time.time() + 8
        while time.time() < deadline:
            xml = self.d.dump_hierarchy(compressed=False)
            root = ET.fromstring(xml)
            parent_map = {child: parent for parent in root.iter() for child in parent}
            for node in root.iter():
                if node.attrib.get("text") != "智能搜索":
                    continue
                parent = parent_map.get(node)
                while parent is not None:
                    if parent.attrib.get("checkable") == "true":
                        if parent.attrib.get("checked") != "true":
                            self.d(text="智能搜索").click()
                            time.sleep(0.5)
                        return
                    parent = parent_map.get(parent)
            time.sleep(0.5)
        raise RuntimeError("没有找到 DeepSeek App 的“智能搜索”开关")

    def new_chat(self) -> None:
        width, height = self.d.window_size()
        point = _new_chat_point(
            self.d.dump_hierarchy(compressed=False), width, height, self.NEW_CHAT
        )
        if point is None:
            # DeepSeek 偶尔不暴露无障碍节点；右上角位置固定，且与底部上传加号相距很远。
            point = (width - max(24, int(width * 0.065)), max(48, int(height * 0.075)))
        self.d.click(*point)
        time.sleep(1)
        if any(self.d(text=hint).exists for hint in self.INPUT_HINTS) or self.d(className="android.widget.EditText").exists:
            return
        raise RuntimeError("已点击 DeepSeek App 右上角新会话按钮，但没有进入可提问状态")

    def send(self, question: str) -> None:
        self.ensure_ready()
        self.new_chat()
        self._ensure_smart_search()
        edit = self.d(className="android.widget.EditText")
        if not edit.exists:
            for hint in self.INPUT_HINTS:
                target = self.d(text=hint)
                if target.exists:
                    target.click()
                    break
            time.sleep(0.5)
            edit = self.d(className="android.widget.EditText")
        if not edit.exists:
            raise RuntimeError("没有找到 DeepSeek App 输入框")
        edit.set_text(question)
        time.sleep(0.5)
        if str(edit.info.get("text") or "") != question:
            raise RuntimeError("DeepSeek App 中文问题写入校验失败")
        send = self.d(description=self.SEND)
        if not send.exists(timeout=3):
            raise RuntimeError("问题已写入，但没有找到 DeepSeek App 发送按钮")
        send.click()

    def wait_for_answer(self, question: str, timeout: int = 180, stable_seconds: int = 4) -> dict[str, Any]:
        """Wait until the App has displayed this question and returned to an idle, stable state."""
        deadline = time.time() + timeout
        stable_since: float | None = None
        previous_signature = ""
        saw_question = False
        last: dict[str, Any] = {}
        ignored = {"快速模式", "深度思考", "智能搜索", "上传文件", "切换至语音", "转至底部"}
        compact_question = re.sub(r"\s+", "", question)
        topic = re.sub(r"^(请|麻烦|帮我|给我|推荐|介绍|想要|需要)+", "", compact_question)
        topic = re.sub(r"^(一款|一个|一种)", "", topic)

        while time.time() < deadline:
            root = ET.fromstring(self.d.dump_hierarchy(compressed=False))
            texts: list[str] = []
            descriptions: list[str] = []
            for node in root.iter():
                resource_id = str(node.attrib.get("resource-id") or "")
                if resource_id.startswith("com.android.systemui:"):
                    continue
                text = str(node.attrib.get("text") or "").strip()
                description = str(node.attrib.get("content-desc") or "").strip()
                if text:
                    texts.append(text)
                if description:
                    descriptions.append(description)

            combined = "\n".join(texts + descriptions)
            compact_combined = re.sub(r"\s+", "", combined)
            saw_question = saw_question or compact_question in compact_combined or (
                len(topic) >= 2 and topic in compact_combined
            )
            busy = any(hint in combined for hint in self.BUSY_HINTS)
            input_ready = any(hint in combined for hint in self.INPUT_HINTS)
            content = [item for item in texts if item not in ignored and item not in self.INPUT_HINTS]
            signature = "\n".join(content)
            has_answer = any(
                len(item) >= 20 and item != question and not item.startswith("已阅读")
                for item in content
            )
            last = {
                "busy": busy,
                "input_ready": input_ready,
                "saw_question": saw_question,
                "has_answer": has_answer,
                "text_length": len(signature),
            }

            if saw_question and has_answer and input_ready and not busy:
                if signature == previous_signature:
                    stable_since = stable_since or time.time()
                    if time.time() - stable_since >= stable_seconds:
                        return last
                else:
                    previous_signature = signature
                    stable_since = None
            else:
                previous_signature = signature
                stable_since = None
            time.sleep(1)

        raise TimeoutError("等待 DeepSeek App 回答完成超时：" + json.dumps(last, ensure_ascii=False))

    def account_identity(self) -> dict[str, str]:
        self.d.app_start(self.PACKAGE, stop=False)
        time.sleep(0.8)
        if not self.d(description="关闭侧边栏").exists:
            opener = self.d(description="打开侧边栏")
            if not opener.exists(timeout=3):
                raise RuntimeError("无法打开 DeepSeek App 账号侧边栏")
            opener.click()
            time.sleep(0.8)
        root = ET.fromstring(self.d.dump_hierarchy(compressed=False))
        candidates: list[str] = []
        for node in root.iter():
            text = str(node.attrib.get("text") or "").strip()
            bounds = [int(value) for value in re.findall(r"\d+", node.attrib.get("bounds") or "")]
            if text and len(bounds) == 4 and bounds[1] >= 850 and len(text) <= 80:
                candidates.append(text)
        closer = self.d(description="关闭侧边栏")
        if closer.exists:
            closer.click()
        if not candidates:
            raise RuntimeError("DeepSeek App 侧边栏没有识别到账号名称")
        return {"name": candidates[-1], "masked": candidates[-1][:2] + "***"}


class DeepSeekWebCollector:
    def __init__(self, port: int = 9333):
        self.host = f"http://127.0.0.1:{port}"

    def _json(self, path: str) -> Any:
        with urllib.request.urlopen(self.host + path, timeout=10) as response:
            return json.loads(response.read().decode("utf-8", errors="replace"))

    def _page(self) -> dict[str, Any]:
        try:
            pages = self._json("/json")
        except Exception as exc:
            raise RuntimeError("DeepSeek 专用 Chrome 未启动；请先运行 open_deepseek_chrome.bat") from exc
        browser_pages = [p for p in pages if p.get("type") == "page"]
        candidates = [p for p in browser_pages if "chat.deepseek.com" in str(p.get("url") or "")]
        # 新建专用用户目录时 Chrome 可能先显示欢迎页；new_chat 会把该页导航到 DeepSeek。
        if not candidates:
            if browser_pages:
                return browser_pages[0]
            raise RuntimeError("专用 Chrome 中没有可用网页")
        candidates.sort(key=lambda p: 0 if "/a/chat/" in str(p.get("url") or "") else 1)
        return candidates[0]

    def _call(self, method: str, params: dict | None = None, timeout: int = 30) -> dict:
        try:
            import websocket
        except ImportError as exc:
            raise RuntimeError("缺少 websocket-client，请运行 pip install -r requirements.txt") from exc
        url = self._page().get("webSocketDebuggerUrl")
        if not url:
            raise RuntimeError("DeepSeek 页面没有调试连接")
        ws = websocket.create_connection(url, timeout=timeout)
        try:
            ws.send(json.dumps({"id": 1, "method": method, "params": params or {}}))
            while True:
                message = json.loads(ws.recv())
                if message.get("id") != 1:
                    continue
                if message.get("error"):
                    raise RuntimeError(json.dumps(message["error"], ensure_ascii=False))
                result = message.get("result") or {}
                if result.get("exceptionDetails"):
                    raise RuntimeError(json.dumps(result["exceptionDetails"], ensure_ascii=False))
                return result
        finally:
            ws.close()

    def browser_call(self, method: str, params: dict | None = None, timeout: int = 30) -> dict:
        try:
            import websocket
        except ImportError as exc:
            raise RuntimeError("缺少 websocket-client，请运行 pip install -r requirements.txt") from exc
        url = str(self._json("/json/version").get("webSocketDebuggerUrl") or "")
        if not url:
            raise RuntimeError("DeepSeek Chrome 没有浏览器级调试连接")
        ws = websocket.create_connection(url, timeout=timeout)
        try:
            ws.send(json.dumps({"id": 1, "method": method, "params": params or {}}))
            while True:
                message = json.loads(ws.recv())
                if message.get("id") != 1:
                    continue
                if message.get("error"):
                    raise RuntimeError(json.dumps(message["error"], ensure_ascii=False))
                return message.get("result") or {}
        finally:
            ws.close()

    def evaluate(self, expression: str, timeout: int = 30) -> Any:
        result = self._call("Runtime.evaluate", {"expression": expression, "awaitPromise": True, "returnByValue": True}, timeout)
        return (result.get("result") or {}).get("value")

    def _navigate(self, url: str) -> None:
        self._call("Page.navigate", {"url": url})

    def latest_chat(self, timeout: int = 20) -> dict[str, Any]:
        """Return the first conversation in today's sidebar without opening it."""
        self._navigate("https://chat.deepseek.com/")
        deadline = time.time() + timeout
        last: Any = None
        while time.time() < deadline:
            time.sleep(1)
            last = self.evaluate(LATEST_CHAT_JS)
            if isinstance(last, dict) and last.get("ok") and last.get("href"):
                return last
        if isinstance(last, dict) and not last.get("ok"):
            return last
        raise TimeoutError("读取 DeepSeek 网页最新会话超时：" + json.dumps(last, ensure_ascii=False)[:500])

    def collect_latest(self, question: str, timeout: int = 180, stable_seconds: int = 8,
                       previous_chat_url: str = "") -> dict:
        """等待 App 会话同步到网页，打开最新会话后只做读取。"""
        deadline = time.time() + timeout
        last: Any = None
        previous_chat_url = str(previous_chat_url or "").split("?", 1)[0].rstrip("/")
        while time.time() < deadline:
            latest = self.latest_chat(timeout=min(20, max(3, int(deadline - time.time()))))
            last = latest
            if isinstance(latest, dict) and latest.get("ok") and latest.get("href"):
                latest_url = str(latest["href"]).split("?", 1)[0].rstrip("/")
                if previous_chat_url and latest_url == previous_chat_url:
                    time.sleep(2)
                    continue
                self._navigate(latest_url)
                time.sleep(2)
                snapshot = self.evaluate(SNAPSHOT_JS)
                last = snapshot
                if isinstance(snapshot, dict) and question in str(snapshot.get("body") or ""):
                    result = self.wait_and_collect(timeout=max(10, int(deadline - time.time())), stable_seconds=stable_seconds, expected_question=question)
                    result["conversation_url"] = latest_url
                    return result
            time.sleep(2)
        raise TimeoutError("等待 DeepSeek App 会话同步到网页超时：" + json.dumps(last, ensure_ascii=False)[:500])

    def account_identity(self) -> dict[str, str]:
        value = self.evaluate(r"""
(async()=>{
  const raw=localStorage.getItem('userToken')||'{}'; let token='';
  try{token=JSON.parse(raw).value||''}catch{}
  if(!token)return {ok:false,error:'网页尚未登录'};
  const response=await fetch('/api/v0/users/current',{headers:{Authorization:'Bearer '+token}});
  const payload=await response.json(); const user=payload?.data?.biz_data;
  if(!user)return {ok:false,error:payload?.msg||'无法读取网页账号'};
  const profile=user.id_profile||{};
  return {ok:true,id:String(user.id||profile.id||''),name:String(profile.name||user.email||user.mobile_number||''),email:String(user.email||''),mobile:String(user.mobile_number||'')};
})()
""")
        if not isinstance(value, dict) or not value.get("ok"):
            raise RuntimeError(str((value or {}).get("error") or "无法读取 DeepSeek 网页账号"))
        stable_id = str(value.get("id") or value.get("email") or value.get("mobile") or "")
        name = str(value.get("name") or "").strip()
        return {"name": name, "masked": name[:2] + "***",
                "identity_hash": hashlib.sha256(stable_id.encode("utf-8")).hexdigest()[:12] if stable_id else ""}

    def wait_and_collect(self, timeout: int = 180, stable_seconds: int = 8, expected_question: str = "") -> dict:
        deadline = time.time() + timeout
        stable_since = None
        previous: tuple[str, int] = ("", -1)
        last = None
        while time.time() < deadline:
            time.sleep(2)
            last = self.evaluate(SNAPSHOT_JS)
            if not isinstance(last, dict):
                continue
            body = str(last.get("body") or "")
            if expected_question and expected_question not in body:
                stable_since = None
                previous = (body, int(last.get("linkCount") or 0))
                continue
            fingerprint = (body, int(last.get("linkCount") or 0))
            if body and fingerprint == previous and not last.get("busy") and last.get("readyState") == "complete":
                stable_since = stable_since or time.time()
                if time.time() - stable_since >= stable_seconds:
                    result = self.evaluate(COLLECT_JS, timeout=60)
                    if isinstance(result, dict) and result.get("ok"):
                        return result
            else:
                stable_since = None
                previous = fingerprint
        raise TimeoutError("等待 DeepSeek 回答完成超时：" + json.dumps(last, ensure_ascii=False)[:500])
