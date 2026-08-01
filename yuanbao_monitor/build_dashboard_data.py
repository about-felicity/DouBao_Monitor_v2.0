"""把元宝 JSONL 结果聚合为 React 面板使用的数据文件。"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from yuanbao_brand_ai import analyze_records, record_hash


BASE_DIR = Path(__file__).resolve().parent
RESULTS = BASE_DIR / "yuanbao_results.jsonl"
OUTPUT = BASE_DIR / "dashboard" / "public" / "data" / "dashboard.json"

MEDIA_NAMES = {
    "mp.weixin.qq.com": "微信公众号", "weixin.qq.com": "微信",
    "news.qq.com": "腾讯新闻", "new.qq.com": "腾讯新闻", "qq.com": "腾讯网",
    "weibo.com": "微博", "toutiao.com": "今日头条",
    "baidu.com": "百度", "zhihu.com": "知乎", "xiaohongshu.com": "小红书",
    "douyin.com": "抖音", "bilibili.com": "哔哩哔哩", "jd.com": "京东",
    "taobao.com": "淘宝", "tmall.com": "天猫", "163.com": "网易", "ifeng.com": "凤凰网",
}
TRACKING_PARAMS = {"refer", "srsltid", "chksm", "from", "source", "spm"}
BEIJING_TZ = timezone(timedelta(hours=8))


def beijing_day(value: str) -> str:
    """把采集时间稳定归入北京时间自然日。"""
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=BEIJING_TZ)
        return parsed.astimezone(BEIJING_TZ).date().isoformat()
    except ValueError:
        match = re.match(r"(\d{4}-\d{2}-\d{2})", text)
        return match.group(1) if match else ""


def media_for(domain: str) -> str:
    for key, value in MEDIA_NAMES.items():
        if domain == key or domain.endswith("." + key):
            return value
    return domain.removeprefix("www.") or "未知媒体"


def canonical_url(url: str) -> str:
    """移除锚点和常见追踪参数，用稳定文章地址做跨轮统计。"""
    try:
        parsed = urlparse(url.strip())
        query = [
            (key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True)
            if key.lower() not in TRACKING_PARAMS and not key.lower().startswith("utm_")
        ]
        return urlunparse((
            parsed.scheme.lower(), parsed.netloc.lower(), parsed.path.rstrip("/") or "/",
            parsed.params, urlencode(query), "",
        ))
    except ValueError:
        return url.strip()


def source_type(domain: str, title: str) -> str:
    haystack = f"{domain} {title}".lower()
    if any(key in haystack for key in ("douyin", "bilibili", "kuaishou", "youtube", "视频")):
        return "视频"
    if any(key in haystack for key in ("weibo", "xiaohongshu", "微博", "小红书")):
        return "社交"
    if any(key in haystack for key in ("jd.com", "taobao", "tmall", "京东", "淘宝", "商城")):
        return "电商"
    return "文章"


def counted(items: list[str], limit: int | None = None) -> list[dict]:
    return [{"name": name, "count": count} for name, count in Counter(items).most_common(limit)]


def source_link_summary(runs: list[dict], kind: str | None = None, limit: int | None = None) -> list[dict]:
    grouped: dict[str, dict] = {}
    for run in runs:
        seen_in_run: set[str] = set()
        for source in run["sources"]:
            if (kind and source["type"] != kind) or not source["canonical_url"]:
                continue
            key = source["canonical_url"]
            if key in seen_in_run:
                continue
            seen_in_run.add(key)
            row = grouped.setdefault(key, {
                "url": source["url"], "canonical_url": key, "title": source["title"],
                "media": source["media"], "type": source["type"],
                "count": 0, "rounds": [], "questions": [],
            })
            row["count"] += 1
            row["rounds"].append(run["sequence"])
            row["questions"].append(run["question"])
            if not row["title"] and source["title"]:
                row["title"] = source["title"]
    rows = list(grouped.values())
    for row in rows:
        row["rounds"] = sorted(set(row["rounds"]))
        row["questions"] = list(dict.fromkeys(row["questions"]))
    rows.sort(key=lambda item: (-item["count"], item["media"], item["canonical_url"]))
    return rows[:limit] if limit else rows


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


def question_category(question: str) -> str:
    if "染发" in question:
        return "染发"
    if "眉毛" in question:
        return "眉毛"
    if "睫毛" in question:
        return "睫毛"
    return "其他"


def record_id(record: dict) -> str:
    stable = "\0".join((
        str(record.get("serial") or ""),
        str(record.get("round") or ""),
        str(record.get("started_at") or ""),
        str(record.get("finished_at") or ""),
        str(record.get("question") or ""),
        str(record.get("web_body") or record.get("reply") or ""),
    ))
    return hashlib.sha256(stable.encode("utf-8")).hexdigest()[:20]


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
    return re.sub(r"[\s·（）()：:、，。/]+", "", value).casefold()


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
    """同品牌同品类的短名称若被另一名称完整包含，统一为信息更完整的正文名称。"""
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


def main() -> None:
    raw_records = []
    if RESULTS.exists():
        for line in RESULTS.read_text(encoding="utf-8").splitlines():
            try:
                raw_records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    success_records = []
    seen_record_ids: set[str] = set()
    for record in raw_records:
        if record.get("status") != "success":
            continue
        unique_id = record_id(record)
        if unique_id in seen_record_ids:
            continue
        seen_record_ids.add(unique_id)
        success_records.append(record)

    ai_results = {}
    ai_meta = {"model": "", "new_analyses": 0, "cached_analyses": 0, "usage": {}}
    ai_error = ""
    if success_records:
        try:
            ai_results, ai_meta = analyze_records(success_records)
        except Exception as exc:
            # 已缓存数据仍可通过下一次运行恢复；错误中不会包含密钥。
            ai_error = str(exc)[:700]

    runs = []
    for sequence, record in enumerate(success_records, 1):
        sources = []
        seen_source_urls: set[str] = set()
        for source in record.get("sources") or []:
            url = str(source.get("url") or "")
            stable_url = canonical_url(url)
            if not stable_url or stable_url in seen_source_urls:
                continue
            seen_source_urls.add(stable_url)
            domain = urlparse(url).netloc.lower()
            title = str(source.get("title") or "").strip()
            sources.append({
                "title": title, "url": url, "canonical_url": stable_url,
                "domain": domain.removeprefix("www."),
                "media": media_for(domain), "type": source_type(domain, title),
            })
        ai = ai_results.get(record_hash(record), {})
        products = [dict(product) for product in (ai.get("products") or [])]
        runs.append({
            "run_id": record_id(record),
            "sequence": sequence,
            "round": int(record.get("round") or 0),
            "serial": str(record.get("serial") or "未标记设备"),
            "question": str(record.get("question") or "未知问题"),
            "reply": str(record.get("reply") or ""),
            "web_body": str(record.get("web_body") or ""),
            "started_at": str(record.get("started_at") or ""),
            "finished_at": str(record.get("finished_at") or ""),
            "day": beijing_day(record.get("finished_at") or record.get("started_at") or ""),
            "status": str(record.get("status") or ""),
            "sources": sources,
            "raw_source_count": len(record.get("sources") or []),
            "products": products,
            "brands": list(dict.fromkeys(product["brand"] for product in products)),
            "ai_cached": bool(ai.get("cached")),
        })
    canonicalize_product_names(runs)

    questions = []
    for question in dict.fromkeys(run["question"] for run in runs):
        question_runs = [run for run in runs if run["question"] == question]
        refs = [source for run in question_runs for source in run["sources"]]
        questions.append({
            "question": question, "runs": len(question_runs), "sources": len(refs),
            "unique_sources": len({ref["canonical_url"] for ref in refs if ref["canonical_url"]}),
            "avg_sources": round(len(refs) / len(question_runs), 2) if question_runs else 0,
            "media": counted([ref["media"] for ref in refs]),
            "types": counted([ref["type"] for ref in refs]),
            "brands": counted([brand for run in question_runs for brand in set(run["brands"])]),
            "products": product_summary(question_runs),
        })
    questions.sort(key=lambda item: item["sources"], reverse=True)

    devices = []
    for serial in dict.fromkeys(run["serial"] for run in runs):
        device_runs = [run for run in runs if run["serial"] == serial]
        devices.append({
            "serial": serial, "runs": len(device_runs),
            "sources": sum(len(run["sources"]) for run in device_runs),
            "latest": max((run["finished_at"] for run in device_runs), default=""),
        })

    all_sources = [source for run in runs for source in run["sources"]]
    times = [run["finished_at"] for run in runs if run["finished_at"]]
    date_range = "等待采集"
    if times:
        start, end = min(times)[:10], max(times)[:10]
        date_range = start if start == end else f"{start} 至 {end}"

    daily = []
    for day in sorted({run["day"] for run in runs if run["day"]}, reverse=True):
        day_runs = [run for run in runs if run["day"] == day]
        day_sources = [source for run in day_runs for source in run["sources"]]
        daily.append({
            "date": day,
            "runs": len(day_runs),
            "successful_runs": len(day_runs),
            "sources": len(day_sources),
            "unique_sources": len({
                source["canonical_url"] for source in day_sources if source["canonical_url"]
            }),
            "question_count": len({run["question"] for run in day_runs}),
            "device_count": len({run["serial"] for run in day_runs}),
            "product_mentions": sum(len(run["products"]) for run in day_runs),
            "brands": counted([
                brand for run in day_runs for brand in set(run["brands"])
            ], 8),
            "media": counted([source["media"] for source in day_sources], 8),
            "types": counted([source["type"] for source in day_sources]),
            "questions": counted([run["question"] for run in day_runs]),
        })

    payload = {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "total_runs": len(raw_records), "successful_runs": len(runs),
        "total_sources": len(all_sources),
        "unique_sources": len({source["canonical_url"] for source in all_sources if source["canonical_url"]}),
        "question_count": len(questions), "device_count": len(devices), "date_range": date_range,
        "questions": questions, "devices": devices,
        "daily": daily,
        "top_media": counted([source["media"] for source in all_sources]),
        "source_types": counted([source["type"] for source in all_sources]),
        "brands": counted([brand for run in runs for brand in set(run["brands"])]),
        "products": product_summary(runs),
        "source_links": source_link_summary(runs),
        "top_article_links": source_link_summary(runs, "文章", 10),
        "top_video_links": source_link_summary(runs, "视频", 10),
        "ai_analysis": {**ai_meta, "status": "error" if ai_error else "ready", "error": ai_error},
        "metric_notes": {
            "mention_rate": "提及该产品的轮次 ÷ 同品类问题的有效轮次",
            "body_rank": "按产品首次证据在回答正文中的出现顺序计算",
            "citation_count": "同一链接每轮最多计 1 次；跨轮再次引用会累计",
        },
        "runs": sorted(runs, key=lambda run: run["sequence"]),
    }
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        f"已生成面板数据：{len(runs)} 轮，{len(all_sources)} 条信源，"
        f"{len(payload['products'])} 个产品 -> {OUTPUT}"
    )
    if ai_error:
        print(f"AI 分析暂未完成：{ai_error}")
    elif ai_meta.get("new_analyses"):
        usage = ai_meta.get("usage") or {}
        print(f"DeepSeek 新分析 {ai_meta['new_analyses']} 篇，token {usage.get('total_tokens', 0)}")


if __name__ == "__main__":
    main()
