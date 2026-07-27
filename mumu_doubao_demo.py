import argparse
import os
import re
import subprocess
import time
import xml.etree.ElementTree as ET


ADB_CANDIDATES = [
    r"C:\Program Files\Netease\MuMu\nx_device\15.0\shell\adb.exe",
    r"C:\Program Files\Netease\MuMu\nx_main\adb.exe",
    "adb",
]
MUMU_MANAGER_CANDIDATES = [
    r"C:\Program Files\Netease\MuMu\nx_main\MuMuManager.exe",
]

ADB = next((path for path in ADB_CANDIDATES if os.path.exists(path)), ADB_CANDIDATES[0])
MUMU_MANAGER = next((path for path in MUMU_MANAGER_CANDIDATES if os.path.exists(path)), MUMU_MANAGER_CANDIDATES[0])

DEVICE = "127.0.0.1:16384"
MUMU_INDEX = "0"
DOUBAO_PACKAGE = "com.larus.nova"
DOUBAO_IME = "com.sohu.inputmethod.sogou.chuizi/com.sohu.inputmethod.sogou.SogouIME"
ADB_IME = "com.android.adbkeyboard/.AdbIME"


def log(message):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), str(message), flush=True)


def run(cmd, timeout=20, check=True):
    p = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="ignore",
        timeout=timeout,
    )
    if check and p.returncode != 0:
        raise RuntimeError("command failed:\n%s\nstdout=%s\nstderr=%s" % (" ".join(cmd), p.stdout, p.stderr))
    return p


def adb(*args, timeout=20, check=True):
    return run([ADB, "-s", DEVICE, *args], timeout=timeout, check=check)


def adb_text(*args, timeout=20, check=True):
    return adb(*args, timeout=timeout, check=check).stdout.strip()


def connect():
    run([ADB, "connect", DEVICE], timeout=10, check=False)
    devices = run([ADB, "devices", "-l"], timeout=10, check=False).stdout
    if DEVICE not in devices and "emulator-5554" not in devices:
        raise RuntimeError("MuMu 未连接成功，请先确认模拟器已启动。")
    return devices.strip()


def bounds_center(bounds):
    nums = [int(x) for x in re.findall(r"\d+", bounds or "")]
    if len(nums) != 4:
        return None
    x1, y1, x2, y2 = nums
    return (x1 + x2) // 2, (y1 + y2) // 2


def dump_ui():
    adb("shell", "uiautomator", "dump", "/sdcard/window.xml", timeout=10, check=False)
    xml = adb_text("exec-out", "cat", "/sdcard/window.xml", timeout=10)
    return ET.fromstring(xml)


def iter_nodes(root):
    yield root
    for child in root:
        yield from iter_nodes(child)


def node_attr(node, key):
    return node.attrib.get(key) or ""


def node_text(node):
    return " ".join(
        value for value in [
            node_attr(node, "text"),
            node_attr(node, "hint"),
            node_attr(node, "content-desc"),
        ] if value
    )


def find_first(root, predicate):
    for node in iter_nodes(root):
        if predicate(node):
            return node
    return None


def find_by_res_id(root, res_id):
    return find_first(root, lambda n: node_attr(n, "resource-id") == res_id)


def tap(x, y):
    adb("shell", "input", "tap", str(int(x)), str(int(y)))
    time.sleep(0.35)


def tap_node(node):
    center = bounds_center(node_attr(node, "bounds"))
    if not center:
        raise RuntimeError("节点没有可点击坐标: " + ET.tostring(node, encoding="unicode"))
    tap(*center)


def current_focus():
    return adb_text("shell", "dumpsys", "window", "|", "grep", "mCurrentFocus", check=False)


def screenshot_bytes():
    p = subprocess.run([ADB, "-s", DEVICE, "exec-out", "screencap", "-p"], capture_output=True, timeout=20)
    return p.stdout if p.returncode == 0 else b""


def is_list_page(root):
    return find_by_res_id(root, "com.larus.nova:id/conversation_list") is not None


def is_chat_page(root):
    return find_by_res_id(root, "com.larus.nova:id/chat_root") is not None


def is_login_page(root):
    return (
        find_by_res_id(root, "com.larus.nova:id/send_code_message") is not None
        or find_by_res_id(root, "com.larus.nova:id/edit_solid") is not None
    )


def is_compose_menu(root):
    return find_first(root, lambda n: "创建新对话" in node_text(n)) is not None


def find_back(root):
    return find_by_res_id(root, "com.larus.nova:id/back_icon")


def find_new_chat(root):
    return find_by_res_id(root, "com.larus.nova:id/right_img")


def find_create_new_chat_menu_item(root):
    node = find_first(root, lambda n: node_attr(n, "text") == "创建新对话")
    if node is None:
        return None
    parent_bounds = node_attr(node, "bounds")
    for candidate in iter_nodes(root):
        if (
            node_attr(candidate, "clickable") == "true"
            and node_attr(candidate, "bounds") == parent_bounds
        ):
            return candidate
    return node


def find_input_toggle(root):
    return find_by_res_id(root, "com.larus.nova:id/action_input")


def find_input_box(root):
    node = find_by_res_id(root, "com.larus.nova:id/input_text")
    if node is not None:
        return node
    return find_first(
        root,
        lambda n: n.attrib.get("class") == "android.widget.EditText" and (
            "发消息" in node_text(n) or "说话" in node_text(n)
        ),
    )


def find_send(root):
    return find_by_res_id(root, "com.larus.nova:id/action_send")


def start_doubao():
    adb("shell", "monkey", "-p", DOUBAO_PACKAGE, "-c", "android.intent.category.LAUNCHER", "1", timeout=15, check=False)
    time.sleep(2.0)


def ensure_chat_ready(root=None):
    root = root or dump_ui()
    if is_login_page(root):
        raise RuntimeError("MuMu 里的豆包当前停在登录/验证码页，请先在模拟器里完成登录。")
    if not is_chat_page(root) and not is_list_page(root):
        raise RuntimeError("当前不在豆包可识别页面，请把 MuMu 切到豆包前台。")
    return root


def wait_ready(timeout=12):
    start = time.time()
    last_error = "豆包页面未就绪"
    while time.time() - start < timeout:
        root = dump_ui()
        if is_login_page(root):
            raise RuntimeError("MuMu 里的豆包当前停在登录/验证码页，请先在模拟器里完成登录。")
        if is_chat_page(root) or is_list_page(root) or is_compose_menu(root):
            return root
        last_error = "当前不在豆包可识别页面，请把 MuMu 切到豆包前台。"
        time.sleep(0.8)
    raise RuntimeError(last_error)


def goto_list_page():
    for _ in range(3):
        root = dump_ui()
        if is_login_page(root):
            raise RuntimeError("MuMu 里的豆包当前停在登录/验证码页，请先在模拟器里完成登录。")
        if is_compose_menu(root):
            adb("shell", "input", "keyevent", "4", check=False)
            time.sleep(0.6)
            continue
        root = ensure_chat_ready(root)
        if is_list_page(root):
            return root
        back = find_back(root)
        if back is None:
            break
        tap_node(back)
        time.sleep(0.8)
    root = dump_ui()
    if not is_list_page(root):
        raise RuntimeError("没有成功回到豆包对话列表页。")
    return root


def create_new_chat():
    root = goto_list_page()
    button = find_new_chat(root)
    if button is None:
        raise RuntimeError("列表页没找到新对话按钮 com.larus.nova:id/right_img")
    tap_node(button)
    time.sleep(0.8)
    root = dump_ui()
    if is_compose_menu(root):
        menu_item = find_create_new_chat_menu_item(root)
        if menu_item is None:
            raise RuntimeError("检测到新对话菜单，但没找到“创建新对话”项。")
        tap_node(menu_item)
        time.sleep(1.0)
        root = dump_ui()
    root = ensure_chat_ready(root)
    if not is_chat_page(root):
        raise RuntimeError("点击新对话后没有进入聊天页。")
    return root


def ensure_text_input():
    root = ensure_chat_ready()
    input_box = find_input_box(root)
    if input_box is not None:
        tap_node(input_box)
        time.sleep(0.4)
        return input_box
    toggle = find_input_toggle(root)
    if toggle is None:
        raise RuntimeError("聊天页里没找到输入切换按钮 com.larus.nova:id/action_input")
    tap_node(toggle)
    time.sleep(0.8)
    root = dump_ui()
    input_box = find_input_box(root)
    if input_box is None:
        raise RuntimeError("点击输入切换按钮后，仍然没找到输入框 com.larus.nova:id/input_text")
    tap_node(input_box)
    time.sleep(0.4)
    return input_box


def clear_input(max_delete=120):
    adb("shell", "input", "keyevent", "123", check=False)
    for _ in range(max_delete):
        adb("shell", "input", "keyevent", "67", check=False)
    time.sleep(0.3)


def set_ime(ime_id):
    adb("shell", "ime", "set", ime_id, timeout=10, check=False)
    time.sleep(0.3)


def input_text_via_mumu_manager(text):
    if not os.path.exists(MUMU_MANAGER):
        return False, "MuMuManager 不存在"
    p = run(
        [MUMU_MANAGER, "adb", "-v", MUMU_INDEX, "-c", "input_text " + text],
        timeout=20,
        check=False,
    )
    return p.returncode == 0, (p.stdout or p.stderr).strip()


def input_text_via_adb_keyboard(text):
    old_ime = adb_text("shell", "settings", "get", "secure", "default_input_method", check=False).strip()
    try:
        set_ime(ADB_IME)
        p = adb(
            "shell", "am", "broadcast", "-a", "ADB_INPUT_TEXT", "--es", "msg", text,
            timeout=20,
            check=False,
        )
        ok = p.returncode == 0 and "Broadcast completed" in ((p.stdout or "") + (p.stderr or ""))
        return ok, (p.stdout or p.stderr).strip()
    finally:
        if old_ime:
            set_ime(old_ime)
        else:
            set_ime(DOUBAO_IME)


def input_text(text):
    ok, detail = input_text_via_mumu_manager(text)
    if ok:
        time.sleep(0.6)
        return "mumu_manager"
    ok, detail2 = input_text_via_adb_keyboard(text)
    if ok:
        time.sleep(0.6)
        return "adb_keyboard"
    raise RuntimeError("中文输入失败。MuMuManager=%s；ADBKeyboard=%s" % (detail, detail2))


def verify_input_contains(text):
    root = dump_ui()
    input_box = find_input_box(root)
    if input_box is None:
        raise RuntimeError("输入后丢失了输入框。")
    current = node_attr(input_box, "text")
    if text not in current:
        raise RuntimeError("输入框文字校验失败。当前内容：%s" % current)
    return True


def fill_input_text(text):
    ensure_text_input()
    clear_input()

    errors = []
    method = None

    try:
        method = input_text(text)
        verify_input_contains(text)
        return method
    except Exception as exc:
        errors.append("mumu_manager/默认链路失败: " + str(exc))

    try:
        ensure_text_input()
        clear_input()
        ok, detail = input_text_via_adb_keyboard(text)
        if not ok:
            raise RuntimeError(detail or "ADBKeyboard 返回失败")
        verify_input_contains(text)
        return "adb_keyboard"
    except Exception as exc:
        errors.append("adb_keyboard 重试失败: " + str(exc))

    raise RuntimeError("输入文字失败：%s" % " | ".join(errors))


def send_current_input():
    root = dump_ui()
    button = find_send(root)
    if button is None:
        raise RuntimeError("没找到发送按钮 com.larus.nova:id/action_send")
    tap_node(button)
    time.sleep(0.8)


def wait_answer_complete(min_wait=12, stable_seconds=4, timeout=120):
    start = time.time()
    stable_since = None
    previous = b""
    while time.time() - start < timeout:
        time.sleep(1.0)
        current = screenshot_bytes()
        if time.time() - start < min_wait:
            previous = current
            stable_since = None
            continue
        if current and current == previous:
            if stable_since is None:
                stable_since = time.time()
            if time.time() - stable_since >= stable_seconds:
                return True
        else:
            previous = current
            stable_since = None
    raise RuntimeError("等待豆包回答完成超时。")


def ask_once(text, min_wait=12, stable_seconds=4, timeout=120):
    create_new_chat()
    method = fill_input_text(text)
    send_current_input()
    wait_answer_complete(min_wait=min_wait, stable_seconds=stable_seconds, timeout=timeout)
    return method


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--text", default="推荐一款染发剂")
    parser.add_argument("--rounds", type=int, default=1)
    parser.add_argument("--min-wait", type=int, default=12)
    parser.add_argument("--stable-seconds", type=int, default=4)
    parser.add_argument("--timeout", type=int, default=120)
    args = parser.parse_args()

    connect()
    start_doubao()
    for index in range(1, args.rounds + 1):
        log("round %s/%s" % (index, args.rounds))
        method = ask_once(
            args.text,
            min_wait=args.min_wait,
            stable_seconds=args.stable_seconds,
            timeout=args.timeout,
        )
        log("input method: " + method)
    log("done")


if __name__ == "__main__":
    main()
