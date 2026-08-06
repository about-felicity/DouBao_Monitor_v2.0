"""把 DeepSeek JSONL 聚合为统一面板使用的数据结构。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

BASE_DIR = Path(__file__).resolve().parent
RESULTS = BASE_DIR / "deepseek_results.jsonl"
OUTPUT = BASE_DIR / "dashboard.json"
BEIJING = timezone(timedelta(hours=8))
TRACKING = {"refer", "from", "source", "spm", "srsltid"}
MEDIA = {
    "zhihu.com": "知乎", "xiaohongshu.com": "小红书", "weibo.com": "微博",
    "bilibili.com": "哔哩哔哩", "douyin.com": "抖音", "jd.com": "京东",
    "taobao.com": "淘宝", "tmall.com": "天猫", "baidu.com": "百度",
    "qq.com": "腾讯网", "163.com": "网易", "ifeng.com": "凤凰网",
}
PRODUCT_MARKERS = (
    "染发剂", "染发膏", "染发霜", "染发乳",
    "眉毛增长液", "眉毛精华液", "眉毛植萃精华液", "眉毛密润精粹液", "密眉赋活液",
    "睫毛增长液", "睫毛精华液", "睫毛营养液",
)
GENERIC_PREFIXES = (
    "挑选", "选购", "市面", "目前", "以下", "根据", "总结", "避雷", "使用",
    "染发剂需要", "染发剂种类", "眉毛增长液通过", "睫毛增长液通过",
)
PROMPT_VERSION = "deepseek-product-v6"
DEFAULT_MODEL = "deepseek-v4-flash"
CACHE_FILE = BASE_DIR / "deepseek_brand_ai_cache.json"


def _load_local_env() -> None:
    for path in (BASE_DIR / "deepseek_api.env", BASE_DIR.parent / "yuanbao_monitor" / "yuanbao_api.env"):
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, value = stripped.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
        break


# ──────────────────── 正文预处理 ────────────────────

def _preprocess_body_for_ai(body: str) -> str:
    """DeepSeek 的 web_body 被 JS clean() 压成了单行（无换行）。

    yuanbao_brand_ai._candidate_lines 靠 splitlines() 逐行筛选产品行，
    且丢弃 >110 字的行。这里做几件事让候选行筛选能正常工作。
    """
    text = str(body or "").strip()
    if not text:
        return text
    for marker in ("内容由 AI 生成", "本回答由 AI 生成"):
        if marker in text:
            text = text.split(marker, 1)[0]
    # 1. 剥离侧边栏：多种模式
    text = re.sub(r"^(开启新对话\s+今天\s+.*?(?:快速模式|深度思考|智能搜索))\s*", "", text)
    text = re.sub(r"^(?:\S+推荐\s+)+快速模式\s*", "", text)
    text = re.sub(r"^[^推荐]*(?=推荐一款)", "", text)
    # 2. 清理问题回声 + 元信息
    text = re.sub(r"^推荐一款[^。]*?已阅读\s*\d+\s*个网页\s*", "", text)
    # 3. 清理引用标记
    text = re.sub(r"\s*[A-Za-z0-9._-]*\.[A-Za-z]{2,}\s+\d+\s*", " ", text)
    text = re.sub(r"\s*[\u4e00-\u9fff]{2,8}\s+\d{4}[/-]\d{1,2}[/-]\d{1,2}\s+\d+\s*", " ", text)
    text = re.sub(r"\s*[A-Za-z0-9._-]+\s+\d{4}[/-]\d{1,2}[/-]\d{1,2}\s+\d+\s*", " ", text)
    # 4. 引用标记 - 数字 - → 换行
    text = re.sub(r"-\s*\d+\s*-", "\n", text)
    # 5. 清理元信息
    text = re.sub(r"\s*已阅读\s*\d+\s*个网页\s*", " ", text)
    text = re.sub(r"\s*内容由\s*AI\s*生成.*?(?=。|$)", "", text)
    text = re.sub(r"\s*本回答由\s*AI\s*生成.*?(?=。|$)", "", text)
    # 6. 句末标点后换行
    text = re.sub(r"([。！？；：])\s*", r"\1\n", text)
    return text


# ──────────────────── AI 产品提取（复用 yuanbao_brand_ai 逻辑）────────────────────

def _body_hash(body: str) -> str:
    return hashlib.sha256(f"{PROMPT_VERSION}\0{body}".encode("utf-8")).hexdigest()


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
    temp = CACHE_FILE.with_suffix(CACHE_FILE.suffix + ".tmp")
    temp.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(CACHE_FILE)


def _candidate_lines(body: str) -> list[str]:
    candidates: list[str] = []
    lines = [line.strip(" \t\u2022\u00b7") for line in body.splitlines()]
    for index, line in enumerate(lines):
        if not line or len(line) > 180:
            continue
        if re.search(r"taobao|淘宝|京东|智商税|中国.*老年|如何变浓|真的有效吗|风险提醒", line, re.I):
            continue
        has_category = any(marker in line for marker in PRODUCT_MARKERS)
        has_growth_product = bool(
            re.search(r"(?:眉毛|睫毛).{0,12}(?:增长|滋润|精华|美容).{0,5}液", line)
            or re.search(r"[A-Za-z][A-Za-z0-9-]{2,}.{0,20}(?:增长|滋润|精华|美容).{0,5}液", line)
        )
        has_recommendation_shape = bool(
            re.search(r"(?:首选|推荐|备选|产品参考|国际品牌|国货|综合实力).{0,100}[：:]", line)
            or re.match(r"^[A-Za-z\u4e00-\u9fff][A-Za-z0-9\u4e00-\u9fff ·-]{1,35}[（(].{0,24}[）)][：:]", line)
            or any(brand in line for brand in ("梵玢", "科熙本", "姿生怡", "道和", "焕颜计", "茗媛萃"))
        )
        if not has_category and not has_growth_product and not has_recommendation_shape:
            continue
        if line.startswith(GENERIC_PREFIXES) and not re.search(r"[A-Za-z][A-Za-z0-9-]{2,}", line):
            continue
        candidates.append(line)
        if index and "\uff1a" in lines[index - 1] and len(lines[index - 1]) <= 40:
            candidates.append(lines[index - 1])
    return list(dict.fromkeys(candidates))[:20]


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
        '例如"梵玢 FBCY"。product_name 使用去掉容量、颜色、价格后的正文商品名。'
        "evidence 必须逐字复制一条包含品牌和产品的候选行。"
    )
    user = (
        '输出严格 JSON：{"items":[{"id":"...","products":['
        '{"brand":"...","product_name":"...","evidence":"...","category":"染发/眉毛/睫毛"}'
        ']}]}。没有产品则 products=[]。输入：\n'
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
    tokens = [token for token in re.split(r"[\s/\u00b7\uff08\uff09()]+", brand) if len(token) >= 2]
    return bool(tokens) and all(token.lower() in evidence.lower() for token in tokens)


def _compact(value: str) -> str:
    return re.sub(r"[\s\u00b7\uff08\uff09\uff1a:\u3001\uff0c\u3002/]+", "", value).casefold()


def _category_for(text: str) -> str:
    if "染发" in text:
        return "染发"
    if "眉毛" in text or "密眉" in text:
        return "眉毛"
    if "睫毛" in text:
        return "睫毛"
    return "其他"


def _validate_products(body: str, raw_products: Any, question: str = "") -> list[dict[str, Any]]:
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
        evidence_category = _category_for(evidence)
        expected_category = _category_for(question)
        if evidence_category == "其他" and expected_category == "其他":
            continue
        key = (brand.casefold(), product_name.casefold())
        if key in seen:
            continue
        seen.add(key)
        validated.append({
            "brand": brand, "raw_brand": brand, "product_name": product_name,
            "evidence": evidence, "category": evidence_category if evidence_category != "其他" else expected_category,
            "position": position, "extraction_method": "deepseek",
        })
    for line in _candidate_lines(body):
        content = line.split("\uff1a", 1)[-1].strip()
        for marker in sorted(PRODUCT_MARKERS, key=len, reverse=True):
            marker_at = content.find(marker)
            if marker_at <= 0:
                break
            before = content[:marker_at]
            if not before[-1:].isspace():
                break
            brand = before.strip()
            if not (1 < len(brand) <= 24) or any(char in brand for char in "\uff1a:\uff0c\u3002\uff1b"):
                break
            covered = any(
                item["brand"].casefold() == brand.casefold()
                and (marker in item["product_name"] or item["product_name"] in marker)
                for item in validated
            )
            if not covered:
                validated.append({
                    "brand": brand, "raw_brand": brand, "product_name": marker,
                    "evidence": line, "category": _category_for(marker),
                    "position": body.find(line), "extraction_method": "grounded_coverage",
                })
            break
    validated.sort(key=lambda item: item["position"])
    for rank, item in enumerate(validated, 1):
        item["rank"] = rank
    return validated


def analyze_deepseek_records(records: list[dict]) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """用原始 body 哈希匹配缓存，用预处理 body 提取候选行。"""
    cache = _read_cache()
    entries: dict[str, Any] = cache["entries"]
    prepared: list[dict[str, Any]] = []
    record_by_hash: dict[str, dict[str, Any]] = {}

    for record in records:
        original_body = record.get("_original_body", "")
        preprocessed_body = record.get("_analysis_body", "")
        key = _body_hash(original_body)
        record_by_hash[key] = record
        if key not in entries:
            candidates = _candidate_lines(preprocessed_body)
            prepared.append({
                "id": key[:16], "hash": key,
                "question": str(record.get("question") or ""),
                "body": preprocessed_body, "candidates": candidates,
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
            # AI 看到的是预处理 body，证据也在预处理 body 里，用预处理 body 验证
            preprocessed_body = record_by_hash[item["hash"]].get("_analysis_body", "")
            products = _validate_products(preprocessed_body, raw.get("products"), str(record_by_hash[item["hash"]].get("question") or ""))
            entries[item["hash"]] = {
                "model": model, "products": products,
                "candidate_count": len(item["candidates"]),
            }
        cache["model"] = model
        _write_cache(cache)

    output: dict[str, dict[str, Any]] = {}
    for key, record in record_by_hash.items():
        preprocessed_body = record.get("_analysis_body", "")
        cached = entries.get(key, {})
        output[key] = {
            "model": cached.get("model") or model,
            "products": _validate_products(preprocessed_body, cached.get("products"), str(record.get("question") or "")),
            "cached": key not in {item["hash"] for item in prepared},
        }
    return output, {
        "model": model, "prompt_version": PROMPT_VERSION,
        "new_analyses": len(prepared),
        "cached_analyses": len(records) - len(prepared),
        "usage": usage,
    }


# ─────────────────── 面板聚合 ────────────────────

def canonical_url(value: str) -> str:
    try:
        parsed = urlparse(value.strip())
        query = [(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True) if k.lower() not in TRACKING and not k.lower().startswith("utm_")]
        return urlunparse((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path.rstrip("/") or "/", parsed.params, urlencode(query), ""))
    except ValueError:
        return value.strip()


def day(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=BEIJING)
        return parsed.astimezone(BEIJING).date().isoformat()
    except ValueError:
        match = re.match(r"\d{4}-\d{2}-\d{2}", str(value or ""))
        return match.group(0) if match else ""


def media_name(domain: str) -> str:
    for key, label in MEDIA.items():
        if domain == key or domain.endswith("." + key):
            return label
    return domain.removeprefix("www.") or "未知媒体"


def source_type(domain: str, title: str) -> str:
    text = f"{domain} {title}".lower()
    if any(value in text for value in ("bilibili", "douyin", "youtube", "视频")):
        return "视频"
    if any(value in text for value in ("xiaohongshu", "weibo", "小红书", "微博")):
        return "社交"
    if any(value in text for value in ("jd.com", "taobao", "tmall", "商城")):
        return "电商"
    return "文章"


def counted(values) -> list[dict]:
    return [{"name": name, "count": count} for name, count in Counter(values).most_common()]


def record_id(record: dict) -> str:
    stable = "\0".join(str(record.get(key) or "") for key in ("round", "question", "started_at", "finished_at"))
    return hashlib.sha256(stable.encode()).hexdigest()[:20]


def question_category(question: str) -> str:
    if "染发" in question:
        return "染发"
    if "眉毛" in question:
        return "眉毛"
    if "睫毛" in question:
        return "睫毛"
    return "其他"


def product_summary(runs: list[dict]) -> list[dict]:
    grouped: dict[tuple[str, str], dict] = {}
    for run in runs:
        for product in run["products"]:
            key = (product["brand"].casefold(), product["product_name"].casefold())
            row = grouped.setdefault(key, {
                "brand": product["brand"], "product_name": product["product_name"],
                "category": product["category"], "mention_runs": set(), "total_mentions": 0,
                "ranks": [], "details": [],
            })
            row["mention_runs"].add(run["run_id"])
            row["total_mentions"] += 1
            row["ranks"].append(product["rank"])
            row["details"].append({
                "run_id": run["run_id"], "sequence": run["sequence"], "round": run["round"],
                "serial": run["serial"], "question": run["question"],
                "rank": product["rank"], "evidence": product["evidence"],
            })
    output = []
    for row in grouped.values():
        mentions = len(row.pop("mention_runs"))
        ranks = row.pop("ranks")
        eligible_runs = sum(question_category(run["question"]) == row["category"] for run in runs)
        if not eligible_runs:
            eligible_runs = len(runs)
        row.update({
            "mention_runs": mentions,
            "eligible_runs": eligible_runs,
            "mention_rate": round(mentions / eligible_runs * 100, 1) if eligible_runs else 0,
            "average_rank": round(sum(ranks) / len(ranks), 2) if ranks else 0,
            "best_rank": min(ranks) if ranks else 0,
            "rank_counts": counted([str(rank) for rank in ranks]),
        })
        output.append(row)
    return sorted(output, key=lambda item: (-item["mention_runs"], item["average_rank"], item["brand"]))


def canonical_brand(value: str) -> str:
    compact = value.replace(" ", "").casefold()
    if compact in {"梵玢", "梵玢fbcy", "fbcy"}:
        return "梵玢 FBCY"
    if compact in {"liese花王莉婕", "花王莉婕"}:
        return "花王莉婕 LIESE"
    if compact in {"卡维拉", "cavilla卡维拉", "卡维拉cavilla"}:
        return "卡维拉 CAVILLA"
    return value.strip()


def compact_entity(value: str) -> str:
    return re.sub(r"[\s\u00b7\uff08\uff09\uff1a:\u3001\uff0c\u3002/]+", "", value).casefold()


def evidence_matches_brand(brand: str, evidence: str) -> bool:
    aliases = {
        "梵玢 FBCY": ("梵玢", "FBCY"),
        "花王莉婕 LIESE": ("花王莉婕", "LIESE"),
        "卡维拉 CAVILLA": ("卡维拉", "CAVILLA"),
    }.get(brand, (brand,))
    compact_evidence = compact_entity(evidence)
    return any(compact_entity(alias) in compact_evidence for alias in aliases)


def canonical_product(brand: str, product_name: str, category: str) -> str:
    compact = product_name.replace(" ", "")
    compact = compact.replace("植萃奢护型染发剂", "植萃奢护染发剂")
    if category == "眉毛" and brand in {"依思佩尔", "蓝鲸眼泪"}:
        return "眉毛精华液"
    if category == "染发" and brand == "欧莱雅" and "臻萃" in compact:
        return "臻萃精油染发剂"
    if brand == "后兔" and category == "睫毛":
        return "乌斯玛草睫毛营养液"
    return compact


def canonicalize_product_names(runs: list[dict]) -> None:
    names: dict[tuple[str, str], set[str]] = defaultdict(set)
    for run in runs:
        for product in run["products"]:
            product["raw_brand"] = product.get("raw_brand") or product["brand"]
            product["brand"] = canonical_brand(product["brand"])
            product["product_name"] = canonical_product(
                product["brand"], product["product_name"], product["category"]
            )
            names[(product["brand"].casefold(), product["category"])].add(product["product_name"])
    for run in runs:
        for product in run["products"]:
            candidates = names[(product["brand"].casefold(), product["category"])]
            supersets = [name for name in candidates if product["product_name"] in name]
            if supersets:
                product["product_name"] = max(supersets, key=len)
        unique_products = []
        seen_products: set[tuple[str, str]] = set()
        for product in run["products"]:
            if not evidence_matches_brand(product["brand"], product["evidence"]):
                continue
            key = (product["brand"].casefold(), product["product_name"].casefold())
            if key in seen_products:
                continue
            seen_products.add(key)
            unique_products.append(product)
        unique_products.sort(key=lambda item: item["position"])
        for rank, product in enumerate(unique_products, 1):
            product["rank"] = rank
        run["products"] = unique_products
        run["brands"] = list(dict.fromkeys(product["brand"] for product in unique_products))


def empty_payload() -> dict:
    return {"generated_at": datetime.now(BEIJING).isoformat(timespec="seconds"), "total_runs": 0, "successful_runs": 0, "total_sources": 0, "unique_sources": 0, "question_count": 0, "device_count": 0, "date_range": "等待采集", "questions": [], "devices": [], "runs": [], "daily": [], "top_media": [], "source_types": [], "brands": [], "products": []}


def _conversation_mismatch(record: dict[str, Any]) -> str:
    expected = re.sub(r"\s+", "", str(record.get("question") or ""))
    explicit = re.sub(r"\s+", "", str(record.get("conversation_question") or ""))
    if explicit:
        return "" if explicit == expected else f"当前网页问题为“{explicit}”"
    raw = str(record.get("web_body") or record.get("reply") or "")
    # Legacy full-page captures contain the current prompt immediately before
    # “已阅读 N 个网页”; sidebar titles do not use the “推荐一款” wording.
    match = re.search(r"(推荐一款[^。！？\n]{1,30}?)\s*已阅读\s*\d+\s*个网页", raw)
    if not match:
        return ""
    actual = re.sub(r"\s+", "", match.group(1))
    return "" if actual == expected else f"历史网页正文实际问题为“{actual}”"


def build() -> dict:
    records = []
    if RESULTS.exists():
        for line in RESULTS.read_text(encoding="utf-8").splitlines():
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    quarantine = []
    success_records = []
    for record in records:
        if record.get("status") != "success":
            continue
        reason = _conversation_mismatch(record)
        if reason:
            quarantine.append({"round": record.get("round"), "question": record.get("question"),
                               "page_url": record.get("page_url"), "reason": reason})
            continue
        success_records.append(record)
    # 原始回答完整保留给面板；预处理文本只进入产品识别。
    for record in success_records:
        record["_original_body"] = str(record.get("web_body") or record.get("reply") or "")
        record["_analysis_body"] = _preprocess_body_for_ai(record["_original_body"])
    # AI 提取产品
    ai_results: dict[str, dict] = {}
    ai_meta: dict = {"model": "", "new_analyses": 0, "cached_analyses": 0, "usage": {}}
    ai_error = ""
    if success_records:
        try:
            ai_results, ai_meta = analyze_deepseek_records(success_records)
        except Exception as exc:
            ai_error = str(exc)[:500]
    # 构建 runs
    runs = []
    seen = set()
    for record in success_records:
        rid = record_id(record)
        if rid in seen:
            continue
        seen.add(rid)
        sources = []
        source_seen = set()
        for raw in record.get("sources") or []:
            url = str(raw.get("url") or raw.get("href") or "")
            stable = canonical_url(url)
            if not stable or stable in source_seen:
                continue
            source_seen.add(stable)
            domain = urlparse(url).netloc.lower().removeprefix("www.")
            title = str(raw.get("title") or "").strip()
            sources.append({"title": title, "url": url, "canonical_url": stable, "domain": domain, "media": media_name(domain), "type": source_type(domain, title)})
        ai = ai_results.get(_body_hash(record.get("_original_body", "")), {})
        products = [dict(product) for product in (ai.get("products") or [])]
        runs.append({"run_id": rid, "sequence": len(runs) + 1, "round": int(record.get("round") or 0), "serial": str(record.get("serial") or "DeepSeek Web"), "question": str(record.get("question") or "未知问题"), "reply": str(record.get("reply") or ""), "web_body": str(record.get("web_body") or ""), "started_at": str(record.get("started_at") or ""), "finished_at": str(record.get("finished_at") or ""), "day": day(record.get("finished_at") or record.get("started_at") or ""), "status": "success", "sources": sources, "products": products, "brands": list(dict.fromkeys(product["brand"] for product in products)), "ai_cached": bool(ai.get("cached"))})
    canonicalize_product_names(runs)
    payload = empty_payload()
    all_sources = [source for run in runs for source in run["sources"]]
    questions = []
    for question in dict.fromkeys(run["question"] for run in runs):
        selected = [run for run in runs if run["question"] == question]
        refs = [source for run in selected for source in run["sources"]]
        questions.append({"question": question, "runs": len(selected), "sources": len(refs), "unique_sources": len({item["canonical_url"] for item in refs}), "brands": counted([brand for run in selected for brand in set(run["brands"])]), "products": product_summary(selected)})
    devices = []
    for serial in dict.fromkeys(run["serial"] for run in runs):
        selected = [run for run in runs if run["serial"] == serial]
        devices.append({"serial": serial, "runs": len(selected), "sources": sum(len(run["sources"]) for run in selected), "latest": max((run["finished_at"] for run in selected), default="")})
    daily = []
    for date in sorted({run["day"] for run in runs if run["day"]}, reverse=True):
        selected = [run for run in runs if run["day"] == date]
        refs = [source for run in selected for source in run["sources"]]
        daily.append({"date": date, "runs": len(selected), "successful_runs": len(selected), "sources": len(refs), "unique_sources": len({item["canonical_url"] for item in refs}), "question_count": len({run["question"] for run in selected}), "device_count": len({run["serial"] for run in selected}), "product_mentions": sum(len(run["products"]) for run in selected), "brands": counted([brand for run in selected for brand in set(run["brands"])]), "media": counted(item["media"] for item in refs), "types": counted(item["type"] for item in refs), "questions": counted(run["question"] for run in selected)})
    times = [run["finished_at"] for run in runs if run["finished_at"]]
    date_range = "等待采集"
    if times:
        first, last = min(times)[:10], max(times)[:10]
        date_range = first if first == last else f"{first} 至 {last}"
    payload.update({"total_runs": len(records), "successful_runs": len(runs), "total_sources": len(all_sources), "unique_sources": len({item["canonical_url"] for item in all_sources}), "question_count": len(questions), "device_count": len(devices), "date_range": date_range, "questions": questions, "devices": devices, "runs": runs, "daily": daily, "top_media": counted(item["media"] for item in all_sources), "source_types": counted(item["type"] for item in all_sources), "brands": counted([brand for run in runs for brand in set(run["brands"])]), "products": product_summary(runs), "quality_quarantine": {"count": len(quarantine), "records": quarantine}, "ai_analysis": {**ai_meta, "status": "error" if ai_error else "ready", "error": ai_error}})
    return payload


def main() -> None:
    payload = build()
    OUTPUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"DeepSeek 面板数据：{payload['successful_runs']} 轮，{payload['total_sources']} 条信源，{len(payload['products'])} 个产品")


if __name__ == "__main__":
    main()
