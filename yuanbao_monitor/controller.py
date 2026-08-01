import re
import time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError

import uiautomator2 as u2


class YuanbaoController:
    """封装MuMu模拟器里对腾讯元宝App的自动化操作"""

    PKG = "com.tencent.hunyuan.app.chat"
    INPUT_ID = f"{PKG}:id/edConversationInput"
    SEND_DESC = "发送消息"
    NEW_CHAT_DESC = "新建对话"
    CHAT_LIST_ID = f"{PKG}:id/chat_recycler_view"

    def __init__(self, serial: str = "127.0.0.1:16384", connect_timeout: int = 15):
        print(f"正在连接模拟器 {serial}...")
        try:
            with ThreadPoolExecutor(max_workers=1) as ex:
                future = ex.submit(u2.connect, serial)
                self.d = future.result(timeout=connect_timeout)
        except FutureTimeoutError:
            raise RuntimeError(
                f"连接模拟器 {serial} 超时（{connect_timeout}秒）。\n"
                "请确认 MuMu 模拟器已启动，且 adb 能连上该地址。\n"
                "可先用命令测试：adb connect 127.0.0.1:16384"
            )
        print("模拟器连接成功，设置输入法...")
        try:
            with ThreadPoolExecutor(max_workers=1) as ex:
                future = ex.submit(self.d.set_fastinput_ime, True)
                future.result(timeout=5)
        except Exception as e:
            print(f"设置 fastinput 输入法失败（不影响运行）: {e}")
        print("模拟器就绪")

    def _dismiss_guide(self):
        """只关闭明确可识别的引导层，绝不盲点聊天正文。"""
        for text in ("知道了", "我知道了", "跳过", "暂不开启"):
            target = self.d(text=text)
            if target.exists:
                target.click()
                time.sleep(0.3)
                return

    def _scroll_to_bottom(self):
        """先把聊天列表滚到底部，避免后续打开侧边栏/新建对话被拦截。"""
        print("[模拟器] 滚动到底部...")
        try:
            recycler = self.d(resourceId=self.CHAT_LIST_ID)
            if recycler.exists:
                recycler.scroll.toEnd()
                time.sleep(0.5)
        except Exception:
            pass
        for _ in range(3):
            self.d.swipe(270, 700, 270, 300, duration=0.3)
            time.sleep(0.3)
        print("[模拟器] 已滚动到底部")

    def _open_sidebar(self):
        """打开左侧侧边栏，支持多种方式兜底。"""
        print("[模拟器] 打开侧边栏...")
        for selector in [
            {"resourceId": "com.tencent.hunyuan.app.chat:id/ic_navigation_show"},
            {"description": "抽屉页入口"},
        ]:
            try:
                el = self.d(**selector)
                if el.exists:
                    el.click()
                    print("[模拟器] 已点击抽屉入口")
                    time.sleep(0.8)
                    return
            except Exception:
                continue
        print("[模拟器] 尝试滑开侧边栏...")
        self.d.swipe(10, 480, 350, 480, duration=0.4)
        time.sleep(0.8)

    def _open_new_conversation(self):
        """唤出侧边栏并新建对话"""
        self._scroll_to_bottom()
        # 优先点右上角"新建对话"按钮
        direct = self.d(description=self.NEW_CHAT_DESC)
        if direct.exists(timeout=2):
            direct.click()
            time.sleep(1.2)
            if self.d(resourceId=self.INPUT_ID).exists(timeout=3):
                print("[模拟器] 已通过顶部按钮新建对话")
                return
        # 兜底：滑开侧边栏，点"新建对话"
        self._open_sidebar()
        for attempt in range(2):
            try:
                self.d(text="新建对话").click()
                print("[模拟器] 已通过侧边栏新建对话")
                time.sleep(1.5)
                return
            except Exception as e:
                print(f"[模拟器] 点击新建对话失败（尝试 {attempt+1}/2）: {e}")
                self._open_sidebar()
        raise RuntimeError("无法点击'新建对话'，请检查模拟器界面")

    def _type_and_send(self, question: str):
        """点击输入框、输入内容、点击发送"""
        input_box = self.d(resourceId=self.INPUT_ID)
        for attempt in range(3):
            try:
                input_box.click()
                time.sleep(0.3)
                input_box.set_text(question)
                break
            except Exception as e:
                print(f"[模拟器] 输入失败（尝试 {attempt+1}/3）: {e}")
                time.sleep(0.5)
        else:
            self.d.send_keys(question)
        time.sleep(0.3)
        send = self.d(description=self.SEND_DESC)
        if send.exists(timeout=3):
            send.click()
            return
        for suffix in ("ivConversationSend", "btn_send", "send_button"):
            candidate = self.d(resourceId=f"{self.PKG}:id/{suffix}")
            if candidate.exists:
                candidate.click()
                return
        raise RuntimeError("已输入问题，但未找到发送按钮")

    def ensure_ready(self):
        """确保元宝在前台且输入框可用；异常恢复时可重复调用。"""
        try:
            current = self.d.app_current()
        except Exception:
            current = {}
        if current.get("package") != self.PKG:
            self.d.app_start(self.PKG, stop=False)
            time.sleep(2)
        if self.d(resourceId=self.INPUT_ID).exists(timeout=4):
            return
        if self.d(textStartsWith="引用来源").exists:
            self.d.click(55, 86)
            time.sleep(1)
        for _ in range(2):
            if self.d(resourceId=self.INPUT_ID).exists(timeout=1):
                return
            self.d.press("back")
            time.sleep(0.7)
        if not self.d(resourceId=self.INPUT_ID).exists(timeout=3):
            self.d.app_start(self.PKG, stop=True)
            time.sleep(3)
        if not self.d(resourceId=self.INPUT_ID).exists(timeout=5):
            raise RuntimeError("元宝已启动，但聊天输入框仍不可用")

    @staticmethod
    def extract_visible_reply(xml: str, question: str = "") -> str:
        """从层级 XML 中提取当前可见的回答文本，供结果与诊断使用。"""
        try:
            import xml.etree.ElementTree as ET
            root = ET.fromstring(xml)
        except Exception:
            return ""
        ignored = {
            question, "问元宝", "发现", "我们", "快速思考", "元宝",
            "发消息或按住说话...", "源",
        }
        parts = []
        for node in root.iter("node"):
            text = (node.attrib.get("text") or "").strip()
            if not text or text in ignored or text.startswith("猜你想问"):
                continue
            if re.fullmatch(r"\d{1,2}:\d{2}", text):
                continue
            if any(marker in text for marker in ("正在搜索", "正在生成", "正在思考", "生成中")):
                continue
            if node.attrib.get("class") not in {
                "android.widget.TextView", "android.view.View"
            }:
                continue
            if len(text) >= 2 and text not in parts:
                parts.append(text)
        return "\n".join(p for p in parts if len(p) > 4)

    def save_diagnostics(self, directory: str | Path, prefix: str, error: str = ""):
        """保存 XML、截图和错误说明，失败时不掩盖原始异常。"""
        target = Path(directory)
        target.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^0-9A-Za-z_.-]+", "_", prefix).strip("_") or "failure"
        try:
            (target / f"{safe}.xml").write_text(self.d.dump_hierarchy(), encoding="utf-8")
        except Exception:
            pass
        try:
            self.d.screenshot(str(target / f"{safe}.png"))
        except Exception:
            pass
        if error:
            (target / f"{safe}.txt").write_text(error, encoding="utf-8")

    def _wait_for_reply(self, question: str = "", max_wait: int = 120, poll_interval: float = 1.5) -> str:
        """轮询等待回复生成完成，返回最终的xml"""
        print("等待回复生成...")
        last_xml = ""
        stable_count = 0
        start = time.time()
        loading_markers = (
            "正在搜索", "正在生成", "正在思考", "正在分析", "正在整理",
            "思考中", "生成中", "停止生成",
        )

        while time.time() - start < max_wait:
            time.sleep(poll_interval)
            current_xml = self.d.dump_hierarchy()

            visible_reply = self.extract_visible_reply(current_xml, question)
            is_loading = any(marker in current_xml for marker in loading_markers)
            has_answer = len(visible_reply) >= 30

            if current_xml == last_xml and has_answer and not is_loading:
                stable_count += 1
                if stable_count >= 2:
                    print(f"检测到内容已稳定，用时 {time.time()-start:.1f} 秒")
                    return current_xml
            else:
                stable_count = 0

            last_xml = current_xml

        print("等待超时，返回当前状态")
        return last_xml

    def ask(self, question: str, save_xml_path: str = "yuanbao_ui_reply.xml") -> str:
        """
        完整流程：新建对话 -> 输入问题 -> 发送 -> 等待回复 -> 返回最终xml

        参数:
            question: 要问元宝的问题
            save_xml_path: 回复完成后xml的保存路径

        返回:
            回复生成完成后的完整界面xml
        """
        print(f"[模拟器] 开始提问: {question}")
        self.ensure_ready()
        self._dismiss_guide()
        print("[模拟器] 打开新对话...")
        self._open_new_conversation()
        print("[模拟器] 输入并发送...")
        self._type_and_send(question)
        print("[模拟器] 等待回复...")
        xml = self._wait_for_reply(question=question)

        with open(save_xml_path, "w", encoding="utf-8") as f:
            f.write(xml)
        print(f"[模拟器] 已保存 {save_xml_path}，共 {len(xml)} 字符")

        return xml


if __name__ == "__main__":
    bot = YuanbaoController()
    bot.ask("推荐一款染发剂")