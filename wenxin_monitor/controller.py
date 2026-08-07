from __future__ import annotations

import re
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import uiautomator2 as u2

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from monitor_core.cdp_chat import CDPPage, capture_json_responses, ensure_chrome, external_sources


BASE_DIR = Path(__file__).resolve().parent


class WenxinAppController:
    PACKAGE = "com.baidu.newapp"

    def __init__(self, serial: str = "127.0.0.1:16384"):
        self.serial = serial
        self.d = u2.connect(serial)

    def _edit(self):
        return self.d(className="android.widget.EditText")

    def ensure_ready(self) -> None:
        self.d.app_start(self.PACKAGE, stop=False)
        if self._edit().exists(timeout=8):
            return
        current = str(self.d.app_current().get("package") or "")
        if current != self.PACKAGE:
            raise RuntimeError("文心 App 未正常启动，请检查模拟器是否已开启")
        raise RuntimeError("文心 App 尚未登录或未进入可提问页面")

    def new_chat(self) -> None:
        self.ensure_ready()
        width, height = self.d.window_size()
        self.d.click(max(30, int(width * 0.085)), max(55, int(height * 0.075)))
        if self.d(text="新建对话").exists(timeout=3):
            self.d(text="新建对话").click()
        else:
            self.d.click(int(width * 0.42), int(height * 0.18))
        time.sleep(1)
        if not self._edit().exists(timeout=5):
            raise RuntimeError("文心 App 新建会话失败，未找到问题输入框")

    def send(self, prompt: str) -> dict[str, Any]:
        self.new_chat()
        edit = self._edit()
        width, height = self.d.window_size()
        if not edit.exists:
            raise RuntimeError("文心 App 没有可用的问题输入框")
        edit.set_text(prompt)
        time.sleep(1)
        if re.sub(r"\s+", "", str(edit.info.get("text") or "")) != re.sub(r"\s+", "", prompt):
            raise RuntimeError("文心 App 问题写入校验失败")
        # Filled composer replaces the bottom-right plus with a purple send arrow.
        self.d.click(width - max(42, int(width * 0.105)), height - max(70, int(height * 0.095)))
        time.sleep(0.25)
        generation_indicator_seen = self.generation_indicator_visible()
        time.sleep(0.75)
        if self._edit().exists and str(self._edit().info.get("text") or "").strip() == prompt:
            raise RuntimeError("文心 App 发送按钮没有生效")
        return {"generation_indicator_seen_at_send": generation_indicator_seen}

    def wait_for_mobile_accept(self, timeout: int = 60, prompt: str = "") -> dict[str, Any]:
        deadline = time.monotonic() + max(3, timeout)
        normalized_prompt = re.sub(r"\s+", "", prompt)
        while time.monotonic() < deadline:
            if str(self.d.app_current().get("package") or "") != self.PACKAGE:
                raise RuntimeError("文心 App 在发送后意外退出")
            edit = self._edit()
            current = str(edit.info.get("text") or "") if edit.exists else ""
            if not normalized_prompt or re.sub(r"\s+", "", current) != normalized_prompt:
                return {"ok": True, "title": "App 已接受问题，等待网页同步", "input_cleared": True}
            time.sleep(1)
        raise TimeoutError("文心 App 输入框未清空，问题可能没有发送成功")

    @staticmethod
    def generation_indicator_in_xml(xml: str) -> bool:
        try:
            root = ET.fromstring(xml)
        except ET.ParseError:
            return False
        markers = ("停止生成", "停止回答", "停止响应", "stop generating", "stop response")
        for node in root.iter():
            identity = " ".join(
                str(node.attrib.get(key) or "")
                for key in ("text", "content-desc", "resource-id", "hint")
            ).casefold()
            if any(marker in identity for marker in markers):
                return True
        return False

    @staticmethod
    def generation_indicator_in_image(xml: str, image: Any) -> bool:
        try:
            root = ET.fromstring(xml)
            grayscale = image.convert("L")
        except (ET.ParseError, AttributeError):
            return False
        screen_width, screen_height = grayscale.size
        for node in root.iter():
            bounds = [int(value) for value in re.findall(r"\d+", str(node.attrib.get("bounds") or ""))]
            if len(bounds) != 4:
                continue
            left, top, right, bottom = bounds
            width, height = right - left, bottom - top
            center_x, center_y = (left + right) / 2, (top + bottom) / 2
            if not (center_x >= screen_width * 0.82 and center_y >= screen_height * 0.70):
                continue
            if not (28 <= width <= 120 and 28 <= height <= 120 and 0.65 <= width / max(1, height) <= 1.45):
                continue
            crop = grayscale.crop((left + width * 0.30, top + height * 0.30,
                                   right - width * 0.30, bottom - height * 0.30))
            dark = [(pixel < 125) for pixel in crop.getdata()]
            if not dark or sum(dark) < 12:
                continue
            pixels = list(crop.getdata())
            crop_width, crop_height = crop.size
            positions = [(index % crop_width, index // crop_width)
                         for index, pixel in enumerate(pixels) if pixel < 125]
            dark_left = min(x for x, _ in positions)
            dark_right = max(x for x, _ in positions)
            dark_top = min(y for _, y in positions)
            dark_bottom = max(y for _, y in positions)
            box_area = max(1, (dark_right - dark_left + 1) * (dark_bottom - dark_top + 1))
            if len(positions) / box_area >= 0.55:
                return True
        return False

    def generation_indicator_visible(self) -> bool:
        selectors = (
            {"textMatches": ".*(停止生成|停止回答|停止响应).*"},
            {"descriptionMatches": ".*(停止生成|停止回答|停止响应).*"},
            {"resourceIdMatches": ".*(stop|generating|answering).*"},
        )
        for selector in selectors:
            try:
                if self.d(**selector).exists:
                    return True
            except Exception:
                continue
        xml = self.d.dump_hierarchy(compressed=False)
        if self.generation_indicator_in_xml(xml):
            return True
        try:
            image = self.d.screenshot(format="pillow")
        except Exception:
            return False
        return self.generation_indicator_in_image(xml, image)

    def wait_for_generation_complete(self, timeout: int = 180, already_seen: bool = False) -> dict[str, Any]:
        deadline = time.monotonic() + max(10, timeout)
        appeared = already_seen
        disappeared_stably = 0
        while time.monotonic() < deadline:
            visible = self.generation_indicator_visible()
            if visible:
                appeared = True
                disappeared_stably = 0
            elif appeared:
                disappeared_stably += 1
                if disappeared_stably >= 2:
                    return {"ok": True, "generation_indicator_seen": True,
                            "generation_complete": True, "rendering_required": False}
            time.sleep(1)
        if not appeared:
            raise TimeoutError("没有检测到文心 App 右下角的停止生成按钮")
        raise TimeoutError("文心 App 停止生成按钮长时间未消失")
    def account_identity(self) -> dict[str, str]:
        self.ensure_ready()
        # The App no longer exposes the account name in its accessibility tree.
        # Account equality is proven by the App-created conversation appearing as
        # the newest web conversation; never report footer text as a username.
        return {"name": "已登录文心 App", "masked": "文心***"}


class WenxinWebCollector:
    HOME = "https://wenxin.baidu.com/"
    EXCLUDED = ("wenxin.baidu.com", "chat.baidu.com", "passport.baidu.com",
                "beian.miit.gov.cn", "aisearch.cdn.bcebos.com")

    def __init__(self, port: int = 9444):
        ensure_chrome(port, BASE_DIR / "chrome_profile", self.HOME)
        self.page = CDPPage(port)

    def _snapshot(self) -> dict[str, Any]:
        return self.page.evaluate("({url:location.href,body:(document.body?.innerText||''),title:document.title})") or {}

    def account_identity(self) -> dict[str, str]:
        snapshot = self._snapshot()
        body = str(snapshot.get("body") or "")
        match = re.search(r"\n([^\n]{2,40})\n百度首页\n", body)
        name = match.group(1).strip() if match else ""
        if not name and "对话历史" in body and "登录后" not in body:
            name = "已登录文心网页"
        return {"name": name, "masked": name[:2] + "***" if name else ""}

    def _open_latest_once(self) -> str:
        self.page.call("Page.navigate", {"url": self.HOME})
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            body = str(self._snapshot().get("body") or "")
            if "对话历史" in body and len(body.splitlines()) >= 8:
                break
            time.sleep(0.5)
        # The first history row is directly below the expanded history heading.
        for _ in range(3):
            self.page.evaluate("document.querySelector('.chat-side-list-item')?.click(); true")
            time.sleep(2)
            url = str(self._snapshot().get("url") or "")
            if "/search/" in url:
                return url
        return str(self._snapshot().get("url") or "")

    def latest_reference(self) -> str:
        return self._open_latest_once()

    def collect_latest(self, previous_url: str, timeout: int = 180, expected_question: str = "") -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        current_url = ""
        while time.monotonic() < deadline:
            current_url = self._open_latest_once()
            if "/search/" in current_url and current_url != previous_url:
                break
            time.sleep(4)
        else:
            raise TimeoutError("文心网页端没有同步出新的会话")

        stable = 0
        last_body = ""
        while time.monotonic() < deadline:
            snapshot = self._snapshot()
            body = str(snapshot.get("body") or "")
            busy = any(marker in body for marker in ("停止生成", "正在思考", "搜索中", "思考中"))
            if len(body) >= 120 and body == last_body and not busy:
                stable += 1
                if stable >= 3:
                    break
            else:
                stable = 0
            last_body = body
            time.sleep(2)
        if stable < 3:
            raise TimeoutError("文心网页回答未稳定完成")

        responses = capture_json_responses(
            self.page,
            lambda: self.page.send("Page.reload", {"ignoreCache": True}),
            seconds=12,
        )
        current_id = current_url.rstrip("/").split("/")[-1].split("?")[0]
        sources = []
        for url, payload in responses:
            if "/csaitab/history/list?" not in url or "req_type=5" not in url:
                continue
            if "cur_ori_lid=" not in url or current_id not in url:
                continue
            current_payload = payload
            if isinstance(payload, dict):
                history = ((payload.get("data") or {}).get("historyData")
                           if isinstance(payload.get("data"), dict) else None)
                if isinstance(history, list) and history:
                    current_payload = history[0]
            sources.extend(external_sources(current_payload, self.EXCLUDED))
        deduped = {item["url"]: item for item in sources}
        snapshot = self._snapshot()
        if str(snapshot.get("url") or "") != current_url:
            raise RuntimeError("文心网页会话在采集时发生切换")
        body = str(snapshot.get("body") or "")
        compact_body = re.sub(r"\s+", "", body)
        compact_question = re.sub(r"\s+", "", expected_question)
        if compact_question and compact_question not in compact_body:
            raise RuntimeError("文心网页会话与当前问题不匹配")
        count_match = re.search(r"共参考\s*(\d+)\s*篇资料", body)
        expected_source_count = int(count_match.group(1)) if count_match else 0
        answer_match = re.search(
            r"共参考\s*\d+\s*篇资料\s*(.*?)(?:聊聊新话题|任务\s*AI生图)",
            body,
            re.S,
        )
        if not answer_match or not answer_match.group(1).strip():
            raise RuntimeError("文心正文区域提取失败，拒绝保存整页或旧会话文本")
        answer = answer_match.group(1).strip()
        if len(deduped) < expected_source_count:
            raise RuntimeError(
                f"文心信源抓取不完整：页面显示 {expected_source_count} 条，实际获取 {len(deduped)} 条"
            )
        return {"url": current_url, "body": answer, "page_body": body,
                "sources": list(deduped.values()), "source_count": len(deduped),
                "expected_source_count": expected_source_count, "source_capture_complete": True}
