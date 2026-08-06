from __future__ import annotations

import re


ERROR_MARKERS = (
    "系统异常", "服务异常", "网络异常", "出了点问题", "请稍后重试",
    "生成失败", "回答失败", "设备环境有风险", "重新尝试请求",
    "server error", "system error", "try again later",
)

REFUSAL_MARKERS = (
    "不在我的医疗健康服务范围内", "无法为你推荐", "不能为你推荐",
    "无法提供相关产品推荐", "不能提供相关产品推荐",
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
    if any(normalize_text(marker) in compact for marker in REFUSAL_MARKERS):
        return "模型拒绝或无法完成产品推荐"
    if len(compact) < minimum_length:
        return f"模型回答过短（{len(compact)} 字）"
    return ""


def expected_topic(question: str) -> str:
    text = re.sub(r"\s+", "", str(question or ""))
    text = re.sub(r"^(?:请|麻烦|帮我|给我|想要|需要)*推荐(?:一款|一个|一种)?", "", text)
    text = re.sub(r"推荐$", "", text)
    return text.strip("，。！？?：:")

def topic_matches(topic: str, body: str) -> bool:
    variants = {topic}
    if "增长" in topic:
        variants.add(topic.replace("增长", "生长"))
    if "生长" in topic:
        variants.add(topic.replace("生长", "增长"))
    if any(value in body for value in variants):
        return True
    bigrams = {value[index:index + 2] for value in variants for index in range(len(value) - 1)}
    required = 1 if len(topic) <= 3 else 2
    aliases = {"控油": ("油头", "去油", "清爽"), "增长": ("生长",), "生长": ("增长",)}
    matched = sum(
        1 for value in bigrams
        if value in body or any(alias in body for alias in aliases.get(value, ()))
    )
    return matched >= required

def answer_quality_reason(question: str, answer: str, *, minimum_length: int = 12) -> str:
    """Reject empty/error replies and obvious cross-topic conversation mix-ups."""
    reason = invalid_answer_reason(answer, minimum_length=minimum_length)
    if reason:
        return reason
    topic = normalize_text(expected_topic(question))
    body = normalize_text(answer)
    if len(topic) >= 2:

        if not topic_matches(topic, body):

            return f"回答主题与问题不一致（未出现“{expected_topic(question)}”）"
    return ""
