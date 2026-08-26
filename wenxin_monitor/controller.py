from __future__ import annotations

import os
import random
import re
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urljoin, urlparse

import requests
try:
    import uiautomator2 as u2
except ImportError:  # Legacy App controller only; Baidu search collection does not need it.
    u2 = None

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from monitor_core.cdp_chat import CDPPage, capture_json_responses, ensure_chrome, external_sources


BASE_DIR = Path(__file__).resolve().parent


class ScraplingStealthPage:
    """Small compatibility adapter around Scrapling's persistent stealth browser."""

    def __init__(self, profile: Path, timeout: int = 60):
        self.is_stealth = True
        try:
            from scrapling.fetchers import StealthySession
        except ImportError as exc:
            raise RuntimeError(
                "文心隐身浏览器不可用，请安装 scrapling[fetchers]"
            ) from exc
        profile.mkdir(parents=True, exist_ok=True)
        self.adaptive_storage = profile / "scrapling_adaptive_elements.db"
        self._adaptive_snapshot_cache: dict[str, Any] | None = None
        self.timeout_ms = max(30, int(timeout)) * 1000
        self.session = StealthySession(
            # Wenxin collection is a background production job. Keep the
            # stealth browser hidden unless an operator explicitly opts into
            # a visible troubleshooting window.
            headless=os.environ.get("WENXIN_STEALTH_HEADLESS", "1") != "0",
            user_data_dir=str(profile),
            max_pages=1,
            timeout=self.timeout_ms,
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
            google_search=False,
            extra_headers={"Accept-Language": "zh-CN,zh;q=0.9"},
            disable_resources=False,
            block_webrtc=True,
            hide_canvas=True,
            allow_webgl=True,
            retries=1,
        )
        self.session.start()
        context = self.session.context
        if context is None:
            self.session.close()
            raise RuntimeError("Scrapling 隐身浏览器没有创建持久化上下文")
        self.page = context.pages[0] if context.pages else context.new_page()
        self.page.set_default_timeout(self.timeout_ms)
        self.page.set_default_navigation_timeout(self.timeout_ms)

    def _adaptive_document(self):
        """Parse the live DOM with Scrapling's persisted adaptive relocator."""
        from scrapling import Selector
        return Selector(
            self.page.content(),
            # Use one stable domain key so changing search query/element IDs do
            # not fragment Scrapling's learned element fingerprints.
            url="https://www.baidu.com/",
            adaptive=True,
            storage_args={
                "storage_file": str(self.adaptive_storage),
                "url": "https://www.baidu.com/",
            },
        )

    @staticmethod
    def _adaptive_find(document: Any, selectors: tuple[str, ...], identifier: str,
                       percentage: int = 40) -> list[Any]:
        for css in selectors:
            elements = list(document.css(css))
            if elements:
                document.save(elements[0], identifier)
                return elements
        saved = document.retrieve(identifier)
        if not saved:
            return []
        relocated = document.relocate(saved, percentage)
        if not relocated:
            return []
        return [relocated, *list(relocated.find_similar())]

    def adaptive_ai_snapshot(self) -> dict[str, Any]:
        """Locate the AI card/body/citations even after volatile IDs change."""
        if self._adaptive_snapshot_cache is not None:
            return dict(self._adaptive_snapshot_cache)
        document = self._adaptive_document()
        cards = self._adaptive_find(document, (
            '#content_left [tpl="new_baikan_index"]',
            '#content_left [m-name*="new_baikan_index"]',
            '#content_left [tpl="wenda_generate"]',
            '#content_left .ai-entry',
        ), "baidu-wenxin-ai-card", 42)
        if not cards:
            return {"ok": False, "body": "", "citationCount": 0, "citationXpaths": []}
        card = cards[0]
        answers = self._adaptive_find(card, (
            '.cosd-markdown-content',
            '[class*="accordion-panels-title"]',
            '[class*="markdown-content"]',
        ), "baidu-wenxin-answer-body", 38)
        # Adaptive relocation can return structurally similar nodes elsewhere;
        # retain only nodes that are still descendants of the learned AI card.
        answers = [item for item in answers if card._root in item._root.iterancestors()]
        chunks: list[str] = []
        for item in answers:
            value = re.sub(r"[\u200b-\u200f\u202a-\u202e\u2060\ufeff]", "",
                           str(item.get_all_text(separator="\n", strip=True))).strip()
            if value and value not in chunks:
                chunks.append(value)
        citations = self._adaptive_find(card, (
            '.cosd-citation', '[data-citation]', '[data-source-index]',
            'a[class*="citation"]', 'button[class*="citation"]',
            'span[class*="citation"]', '[class*="reference"] a[href]',
        ), "baidu-wenxin-citation", 36)
        citations = [item for item in citations if card._root in item._root.iterancestors()]
        xpaths: list[str] = []
        for item in citations:
            path = str(item._root.getroottree().getpath(item._root))
            if path and path not in xpaths:
                xpaths.append(path)
        body = "\n\n".join(chunks).strip()
        result = {"ok": bool(body), "body": body, "citationCount": len(xpaths),
                  "citationXpaths": xpaths, "adaptive": True}
        if result["ok"]:
            self._adaptive_snapshot_cache = dict(result)
        return result

    def adaptive_citation_point(self, index: int) -> dict[str, float] | None:
        snapshot = self.adaptive_ai_snapshot()
        paths = list(snapshot.get("citationXpaths") or [])
        if index < 0 or index >= len(paths):
            return None
        locator = self.page.locator(f"xpath={paths[index]}").first
        # ``scroll_into_view_if_needed`` leaves a citation at the bottom edge
        # when even one pixel is visible.  Its tooltip then opens off-screen.
        # Always center the marker so the complete popup can render and be read.
        locator.evaluate(
            "element => element.scrollIntoView({block:'center', inline:'nearest'})",
            timeout=5_000,
        )
        self.page.wait_for_timeout(300)
        box = locator.bounding_box(timeout=5_000)
        if not box:
            return None
        return {"x": float(box["x"] + box["width"] / 2),
                "y": float(box["y"] + box["height"] / 2)}

    @staticmethod
    def _target(page: Any) -> str:
        return f"scrapling-{id(page):x}"

    def call(self, method: str, params: dict[str, Any] | None = None,
             timeout: float = 15) -> dict[str, Any]:
        values = params or {}
        timeout_ms = max(1_000, int(timeout * 1000))
        if method == "Page.navigate":
            self._adaptive_snapshot_cache = None
            self.page.goto(
                str(values.get("url") or "about:blank"),
                wait_until="domcontentloaded",
                timeout=timeout_ms,
            )
            return {"frameId": self._target(self.page)}
        if method == "Input.dispatchMouseEvent":
            event_type = str(values.get("type") or "")
            x, y = float(values.get("x") or 0), float(values.get("y") or 0)
            if event_type == "mouseMoved":
                self.page.mouse.move(x, y)
            elif event_type == "mousePressed":
                self.page.mouse.move(x, y)
                self.page.mouse.down(button=str(values.get("button") or "left"))
            elif event_type == "mouseReleased":
                self.page.mouse.move(x, y)
                self.page.mouse.up(button=str(values.get("button") or "left"))
            return {}
        raise NotImplementedError(f"Scrapling 页面不支持调试命令：{method}")

    def evaluate(self, expression: str, timeout: float = 15) -> Any:
        self.page.set_default_timeout(max(1_000, int(timeout * 1000)))
        try:
            for attempt in range(2):
                try:
                    return self.page.evaluate(expression)
                except Exception as exc:
                    transient = any(token in str(exc).casefold() for token in (
                        "execution context was destroyed", "most likely because of a navigation",
                        "cannot find context", "target page, context or browser has been closed",
                    ))
                    if not transient or attempt:
                        raise
                    self.page.wait_for_load_state("domcontentloaded", timeout=self.timeout_ms)
                    self.page.wait_for_timeout(350)
        finally:
            self.page.set_default_timeout(self.timeout_ms)

    def click(self, x: int, y: int) -> None:
        self.page.mouse.click(float(x), float(y), button="left")

    def replace_tab(self, url: str, timeout: float = 20) -> dict[str, str]:
        old_page = self.page
        old_target = self._target(old_page)
        context = self.session.context
        if context is None:
            raise RuntimeError("Scrapling 隐身浏览器上下文已经关闭")
        new_page = context.new_page()
        new_page.set_default_timeout(self.timeout_ms)
        new_page.set_default_navigation_timeout(self.timeout_ms)
        try:
            new_page.goto(
                str(url or "about:blank"),
                wait_until="domcontentloaded",
                timeout=max(3_000, int(timeout * 1000)),
            )
        except Exception:
            new_page.close()
            raise
        self.page = new_page
        self._adaptive_snapshot_cache = None
        old_page.close()
        return {
            "old_target": old_target,
            "new_target": self._target(new_page),
            "url": str(url or "about:blank"),
        }

    def close(self) -> None:
        self.session.close()


class WenxinAppController:
    PACKAGE = "com.baidu.newapp"

    def __init__(self, serial: str = "127.0.0.1:16384"):
        if u2 is None:
            raise RuntimeError("旧版文心 App 控制器需要 uiautomator2；百度搜索直采不需要此依赖")
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


class LegacyWenxinWebCollector:
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


BAIDU_AI_SNAPSHOT_JS = r"""
(()=>{
  const card = document.querySelector(
    '#content_left [tpl="new_baikan_index"], #content_left [m-name*="new_baikan_index"], ' +
    '#content_left [tpl="wenda_generate"], #content_left .ai-entry'
  );
  const query = String(
    document.querySelector('#kw')?.value ||
    document.querySelector('input[name="wd"]')?.value || ''
  ).trim();
  if (!card) {
    return {
      ok:false,
      url:location.href,
      query,
      readyState:document.readyState,
      pageText:String(document.body?.innerText || '').slice(0, 1200)
    };
  }
  const chunks = [];
  const seen = new Set();
  for (const element of card.querySelectorAll(
    '[class*="accordion-panels-title"], .ai-entry .cosd-markdown-content'
  )) {
    const text = String(element.innerText || '')
      .replace(/[\u200b-\u200f\u202a-\u202e\u2060\ufeff]/g, '')
      .trim();
    if (!text || seen.has(text)) continue;
    seen.add(text);
    chunks.push(text);
  }
  const body = chunks.join('\n\n').trim();
  const folded = Array.from(card.querySelectorAll('button, [role="button"], div, span'))
    .some(element => /^展开剩余\d+%内容$/.test(String(element.innerText || '').trim()));
  const citationSelectors = [
    '.cosd-citation',
    '[data-citation]',
    '[data-source-index]',
    'a[class*="citation"], button[class*="citation"], span[class*="citation"]',
    '[class*="reference"] a[href]'
  ];
  let citations = [];
  for (const selector of citationSelectors) {
    citations = Array.from(card.querySelectorAll(selector));
    if (citations.length) break;
  }
  return {
    ok:Boolean(body),
    url:location.href,
    query,
    body,
    folded,
    citationCount:citations.length,
    readyState:document.readyState,
    cardId:String(card.id || ''),
    cardTemplate:String(card.getAttribute('tpl') || '')
  };
})()
"""


BAIDU_AI_SOURCES_JS = r"""
(async()=>{
  const card = document.querySelector(
    '#content_left [tpl="new_baikan_index"], #content_left [m-name*="new_baikan_index"], ' +
    '#content_left [tpl="wenda_generate"], #content_left .ai-entry'
  );
  if (!card) return {ok:false, error:'ai_card_missing', sources:[]};
  const citations = Array.from(card.querySelectorAll('.cosd-citation'));
  const found = new Map();
  let citationsWithSources = 0;
  const delay = (milliseconds) => new Promise(resolve => setTimeout(resolve, milliseconds));
  const visible = (element) => {
    const style = getComputedStyle(element);
    const rect = element.getBoundingClientRect();
    return style.display !== 'none' && style.visibility !== 'hidden' &&
      rect.width > 0 && rect.height > 0;
  };
  for (const citation of citations) {
    citation.scrollIntoView({block:'center', inline:'nearest'});
    try {
      citation.click();
    } catch (_) {
      citation.dispatchEvent(new MouseEvent('click', {bubbles:true, cancelable:true, view:window}));
    }
    await delay(450);
    const current = new Set();
    for (const popup of Array.from(document.querySelectorAll('.cos-tooltip-content')).filter(visible)) {
      for (const anchor of popup.querySelectorAll('a[href]')) {
        const href = String(anchor.href || anchor.getAttribute('href') || '').trim();
        const title = String(
          anchor.querySelector('.cosd-citation-title-text')?.innerText ||
          anchor.innerText || anchor.title || ''
        ).trim();
        if (!href || !title) continue;
        const item = anchor.closest('.cosd-citation-aggregated-item') || anchor.parentElement;
        const media = String(item?.querySelector('.cos-color-text-slim')?.innerText || '').trim();
        const key = href + '\u0000' + title;
        current.add(key);
        found.set(key, {url:href, title, media});
      }
    }
    if (current.size) citationsWithSources += 1;
  }
  return {
    ok:true,
    citationCount:citations.length,
    citationsWithSources,
    sources:Array.from(found.values())
  };
})()
"""


BAIDU_VISIBLE_SOURCES_JS = r"""
(()=>{
  const visible = (element) => {
    const style = getComputedStyle(element);
    const rect = element.getBoundingClientRect();
    return style.display !== 'none' && style.visibility !== 'hidden' &&
      rect.width > 0 && rect.height > 0;
  };
  const sources = [];
  for (const popup of Array.from(document.querySelectorAll('.cos-tooltip-content')).filter(visible)) {
    for (const anchor of popup.querySelectorAll('a[href]')) {
      const href = String(anchor.href || anchor.getAttribute('href') || '').trim();
      const title = String(
        anchor.querySelector('.cosd-citation-title-text')?.innerText ||
        anchor.innerText || anchor.title || ''
      ).trim();
      if (!href || !title) continue;
      const item = anchor.closest('.cosd-citation-aggregated-item') || anchor.parentElement;
      sources.push({
        url:href,
        title,
        media:String(item?.querySelector('.cos-color-text-slim')?.innerText || '').trim()
      });
    }
  }
  return {sources};
})()
"""


WENXIN_SEARCH_SNAPSHOT_JS = r"""
(()=>{
  const pageText = String(document.body?.innerText || '');
  const chunks = [];
  const seen = new Set();
  for (const element of document.querySelectorAll('.cosd-markdown-content')) {
    const text = String(element.innerText || '')
      .replace(/[\u200b-\u200f\u202a-\u202e\u2060\ufeff]/g, '')
      .trim();
    if (!text || seen.has(text)) continue;
    seen.add(text);
    chunks.push(text);
  }
  const sources = [];
  const sourceKeys = new Set();
  let parsedSourceItemCount = 0;
  const sourceItems = Array.from(document.querySelectorAll('li[data-long-press-ext-info]'));
  for (const item of sourceItems) {
    let metadata = {};
    try { metadata = JSON.parse(item.getAttribute('data-long-press-ext-info') || '{}'); }
    catch (_) { metadata = {}; }
    const anchor = item.querySelector('a[href]');
    const url = String(metadata.link || anchor?.href || anchor?.getAttribute('href') || '').trim();
    const title = String(
      metadata.linkTitle || item.querySelector('[class*="_text_"]')?.innerText ||
      anchor?.innerText || anchor?.title || item.innerText || ''
    ).trim();
    const key = url + '\u0000' + title;
    if (!url || !title) continue;
    parsedSourceItemCount += 1;
    if (sourceKeys.has(key)) continue;
    sourceKeys.add(key);
    sources.push({url, title, media:''});
  }
  const countMatch = pageText.match(/共参考\s*(\d+)\s*篇资料/);
  const busy = ['停止生成', '正在思考', '搜索中', '思考中']
    .some(marker => pageText.includes(marker));
  return {
    ok:chunks.join('\n\n').trim().length >= 40,
    url:location.href,
    pageText:pageText.slice(0, 1200),
    readyState:document.readyState,
    finished:Boolean(document.querySelector('[class*="answer-finished"]')) && !busy,
    body:chunks.join('\n\n').trim(),
    expectedSourceCount:countMatch ? Number(countMatch[1]) : 0,
    sourceItemCount:sourceItems.length,
    extractedSourceItemCount:parsedSourceItemCount,
    sources
  };
})()
"""


class WenxinWebCollector:
    """Collect Baidu search AI answers directly; no emulator or Wenxin App is used."""

    HOME = "https://www.baidu.com/"
    SEARCH = "https://www.baidu.com/s"
    WENXIN_SEARCH = "https://chat.baidu.com/search"

    def __init__(
        self,
        port: int = 9444,
        *,
        page: CDPPage | None = None,
        session: requests.Session | None = None,
        profile: Path | None = None,
    ):
        if page is None:
            del port  # Kept in the public signature for existing control-panel settings.
            page = ScraplingStealthPage(
                profile or (BASE_DIR / "scrapling_profile"), timeout=60
            )
        self.page = page
        self.session = session or requests.Session()

    def close(self) -> None:
        close = getattr(self.page, "close", None)
        if callable(close):
            close()
        self.session.close()

    @staticmethod
    def search_url(question: str) -> str:
        return f"{WenxinWebCollector.SEARCH}?{urlencode({'wd': str(question or '').strip()})}"

    @staticmethod
    def _compact(value: str) -> str:
        return re.sub(r"\s+", "", str(value or ""))

    @staticmethod
    def _is_security_verification(snapshot: dict[str, Any]) -> bool:
        url = str(snapshot.get("url") or "").casefold()
        text = str(snapshot.get("pageText") or "")
        return (
            "wappass.baidu.com" in url
            or "/static/captcha/" in url
            or "百度安全验证" in text
            or "请完成下方验证" in text
        )

    def ensure_ready(self, timeout: int = 30) -> dict[str, Any]:
        self.page.call("Page.navigate", {"url": self.HOME})
        deadline = time.monotonic() + max(5, timeout)
        last: dict[str, Any] = {}
        while time.monotonic() < deadline:
            last = self.page.evaluate(
                "({url:location.href,title:document.title,body:String(document.body?.innerText||'').slice(0,500)})"
            ) or {}
            if "baidu.com" in str(last.get("url") or "") and "百度" in (
                str(last.get("title") or "") + str(last.get("body") or "")
            ):
                return {"ok": True, **last}
            time.sleep(0.5)
        raise TimeoutError(f"百度搜索专用 Chrome 未就绪：{last}")

    def account_identity(self) -> dict[str, str]:
        self.ensure_ready()
        return {"name": "百度搜索 AI 直采", "masked": "无需登录"}

    def reset_after_round(self) -> dict[str, str]:
        """Open one clean Baidu tab and close the exact tab used by the completed round."""
        return self.page.replace_tab(self.HOME)

    def _navigate_fresh(self, url: str) -> str:
        """Navigate to a new document and return its CDP loader identity.

        A repeated Baidu card is still a valid new observation when it came
        from a new search document.  The loader id distinguishes that case from
        accidentally rereading the previous tab while its DOM is still visible.
        """
        value = self.page.call("Page.navigate", {"url": url}) or {}
        loader_id = str(value.get("loaderId") or "") if isinstance(value, dict) else ""
        if loader_id:
            return f"loader:{loader_id}"
        # Scrapling wraps Playwright and exposes a stable page/frame identity
        # instead of CDP's loaderId.  performance.timeOrigin changes for every
        # real document navigation, so their combination is equally suitable
        # for rejecting a stale DOM without rejecting a valid repeated card.
        frame_id = str(value.get("frameId") or "") if isinstance(value, dict) else ""
        time_origin = str(self.page.evaluate("String(performance.timeOrigin||0)") or "")
        valid_time_origin = time_origin not in {"", "0", "0.0", "nan", "NaN"}
        if getattr(self.page, "is_stealth", False) and (not frame_id or not valid_time_origin):
            raise RuntimeError("百度新页面导航凭证不完整，拒绝把旧页面当成新轮次")
        return f"frame:{frame_id}:time:{time_origin}"

    def _resolve_source_url(self, url: str) -> str:
        value = str(url or "").strip()
        parsed = urlparse(value)
        if parsed.netloc.casefold().removeprefix("www.") != "baidu.com" or parsed.path != "/link":
            return value
        try:
            response = self.session.get(
                value,
                allow_redirects=False,
                stream=True,
                timeout=10,
                headers={"User-Agent": "Mozilla/5.0"},
            )
            try:
                location = str(response.headers.get("Location") or "").strip()
            finally:
                response.close()
            resolved = urljoin(value, location)
            if urlparse(resolved).scheme in {"http", "https"}:
                return resolved
        except requests.RequestException:
            pass
        return value

    def _normalize_sources(self, raw_sources: list[Any]) -> list[dict[str, str]]:
        output: list[dict[str, str]] = []
        seen: set[str] = set()
        for raw in raw_sources:
            if not isinstance(raw, dict):
                continue
            original = str(raw.get("url") or "").strip()
            title = str(raw.get("title") or "").strip()
            if not original or not title:
                continue
            resolved = self._resolve_source_url(original)
            key = resolved.rstrip("/").casefold()
            if key in seen:
                continue
            seen.add(key)
            output.append(
                {
                    "url": resolved,
                    "baidu_redirect_url": original if resolved != original else "",
                    "title": title,
                    "media": str(raw.get("media") or "").strip(),
                }
            )
        return output

    def _expand_ai_card(self) -> bool:
        point = self.page.evaluate(r"""
(()=>{
  const card = document.querySelector(
    '#content_left [tpl="new_baikan_index"], #content_left [m-name*="new_baikan_index"], ' +
    '#content_left [tpl="wenda_generate"], #content_left .ai-entry'
  );
  if (!card) return null;
  const control = Array.from(card.querySelectorAll('button, [role="button"], div, span'))
    .find(element => /^展开剩余\d+%内容$/.test(String(element.innerText || '').trim()));
  if (!control) return null;
  control.scrollIntoView({block:'center', inline:'nearest'});
  const rect = control.getBoundingClientRect();
  return rect.width > 0 && rect.height > 0
    ? {x:rect.left + rect.width / 2, y:rect.top + rect.height / 2}
    : null;
})()
""")
        if isinstance(point, dict):
            self.page.click(int(point["x"]), int(point["y"]))
            time.sleep(0.6)
            return True
        return False

    def _scroll_ai_card(self) -> None:
        """Visibly walk the full AI card so lazy content and citations are loaded."""
        bounds = self.page.evaluate(r"""
(()=>{
  const card = document.querySelector(
    '#content_left [tpl="new_baikan_index"], #content_left [m-name*="new_baikan_index"], ' +
    '#content_left [tpl="wenda_generate"], #content_left .ai-entry'
  );
  if (!card) return null;
  const rect = card.getBoundingClientRect();
  return {top:Math.max(0, rect.top + scrollY - 100), bottom:rect.bottom + scrollY};
})()
""")
        if not isinstance(bounds, dict):
            return
        position = int(bounds.get("top") or 0)
        bottom = int(bounds.get("bottom") or position)
        step = 520
        while position < bottom:
            self.page.evaluate(f"window.scrollTo({{top:{position},behavior:'instant'}})")
            time.sleep(0.25)
            position += step
        self.page.evaluate(f"window.scrollTo({{top:{max(position, bottom)},behavior:'instant'}})")
        time.sleep(0.4)

    def _scroll_document(self) -> None:
        height = int(self.page.evaluate("Math.max(document.body.scrollHeight,document.documentElement.scrollHeight)") or 0)
        position = 0
        while position < height:
            self.page.evaluate(f"window.scrollTo({{top:{position},behavior:'instant'}})")
            time.sleep(0.2)
            position += 560
        self.page.evaluate(f"window.scrollTo({{top:{height},behavior:'instant'}})")
        time.sleep(0.4)

    def collect_wenxin_search(self, question: str, timeout: int = 90) -> dict[str, Any]:
        current_url = str(self.page.evaluate("location.href") or "") if getattr(self.page, "is_stealth", False) else self.HOME
        if "baidu.com" not in current_url:
            self.page.call("Page.navigate", {"url": self.HOME}, timeout=30)
            time.sleep(random.uniform(1.8, 3.2))
        url = f"{self.WENXIN_SEARCH}?{urlencode({'word': question, 'pd': 'csaitab', 'setype': 'csaitab'})}"
        navigation_id = self._navigate_fresh(url)
        deadline = time.monotonic() + max(30, timeout)
        stable = 0
        previous_body = ""
        last: dict[str, Any] = {}
        while time.monotonic() < deadline:
            value = self.page.evaluate(WENXIN_SEARCH_SNAPSHOT_JS, timeout=30)
            last = value if isinstance(value, dict) else {}
            if self._is_security_verification(last):
                raise RuntimeError("检测到百度安全验证，立即关闭当前页面")
            body = str(last.get("body") or "")
            sources = list(last.get("sources") or [])
            expected = int(last.get("expectedSourceCount") or 0)
            source_items = int(last.get("sourceItemCount") or expected)
            extracted_items = int(last.get("extractedSourceItemCount") or len(sources))
            body_complete = bool(last.get("ok") and last.get("finished") and len(self._compact(body)) >= 40)
            sources_complete = bool(source_items > 0 and extracted_items >= source_items)
            stable = stable + 1 if body_complete and body == previous_body else (1 if body_complete else 0)
            previous_body = body
            if stable >= 2 and sources_complete:
                break
            time.sleep(1)
        else:
            # A finished answer must not block the entire production queue merely
            # because Baidu reports one more reference card than it exposes as a
            # usable unique URL.  Preserve the completeness flag and let the
            # pipeline ingest the usable answer and links transparently.
            if stable < 2 or len(self._compact(previous_body)) < 40:
                raise TimeoutError(
                    f"文心入口回答未完整：{question}（正文 {len(previous_body)} 字，"
                    f"信源 {len(last.get('sources') or [])}/{int(last.get('expectedSourceCount') or 0)} 条）"
                )
        self._scroll_document()
        # Re-read after visible scrolling in case the page lazy-loaded more references.
        final = self.page.evaluate(WENXIN_SEARCH_SNAPSHOT_JS, timeout=30) or last
        body = str(final.get("body") or "")
        raw_sources = list(final.get("sources") or [])
        sources = self._normalize_sources(raw_sources)
        displayed_items = int(final.get("expectedSourceCount") or 0)
        source_items = int(final.get("sourceItemCount") or displayed_items)
        extracted_items = int(final.get("extractedSourceItemCount") or len(raw_sources))
        if len(self._compact(body)) < 40:
            raise RuntimeError(f"文心入口正文完整性复核失败：正文 {len(body)} 字")
        source_capture_complete = bool(
            sources and source_items > 0 and extracted_items >= source_items
        )
        # The page's article count includes duplicate cards.  Once every card
        # has been parsed, production statistics intentionally use unique URLs.
        expected_unique = len(sources) if source_capture_complete else displayed_items
        return {
            "url": str(final.get("url") or url),
            "body": body,
            "page_body": body,
            "body_capture_complete": True,
            "sources": sources,
            "source_count": len(sources),
            "expected_source_count": expected_unique,
            "citation_count": source_items,
            "source_capture_complete": source_capture_complete,
            "capture_warning": (
                "" if source_capture_complete else
                f"页面标示 {displayed_items} 篇资料，解析 {extracted_items}/{source_items} 个资料项，"
                f"取得 {len(sources)} 条有效唯一链接"
            ),
            "capture_mode": "baidu_wenxin_search",
            "page_navigation_id": navigation_id,
        }

    def _citation_point(self, index: int) -> dict[str, float] | None:
        adaptive_point = getattr(self.page, "adaptive_citation_point", None)
        if callable(adaptive_point):
            try:
                point = adaptive_point(index)
                if isinstance(point, dict):
                    return point
            except Exception:
                # Keep the live-DOM selector fallback for first-run pages before
                # Scrapling has learned a stable element fingerprint.
                pass
        value = self.page.evaluate(rf"""
(()=>{{
  const card = document.querySelector(
    '#content_left [tpl="new_baikan_index"], #content_left [m-name*="new_baikan_index"], ' +
    '#content_left [tpl="wenda_generate"], #content_left .ai-entry'
  );
  const citationSelectors = [
    '.cosd-citation',
    '[data-citation]',
    '[data-source-index]',
    'a[class*="citation"], button[class*="citation"], span[class*="citation"]',
    '[class*="reference"] a[href]'
  ];
  let citations = [];
  for (const selector of citationSelectors) {{
    citations = Array.from(card?.querySelectorAll(selector) || []);
    if (citations.length) break;
  }}
  // Keep the legacy lookup as a compatibility fallback for cached result DOMs.
  const legacyCitation = card?.querySelectorAll('.cosd-citation')[{int(index)}];
  const citation = citations[{int(index)}] || legacyCitation;
  if (!citation) return null;
  citation.scrollIntoView({{block:'center', inline:'nearest'}});
  const rect = citation.getBoundingClientRect();
  return rect.width > 0 && rect.height > 0
    ? {{x:rect.left + rect.width / 2, y:rect.top + rect.height / 2}}
    : null;
}})()
""")
        return value if isinstance(value, dict) else None

    def _visible_sources(self) -> list[dict[str, str]]:
        value = self.page.evaluate(BAIDU_VISIBLE_SOURCES_JS, timeout=20)
        return list(value.get("sources") or []) if isinstance(value, dict) else []

    def _collect_citation_sources(self, citation_count: int) -> tuple[list[dict[str, str]], int]:
        raw_sources: list[dict[str, str]] = []
        citations_with_sources = 0
        for index in range(citation_count):
            current: list[dict[str, str]] = []
            for attempt in range(3):
                # Close the previous tooltip first.  Without this, a slow Baidu
                # render can leave the preceding citation visible and make the
                # next marker look empty (or falsely reuse the previous links).
                self.page.call(
                    "Input.dispatchMouseEvent",
                    {"type": "mouseMoved", "x": 8 + attempt, "y": 8 + attempt},
                )
                time.sleep(0.3 + attempt * 0.2)
                point = self._citation_point(index)
                if point is None:
                    continue
                coordinates = {"x": int(point["x"]), "y": int(point["y"])}
                self.page.call("Input.dispatchMouseEvent", {"type": "mouseMoved", **coordinates})
                time.sleep(0.8 + attempt * 0.35)
                current = self._visible_sources()
                if not current:
                    self.page.click(coordinates["x"], coordinates["y"])
                    time.sleep(0.8 + attempt * 0.35)
                    current = self._visible_sources()
                if current:
                    break
            if current:
                citations_with_sources += 1
                raw_sources.extend(current)
        return raw_sources, citations_with_sources

    def collect_search(self, question: str, timeout: int = 90) -> dict[str, Any]:
        question = str(question or "").strip()
        if not question:
            raise ValueError("百度搜索问题不能为空")
        current_url = str(self.page.evaluate("location.href") or "") if getattr(self.page, "is_stealth", False) else self.HOME
        if "baidu.com" not in current_url:
            self.page.call("Page.navigate", {"url": self.HOME}, timeout=30)
            time.sleep(random.uniform(1.8, 3.2))
        url = self.search_url(question)
        navigation_id = self._navigate_fresh(url)
        deadline = time.monotonic() + max(10, timeout)
        stable = 0
        complete_without_ai = 0
        previous_body = ""
        last: dict[str, Any] = {}
        while time.monotonic() < deadline:
            value = self.page.evaluate(BAIDU_AI_SNAPSHOT_JS, timeout=30)
            last = value if isinstance(value, dict) else {}
            if self._is_security_verification(last):
                raise RuntimeError("检测到百度安全验证，立即关闭当前页面")
            body = str(last.get("body") or "")
            query_matches = self._compact(last.get("query") or "") == self._compact(question)
            ready = last.get("readyState") == "complete"
            if last.get("ok") and query_matches and ready and len(self._compact(body)) >= 40:
                complete_without_ai = 0
                stable = stable + 1 if body == previous_body else 1
                previous_body = body
                if stable >= 2:
                    break
            else:
                stable = 0
                previous_body = body
                complete_without_ai = complete_without_ai + 1 if ready and query_matches else 0
                if complete_without_ai >= 12:
                    return self.collect_wenxin_search(question, timeout=max(45, timeout))
            time.sleep(1)
        else:
            raise TimeoutError(f"等待百度 AI 回答超时：{question}")

        self._expand_ai_card()
        expanded_stable = 0
        expanded_body = ""
        expanded_deadline = min(deadline, time.monotonic() + 15)
        while time.monotonic() < expanded_deadline:
            value = self.page.evaluate(BAIDU_AI_SNAPSHOT_JS, timeout=30)
            expanded = value if isinstance(value, dict) else {}
            body = str(expanded.get("body") or "")
            if expanded.get("ok") and not expanded.get("folded") and len(body) >= len(previous_body):
                expanded_stable = expanded_stable + 1 if body == expanded_body else 1
                expanded_body = body
                last = expanded
                if expanded_stable >= 2:
                    previous_body = expanded_body
                    break
            else:
                expanded_stable = 0
                expanded_body = body
            time.sleep(0.5)
        else:
            raise RuntimeError("百度 AI 回答未能完全展开并稳定，拒绝保存可能截断的正文")

        self._scroll_ai_card()
        adaptive_snapshot = getattr(self.page, "adaptive_ai_snapshot", None)
        if callable(adaptive_snapshot):
            try:
                adaptive = adaptive_snapshot()
                adaptive_body = str(adaptive.get("body") or "")
                # Prefer the larger stable body; this also protects against a
                # renamed answer class truncating the JavaScript extraction.
                if len(self._compact(adaptive_body)) > len(self._compact(previous_body)):
                    previous_body = adaptive_body
                last["citationCount"] = max(
                    int(last.get("citationCount") or 0),
                    int(adaptive.get("citationCount") or 0),
                )
            except Exception:
                pass
        citation_count = int(last.get("citationCount") or 0)
        # A Baidu AI answer always has provenance.  A zero count means the result
        # page changed its citation markup (or lazy rendering has not completed),
        # never that a source-less capture is production-complete.  Use the
        # dedicated Wenxin result page as the authoritative fallback instead of
        # incorrectly persisting a successful 0/0 round.
        if citation_count <= 0:
            return self.collect_wenxin_search(question, timeout=max(45, timeout))
        raw_sources, captured_citations = self._collect_citation_sources(citation_count)
        sources = self._normalize_sources(raw_sources)
        if citation_count and captured_citations < citation_count:
            # The card exists but one or more citation popups are not usable.
            # This is the same fallback condition as a missing card: obtain the
            # authoritative answer/reference list from the Wenxin result page.
            return self.collect_wenxin_search(question, timeout=max(45, timeout))
        if not sources:
            return self.collect_wenxin_search(question, timeout=max(45, timeout))
        return {
            "url": url,
            "body": previous_body,
            "page_body": previous_body,
            "body_capture_complete": True,
            "sources": sources,
            "source_count": len(sources),
            "expected_source_count": len(sources),
            "citation_count": citation_count,
            "source_capture_complete": True,
            "capture_mode": "baidu_search_ai",
            "page_navigation_id": navigation_id,
        }
