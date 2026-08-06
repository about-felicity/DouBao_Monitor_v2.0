from __future__ import annotations

import re
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import uiautomator2 as u2

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from monitor_core.cdp_chat import CDPPage, capture_json_responses, ensure_chrome, external_sources
from monitor_core.quality import invalid_answer_reason


BASE_DIR = Path(__file__).resolve().parent


class AfuAppController:
    PACKAGE = "com.antgroup.aijk.android"
    INPUT = "com.antgroup.aijk.android.ijkchat:id/v_text_input"
    SEND = "com.antgroup.aijk.android.ijkchat:id/v_send_status"

    def __init__(self, serial: str = "127.0.0.1:16384"):
        self.serial = serial
        self.d = u2.connect(serial)
        try:
            self.d.set_input_ime(True)
        except Exception:
            self.d.set_fastinput_ime(True)

    def ensure_ready(self) -> None:
        self.d.app_start(self.PACKAGE, stop=False)
        if self.d.app_current().get("activity", "").endswith("XRiverActivity"):
            self.d.press("back")
        if not self.d(resourceId=self.INPUT).exists(timeout=12):
            raise RuntimeError("蚂蚁阿福 App 未显示输入框，请检查登录状态")

    def new_chat(self) -> None:
        self.ensure_ready()
        width, height = self.d.window_size()
        # The new-conversation action is inside the top-right overflow menu.
        self.d.click(int(width * 0.883), int(height * 0.071))
        create = self.d(text="新建会话")
        if not create.exists(timeout=3):
            raise RuntimeError("蚂蚁阿福没有打开新建会话菜单")
        create.click()
        time.sleep(1.5)
        if not self.d(resourceId=self.INPUT).exists(timeout=5):
            raise RuntimeError("蚂蚁阿福 App 新建会话失败")

    def send(self, prompt: str) -> None:
        self.new_chat()
        edit = self.d(resourceId=self.INPUT)
        edit.set_text(prompt)
        if re.sub(r"\s+", "", str(edit.info.get("text") or "")) != re.sub(r"\s+", "", prompt):
            raise RuntimeError("蚂蚁阿福问题写入校验失败")
        send = self.d(resourceId=self.SEND)
        if not send.exists(timeout=3):
            raise RuntimeError("蚂蚁阿福发送按钮不可用")
        send.click()

    def _reply(self, prompt: str) -> str:
        root = ET.fromstring(self.d.dump_hierarchy(compressed=False))
        candidates = []
        ignored = {prompt, "智能体", "AI诊室", "报告解读", "拍皮肤", "就医服务"}
        for node in root.iter():
            text = str(node.attrib.get("text") or "").replace("\xa0", "\n").strip()
            if text and text not in ignored and len(text) >= 20:
                candidates.append(text)
        return max(candidates, key=len, default="")

    def wait_for_answer(self, prompt: str, timeout: int = 180) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        stable = 0
        previous = ""
        while time.monotonic() < deadline:
            answer = self._reply(prompt)
            xml = self.d.dump_hierarchy(compressed=False)
            busy = any(marker in xml for marker in ("正在分析", "正在生成", "思考中", "停止生成"))
            invalid = invalid_answer_reason(answer)
            if answer and invalid == "模型返回系统或服务异常":
                return {"ok": False, "reply": answer, "citation_numbers": [], "skip_reason": invalid}
            if len(answer) >= 60 and answer == previous and not busy:
                stable += 1
                if stable >= 3:
                    citations = sorted({int(item) for item in re.findall(r"\[\^(\d+)\]", answer)})
                    return {"ok": True, "reply": answer, "citation_numbers": citations}
            else:
                stable = 0
            previous = answer
            time.sleep(2)
        raise TimeoutError("蚂蚁阿福回答没有稳定完成")

    def account_identity(self) -> dict[str, str]:
        self.ensure_ready()
        body = self.d.dump_hierarchy(compressed=False)
        logged = self.INPUT in body
        return {"name": "已登录阿福 App" if logged else "", "masked": "阿福***" if logged else ""}


class AfuWebCollector:
    HOME = "https://chat.antafu.com/"
    EXCLUDED = ("chat.antafu.com", "antafu.com", "render.alipay.com")

    def __init__(self, port: int = 9555):
        ensure_chrome(port, BASE_DIR / "chrome_profile", self.HOME)
        self.page = CDPPage(port)

    def _snapshot(self) -> dict[str, Any]:
        return self.page.evaluate("({url:location.href,body:(document.body?.innerText||''),title:document.title})") or {}

    def account_identity(self) -> dict[str, str]:
        body = str(self._snapshot().get("body") or "")
        landing_only = "你的AI医生朋友" in body and not any(
            marker in body for marker in ("对话记录", "历史", "开启新对话", "新建对话", "最近对话")
        )
        return {"name": "" if landing_only else "已登录阿福网页", "masked": "" if landing_only else "阿福网页***"}

    def _latest_meta(self) -> dict[str, str]:
        return self.page.evaluate("""
          (() => {
            const row = document.querySelector('[data-aspm-param^="sessionId="]');
            if (!row) return {};
            const raw = row.getAttribute('data-aspm-param') || '';
            const sessionId = raw.replace(/^sessionId=/, '');
            const title = (row.querySelector('[class*="sessionTitleText"]')?.innerText || row.innerText || '').trim();
            return {sessionId, title};
          })()
        """) or {}

    def _click_latest(self) -> None:
        clicked = self.page.evaluate("""
          (() => {
            const row = document.querySelector('[data-aspm-param^="sessionId="]');
            if (!row) return false;
            const target = row.querySelector('[class*="sessionTitleText"]') || row;
            const value = target.getBoundingClientRect();
            const options = {bubbles:true, cancelable:true, view:window,
              clientX:value.x + value.width / 2, clientY:value.y + value.height / 2, button:0};
            ['pointerdown','mousedown','pointerup','mouseup','click'].forEach(
              type => target.dispatchEvent(new MouseEvent(type, options))
            );
            return true;
          })()
        """)
        if not clicked:
            raise RuntimeError("蚂蚁阿福网页没有找到最新会话")

    def _open_latest_once(self) -> str:
        self._go_home()
        time.sleep(3)
        meta = self._latest_meta()
        if not meta.get("sessionId"):
            return ""
        return str(meta["sessionId"])

    def latest_reference(self) -> str:
        if not self.account_identity().get("name"):
            raise RuntimeError("蚂蚁阿福专用 Chrome 尚未登录或未显示历史会话")
        return self._open_latest_once()

    def collect_latest(self, previous_url: str, timeout: int = 180) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        current_session = ""
        responses: list[tuple[str, Any]] = []
        while time.monotonic() < deadline:
            self._go_home()
            time.sleep(3)
            meta = self._latest_meta()
            current_session = str(meta.get("sessionId") or "")
            if current_session and current_session != previous_url:
                responses = capture_json_responses(self.page, self._click_latest, seconds=12)
                body = self._answer_body()
            else:
                body = ""
            if len(body) >= 100:
                break
            time.sleep(4)
        else:
            raise TimeoutError("蚂蚁阿福网页端没有同步出新的会话")

        stable = 0
        previous_body = ""
        while time.monotonic() < deadline:
            body = self._answer_body()
            busy = any(marker in body for marker in ("正在分析", "正在生成", "思考中", "停止生成"))
            if len(body) >= 100 and body == previous_body and not busy:
                stable += 1
                if stable >= 3:
                    break
            else:
                stable = 0
            previous_body = body
            time.sleep(2)
        if stable < 3:
            raise TimeoutError("蚂蚁阿福网页回答未稳定完成")

        sources = []
        for response_url, payload in responses:
            if "/chat/queryHistory" in response_url:
                sources.extend(external_sources(payload, self.EXCLUDED))
        dom_sources = self.page.evaluate("""
          [...document.querySelectorAll('a[href]')].map(a=>({url:a.href,title:(a.innerText||a.textContent||a.title||'').trim()}))
            .filter(x=>/^https?:/.test(x.url))
        """) or []
        for item in dom_sources:
            try:
                host = re.sub(r"^www\.", "", urlparse(item["url"]).netloc.casefold())
            except Exception:
                continue
            if not any(host == excluded or host.endswith("." + excluded) for excluded in self.EXCLUDED):
                sources.append(item)
        deduped = {item["url"]: item for item in sources if item.get("url")}
        body = self._answer_body()
        return {"url": f"{self.HOME}chat?sessionId={current_session}", "body": body,
                "sources": list(deduped.values()),
                "source_count": len(deduped)}

    def _answer_body(self) -> str:
        return str(self.page.evaluate("""
          (() => {
            const answers = [...document.querySelectorAll('.stream-message-renderer')]
              .map(item => (item.innerText || '').trim()).filter(Boolean);
            if (answers.length) return answers[answers.length - 1];
            const main = document.querySelector('[class*="mainChatContent"]');
            return (main?.innerText || '').trim();
          })()
        """) or "")

    def _go_home(self) -> None:
        # The site keeps the selected session only in SPA state while location
        # remains '/'. Passing an extra query string prevents its row click from
        # routing, so reset through a blank document and reopen the exact home URL.
        self.page.call("Page.navigate", {"url": "about:blank"})
        time.sleep(0.2)
        self.page.call("Page.navigate", {"url": self.HOME})
