"""在 Chrome 网页版元宝中收集最新一轮回复的正文与引用来源。

基于真实 DOM 结构（2026-07-30 快照）：
- 侧边栏: .yb-nav
- 会话列表: .yb-recent-conv-list
- 聊天内容: #chat-content
- 新建对话: [data-desc="new-chat"]
- 侧边栏折叠/展开: [data-desc="fold"] / [data-desc="unfold"]
"""

import json
import time
import re
from datetime import datetime
from typing import List, Dict, Optional

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.remote.webelement import WebElement
from selenium.common.exceptions import NoSuchElementException, TimeoutException
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

try:
    from .bowser import connect_or_launch_chrome
except ImportError:
    from bowser import connect_or_launch_chrome


class YuanbaoSourceCollector:
    YUANBAO_CHAT_URL = "https://yuanbao.tencent.com/chat/"

    def __init__(
        self,
        driver: Optional[webdriver.Chrome] = None,
        debug: bool = True,
        debug_port: int = 9222,
        user_data_dir: Optional[str] = None,
    ):
        if driver is None:
            print(f"[Chrome] 连接或启动 Chrome（端口 {debug_port}）...")
            self.driver = connect_or_launch_chrome(
                debug_port=debug_port,
                user_data_dir=user_data_dir,
            )
        else:
            self.driver = driver
        self.debug = debug

    def _wait(self, timeout: int = 15):
        return WebDriverWait(self.driver, timeout)

    def _safe_find(self, by: By, value: str, parent=None) -> Optional[WebElement]:
        root = parent if parent is not None else self.driver
        try:
            return root.find_element(by, value)
        except NoSuchElementException:
            return None

    def _safe_finds(self, by: By, value: str, parent=None) -> List[WebElement]:
        root = parent if parent is not None else self.driver
        try:
            return root.find_elements(by, value)
        except Exception:
            return []

    def _dump_snapshot(self, name: str):
        if not self.debug:
            return
        path = f"snapshot_{name}.html"
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(self.driver.page_source)
            print(f"[debug] 已保存页面快照: {path}")
        except Exception as e:
            print(f"[debug] 保存快照失败: {e}")

    def _is_generating(self) -> bool:
        try:
            page_text = self.driver.find_element(By.TAG_NAME, "body").text
        except Exception:
            return False
        indicators = ["正在搜索资料", "正在生成", "思考中", "搜索中"]
        return any(ind in page_text for ind in indicators)

    # ---------- 页面/会话同步 ----------
    def ensure_chat_page(self):
        current = str(self.driver.current_url or "").split("?", 1)[0]
        if current.rstrip("/") != self.YUANBAO_CHAT_URL.rstrip("/"):
            print(f"正在跳转到 {self.YUANBAO_CHAT_URL}")
            self.driver.get(self.YUANBAO_CHAT_URL)
        else:
            print("刷新页面以同步最新对话...")
            self.driver.refresh()
        deadline = time.time() + 20
        while time.time() < deadline:
            if self._safe_finds(By.CSS_SELECTOR, ".yb-recent-conv-list__item"):
                return
            time.sleep(0.5)

    def click_latest_conversation(self, timeout: int = 20) -> bool:
        """点击左侧聊天列表最上面的最新对话。"""
        print("点击左侧最新对话...")

        # 等会话列表加载完成（去掉 loading 状态）
        start = time.time()
        while time.time() - start < timeout:
            conv_list = self._safe_find(By.CSS_SELECTOR, ".yb-recent-conv-list")
            if conv_list is not None:
                cls = conv_list.get_attribute("class") or ""
                if "loading" not in cls:
                    break
            time.sleep(1)

        # 找第一个会话项
        selectors = [
            ".yb-recent-conv-list__item",
            ".yb-recent-conv-list [class*='item']",
        ]
        for sel in selectors:
            el = self._safe_find(By.CSS_SELECTOR, sel)
            if el is not None and el.is_displayed():
                try:
                    self.driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", el)
                    time.sleep(0.3)
                    el.click()
                    print("已点击左侧最新对话")
                    time.sleep(2)
                    return True
                except Exception:
                    continue

        self._dump_snapshot("click_latest_conv_failed")
        print("未能点击最新对话")
        return False

    @staticmethod
    def _conversation_reference(element: WebElement) -> str:
        return str(
            element.get_attribute("dt-cid")
            or element.get_attribute("data-item-id")
            or element.get_attribute("id")
            or element.text
            or ""
        ).strip()

    def latest_conversation_reference(self, refresh: bool = False, timeout: int = 20) -> str:
        if refresh:
            self.ensure_chat_page()
        deadline = time.time() + timeout
        while time.time() < deadline:
            items = self._safe_finds(By.CSS_SELECTOR, ".yb-recent-conv-list__item")
            if items:
                return self._conversation_reference(items[0])
            time.sleep(1)
        return ""

    def click_new_conversation(self, previous_reference: str, question: str = "", timeout: int = 90) -> str:
        """Open the newly synced conversation whose main panel has this question."""
        deadline = time.time() + timeout
        last_refresh = 0.0
        attempted: set[str] = set()
        # Yuanbao rewrites conversation titles, but normally keeps the product
        # phrase. Prefer those rows before probing unrelated conversations.
        topic = re.sub(
            r"(?:请|帮我|给我|推荐|介绍|选择|一款|一个|一些|适合的|好用的)",
            "",
            str(question or ""),
        ).strip(" ，。！？?")
        while time.time() < deadline:
            items = self._safe_finds(By.CSS_SELECTOR, ".yb-recent-conv-list__item")
            candidates = items[:60]
            if topic:
                candidates = sorted(
                    candidates,
                    key=lambda item: self._conversation_topic_priority(item, topic),
                )
            for item in candidates:
                try:
                    reference = self._conversation_reference(item)
                    if not reference or reference == previous_reference or reference in attempted:
                        continue
                    target = self._safe_find(
                        By.CSS_SELECTOR,
                        ".yb-recent-conv-list__item-name",
                        item,
                    )
                    if target is None:
                        target = item
                    self.driver.execute_script("arguments[0].click();", target)
                    # React changes the URL before the chat body is hydrated.
                    # Wait for the selected CID and question instead of doing
                    # one fixed two-second read that can miss a valid sync.
                    load_deadline = min(deadline, time.time() + 12)
                    while time.time() < load_deadline:
                        chat = self._safe_find(By.CSS_SELECTOR, "#chat-content")
                        chat_text = str(chat.text if chat is not None else "")
                        url_ready = reference in str(self.driver.current_url or "")
                        if url_ready and (not question or question in chat_text):
                            return reference
                        time.sleep(0.5)
                    attempted.add(reference)
                except Exception:
                    continue
            if time.time() - last_refresh >= 4:
                self.driver.refresh()
                last_refresh = time.time()
                attempted.clear()
            time.sleep(1)
        return ""

    @staticmethod
    def _conversation_topic_priority(item: WebElement, topic: str) -> int:
        try:
            return 0 if topic in str(item.text or "") else 1
        except Exception:
            return 1

    def wait_for_chat_loaded(self, timeout: int = 60) -> bool:
        print("等待右侧聊天内容加载...")
        start = time.time()
        while time.time() - start < timeout:
            if self._find_last_message() is not None:
                return True
            if "/chat/" in self.driver.current_url and len(self.driver.current_url) > len(self.YUANBAO_CHAT_URL) + 5:
                time.sleep(1.5)
                if self._find_last_message() is not None:
                    return True
            time.sleep(1)
        self._dump_snapshot("chat_load_timeout")
        print("右侧聊天内容加载超时")
        return False

    # ---------- 定位消息 ----------
    def _find_last_message(self) -> Optional[WebElement]:
        """定位最后一条 AI 回复。"""
        # 真实 DOM: AI 回复在 #chat-content 里，类名含 agent-chat__bubble--ai
        msgs = self._safe_finds(By.CSS_SELECTOR, ".agent-chat__bubble--ai")
        if msgs:
            return msgs[-1]
        # 兜底
        for sel in [
            "[class*='assistant']",
            "[class*='answer']",
            "[class*='model']",
            "[class*='bot-message']",
            "[class*='message'][class*='left']",
            "[class*='message']",
        ]:
            msgs = self._safe_finds(By.CSS_SELECTOR, sel)
            if msgs:
                return msgs[-1]
        return None

    def extract_body(self, msg: WebElement) -> str:
        """从消息元素中提取正文文本。"""
        # 真实 DOM: 正文在 .hyc-common-markdown 里
        for sel in [
            ".hyc-common-markdown",
            "[class*='markdown']",
            "[class*='rich-text']",
            "[class*='message-content']",
            "[class*='content']",
            "[class*='answer-body']",
        ]:
            content = self._safe_find(By.CSS_SELECTOR, sel, msg)
            if content is not None:
                text = content.text.strip()
                if len(text) > 10:
                    return text

        # 兜底
        text = msg.text.strip()
        text = re.sub(r"(复制|点赞|踩|重发|引用.*资料作为参考|源).*", "", text, flags=re.S)
        return text

    # ---------- 信源按钮与抽屉 ----------
    def _find_source_button(self, msg: WebElement) -> Optional[WebElement]:
        """找底部信源按钮。
        真实 DOM: 按钮在消息下方的工具栏里，id=search-guide-tool，
        类名含 ToolbarSearchGuid_searchGuidTool，aria-label="引用xx篇资料作为参考"
        """
        # 优先：在整个页面找（不在 msg 内部，而是在 msg 下方的 toolbar 里）
        btn = self._safe_find(By.CSS_SELECTOR, "#search-guide-tool")
        if btn is not None and btn.is_displayed():
            return btn

        # 兜底 1: 按 aria-label 找
        btn = self._safe_find(By.XPATH, "//*[@aria-label and contains(@aria-label, '引用') and contains(@aria-label, '资料')]")
        if btn is not None and btn.is_displayed():
            return btn

        # 兜底 2: 按 data-toolbar-type=citation 找
        btn = self._safe_find(By.CSS_SELECTOR, "[data-toolbar-type='citation']")
        if btn is not None and btn.is_displayed():
            return btn

        # 兜底 3: 在 msg 内部找
        xpaths = [
            ".//*[contains(text(), '源') and string-length(text()) <= 3]",
            ".//*[contains(text(), '引用') and contains(text(), '资料')]",
        ]
        for xp in xpaths:
            btn = self._safe_find(By.XPATH, xp, msg)
            if btn is not None and btn.is_displayed():
                return btn
        return None

    def _wait_for_drawer(self, timeout: int = 15) -> Optional[WebElement]:
        print("等待引用来源弹出...")
        start = time.time()
        while time.time() - start < timeout:
            selectors = [
                (By.CSS_SELECTOR, ".hyc-common-markdown__ref-list__popup"),
                (By.CSS_SELECTOR, ".hyc-common-markdown__ref-list__content"),
                (By.CSS_SELECTOR, "[class*='drawer']"),
                (By.CSS_SELECTOR, "[class*='reference-panel']"),
                (By.CSS_SELECTOR, "[class*='source-panel']"),
                (By.CSS_SELECTOR, "[class*='citation']"),
                (By.XPATH, "//div[contains(text(), '引用来源')]"),
                (By.XPATH, "//div[contains(text(), '参考资料')]"),
            ]
            for by, val in selectors:
                el = self._safe_find(by, val)
                if el is not None and el.is_displayed() and el.size.get("width", 0) > 50:
                    print("引用来源弹出已出现")
                    return el
            time.sleep(0.5)
        self._dump_snapshot("drawer_not_found")
        print("引用来源弹出未出现")
        return None

    def _extract_sources_from_network(self) -> List[Dict[str, str]]:
        """通过网络请求拦截获取信源数据。"""
        sources = []
        try:
            # 尝试从页面 JS 变量或 API 响应中提取
            # 方法1: 检查是否有全局变量存储信源
            script = """
            (function() {
                var results = [];
                // 查找所有 ref_card 的 data-url
                document.querySelectorAll('.hyc-common-markdown__ref_card').forEach(function(card) {
                    var url = card.getAttribute('data-url');
                    var titleEl = card.querySelector('.hyc-common-markdown__ref_card-title span');
                    var title = titleEl ? titleEl.textContent.trim() : '';
                    if (url) results.push({url: url, title: title});
                });
                return JSON.stringify(results);
            })()
            """
            result = self.driver.execute_script(script)
            if result:
                sources = json.loads(result)
        except Exception as e:
            print(f"JS 提取失败: {e}")

        # 方法2: 如果上面没拿到，尝试从 inline ref 元素提取
        if not sources:
            try:
                ref_items = self._safe_finds(By.CSS_SELECTOR, ".hyc-common-markdown__ref-list__item")
                for item in ref_items:
                    url = item.get_attribute("data-url") or ""
                    title = item.text.strip() or ""
                    if url:
                        sources.append({"title": title, "url": url})
            except Exception:
                pass

        seen = set()
        unique = []
        for s in sources:
            if s["url"] not in seen:
                seen.add(s["url"])
                unique.append(s)
        return unique

    def _extract_sources(self, drawer: WebElement) -> List[Dict[str, str]]:
        sources = []

        # 方式1: ref_card 结构（data-url + title）
        cards = self._safe_finds(By.CSS_SELECTOR, ".hyc-common-markdown__ref_card", drawer)
        for card in cards:
            url = card.get_attribute("data-url") or ""
            title_el = self._safe_find(By.CSS_SELECTOR, ".hyc-common-markdown__ref_card-title", card)
            title = title_el.text.strip() if title_el is not None else ""
            if url:
                sources.append({"title": title, "url": url})

        # 方式2: 普通 <a> 标签
        if not sources:
            anchors = self._safe_finds(By.TAG_NAME, "a", drawer)
            for a in anchors:
                href = a.get_attribute("href") or ""
                title = a.text.strip() or a.get_attribute("title") or ""
                if href and not href.startswith("javascript:"):
                    sources.append({"title": title, "url": href})

        # 方式3: 从整个页面提取（兜底）
        if not sources:
            sources = self._extract_sources_from_network()

        seen = set()
        unique = []
        for s in sources:
            if s["url"] not in seen:
                seen.add(s["url"])
                unique.append(s)
        return unique

    def _collect_all_sources(self, msg: WebElement) -> List[Dict[str, str]]:
        """按需打开引用抽屉，等待来源加载完成，提取后关闭抽屉。"""
        drawer_selector = ".t-drawer.agent-dialogue__drawer.t-drawer--open"
        drawer = self._safe_find(By.CSS_SELECTOR, drawer_selector)

        # 重试时抽屉可能仍然开着；这时再次点击会把它关闭并导致一直 0/N。
        if drawer is None:
            btn = self._safe_find(By.CSS_SELECTOR, "#search-guide-tool")
            if btn is None:
                print("未找到引用来源按钮")
                return []
            self.driver.execute_script("arguments[0].scrollIntoView({block:'center'});", msg)
            self.driver.execute_script("arguments[0].scrollIntoView({block:'center'});", btn)
            self.driver.execute_script(
                "arguments[0].dispatchEvent(new MouseEvent('click',"
                "{bubbles:true,cancelable:true,view:window}));",
                btn,
            )
            for _ in range(20):
                drawer = self._safe_find(By.CSS_SELECTOR, drawer_selector)
                if drawer is not None:
                    break
                time.sleep(0.5)

        if drawer is None:
            print("引用来源抽屉未打开")
            self._dump_snapshot("drawer_not_opened")
            return []

        expected = self._expected_source_count()
        best = []
        previous_count = -1
        stable_rounds = 0
        deadline = time.time() + 10
        try:
            while time.time() < deadline:
                js_result = self.driver.execute_script("""
            function extractTitle(card) {
                var titleEl = card.querySelector('.hyc-common-markdown__ref_card-title');
                if (titleEl && titleEl.textContent.trim()) {
                    return titleEl.textContent.trim();
                }
                var aEl = card.querySelector('a[href]');
                if (aEl) {
                    var aText = (aEl.textContent || aEl.getAttribute('title') || '').trim();
                    if (aText) return aText.substring(0, 120);
                }
                var selfText = (card.textContent || '').trim().replace(/\\s+/g, ' ');
                return selfText.substring(0, 120);
            }
            var results = [];
            var cards = document.querySelectorAll(
                '.t-drawer.agent-dialogue__drawer.t-drawer--open .hyc-common-markdown__ref_card[data-url]'
            );
            for (var i = 0; i < cards.length; i++) {
                var url = cards[i].getAttribute('data-url') || '';
                if (url) results.push({url: url, title: extractTitle(cards[i])});
            }
            // 部分元宝版本使用另一套来源列表 DOM。
            if (results.length === 0) {
                var items = document.querySelectorAll(
                    '.t-drawer.agent-dialogue__drawer.t-drawer--open .agent-dialogue-references__item[dt-ext6]'
                );
                for (var i = 0; i < items.length; i++) {
                    var url = items[i].getAttribute('dt-ext6') || '';
                    var title = (items[i].textContent || '').trim().split('\\n')[0].substring(0, 120);
                    if (url) results.push({url: url, title: title});
                }
            }
            return results;
                """) or []

                seen_urls = set()
                current = []
                for item in js_result:
                    url = item.get("url", "")
                    if url and url not in seen_urls:
                        seen_urls.add(url)
                        current.append({"title": item.get("title", ""), "url": url})
                if len(current) > len(best):
                    best = current
                if expected and len(best) >= expected:
                    break
                if len(current) == previous_count and len(current) > 0:
                    stable_rounds += 1
                else:
                    stable_rounds = 0
                if stable_rounds >= 3:
                    break
                previous_count = len(current)
                time.sleep(0.5)
        finally:
            self._close_drawer()

        print(f"共收集 {len(best)} 条引用来源（预期 {expected or '未知'}）")
        return best

    def _expected_source_count(self) -> int:
        # Only the citation toolbar owns the authoritative count. Never scan
        # answer/drawer text: product copy such as "参考价 69 元" is not a
        # source count and previously caused an endless 16/69 retry loop.
        texts = self.driver.execute_script("""
            return Array.from(document.querySelectorAll(
                '#search-guide-tool, [data-toolbar-type="citation"]'
            )).map(function(el) {
                return [el.getAttribute('aria-label') || '', el.getAttribute('title') || ''].join(' ');
            }).join('\\n');
        """) or ""
        matches = re.findall(
            r"(?:引用\s*)?(\d{1,3})\s*篇(?:资料)?(?:作为参考)?",
            str(texts),
        )
        counts = [int(value) for value in matches if 0 < int(value) <= 100]
        return max(counts, default=0)

    def _close_drawer(self):
        close_xpaths = [
            "//div[contains(@class,'drawer')]//button[contains(@class,'close')]",
            "//body",
        ]
        for xp in close_xpaths:
            try:
                el = self.driver.find_element(By.XPATH, xp)
                if xp == "//body":
                    from selenium.webdriver.common.keys import Keys
                    el.send_keys(Keys.ESCAPE)
                else:
                    el.click()
                time.sleep(0.3)
                return
            except Exception:
                continue

    # ---------- 主流程 ----------
    def collect(
        self,
        question: str,
        output_path: str = "result.json",
        wait_reply_timeout: int = 120,
        extra: Optional[Dict] = None,
        previous_conversation: str = "",
    ) -> Dict:
        collect_start = datetime.now().astimezone().isoformat()
        result = {
            "question": question,
            "body": "",
            "sources": [],
            "emulator_started_at": (extra or {}).get("emulator_started_at"),
            "emulator_reply_saved_at": (extra or {}).get("emulator_reply_saved_at"),
            "chrome_collect_started_at": collect_start,
            "chrome_collect_finished_at": None,
            "body_length": 0,
            "source_count": 0,
            "error": None,
        }
        result.update(extra or {})

        self.ensure_chat_page()
        conversation_reference = self.click_new_conversation(previous_conversation, question, timeout=wait_reply_timeout)
        if not conversation_reference:
            result["error"] = "new_conversation_sync_timeout"
            self._save(result, output_path)
            return result
        result["conversation_reference"] = conversation_reference

        if not self.wait_for_chat_loaded(timeout=60):
            print("未能在 Chrome 中加载聊天内容，保存空结果")
            result["error"] = "chat_load_timeout"
            result["chrome_collect_finished_at"] = datetime.now().astimezone().isoformat()
            self._save(result, output_path)
            return result

        # 模拟器已等回复完整，直接找最后一条 AI 回复
        last_msg = self._find_last_message()
        if last_msg is None:
            print("未找到模型回复")
            self._dump_snapshot("no_reply")
            self._save(result, output_path)
            return result

        result["body"] = self.extract_body(last_msg)
        try:
            chat_text = self.driver.find_element(By.CSS_SELECTOR, "#chat-content").text
        except Exception:
            chat_text = result["body"]
        if question and question not in chat_text:
            result["error"] = "conversation_question_mismatch"
            self._save(result, output_path)
            return result
        print(f"正文长度: {len(result['body'])} 字符")

        print("收集信源...")
        sources = self._collect_all_sources(last_msg)
        result["sources"] = sources
        expected_source_count = self._expected_source_count()
        result["expected_source_count"] = expected_source_count
        result["source_capture_complete"] = len(sources) >= expected_source_count
        if len(sources) < expected_source_count:
            result["error"] = f"incomplete_sources:{len(sources)}/{expected_source_count}"
        print(f"收集到 {len(result['sources'])} 条信源")

        self._save(result, output_path)
        return result

    def _save(self, data: Dict, path: str):
        data["body_length"] = len(data.get("body", ""))
        data["source_count"] = len(data.get("sources", []))
        data["chrome_collect_finished_at"] = datetime.now().astimezone().isoformat()
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        print(f"已保存结果: {path}")


if __name__ == "__main__":
    collector = YuanbaoSourceCollector()
    collector.collect(
        question="推荐一款染发剂",
        output_path="result_test.json"
    )
