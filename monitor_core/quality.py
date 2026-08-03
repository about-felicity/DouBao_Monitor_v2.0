from __future__ import annotations

import re


ERROR_MARKERS = (
    "系统异常", "服务异常", "网络异常", "出了点问题", "请稍后重试",
    "生成失败", "回答失败", "server error", "system error", "try again later",
)


def normalize_text(value: str) -> str:
    return re.sub(r"\s+", "", str(value or "")).casefold()


def invalid_answer_reason(value: str, *, minimum_length: int = 12) -> str:
    text = str(value or "").strip()
    compact = normalize_text(text)
    if not compact:
        return "模型回答为空"
    if any(normalize_text(marker) in compact for marker in ERROR_MARKERS):
        return "模型返回系统或服务异常"
    if len(compact) < minimum_length:
        return f"模型回答过短（{len(compact)} 字）"
    return ""
