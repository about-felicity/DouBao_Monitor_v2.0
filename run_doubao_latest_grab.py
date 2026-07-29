import csv
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone, timedelta

import doubao_env_loader  # noqa: F401  loads API keys from local .env file


for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    except Exception:
        pass


CDP_HOST = "http://127.0.0.1:9222"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SAVE_SCRIPT = os.path.join(BASE_DIR, "save_doubao_refs.py")
PAGE_GRABBER_SCRIPT = os.path.join(BASE_DIR, "doubao_ref_extension", "content.js")
SOURCE_AI_WORKER_SCRIPT = os.path.join(BASE_DIR, "doubao_source_ai_worker.py")
PRODUCT_AI_WORKER_SCRIPT = os.path.join(BASE_DIR, "doubao_product_ai_worker.py")
PENDING_SAVE_DIR = os.path.join(BASE_DIR, "doubao_pending_saves")
CAPTURE_SKIP_CSV = os.path.join(BASE_DIR, "doubao_capture_skips.csv")
CAPTURE_RUNTIME_ERROR_CSV = os.path.join(BASE_DIR, "doubao_capture_runtime_errors.csv")
EXPECTED_REFERENCE_HINTS = {}
CAPTURE_SKIP_FIELDS = [
    "skip_no", "first_seen_at", "last_seen_at", "attempts", "status",
    "chat_url", "chat_title", "question", "reason", "body_length",
    "assistant_text_length", "has_reference_header", "external_anchor_count",
]


# 影刀可能继承系统代理。CDP 是本机服务，必须绕过代理，否则
# 127.0.0.1:9222 会被送到代理端口并表现为 HTTP 502。
for _no_proxy_key in ("NO_PROXY", "no_proxy"):
    _no_proxy_values = [
        item.strip() for item in os.environ.get(_no_proxy_key, "").split(",")
        if item.strip()
    ]
    for _local_host in ("127.0.0.1", "localhost"):
        if _local_host not in _no_proxy_values:
            _no_proxy_values.append(_local_host)
    os.environ[_no_proxy_key] = ",".join(_no_proxy_values)


class CdpConnectionError(RuntimeError):
    """The local Chrome debugging endpoint is unavailable."""


def record_runtime_error(stage, reason):
    exists = os.path.exists(CAPTURE_RUNTIME_ERROR_CSV)
    with open(CAPTURE_RUNTIME_ERROR_CSV, "a", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["recorded_at", "stage", "status", "reason", "cdp_host"],
        )
        if not exists or os.path.getsize(CAPTURE_RUNTIME_ERROR_CSV) == 0:
            writer.writeheader()
        writer.writerow({
            "recorded_at": now_cst(),
            "stage": stage,
            "status": "skipped",
            "reason": str(reason or "")[-1000:],
            "cdp_host": CDP_HOST,
        })


def now_cst():
    return datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")


def read_capture_skips():
    if not os.path.exists(CAPTURE_SKIP_CSV):
        return []
    try:
        with open(CAPTURE_SKIP_CSV, "r", encoding="utf-8-sig", newline="") as f:
            return list(csv.DictReader(f))
    except Exception:
        return []


def write_capture_skips(rows):
    fd, temp_path = tempfile.mkstemp(prefix="doubao_capture_skips_", suffix=".csv", dir=BASE_DIR)
    os.close(fd)
    try:
        with open(temp_path, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=CAPTURE_SKIP_FIELDS, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
        os.replace(temp_path, CAPTURE_SKIP_CSV)
    finally:
        if os.path.exists(temp_path):
            os.unlink(temp_path)


def record_capture_skip(chat_url, chat_title, reason, page_state=None):
    rows = read_capture_skips()
    page_state = page_state if isinstance(page_state, dict) else {}
    now = now_cst()
    target = str(chat_url or "").rstrip("/")
    row = next((r for r in rows if str(r.get("chat_url") or "").rstrip("/") == target), None)
    if row is None:
        numbers = []
        for item in rows:
            try:
                numbers.append(int(item.get("skip_no") or 0))
            except Exception:
                pass
        row = {field: "" for field in CAPTURE_SKIP_FIELDS}
        row["skip_no"] = str((max(numbers) if numbers else 0) + 1)
        row["first_seen_at"] = now
        row["attempts"] = "0"
        rows.append(row)
    try:
        row["attempts"] = str(int(row.get("attempts") or 0) + 1)
    except Exception:
        row["attempts"] = "1"
    row.update({
        "last_seen_at": now,
        "status": "skipped",
        "chat_url": chat_url or "",
        "chat_title": chat_title or "",
        "question": chat_title or "",
        "reason": str(reason or "")[:500],
        "body_length": page_state.get("bodyLength", ""),
        "assistant_text_length": page_state.get("assistantTextLength", ""),
        "has_reference_header": page_state.get("hasReferenceHeader", ""),
        "external_anchor_count": page_state.get("externalAnchorCount", ""),
    })
    write_capture_skips(rows)
    return row


def resolve_capture_skip(chat_url):
    target = str(chat_url or "").rstrip("/")
    if not target:
        return
    rows = read_capture_skips()
    changed = False
    for row in rows:
        if str(row.get("chat_url") or "").rstrip("/") == target and row.get("status") != "resolved":
            row["status"] = "resolved"
            row["last_seen_at"] = now_cst()
            changed = True
    if changed:
        write_capture_skips(rows)


RUN_JS = r"""
(async () => {
  function sleep(ms) {
    return new Promise((resolve) => setTimeout(resolve, ms));
  }

  const HEADER_RE = /\u641c\u7d22\s*\d+\s*\u4e2a\u5173\u952e\u8bcd[\s\S]{0,120}?\u53c2\u8003\s*(\d+)\s*\u7bc7\u8d44\u6599/;

  function cleanText(text) {
    return String(text || "")
      .replace(/\s+/g, " ")
      .trim();
  }

  function isVisible(el) {
    const rect = el.getBoundingClientRect();
    const style = window.getComputedStyle(el);
    return rect.width > 0 && rect.height > 0 && style.visibility !== "hidden" && style.display !== "none";
  }

  function valueOf(selector) {
    const el = document.querySelector(selector);
    return el ? String(el.value || "") : "";
  }

  function clickElement(el) {
    if (!el) return false;
    el.scrollIntoView({ block: "center", inline: "nearest" });
    const rect = el.getBoundingClientRect();
    const x = rect.left + Math.min(rect.width / 2, 80);
    const y = rect.top + rect.height / 2;
    const target = document.elementFromPoint(x, y) || el;
    for (const node of [target, el]) {
      node.dispatchEvent(new PointerEvent("pointerdown", { bubbles: true, cancelable: true, pointerType: "mouse" }));
      node.dispatchEvent(new MouseEvent("mousedown", { bubbles: true, cancelable: true }));
      node.dispatchEvent(new PointerEvent("pointerup", { bubbles: true, cancelable: true, pointerType: "mouse" }));
      node.dispatchEvent(new MouseEvent("mouseup", { bubbles: true, cancelable: true }));
      node.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true }));
      if (typeof node.click === "function") node.click();
    }
    return true;
  }

  function findLatestHistoryItem() {
    const excluded = new Set([
      "\u8c46\u5305",
      "\u65b0\u5bf9\u8bdd",
      "\u65b0\u529e\u516c\u4efb\u52a1",
      "AI \u521b\u4f5c",
      "\u4e91\u76d8",
      "\u66f4\u591a",
      "\u4e3b\u5bf9\u8bdd"
    ]);
    const label = Array.from(document.querySelectorAll("div, span"))
      .filter(isVisible)
      .find((el) => cleanText(el.innerText || el.textContent) === "\u5386\u53f2\u5bf9\u8bdd");
    const top = label ? label.getBoundingClientRect().bottom : 220;
    const right = Math.min(360, window.innerWidth * 0.35);

    const chatLinks = Array.from(document.querySelectorAll("a[href]"))
      .filter((a) => {
        if (!isVisible(a)) return false;
        const href = a.getAttribute("href") || "";
        if (!/^\/chat\/\d+/.test(href) && !/^https?:\/\/www\.doubao\.com\/chat\/\d+/.test(href)) return false;
        const rect = a.getBoundingClientRect();
        const text = cleanText(a.innerText || a.textContent || "");
        if (!text || excluded.has(text)) return false;
        return rect.left < right && rect.top > top && rect.width >= 80 && rect.height >= 18;
      })
      .sort((a, b) => a.getBoundingClientRect().top - b.getBoundingClientRect().top);

    if (chatLinks[0]) return chatLinks[0];

    const items = Array.from(document.querySelectorAll("a[href], [role='button'], [role='link']"))
      .filter((el) => {
        if (!isVisible(el)) return false;
        const rect = el.getBoundingClientRect();
        const text = cleanText(el.innerText || el.textContent || "");
        if (!text || text.length > 60) return false;
        if (excluded.has(text)) return false;
        if (HEADER_RE.test(text)) return false;
        if (rect.left >= right || rect.top <= top) return false;
        if (rect.height < 18 || rect.width < 80) return false;
        return true;
      })
      .sort((a, b) => {
        const ar = a.getBoundingClientRect();
        const br = b.getBoundingClientRect();
        return ar.top - br.top || ar.left - br.left;
      });

    return items[0] || null;
  }

  async function openLatestHistoryItem() {
    const before = location.href;
    let item = null;
    for (let waitIndex = 0; waitIndex < 40; waitIndex += 1) {
      item = findLatestHistoryItem();
      if (item) break;
      await sleep(1000);
    }
    if (!item) return { ok: false, error: "cannot find latest history item" };
    const text = cleanText(item.innerText || item.textContent || "");
    const href = item.getAttribute && item.getAttribute("href");
    if (href && /^\/chat\/\d+/.test(href)) {
      location.href = new URL(href, location.href).href;
    } else if (href && /^https?:\/\/www\.doubao\.com\/chat\/\d+/.test(href)) {
      location.href = href;
    } else {
      clickElement(item);
    }
    for (let i = 0; i < 12; i += 1) {
      await sleep(500);
      if (location.href !== before || HEADER_RE.test(document.body.innerText || "")) {
        return { ok: true, text, url: location.href };
      }
    }
    return { ok: true, text, url: location.href };
  }

  const openResult = await openLatestHistoryItem();
  if (!openResult.ok) return openResult;
  await sleep(1200);

  const btn = document.querySelector("#__doubao_ref_latest_grab");
  if (!btn) {
    return {
      ok: false,
      error: "cannot find #__doubao_ref_latest_grab, please reload extension and refresh Doubao page"
    };
  }

  btn.click();

  let status = "";
  for (let i = 0; i < 60; i += 1) {
    await sleep(1000);
    status = valueOf("#__doubao_ref_status");
    if (status && status !== "running") break;
  }

  const complete = valueOf("#__doubao_ref_complete");
  const count = valueOf("#__doubao_ref_count");
  const result = valueOf("#__doubao_ref_panel_textarea") || valueOf("#__doubao_ref_result");

  return {
    ok: status === "done" && !!result,
    status,
    complete,
    count,
    result
  };
})()
"""


OPEN_LATEST_JS = r"""
(() => {
  function cleanText(text) {
    return String(text || "")
      .replace(/\s+/g, " ")
      .trim();
  }

  function isVisible(el) {
    const rect = el.getBoundingClientRect();
    const style = window.getComputedStyle(el);
    return rect.width > 0 && rect.height > 0 && style.visibility !== "hidden" && style.display !== "none";
  }

  const excluded = new Set([
    "\u8c46\u5305",
    "\u65b0\u5bf9\u8bdd",
    "\u65b0\u529e\u516c\u4efb\u52a1",
    "AI \u521b\u4f5c",
    "\u4e91\u76d8",
    "\u66f4\u591a",
    "\u4e3b\u5bf9\u8bdd"
  ]);

  const historyLabel = Array.from(document.querySelectorAll("div, span"))
    .filter(isVisible)
    .find((el) => cleanText(el.innerText || el.textContent || "") === "\u5386\u53f2\u5bf9\u8bdd");

  const historyTop = historyLabel ? historyLabel.getBoundingClientRect().bottom : 220;
  const sidebarRight = Math.min(360, window.innerWidth * 0.35);

  const links = Array.from(document.querySelectorAll("a[href]"))
    .filter((a) => {
      if (!isVisible(a)) return false;
      const href = a.getAttribute("href") || "";
      if (!/^\/chat\/\d+/.test(href) && !/^https?:\/\/www\.doubao\.com\/chat\/\d+/.test(href)) return false;
      const rect = a.getBoundingClientRect();
      const text = cleanText(a.innerText || a.textContent || "");
      if (!text || excluded.has(text)) return false;
      return rect.left < sidebarRight && rect.top > historyTop && rect.width >= 80 && rect.height >= 18;
    })
    .sort((a, b) => a.getBoundingClientRect().top - b.getBoundingClientRect().top);

  const latest = links[0];
  if (!latest) {
    return { ok: false, error: "cannot find latest history chat link" };
  }

  const href = new URL(latest.getAttribute("href"), location.href).href;
  return {
    ok: true,
    href,
    text: cleanText(latest.innerText || latest.textContent || ""),
    current: location.href
  };
})()
"""


GRAB_ONLY_JS = r"""
(async () => {
  function sleep(ms) {
    return new Promise((resolve) => setTimeout(resolve, ms));
  }

  function valueOf(selector) {
    const el = document.querySelector(selector);
    return el ? String(el.value || "") : "";
  }

  const btn = document.querySelector("#__doubao_ref_button");
  if (!btn) {
    return {
      ok: false,
      error: "cannot find #__doubao_ref_button, please reload extension and refresh Doubao page"
    };
  }

  btn.click();

  let status = "";
  for (let i = 0; i < 60; i += 1) {
    await sleep(1000);
    status = valueOf("#__doubao_ref_status");
    if (status && status !== "running") break;
  }

  const complete = valueOf("#__doubao_ref_complete");
  const count = valueOf("#__doubao_ref_count");
  const result = valueOf("#__doubao_ref_panel_textarea") || valueOf("#__doubao_ref_result");

  return {
    ok: status === "done" && !!result,
    status,
    complete,
    count,
    result
  };
})()
"""


LATEST_GRAB_ONLY_JS = r"""
(async () => {
  function sleep(ms) {
    return new Promise((resolve) => setTimeout(resolve, ms));
  }

  function valueOf(selector) {
    const el = document.querySelector(selector);
    return el ? String(el.value || "") : "";
  }

  const btn = document.querySelector("#__doubao_ref_latest_grab");
  if (!btn) {
    return {
      ok: false,
      error: "cannot find #__doubao_ref_latest_grab, please reload extension and refresh Doubao page"
    };
  }

  btn.click();

  let status = "";
  for (let i = 0; i < 90; i += 1) {
    await sleep(1000);
    status = valueOf("#__doubao_ref_status");
    if (status && status !== "running") break;
  }

  const complete = valueOf("#__doubao_ref_complete");
  const count = valueOf("#__doubao_ref_count");
  const result = valueOf("#__doubao_ref_panel_textarea") || valueOf("#__doubao_ref_result");

  return {
    ok: status === "done" && !!result,
    status,
    complete,
    count,
    result
  };
})()
"""


FORCE_EXPAND_REFERENCES_JS = r"""
(async () => {
  function sleep(ms) {
    return new Promise((resolve) => setTimeout(resolve, ms));
  }

  const HEADER_RE = /\u641c\u7d22\s*\d+\s*\u4e2a\u5173\u952e\u8bcd[\s\S]{0,120}?\u53c2\u8003\s*(\d+)\s*\u7bc7\u8d44\u6599/;

  function isVisible(el) {
    const rect = el.getBoundingClientRect();
    const style = window.getComputedStyle(el);
    return rect.width > 0 && rect.height > 0 && style.visibility !== "hidden" && style.display !== "none";
  }

  function externalAnchorCount() {
    return Array.from(document.querySelectorAll("a[href]")).filter((a) => {
      try {
        const url = new URL(a.getAttribute("href"), location.href);
        const host = url.hostname.replace(/^www\./, "");
        return url.protocol.startsWith("http") && host !== "doubao.com";
      } catch (_) {
        return false;
      }
    }).length;
  }

  const header = Array.from(document.querySelectorAll("div, button, span"))
    .filter((el) => isVisible(el) && HEADER_RE.test(el.innerText || el.textContent || ""))
    .sort((a, b) => {
      const ar = a.getBoundingClientRect();
      const br = b.getBoundingClientRect();
      return (ar.width * ar.height) - (br.width * br.height);
    })[0];

  if (!header) {
    return {
      ok: false,
      reason: "no reference header",
      url: location.href,
      externalAnchorCount: externalAnchorCount()
    };
  }

  for (let i = 0; i < 8; i += 1) {
    header.scrollIntoView({ block: "center", inline: "nearest" });
    await sleep(250);
    const rect = header.getBoundingClientRect();
    const target = document.elementFromPoint(rect.left + rect.width / 2, rect.top + rect.height / 2) || header;
    target.dispatchEvent(new PointerEvent("pointerdown", { bubbles: true, cancelable: true, pointerType: "mouse" }));
    target.dispatchEvent(new MouseEvent("mousedown", { bubbles: true, cancelable: true }));
    target.dispatchEvent(new PointerEvent("pointerup", { bubbles: true, cancelable: true, pointerType: "mouse" }));
    target.dispatchEvent(new MouseEvent("mouseup", { bubbles: true, cancelable: true }));
    target.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true }));
    if (typeof target.click === "function") target.click();
    await sleep(900);
    if (externalAnchorCount() > 0) break;
  }

  return {
    ok: true,
    url: location.href,
    externalAnchorCount: externalAnchorCount()
  };
})()
"""


GET_QUESTION_JS = r"""
(() => {
  function cleanText(text) {
    return String(text || "")
      .replace(/\s+/g, " ")
      .trim();
  }

  function isVisible(el) {
    const rect = el.getBoundingClientRect();
    const style = window.getComputedStyle(el);
    return rect.width > 0 && rect.height > 0 && style.visibility !== "hidden" && style.display !== "none";
  }

  const excludedFragments = [
    "\u53c2\u8003", "\u7bc7\u8d44\u6599",  // 参考 / 篇资料
    "\u641c\u7d22",                         // 搜索
    "\u8c46\u5305", "Doubao",              // 豆包
    "\u65b0\u5bf9\u8bdd",                  // 新对话
    "\u65b0\u529e\u516c\u4efb\u52a1",      // 新办公任务
    "AI \u521b\u4f5c",                     // AI 创作
    "\u4e91\u76d8",                        // 云盘
    "\u66f4\u591a",                        // 更多
    "\u4e3b\u5bf9\u8bdd",                  // 主对话
    "\u53c2\u8003\u8d44\u6599",            // 参考资料
    "Ctrl K",
  ];

  function isBadText(text) {
    if (!text || text.length > 500 || text.length < 2) return true;
    const lower = text.toLowerCase();
    for (const frag of excludedFragments) {
      if (lower.includes(frag.toLowerCase())) return true;
    }
    return false;
  }

  // 策略1: 按聊天气泡 class 名找用户消息（只匹配用户侧的选择器）
  const userSelectors = [
    "[class*='user'][class*='message']",
    "[class*='user-message']", "[class*='userMessage']",
    "[class*='human-message']", "[class*='humanMessage']",
    "[class*='message-user']", "[class*='messageUser']",
    "[class*='message'][class*='sent']",
    "[data-role='user']", "[data-sender='user']",
  ];

  let bestText = "";

  for (const selector of userSelectors) {
    try {
      const els = document.querySelectorAll(selector);
      for (const el of els) {
        if (!isVisible(el)) continue;
        const text = cleanText(el.innerText || el.textContent || "");
        if (isBadText(text)) continue;
        const rect = el.getBoundingClientRect();
        if (rect.top > 60 && rect.height >= 16) {
          bestText = text;
          break;
        }
      }
    } catch (_) {}
    if (bestText) break;
  }

  // 策略2: 宽泛的聊天气泡选择器 + 位置过滤（左侧第一个气泡通常是用户消息）
  if (!bestText) {
    const bubbleSelectors = [
      "[class*='chat-item']", "[class*='conversation-item']",
      "[role='listitem']",
      "[class*='bubble']", "[class*='message-row']",
      "[class*='message']",
    ];
    const candidates = [];
    for (const selector of bubbleSelectors) {
      try {
        for (const el of document.querySelectorAll(selector)) {
          if (!isVisible(el)) continue;
          const text = cleanText(el.innerText || el.textContent || "");
          if (isBadText(text)) continue;
          const rect = el.getBoundingClientRect();
          if (rect.top > 60 && rect.top < window.innerHeight * 0.7 && rect.height >= 16) {
            candidates.push({ text, left: rect.left, top: rect.top });
          }
        }
      } catch (_) {}
    }
    // 取最靠左的气泡（用户消息通常靠左，AI 回复靠右）
    candidates.sort((a, b) => a.left - b.left || a.top - b.top);
    if (candidates.length > 0) {
      bestText = candidates[0].text;
    }
  }

  // 策略3: 从输入框找（textarea/input 可能保留最近输入）
  if (!bestText) {
    const inputSelectors = [
      "textarea[class*='chat']", "input[class*='chat']",
      "[contenteditable='true']",
      "textarea", "input[type='text']",
    ];
    for (const selector of inputSelectors) {
      try {
        const el = document.querySelector(selector);
        if (el && isVisible(el)) {
          const text = cleanText(el.value || el.innerText || el.textContent || "");
          if (!isBadText(text)) {
            bestText = text;
            break;
          }
        }
      } catch (_) {}
    }
  }

  return bestText
    ? { ok: true, question: bestText }
    : { ok: false, question: "" };
})()
"""

PAGE_READY_JS = r"""
(() => {
  const text = document.body ? document.body.innerText || "" : "";
  const match = text.match(/\u641c\u7d22\s*\d+\s*\u4e2a\u5173\u952e\u8bcd[\s\S]{0,120}?\u53c2\u8003\s*(\d+)\s*\u7bc7\u8d44\u6599/);
  const assistantTextLength = Array.from(document.querySelectorAll('[data-testid], [data-message-id], .markdown, article, main'))
    .map(el => el.innerText || "")
    .join("\n")
    .length;
  const externalAnchorCount = Array.from(document.querySelectorAll("a[href]"))
    .map(a => a.href || "")
    .filter(href =>
      /^https?:\/\//.test(href)
      && !href.includes("doubao.com")
      && !href.includes("bytedance")
      && !href.startsWith("javascript:")
    ).length;
  return {
    url: location.href,
    hasGrab: !!document.querySelector("#__doubao_ref_button"),
    hasLatestGrab: !!document.querySelector("#__doubao_ref_latest_grab"),
    hasReferenceHeader: !!match,
    expectedCount: match ? Number(match[1]) || 10 : 0,
    externalAnchorCount,
    bodyLength: text.length,
    assistantTextLength,
    title: document.title
  };
})()
"""


def request_json(url):
    # Do not let HTTP_PROXY/HTTPS_PROXY intercept Chrome's local CDP endpoint.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(url, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8", errors="ignore"))
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError) as exc:
        raise CdpConnectionError(
            "无法连接豆包浏览器调试端口 127.0.0.1:9222；"
            "本轮已跳过。请运行 open_chrome_debug.bat，并在该 Chrome 中打开豆包页面。"
            f" 原始错误：{exc}"
        ) from exc


def find_doubao_page(prefer_url=""):
    pages = request_json(f"{CDP_HOST}/json")
    candidates = []
    prefer_url = str(prefer_url or "").rstrip("/")

    for page in pages:
        if page.get("type") != "page":
            continue

        title = page.get("title") or ""
        url = page.get("url") or ""
        text = f"{title} {url}".lower()
        if "doubao.com" in text or "豆包" in text:
            candidates.append(page)

    if prefer_url:
        exact_pages = [
            page for page in candidates
            if str(page.get("url") or "").rstrip("/") == prefer_url
        ]
        if exact_pages:
            return exact_pages[0]

    chat_pages = [
        page for page in candidates
        if "https://www.doubao.com/chat" in (page.get("url") or "")
    ]
    if chat_pages:
        chat_pages.sort(key=lambda page: (
            0 if "/chat/" in (page.get("url") or "") else 1,
            page.get("url") or "",
        ))
        return chat_pages[0]

    if candidates:
        return candidates[0]

    raise RuntimeError("没有找到豆包 Web 页面。请确认豆包页面是在带 9222 调试端口的 Chrome 中打开。")


def cdp_call(ws_url, method, params=None, timeout=90):
    try:
        import websocket
    except Exception as exc:
        raise RuntimeError("缺少 websocket-client，请运行：python -m pip install websocket-client") from exc

    ws = websocket.create_connection(ws_url, timeout=timeout)
    try:
        message_id = 1
        ws.send(json.dumps({
            "id": message_id,
            "method": method,
            "params": params or {},
        }))

        while True:
            msg = json.loads(ws.recv())
            if msg.get("id") != message_id:
                continue
            if "error" in msg:
                raise RuntimeError(json.dumps(msg["error"], ensure_ascii=False))
            return msg.get("result", {})
    finally:
        ws.close()


def evaluate_js(ws_url, expression, timeout=90):
    return cdp_call(ws_url, "Runtime.evaluate", {
        "expression": expression,
        "awaitPromise": True,
        "returnByValue": True,
    }, timeout=timeout).get("result", {}).get("value")


def is_target_navigated_error(exc):
    text = str(exc)
    return (
        "Inspected target navigated or closed" in text
        or "Cannot find context" in text
        or "Execution context was destroyed" in text
        or "Target closed" in text
    )


def is_missing_extension_button_error(exc):
    text = str(exc)
    return (
        "__doubao_ref_button" in text
        or "__doubao_ref_latest_grab" in text
        or "please reload extension and refresh Doubao page" in text
    )


def page_content_ready(value):
    """Reject Doubao's empty chat shell/sidebar as an assistant answer."""
    if not isinstance(value, dict):
        return False
    return bool(
        value.get("hasReferenceHeader")
        or int(value.get("externalAnchorCount") or 0) > 0
        or int(value.get("assistantTextLength") or 0) >= 300
    )


def current_page_state(ws_url):
    try:
        value = evaluate_js(ws_url, PAGE_READY_JS, timeout=8)
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def ensure_page_grabber(ws_url):
    """Inject the bundled grabber when Chrome ignores --load-extension."""
    state = current_page_state(ws_url)
    if state.get("hasGrab") and state.get("hasLatestGrab"):
        return state
    if not os.path.exists(PAGE_GRABBER_SCRIPT):
        raise RuntimeError("missing bundled Doubao page grabber: " + PAGE_GRABBER_SCRIPT)
    with open(PAGE_GRABBER_SCRIPT, "r", encoding="utf-8") as handle:
        source = handle.read()
    result = evaluate_js(
        ws_url,
        source
        + "\n;({"
        + "hasGrab:!!document.querySelector('#__doubao_ref_button'),"
        + "hasLatestGrab:!!document.querySelector('#__doubao_ref_latest_grab')"
        + "})",
        timeout=15,
    )
    if not isinstance(result, dict) or not result.get("hasGrab"):
        raise RuntimeError(
            "Doubao page grabber injection failed: "
            + json.dumps(result, ensure_ascii=False)
        )
    state = current_page_state(ws_url)
    state["captureReady"] = True
    return state


def wait_for_doubao_ready(target_href="", timeout=20, require_content=False, require_references=False):
    end_at = time.time() + timeout
    last_value = None
    last_error = None
    while time.time() < end_at:
        time.sleep(1)
        try:
            page = find_doubao_page(target_href)
            ws_url = page.get("webSocketDebuggerUrl")
            if not ws_url:
                continue
            ready_result = cdp_call(ws_url, "Runtime.evaluate", {
                "expression": PAGE_READY_JS,
                "awaitPromise": True,
                "returnByValue": True,
            }, timeout=8)
            last_value = ready_result.get("result", {}).get("value")
            if not isinstance(last_value, dict):
                continue
            current_url = str(last_value.get("url") or "").rstrip("/")
            target_url = str(target_href or "").rstrip("/")
            if target_url and current_url != target_url:
                continue
            if not last_value.get("hasGrab"):
                last_value = ensure_page_grabber(ws_url)
            content_ready = page_content_ready(last_value)
            references_ready = (
                last_value.get("hasReferenceHeader")
                or int(last_value.get("expectedCount") or 0) > 0
                or int(last_value.get("externalAnchorCount") or 0) > 0
            )
            if (
                last_value.get("hasGrab")
                and (content_ready or not require_content)
                and (references_ready or not require_references)
            ):
                return ws_url, last_value
        except Exception as exc:
            last_error = exc
            continue
    raise RuntimeError("等待豆包页面重新就绪失败：" + json.dumps({
        "target": target_href,
        "last": last_value,
        "error": repr(last_error) if last_error else "",
    }, ensure_ascii=False))


def reload_and_wait(ws_url, latest_href="", reason=""):
    print("reload page:", reason)
    try:
        cdp_call(ws_url, "Page.reload", {"ignoreCache": True}, timeout=8)
    except Exception as exc:
        print("reload command failed:", repr(exc))
    return wait_for_doubao_ready(latest_href, timeout=45, require_content=True, require_references=True)


def navigate_and_wait_latest(ws_url, latest_href):
    max_rounds = int(os.environ.get("DOUBAO_NAV_RETRY", "2") or "2")
    wait_each = int(os.environ.get("DOUBAO_NAV_WAIT_SECONDS", "20") or "20")
    latest_href = str(latest_href or "").strip()
    target_url = latest_href.rstrip("/")
    last_value = None
    last_error = None

    for round_index in range(1, max_rounds + 1):
        try:
            page = find_doubao_page(target_url if round_index > 1 else "")
            ws_url = page.get("webSocketDebuggerUrl") or ws_url
        except Exception as exc:
            last_error = exc

        print("navigate latest round %s/%s:" % (round_index, max_rounds), latest_href)

        try:
            cdp_call(ws_url, "Page.stopLoading", {}, timeout=5)
        except Exception:
            pass

        try:
            cdp_call(ws_url, "Page.navigate", {"url": latest_href}, timeout=10)
        except Exception as exc:
            last_error = exc
            if not is_target_navigated_error(exc):
                print("Page.navigate failed:", repr(exc))

        for tick in range(wait_each):
            time.sleep(1)
            try:
                page = find_doubao_page(target_url)
                ws_url = page.get("webSocketDebuggerUrl") or ws_url
                ready_result = cdp_call(ws_url, "Runtime.evaluate", {
                    "expression": PAGE_READY_JS,
                    "awaitPromise": True,
                    "returnByValue": True,
                }, timeout=8)
                last_value = ready_result.get("result", {}).get("value")
                if not isinstance(last_value, dict):
                    continue
                current_url = str(last_value.get("url") or "").rstrip("/")
                if current_url == target_url and not last_value.get("hasGrab"):
                    last_value = ensure_page_grabber(ws_url)
                if current_url == target_url and last_value.get("hasGrab") and page_content_ready(last_value):
                    return ws_url, last_value
                if tick in (5, 12) and current_url != target_url:
                    cdp_call(ws_url, "Runtime.evaluate", {
                        "expression": "location.assign(" + json.dumps(latest_href) + ")",
                        "awaitPromise": False,
                        "returnByValue": True,
                    }, timeout=5)
                if tick == 16 and current_url == target_url and not last_value.get("hasGrab"):
                    cdp_call(ws_url, "Page.reload", {"ignoreCache": True}, timeout=8)
            except Exception as exc:
                last_error = exc
                if is_target_navigated_error(exc):
                    continue

        try:
            page = find_doubao_page(target_url)
            ws_url = page.get("webSocketDebuggerUrl") or ws_url
            cdp_call(ws_url, "Page.reload", {"ignoreCache": True}, timeout=8)
        except Exception as exc:
            last_error = exc

    raise RuntimeError("导航到最新会话失败：" + json.dumps({
        "target": latest_href,
        "last": last_value,
        "error": repr(last_error) if last_error else "",
    }, ensure_ascii=False))


def parse_plugin_value(value):
    if not isinstance(value, dict):
        raise RuntimeError("插件执行返回异常：" + json.dumps(value, ensure_ascii=False))
    if not value.get("ok"):
        raise RuntimeError("插件执行失败：" + json.dumps(value, ensure_ascii=False))
    raw_json = value.get("result") or ""
    return json.loads(raw_json)


def grab_with_retry(ws_url, latest_href=""):
    last_payload = None
    expected_hint_key = str(latest_href or "").rstrip("/")
    max_expected_count_seen = int(EXPECTED_REFERENCE_HINTS.get(expected_hint_key) or 0)
    reload_count = 0
    # The reference header can render well before its anchors. Keep a real
    # hydration budget even when an older launcher still exports "2".
    max_reload_count = max(
        5,
        int(os.environ.get("DOUBAO_GRAB_RELOAD_RETRY", "5") or "5"),
    )
    accept_partial_missing = int(os.environ.get("DOUBAO_ACCEPT_PARTIAL_MISSING", "1") or "1")
    no_reference_answer_min_length = int(os.environ.get("DOUBAO_NO_REFERENCE_ANSWER_MIN_LENGTH", "500") or "500")
    attempts = [
        ("grab", GRAB_ONLY_JS),
        ("expand+grab", GRAB_ONLY_JS),
        ("expand+grab", GRAB_ONLY_JS),
        # run_plugin has already navigated to the latest chat. Calling latest+grab
        # here can trigger a second navigation during Runtime.evaluate, which makes
        # Chrome DevTools return "Inspected target navigated or closed".
        ("recover+grab", GRAB_ONLY_JS),
        ("recover+expand+grab", GRAB_ONLY_JS),
    ]
    max_attempts = max(
        1,
        int(os.environ.get("DOUBAO_GRAB_MAX_ATTEMPTS", str(len(attempts))) or len(attempts)),
    )
    attempts = attempts[:max_attempts]
    evaluate_timeout = max(
        5.0,
        float(os.environ.get("DOUBAO_GRAB_EVAL_TIMEOUT", "45") or "45"),
    )

    for index, (mode, script) in enumerate(attempts, 1):
        try:
            ensure_page_grabber(ws_url)
        except Exception as exc:
            print("page grabber injection failed:", repr(exc))

        if mode.startswith("recover"):
            try:
                ws_url, ready_value = wait_for_doubao_ready(latest_href, timeout=15)
                print("recover attempt %s:" % index, json.dumps(ready_value, ensure_ascii=False))
            except Exception as exc:
                print("recover attempt %s failed:" % index, repr(exc))

        if "expand" in mode:
            try:
                expand_value = evaluate_js(ws_url, FORCE_EXPAND_REFERENCES_JS, timeout=30)
                print("expand attempt %s:" % index, json.dumps(expand_value, ensure_ascii=False))
            except Exception as exc:
                print("expand attempt %s failed:" % index, repr(exc))
                if is_target_navigated_error(exc):
                    try:
                        ws_url, _ready_value = wait_for_doubao_ready(latest_href, timeout=15)
                    except Exception as recover_exc:
                        print("expand recover failed:", repr(recover_exc))
            time.sleep(2)

        try:
            value = evaluate_js(ws_url, script, timeout=evaluate_timeout)
        except Exception as exc:
            print("grab attempt %s/%s failed:" % (index, mode), repr(exc))
            if is_target_navigated_error(exc):
                try:
                    ws_url, _ready_value = wait_for_doubao_ready(latest_href, timeout=20)
                    continue
                except Exception as recover_exc:
                    print("grab recover failed:", repr(recover_exc))
            continue
        try:
            payload = parse_plugin_value(value)
        except Exception as exc:
            print("grab attempt %s/%s parse failed:" % (index, mode), repr(exc))
            if is_missing_extension_button_error(exc) and reload_count < max_reload_count:
                try:
                    reload_count += 1
                    ws_url, ready_value = reload_and_wait(
                        ws_url,
                        latest_href,
                        "extension button missing, refresh page and wait (%s/%s)" % (reload_count, max_reload_count),
                    )
                    print("extension reload ready %s/%s:" % (reload_count, max_reload_count), json.dumps(ready_value, ensure_ascii=False))
                    time.sleep(2)
                    continue
                except Exception as recover_exc:
                    print("extension reload recover failed %s/%s:" % (reload_count, max_reload_count), repr(recover_exc))
                    try:
                        ws_url, _ready_value = wait_for_doubao_ready(latest_href, timeout=20)
                        continue
                    except Exception as ready_exc:
                        print("extension button wait failed:", repr(ready_exc))
            continue

        live_state = current_page_state(ws_url)
        payload_expected_count = int(payload.get("expectedCount") or 0)
        live_expected_count = int(live_state.get("expectedCount") or 0)
        max_expected_count_seen = max(
            max_expected_count_seen,
            payload_expected_count,
            live_expected_count,
        )
        payload_url_key = str(payload.get("url") or latest_href or "").rstrip("/")
        if max_expected_count_seen > 0:
            EXPECTED_REFERENCE_HINTS[expected_hint_key or payload_url_key] = max_expected_count_seen
            EXPECTED_REFERENCE_HINTS[payload_url_key] = max_expected_count_seen

        # A Doubao reload briefly removes the reference header before the
        # conversation payload hydrates. Never turn a previously observed
        # positive count into "no references" during that transient shell.
        if (
            int(payload.get("count") or 0) == 0
            and not live_state.get("hasReferenceHeader")
            and int(live_state.get("externalAnchorCount") or 0) == 0
        ):
            payload["expectedCount"] = max_expected_count_seen
        elif max_expected_count_seen > payload_expected_count:
            payload["expectedCount"] = max_expected_count_seen
        last_payload = payload
        print("grab attempt %s/%s:" % (index, mode), json.dumps({
            "count": payload.get("count"),
            "expectedCount": payload.get("expectedCount"),
            "complete": payload.get("complete"),
            "url": payload.get("url"),
            "chatTitle": payload.get("chatTitle"),
        }, ensure_ascii=False))

        count = int(payload.get("count") or 0)
        expected_count = int(payload.get("expectedCount") or 0)
        missing_count = max(0, expected_count - count)
        answer_text = str(payload.get("answerText") or payload.get("answer_text") or "")
        if payload.get("complete") and count > 0:
            return payload
        if count == 0 and expected_count == 0 and len(answer_text.strip()) >= no_reference_answer_min_length:
            payload["status"] = "no_references"
            payload["complete"] = True
            payload["noReferences"] = True
            payload["items"] = []
            print("answer has no reference section; save answer/products only:", json.dumps({
                "url": payload.get("url"),
                "answerTextLength": len(answer_text),
            }, ensure_ascii=False))
            return payload
        if count > 0 and expected_count > 0 and missing_count <= accept_partial_missing:
            payload["partialAccepted"] = True
            payload["missingCount"] = missing_count
            print("accept partial grab: count=%s expected=%s missing=%s" % (count, expected_count, missing_count))
            return payload
        if (
            reload_count < max_reload_count
            and count == 0
            and expected_count > 0
        ):
            try:
                reload_count += 1
                ws_url, ready_value = reload_and_wait(
                    ws_url,
                    latest_href,
                    "expected references but page has no reference header/items (%s/%s)" % (reload_count, max_reload_count),
                )
                print("reload ready %s/%s:" % (reload_count, max_reload_count), json.dumps(ready_value, ensure_ascii=False))
                time.sleep(2)
                continue
            except Exception as exc:
                print("reload recover failed %s/%s:" % (reload_count, max_reload_count), repr(exc))

        time.sleep(3)

    raise RuntimeError("抓取未完整：" + json.dumps(last_payload, ensure_ascii=False))


def run_plugin():
    page = find_doubao_page()
    ws_url = page.get("webSocketDebuggerUrl")
    if not ws_url:
        raise RuntimeError("豆包页面缺少 webSocketDebuggerUrl，请用 open_chrome_debug.bat 打开 Chrome。")

    latest_result = cdp_call(ws_url, "Runtime.evaluate", {
        "expression": OPEN_LATEST_JS,
        "awaitPromise": True,
        "returnByValue": True,
    })
    latest_value = latest_result.get("result", {}).get("value")
    if not isinstance(latest_value, dict) or not latest_value.get("ok"):
        raise RuntimeError("打开最新会话失败：" + json.dumps(latest_value, ensure_ascii=False))

    latest_href = latest_value.get("href")
    if not latest_href:
        raise RuntimeError("打开最新会话失败：没有 href")

    try:
        ws_url, ready_value = navigate_and_wait_latest(ws_url, latest_href)
        print("latest page ready:", json.dumps(ready_value, ensure_ascii=False))
        payload = grab_with_retry(ws_url, latest_href)
    except Exception as exc:
        try:
            live_page = find_doubao_page(latest_href)
            live_ws_url = live_page.get("webSocketDebuggerUrl") or ws_url
        except Exception:
            live_ws_url = ws_url
        state = current_page_state(live_ws_url)
        current_url = str(state.get("url") or latest_href)
        if current_url.rstrip("/") == str(latest_href).rstrip("/") and not page_content_ready(state):
            skipped = record_capture_skip(
                latest_href,
                latest_value.get("text") or "",
                "Doubao answer was not rendered: " + str(exc),
                state,
            )
            print("capture skipped and recorded:", json.dumps(skipped, ensure_ascii=False))
            return {
                "_capture_skipped": True,
                "skip_no": skipped.get("skip_no"),
                "chatTitle": latest_value.get("text") or "",
                "question": latest_value.get("text") or "",
                "url": latest_href,
                "reason": skipped.get("reason") or str(exc),
            }
        raise
    chat_title = str(payload.get("chatTitle") or "")
    if "Ctrl K" in chat_title or "搜索" in chat_title or not chat_title.strip():
        payload["chatTitle"] = latest_value.get("text") or chat_title

    # 提取用户实际输入的问题文本（必须记录，三级回退）
    question_text = ""
    # 第一级：从页面聊天气泡中提取用户实际输入
    try:
        question_result = cdp_call(ws_url, "Runtime.evaluate", {
            "expression": GET_QUESTION_JS,
            "awaitPromise": True,
            "returnByValue": True,
        }, timeout=10)
        question_value = question_result.get("result", {}).get("value")
        if isinstance(question_value, dict) and question_value.get("ok") and question_value.get("question"):
            question_text = str(question_value.get("question")).strip()
    except Exception as exc:
        print("extract question from page failed:", repr(exc))
    # 第二级：侧边栏历史对话文本
    if not question_text:
        question_text = str(latest_value.get("text") or "").strip()
    # 第三级：浏览器插件返回的 chatTitle
    if not question_text:
        question_text = str(payload.get("chatTitle") or "").strip()
    # 都失败则打标记
    payload["question"] = question_text or "未采集到问题"
    print("question:", payload["question"])

    return payload


def persist_pending_save(payload):
    os.makedirs(PENDING_SAVE_DIR, exist_ok=True)
    fingerprint = "|".join((
        str(payload.get("url") or ""),
        str(payload.get("extractedAt") or ""),
        str(payload.get("answerText") or ""),
    ))
    name = hashlib.sha256(fingerprint.encode("utf-8")).hexdigest() + ".json"
    target = os.path.join(PENDING_SAVE_DIR, name)
    fd, temp_path = tempfile.mkstemp(prefix="pending_", suffix=".json", dir=PENDING_SAVE_DIR)
    os.close(fd)
    try:
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        os.replace(temp_path, target)
    finally:
        if os.path.exists(temp_path):
            os.unlink(temp_path)
    return target


def save_payload(payload):
    raw = json.dumps(payload, ensure_ascii=False)
    pending_path = persist_pending_save(payload)
    env = os.environ.copy()
    # The dashboard reads CSV directly. Keep this foreground step fast; source AI
    # classification is kicked to a detached worker after the CSV is written.
    env.setdefault("DOUBAO_SKIP_XLSX", "1")
    last_error = ""
    for attempt in range(1, 4):
        try:
            out = subprocess.check_output(
                [sys.executable, SAVE_SCRIPT, raw],
                cwd=BASE_DIR,
                env=env,
                stderr=subprocess.STDOUT,
                encoding="utf-8",
                errors="ignore",
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            try:
                os.unlink(pending_path)
            except FileNotFoundError:
                pass
            return {"deferred": False, "output": out.strip()}
        except subprocess.CalledProcessError as exc:
            last_error = str(exc.output or exc)
            if "product data write lock is busy" not in last_error:
                break
            if attempt < 3:
                time.sleep(attempt)
    # The exact payload remains on disk and the product worker will retry it.
    return {
        "deferred": True,
        "output": "save deferred for background retry",
        "reason": last_error[-500:],
        "pending_file": os.path.basename(pending_path),
    }


def _ai_source_explicitly_disabled():
    value = os.environ.get("DOUBAO_USE_AI_SOURCE", "").strip()
    return value in ("0", "false", "FALSE", "no")


def _has_api_key():
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("OPENAI_API_KEY"))


def start_source_ai_worker():
    # Default ON when an API key is configured, unless explicitly disabled.
    if _ai_source_explicitly_disabled():
        return "disabled"
    if not _has_api_key():
        return "disabled"
    if not os.path.exists(SOURCE_AI_WORKER_SCRIPT):
        return "missing"

    env = os.environ.copy()
    env.setdefault("DOUBAO_AI_TIMEOUT", "12")
    env.setdefault("DOUBAO_META_TIMEOUT", "3")
    env.setdefault("DOUBAO_AI_WORKER_MAX_HOSTS", "20")
    log_path = os.path.join(BASE_DIR, "doubao_source_ai_worker.launch.log")
    try:
        with open(log_path, "ab") as log:
            subprocess.Popen(
                [sys.executable, SOURCE_AI_WORKER_SCRIPT],
                cwd=BASE_DIR,
                env=env,
                stdout=log,
                stderr=log,
                stdin=subprocess.DEVNULL,
                close_fds=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        return "started"
    except Exception as exc:
        return "failed: " + repr(exc)


def start_product_ai_worker():
    """Retry failed product reviews without holding up the next capture round."""
    if not os.path.exists(PRODUCT_AI_WORKER_SCRIPT):
        return "missing"
    env = os.environ.copy()
    env.setdefault("DOUBAO_PRODUCT_AI_MODEL", "deepseek-v4-flash")
    # Commit in small batches so solved rounds appear on the dashboard quickly
    # and foreground capture never competes with a long CSV transaction.
    env.setdefault("DOUBAO_PRODUCT_AI_RETRY_BATCH", "5")
    log_path = os.path.join(BASE_DIR, "doubao_product_ai_worker.launch.log")
    try:
        with open(log_path, "ab") as log:
            subprocess.Popen(
                [sys.executable, PRODUCT_AI_WORKER_SCRIPT],
                cwd=BASE_DIR,
                env=env,
                stdout=log,
                stderr=log,
                stdin=subprocess.DEVNULL,
                close_fds=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        return "started"
    except Exception as exc:
        return "failed: " + repr(exc)


def main():
    try:
        payload = run_plugin()
        if payload.get("_capture_skipped"):
            print(json.dumps({
                "ok": True,
                "skipped": True,
                "recorded": True,
                "skip_no": payload.get("skip_no"),
                "chatTitle": payload.get("chatTitle"),
                "url": payload.get("url"),
                "reason": payload.get("reason"),
            }, ensure_ascii=False))
            return
        save_result = save_payload(payload)
        if save_result.get("deferred"):
            record_capture_skip(
                payload.get("url"),
                payload.get("chatTitle") or payload.get("title") or "",
                "Save deferred for background retry: " + str(save_result.get("reason") or "")[-300:],
                {
                    "bodyLength": len(str(payload.get("answerText") or "")),
                    "assistantTextLength": len(str(payload.get("answerText") or "")),
                    "hasReferenceHeader": True,
                    "externalAnchorCount": payload.get("count") or 0,
                },
            )
        else:
            resolve_capture_skip(payload.get("url"))
        worker_result = start_source_ai_worker()
        product_worker_result = start_product_ai_worker()
        print(json.dumps({
            "ok": True,
            "count": payload.get("count"),
            "expectedCount": payload.get("expectedCount"),
            "complete": payload.get("complete"),
            "chatTitle": payload.get("chatTitle"),
            "answerTextLength": len(str(payload.get("answerText") or "")),
            "save": save_result,
            "source_ai_worker": worker_result,
            "product_ai_worker": product_worker_result,
        }, ensure_ascii=False))
    except CdpConnectionError as exc:
        record_runtime_error("cdp_connection", exc)
        print(json.dumps({
            "ok": True,
            "skipped": True,
            "recorded": True,
            "stage": "cdp_connection",
            "reason": str(exc),
        }, ensure_ascii=False))
        return
    except Exception as exc:
        print(json.dumps({
            "ok": False,
            "error": str(exc),
        }, ensure_ascii=False))
        raise


if __name__ == "__main__":
    main()
