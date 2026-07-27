import argparse
import json
import os
import random
import re
import time
from datetime import datetime, timezone

import run_doubao_latest_grab as grabber


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DASHBOARD_URL = "http://127.0.0.1:8765/"
CHAT_HOME_URL = "https://www.doubao.com/chat/"
DEFAULT_QUESTION = "推荐一款染发剂"
RATE_LIMIT_RE = re.compile(r"(操作频繁|请求过于频繁|稍后再试|访问受限|安全验证|异常访问|验证码)")


INPUT_READY_JS = r"""
(() => {
  const visible = (el) => {
    const r = el.getBoundingClientRect();
    const s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.display !== "none" && s.visibility !== "hidden";
  };
  const rectObj = (el) => {
    const r = el.getBoundingClientRect();
    return {
      left: r.left,
      top: r.top,
      width: r.width,
      height: r.height,
      cx: r.left + r.width / 2,
      cy: r.top + r.height / 2
    };
  };
  const textarea = Array.from(document.querySelectorAll("textarea"))
    .filter(visible)
    .filter((el) => !/json result/i.test(el.getAttribute("placeholder") || ""))
    .sort((a, b) => b.getBoundingClientRect().top - a.getBoundingClientRect().top)[0];
  if (!textarea) {
    return { ok: false, error: "cannot find visible textarea", url: location.href, title: document.title };
  }
  return {
    ok: true,
    url: location.href,
    title: document.title,
    textarea: rectObj(textarea),
    placeholder: textarea.getAttribute("placeholder") || "",
    valueLength: String(textarea.value || "").length
  };
})()
"""


PREPARE_INPUT_JS = r"""
(() => {
  const visible = (el) => {
    const r = el.getBoundingClientRect();
    const s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.display !== "none" && s.visibility !== "hidden";
  };
  const rectObj = (el) => {
    const r = el.getBoundingClientRect();
    return {
      left: r.left,
      top: r.top,
      width: r.width,
      height: r.height,
      cx: r.left + r.width / 2,
      cy: r.top + r.height / 2
    };
  };
  const textarea = Array.from(document.querySelectorAll("textarea"))
    .filter(visible)
    .filter((el) => !/json result/i.test(el.getAttribute("placeholder") || ""))
    .sort((a, b) => b.getBoundingClientRect().top - a.getBoundingClientRect().top)[0];
  if (!textarea) {
    return { ok: false, error: "cannot find visible textarea" };
  }
  textarea.focus();
  textarea.value = "";
  textarea.dispatchEvent(new Event("input", { bubbles: true }));
  textarea.dispatchEvent(new Event("change", { bubbles: true }));
  return {
    ok: true,
    textarea: rectObj(textarea),
    placeholder: textarea.getAttribute("placeholder") || ""
  };
})()
"""


LOCATE_SEND_BUTTON_JS = r"""
(() => {
  const visible = (el) => {
    const r = el.getBoundingClientRect();
    const s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.display !== "none" && s.visibility !== "hidden";
  };
  const rectObj = (el) => {
    const r = el.getBoundingClientRect();
    return {
      left: r.left,
      top: r.top,
      width: r.width,
      height: r.height,
      cx: r.left + r.width / 2,
      cy: r.top + r.height / 2
    };
  };
  const textarea = Array.from(document.querySelectorAll("textarea"))
    .filter(visible)
    .filter((el) => !/json result/i.test(el.getAttribute("placeholder") || ""))
    .sort((a, b) => b.getBoundingClientRect().top - a.getBoundingClientRect().top)[0];
  if (!textarea) {
    return { ok: false, error: "cannot find visible textarea" };
  }
  const tr = textarea.getBoundingClientRect();
  const buttons = Array.from(document.querySelectorAll("button,[role='button']"))
    .filter(visible)
    .map((el) => ({
      el,
      rect: el.getBoundingClientRect(),
      cls: String(el.className || ""),
      aria: el.getAttribute("aria-label") || "",
      title: el.getAttribute("title") || ""
    }))
    .filter((x) => x.rect.top > tr.top - 30 && x.rect.bottom < tr.bottom + 120 && x.rect.left >= tr.right - 90)
    .sort((a, b) => b.rect.left - a.rect.left);
  const send = buttons.find((x) =>
    /send-msg-btn|highlight|static-white-primary|bg-g-send-msg-btn/i.test(x.cls) ||
    x.rect.width <= 48
  ) || buttons[0];
  if (!send) {
    return { ok: false, error: "cannot find send button", candidates: buttons.length };
  }
  return {
    ok: true,
    send: rectObj(send.el),
    aria: send.aria,
    title: send.title,
    cls: send.cls
  };
})()
"""


SEND_STARTED_JS = r"""
(() => {
  const visible = (el) => {
    const r = el.getBoundingClientRect();
    const s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.display !== "none" && s.visibility !== "hidden";
  };
  const textarea = Array.from(document.querySelectorAll("textarea"))
    .filter(visible)
    .filter((el) => !/json result/i.test(el.getAttribute("placeholder") || ""))
    .sort((a, b) => b.getBoundingClientRect().top - a.getBoundingClientRect().top)[0];
  const body = String(document.body ? document.body.innerText || "" : "");
  return {
    url: location.href,
    title: document.title,
    textareaLength: textarea ? String(textarea.value || "").length : -1,
    hasReferenceHeader: /\u641c\u7d22\s*\d+\s*\u4e2a\u5173\u952e\u8bcd[\s\S]{0,120}?\u53c2\u8003\s*\d+\s*\u7bc7\u8d44\u6599/.test(body),
    bodyLength: body.length,
    isChatPage: /\/chat\/\d+/.test(location.href)
  };
})()
"""


WAIT_READY_JS = r"""
(() => {
  const body = String(document.body ? document.body.innerText || "" : "");
  const match = body.match(/\u641c\u7d22\s*\d+\s*\u4e2a\u5173\u952e\u8bcd[\s\S]{0,120}?\u53c2\u8003\s*(\d+)\s*\u7bc7\u8d44\u6599/);
  const externalAnchorCount = Array.from(document.querySelectorAll("a[href]")).filter((a) => {
    try {
      const url = new URL(a.getAttribute("href"), location.href);
      const host = url.hostname.replace(/^www\./, "");
      return /^https?:$/.test(url.protocol) && host !== "doubao.com";
    } catch (_) {
      return false;
    }
  }).length;
  return {
    url: location.href,
    title: document.title,
    hasReferenceHeader: !!match,
    expectedCount: match ? Number(match[1]) || 0 : 0,
    externalAnchorCount,
    bodyLength: body.length,
    bodyTail: body.slice(-1200),
    bodyHead: body.slice(0, 600)
  };
})()
"""


GET_CHAT_TITLE_JS = r"""
(() => {
  const title = String(document.title || "").trim();
  return title.replace(/\s*-\s*豆包\s*$/, "").trim();
})()
"""


def log(message):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), str(message), flush=True)


def load_plan(args):
    if args.plan_file:
        with open(args.plan_file, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
        items = data.get("items") or []
        plan = []
        for item in items:
            question = str(item.get("question") or "").strip()
            rounds = int(item.get("rounds") or 0)
            if question and rounds > 0:
                plan.append({"question": question, "rounds": rounds})
        if not plan:
            raise RuntimeError("计划文件中没有有效问题")
        mode = str(data.get("mode") or args.mode or "sequential").strip().lower()
        return plan, mode

    if args.questions_file:
        with open(args.questions_file, "r", encoding="utf-8-sig") as f:
            questions = [line.strip() for line in f if line.strip() and not line.strip().startswith("#")]
    elif args.questions:
        questions = [item.strip() for item in args.questions.split("|||") if item.strip()]
    else:
        questions = [args.question.strip() or DEFAULT_QUESTION]
    if not questions:
        raise RuntimeError("问题列表为空")
    rounds_per_question = args.rounds_per_question or args.rounds
    plan = [{"question": question, "rounds": rounds_per_question} for question in questions]
    mode = str(args.mode or "sequential").strip().lower()
    return plan, mode


def current_page():
    return grabber.find_doubao_page()


def current_ws_url():
    page = current_page()
    ws_url = page.get("webSocketDebuggerUrl")
    if not ws_url:
        raise RuntimeError("豆包页面缺少 webSocketDebuggerUrl，请确认 Chrome 是用 open_chrome_debug.bat 打开的")
    return ws_url


def evaluate_js(expression, timeout=30):
    return grabber.evaluate_js(current_ws_url(), expression, timeout=timeout)


def cdp_call(method, params=None, timeout=30):
    return grabber.cdp_call(current_ws_url(), method, params or {}, timeout=timeout)


def cdp_click(x, y):
    params = {"x": float(x), "y": float(y), "button": "left", "clickCount": 1}
    cdp_call("Input.dispatchMouseEvent", {"type": "mousePressed", **params}, timeout=10)
    cdp_call("Input.dispatchMouseEvent", {"type": "mouseReleased", **params}, timeout=10)


def cdp_press_enter():
    common = {
        "windowsVirtualKeyCode": 13,
        "nativeVirtualKeyCode": 13,
        "code": "Enter",
        "key": "Enter",
        "unmodifiedText": "\r",
        "text": "\r",
    }
    cdp_call("Input.dispatchKeyEvent", {"type": "keyDown", **common}, timeout=10)
    cdp_call(
        "Input.dispatchKeyEvent",
        {"type": "keyUp", "windowsVirtualKeyCode": 13, "nativeVirtualKeyCode": 13, "code": "Enter", "key": "Enter"},
        timeout=10,
    )


def wait_for_input_ready(timeout=45):
    last_snapshot = None
    start = time.time()
    while time.time() - start < timeout:
        snapshot = evaluate_js(INPUT_READY_JS, timeout=20)
        last_snapshot = snapshot
        if isinstance(snapshot, dict) and snapshot.get("ok"):
            return snapshot
        time.sleep(1)
    raise RuntimeError("等待豆包输入框超时: " + json.dumps(last_snapshot, ensure_ascii=False))


def open_new_chat():
    log("打开新对话页面")
    cdp_call("Page.navigate", {"url": CHAT_HOME_URL}, timeout=20)
    time.sleep(1.4)
    snapshot = wait_for_input_ready(timeout=50)
    time.sleep(random.uniform(0.5, 1.2))
    return snapshot


def prepare_input():
    result = evaluate_js(PREPARE_INPUT_JS, timeout=20)
    if not isinstance(result, dict) or not result.get("ok"):
        raise RuntimeError("准备输入框失败: " + json.dumps(result, ensure_ascii=False))
    return result


def locate_send_button():
    result = evaluate_js(LOCATE_SEND_BUTTON_JS, timeout=20)
    if not isinstance(result, dict) or not result.get("ok"):
        raise RuntimeError("定位发送按钮失败: " + json.dumps(result, ensure_ascii=False))
    return result


def send_question(question):
    state = prepare_input()
    textarea = state["textarea"]
    cdp_click(textarea["cx"], textarea["cy"])
    time.sleep(random.uniform(0.2, 0.6))
    cdp_call("Input.insertText", {"text": question}, timeout=20)
    time.sleep(random.uniform(0.4, 0.9))
    cdp_press_enter()

    last_snapshot = None
    start = time.time()
    while time.time() - start < 4:
        time.sleep(0.8)
        snapshot = evaluate_js(SEND_STARTED_JS, timeout=20)
        last_snapshot = snapshot
        url = str(snapshot.get("url") or "")
        if re.search(r"/chat/\d+", url) or snapshot.get("hasReferenceHeader") or int(snapshot.get("textareaLength", -1)) == 0:
            return snapshot

    log("Enter 未成功发送，尝试点击发送按钮")
    send = locate_send_button()["send"]
    cdp_click(send["cx"], send["cy"])

    start = time.time()
    while time.time() - start < 6:
        time.sleep(0.8)
        snapshot = evaluate_js(SEND_STARTED_JS, timeout=20)
        last_snapshot = snapshot
        url = str(snapshot.get("url") or "")
        if re.search(r"/chat/\d+", url) or snapshot.get("hasReferenceHeader") or int(snapshot.get("textareaLength", -1)) == 0:
            return snapshot

    raise RuntimeError("发送问题后页面没有进入对话态: " + json.dumps(last_snapshot, ensure_ascii=False))


def expand_reference_header():
    try:
        value = evaluate_js(grabber.FORCE_EXPAND_REFERENCES_JS, timeout=20)
        if isinstance(value, dict):
            log("展开参考资料: " + json.dumps({
                "ok": value.get("ok"),
                "anchors": value.get("externalAnchorCount"),
                "reason": value.get("reason", ""),
            }, ensure_ascii=False))
    except Exception as exc:
        log("展开参考资料失败: " + repr(exc))


def wait_answer_ready(timeout=120, stable_seconds=6):
    start = time.time()
    stable_since = None
    last_key = None
    last_snapshot = None
    expand_every = 0

    while time.time() - start < timeout:
        time.sleep(random.uniform(1.5, 2.5))
        snapshot = evaluate_js(WAIT_READY_JS, timeout=20)
        last_snapshot = snapshot
        body_text = (snapshot.get("bodyHead") or "") + "\n" + (snapshot.get("bodyTail") or "")
        if RATE_LIMIT_RE.search(body_text):
            raise RuntimeError("检测到疑似风控/验证页面，请人工介入: " + json.dumps(snapshot, ensure_ascii=False))

        log("等待回答完成: " + json.dumps({
            "url": snapshot.get("url"),
            "expectedCount": snapshot.get("expectedCount"),
            "anchors": snapshot.get("externalAnchorCount"),
            "hasReferenceHeader": snapshot.get("hasReferenceHeader"),
            "bodyLength": snapshot.get("bodyLength"),
        }, ensure_ascii=False))

        if snapshot.get("hasReferenceHeader"):
            expand_every += 1
            if expand_every % 2 == 1:
                expand_reference_header()

            stable_key = (
                int(snapshot.get("expectedCount") or 0),
                int(snapshot.get("externalAnchorCount") or 0),
                int(snapshot.get("bodyLength") or 0),
                str(snapshot.get("bodyTail") or "")[-400:],
            )
            if stable_key == last_key and stable_key[0] > 0:
                stable_since = stable_since or time.time()
                if time.time() - stable_since >= stable_seconds:
                    return snapshot
            else:
                last_key = stable_key
                stable_since = None

    raise RuntimeError("等待豆包回答完成超时: " + json.dumps(last_snapshot, ensure_ascii=False))


def get_chat_title():
    try:
        value = evaluate_js(GET_CHAT_TITLE_JS, timeout=10)
        return str(value or "").strip()
    except Exception:
        return ""


def grab_current_round(question):
    last_error = None
    for attempt in range(1, 4):
        try:
            log(f"抓取信源 attempt {attempt}/3")
            payload = grabber.grab_with_retry(current_ws_url())
            payload["question"] = question
            if not payload.get("extractedAt"):
                payload["extractedAt"] = datetime.now(timezone.utc).isoformat()
            chat_title = str(payload.get("chatTitle") or "").strip()
            if not chat_title or "Ctrl K" in chat_title:
                payload["chatTitle"] = get_chat_title() or chat_title or question
            save_result = grabber.save_payload(payload)
            return payload, save_result
        except Exception as exc:
            last_error = exc
            log("抓取信源失败: " + repr(exc))
            time.sleep(8 + attempt * 4)
    raise RuntimeError("连续抓取失败: " + repr(last_error))


def cooldown_sleep(min_seconds, max_seconds):
    seconds = max(min_seconds, random.randint(min_seconds, max_seconds))
    log(f"进入冷却 {seconds} 秒，降低风控概率")
    remaining = seconds
    while remaining > 0:
        chunk = min(10, remaining)
        time.sleep(chunk)
        remaining -= chunk
        if remaining > 0:
            log(f"冷却剩余 {remaining} 秒")


def run_round(question, timeout, stable_seconds):
    open_new_chat()
    log("发送问题: " + question)
    send_question(question)
    wait_answer_ready(timeout=timeout, stable_seconds=stable_seconds)
    payload, save_result = grab_current_round(question)
    log(
        json.dumps(
            {
                "ok": True,
                "question": question,
                "chatTitle": payload.get("chatTitle"),
                "count": payload.get("count"),
                "expectedCount": payload.get("expectedCount"),
                "save": save_result,
                "dashboard": DASHBOARD_URL,
            },
            ensure_ascii=False,
        )
    )
    return payload


def build_schedule(plan, mode):
    normalized = []
    for item in plan:
        question = str(item.get("question") or "").strip()
        rounds = int(item.get("rounds") or 0)
        if question and rounds > 0:
            normalized.append({"question": question, "rounds": rounds})
    if not normalized:
        raise RuntimeError("没有可执行的问题计划")

    if mode == "interleaved":
        schedule = []
        max_rounds = max(item["rounds"] for item in normalized)
        for round_index in range(1, max_rounds + 1):
            for question_index, item in enumerate(normalized, 1):
                if round_index <= item["rounds"]:
                    schedule.append(
                        {
                            "question": item["question"],
                            "question_index": question_index,
                            "question_total": len(normalized),
                            "round_index": round_index,
                            "round_total": item["rounds"],
                        }
                    )
        return schedule

    schedule = []
    for question_index, item in enumerate(normalized, 1):
        for round_index in range(1, item["rounds"] + 1):
            schedule.append(
                {
                    "question": item["question"],
                    "question_index": question_index,
                    "question_total": len(normalized),
                    "round_index": round_index,
                    "round_total": item["rounds"],
                }
            )
    return schedule


def run_loop(plan, mode, timeout, stable_seconds, cooldown_min, cooldown_max):
    schedule = build_schedule(plan, mode)
    total = len(schedule)
    done = 0

    log("调度模式: " + ("交叉提问" if mode == "interleaved" else "顺序提问"))
    for item in plan:
        log(f"计划: {item['question']} x {item['rounds']}")

    for step in schedule:
        done += 1
        question = step["question"]
        log(
            f"开始第 {done}/{total} 轮，问题 {step['question_index']}/{step['question_total']}，"
            f"该问题轮次 {step['round_index']}/{step['round_total']}：{question}"
        )
        run_round(question, timeout=timeout, stable_seconds=stable_seconds)
        if done < total:
            cooldown_sleep(cooldown_min, cooldown_max)
    log("豆包网页循环监控完成")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--question", default=DEFAULT_QUESTION)
    parser.add_argument("--questions", default="", help="多个问题使用 ||| 分隔")
    parser.add_argument("--questions-file", default="", help="每行一个问题")
    parser.add_argument("--plan-file", default="", help="JSON 计划文件，支持每题单独轮数")
    parser.add_argument("--mode", default="sequential", choices=["sequential", "interleaved"], help="提问调度模式")
    parser.add_argument("--rounds", type=int, default=30, help="兼容旧参数: 单问题总轮数")
    parser.add_argument("--rounds-per-question", type=int, default=0, help="每个问题跑多少轮，优先于 --rounds")
    parser.add_argument("--timeout", type=int, default=120, help="单轮等待回答完成的超时时间")
    parser.add_argument("--stable-seconds", type=int, default=6, help="参考资料与正文稳定多久后才开始抓取")
    parser.add_argument("--cooldown-min", type=int, default=15, help="两轮之间最小冷却秒数")
    parser.add_argument("--cooldown-max", type=int, default=25, help="两轮之间最大冷却秒数")
    args = parser.parse_args()

    if args.cooldown_min <= 0 or args.cooldown_max <= 0:
        raise RuntimeError("冷却时间必须是正整数")
    if args.cooldown_max < args.cooldown_min:
        raise RuntimeError("cooldown-max 不能小于 cooldown-min")

    try:
        plan, mode = load_plan(args)
        run_loop(
            plan=plan,
            mode=mode,
            timeout=args.timeout,
            stable_seconds=args.stable_seconds,
            cooldown_min=args.cooldown_min,
            cooldown_max=args.cooldown_max,
        )
        print(json.dumps({"ok": True}, ensure_ascii=False), flush=True)
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), flush=True)
        raise


if __name__ == "__main__":
    main()
