"""用 DeepSeek 对元宝正文中的品牌与产品做低 token、可校验的结构化提取。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


BASE_DIR = Path(__file__).resolve().parent
ENV_FILE = BASE_DIR / "yuanbao_api.env"
CACHE_FILE = BASE_DIR / "yuanbao_brand_ai_cache.json"
PROMPT_VERSION = "yuanbao-product-v3"
DEFAULT_MODEL = "deepseek-v4-flash"
PRODUCT_MARKERS = (
    "染发剂", "染发膏", "染发霜", "染发乳",
    "眉毛增长液", "眉毛精华液", "眉毛植萃精华液", "眉毛密润精粹液", "密眉赋活液",
    "睫毛增长液", "睫毛精华液", "睫毛营养液",
)
GENERIC_PREFIXES = (
    "挑选", "选购", "市面", "目前", "以下", "根据", "总结", "避雷", "使用",
    "染发剂需要", "染发剂种类", "眉毛增长液通过", "睫毛增长液通过",
)


def _load_local_env() -> None:
    if not ENV_FILE.exists():
        return
    for raw in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def _body_hash(body: str) -> str:
    return hashlib.sha256(f"{PROMPT_VERSION}\0{body}".encode("utf-8")).hexdigest()


def _candidate_lines(body: str) -> list[str]:
    """只把疑似商品行发给模型，避免重复发送解释性长正文。"""
    candidates: list[str] = []
    lines = [line.strip(" \t•·") for line in body.splitlines()]
    for index, line in enumerate(lines):
        if not line or len(line) > 110 or not any(marker in line for marker in PRODUCT_MARKERS):
            continue
        if line.startswith(GENERIC_PREFIXES):
            continue
        # 商品标题有时被放在“卖点标签：”之后，有时直接独立成行。
        candidates.append(line)
        # 保留紧邻的标题行可以帮助模型区分品牌与卖点，但不发送正文段落。
        if index and "：" in lines[index - 1] and len(lines[index - 1]) <= 40:
            candidates.append(lines[index - 1])
    return list(dict.fromkeys(candidates))[:20]


def _read_cache() -> dict[str, Any]:
    if not CACHE_FILE.exists():
        return {"prompt_version": PROMPT_VERSION, "entries": {}}
    try:
        cache = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"prompt_version": PROMPT_VERSION, "entries": {}}
    if cache.get("prompt_version") != PROMPT_VERSION:
        return {"prompt_version": PROMPT_VERSION, "entries": {}}
    cache.setdefault("entries", {})
    return cache


def _write_cache(cache: dict[str, Any]) -> None:
    CACHE_FILE.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")


def _extract_json(text: str) -> dict[str, Any]:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start < 0 or end < start:
        raise ValueError("模型没有返回 JSON 对象")
    return json.loads(cleaned[start:end + 1])


def _call_deepseek(items: list[dict[str, Any]]) -> tuple[dict[str, Any], dict[str, int], str]:
    _load_local_env()
    api_key = os.getenv("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("缺少 DEEPSEEK_API_KEY，请在 yuanbao_api.env 中配置")
    model = os.getenv("DEEPSEEK_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL
    base_url = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/")
    input_rows = [
        {"id": item["id"], "question": item["question"], "candidate_lines": item["candidates"]}
        for item in items
    ]
    system = (
        "你是中文电商商品实体审核器。只审核输入的候选行，不补充常识，不猜测。"
        "识别回答真正推荐的商业产品，排除泛称、成分、平台、店铺、卖点标签和警示文字。"
        "必须逐行审核所有 candidate_lines：只要一行含有明确品牌和具体产品，即使没有价格或店铺链接也必须输出，不能遗漏。"
        "同一产品的标题与规格行必须合并。brand 使用正文中的规范品牌写法；中英文同时出现时保留二者，"
        "例如“梵玢 FBCY”。product_name 使用去掉容量、颜色、价格后的正文商品名。"
        "evidence 必须逐字复制一条包含品牌和产品的候选行。"
    )
    user = (
        "输出严格 JSON：{\"items\":[{\"id\":\"...\",\"products\":["
        "{\"brand\":\"...\",\"product_name\":\"...\",\"evidence\":\"...\",\"category\":\"染发/眉毛/睫毛\"}"
        "]}]}。没有产品则 products=[]。输入：\n"
        + json.dumps(input_rows, ensure_ascii=False, separators=(",", ":"))
    )
    payload = {
        "model": model,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "temperature": 0,
        "thinking": {"type": "disabled"},
        "response_format": {"type": "json_object"},
        "max_tokens": 2600,
        "stream": False,
    }
    request = urllib.request.Request(
        f"{base_url}/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        # 不输出请求头或密钥；只保留状态码与服务端短错误。
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"DeepSeek HTTP {exc.code}: {detail}") from None
    content = result["choices"][0]["message"]["content"]
    usage = result.get("usage") or {}
    safe_usage = {
        "prompt_tokens": int(usage.get("prompt_tokens") or 0),
        "completion_tokens": int(usage.get("completion_tokens") or 0),
        "total_tokens": int(usage.get("total_tokens") or 0),
    }
    return _extract_json(content), safe_usage, str(result.get("model") or model)


def _brand_is_grounded(brand: str, evidence: str, body: str) -> bool:
    if brand in evidence or brand in body:
        return True
    tokens = [token for token in re.split(r"[\s/·（）()]+", brand) if len(token) >= 2]
    return bool(tokens) and all(token.lower() in evidence.lower() for token in tokens)


def _compact(value: str) -> str:
    return re.sub(r"[\s·（）()：:、，。/]+", "", value).casefold()


def _category_for(text: str) -> str:
    if "染发" in text:
        return "染发"
    if "眉毛" in text or "密眉" in text:
        return "眉毛"
    if "睫毛" in text:
        return "睫毛"
    return "其他"


def _validate_products(body: str, raw_products: Any) -> list[dict[str, Any]]:
    validated: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for raw in raw_products if isinstance(raw_products, list) else []:
        if not isinstance(raw, dict):
            continue
        brand = str(raw.get("brand") or "").strip()
        product_name = str(raw.get("product_name") or "").strip()
        evidence = str(raw.get("evidence") or "").strip()
        position = body.find(evidence)
        if not brand or not product_name or position < 0:
            continue
        if not _brand_is_grounded(brand, evidence, body):
            continue
        if _compact(product_name) not in _compact(evidence):
            continue
        if not any(marker in evidence for marker in PRODUCT_MARKERS):
            continue
        key = (brand.casefold(), product_name.casefold())
        if key in seen:
            continue
        seen.add(key)
        validated.append({
            "brand": brand,
            "raw_brand": brand,
            "product_name": product_name,
            "evidence": evidence,
            "category": _category_for(evidence),
            "position": position,
            "extraction_method": "deepseek",
        })
    # 模型偶尔会漏掉没有价格/店铺卡片、但正文明确推荐的条目。
    # 对“标签：品牌 产品词”这一种无歧义结构做确定性兜底，仍要求逐字证据和明确空格边界。
    for line in _candidate_lines(body):
        content = line.split("：", 1)[-1].strip()
        for marker in sorted(PRODUCT_MARKERS, key=len, reverse=True):
            marker_at = content.find(marker)
            if marker_at <= 0:
                continue
            before = content[:marker_at]
            if not before[-1:].isspace():
                break
            brand = before.strip()
            if not (1 < len(brand) <= 24) or any(char in brand for char in "：:，。；"):
                break
            covered = any(
                item["brand"].casefold() == brand.casefold()
                and (marker in item["product_name"] or item["product_name"] in marker)
                for item in validated
            )
            if not covered:
                validated.append({
                    "brand": brand,
                    "raw_brand": brand,
                    "product_name": marker,
                    "evidence": line,
                    "category": _category_for(marker),
                    "position": body.find(line),
                    "extraction_method": "grounded_coverage",
                })
            break
    validated.sort(key=lambda item: item["position"])
    for rank, item in enumerate(validated, 1):
        item["rank"] = rank
    return validated


def analyze_records(records: list[dict[str, Any]]) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """返回按正文哈希索引的结果；所有模型结果都会经过正文证据校验。"""
    cache = _read_cache()
    entries: dict[str, Any] = cache["entries"]
    prepared: list[dict[str, Any]] = []
    record_by_hash: dict[str, dict[str, Any]] = {}
    for record in records:
        body = str(record.get("web_body") or record.get("reply") or "")
        key = _body_hash(body)
        record_by_hash[key] = record
        if key not in entries:
            prepared.append({
                "id": key[:16],
                "hash": key,
                "question": str(record.get("question") or ""),
                "body": body,
                "candidates": _candidate_lines(body),
            })

    usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    model = os.getenv("DEEPSEEK_MODEL", DEFAULT_MODEL)
    if prepared:
        response, usage, model = _call_deepseek(prepared)
        response_items = {
            str(item.get("id")): item for item in response.get("items", []) if isinstance(item, dict)
        }
        for item in prepared:
            raw = response_items.get(item["id"], {})
            products = _validate_products(item["body"], raw.get("products"))
            entries[item["hash"]] = {
                "model": model,
                "products": products,
                "candidate_count": len(item["candidates"]),
            }
        cache["model"] = model
        _write_cache(cache)

    output: dict[str, dict[str, Any]] = {}
    for key, record in record_by_hash.items():
        body = str(record.get("web_body") or record.get("reply") or "")
        cached = entries.get(key, {})
        output[key] = {
            "model": cached.get("model") or model,
            "products": _validate_products(body, cached.get("products")),
            "cached": key not in {item["hash"] for item in prepared},
        }
    return output, {
        "model": model,
        "prompt_version": PROMPT_VERSION,
        "new_analyses": len(prepared),
        "cached_analyses": len(records) - len(prepared),
        "usage": usage,
    }


def record_hash(record: dict[str, Any]) -> str:
    return _body_hash(str(record.get("web_body") or record.get("reply") or ""))
