from __future__ import annotations

import csv
import json
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from monitor_core.owned_products import (
    OWN_PRODUCT_SCHEMA_VERSION,
    brands_for_products,
    own_product_mentions,
)

try:
    import jieba
except ImportError:  # 部署未安装时仍可使用内置保守分词。
    jieba = None


BEIJING = timezone(timedelta(hours=8))
TRACKING = {"refer", "from", "source", "spm", "srsltid", "share_token"}
VIDEO_DOMAINS = ("douyin.com", "iesdouyin.com", "bilibili.com", "youtube.com", "kuaishou.com", "ixigua.com")
SHOP_DOMAINS = ("jd.com", "taobao.com", "tmall.com", "yangkeduo.com")
SOCIAL_DOMAINS = ("xiaohongshu.com", "weibo.com")
MEDIA_NAMES = {"zhihu.com": "知乎", "xiaohongshu.com": "小红书", "weibo.com": "微博", "bilibili.com": "哔哩哔哩", "douyin.com": "抖音", "iesdouyin.com": "抖音", "jd.com": "京东", "taobao.com": "淘宝", "tmall.com": "天猫", "baidu.com": "百度", "qq.com": "腾讯网", "163.com": "网易", "ifeng.com": "凤凰网"}
KEYWORD_STOP = {"推荐", "一款", "什么", "怎么", "可以", "一个", "使用", "产品", "品牌", "最新", "真的", "这款", "哪些", "比较", "选择", "效果", "十大", "排行榜", "测评"}
BRAND_STOP = {"家用", "小助手", "泡沫染发", "植物染发", "染发", "染发剂", "染发膏", "角蛋白", "米诺地尔", "生物素", "多肽", "精华液", "洗发水", "护发素", "防晒", "面膜", "水杨酸", "二硫化硒", "烟酰胺", "玻尿酸", "PCA锌", "辛酰甘氨酸", "氨基酸", "无硅油"}
PARTICLES = "的了是一在于和及与或到用为把被从对跟等很更最也都还就"
ENGLISH_STOP = {"the", "and", "for", "with", "from", "best", "top", "review", "reviews"}
ROOT = Path(__file__).resolve().parent.parent
CONTENT_INDEX_PATH = ROOT / "doubao_source_content_index.json"


def _compact(value: str) -> str:
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", str(value or "").casefold())


@lru_cache(maxsize=2)
def _content_index(stamp: int) -> dict[str, Any]:
    del stamp
    try:
        value = json.loads(CONTENT_INDEX_PATH.read_text(encoding="utf-8-sig"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, json.JSONDecodeError):
        return {}


def content_index() -> dict[str, Any]:
    try:
        stamp = CONTENT_INDEX_PATH.stat().st_mtime_ns
    except OSError:
        stamp = 0
    value = _content_index(stamp)
    entries = value.get("entries")
    return entries if isinstance(entries, dict) else value


def brand_vocabulary() -> list[dict[str, Any]]:
    try:
        import doubao_brand_settings
        return list(doubao_brand_settings.vocabulary())
    except (ImportError, OSError, ValueError):
        return []


def owned_brand_vocabulary() -> list[dict[str, Any]]:
    return [item for item in brand_vocabulary() if item.get("group") == "owned"]


def canonical_question(value: str) -> str:
    from monitor_core.recommendation_questions import canonical_recommendation_question
    recommendation = canonical_recommendation_question(value) if "推荐" in str(value or "") else ""
    if recommendation:
        return recommendation
    try:
        from doubao_question_aliases import canonical_question_name
        return canonical_question_name(value) or str(value or "未知问题")
    except ImportError:
        return str(value or "未知问题")


def valid_brand(value: str) -> bool:
    text = str(value or "").strip()
    compact = _compact(text)
    if not compact or compact in {_compact(item) for item in BRAND_STOP}:
        return False
    if len(compact) < 2 or len(compact) > 40:
        return False
    if re.fullmatch(r"\d+(?:\.\d+)?(?:ml|g|mg|%)?", compact, re.I):
        return False
    return True


def canonical_url(value: str) -> str:
    try:
        parsed = urlparse(str(value or "").strip())
        query = [(key, val) for key, val in parse_qsl(parsed.query, keep_blank_values=True) if key.lower() not in TRACKING and not key.lower().startswith("utm_")]
        return urlunparse((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path.rstrip("/") or "/", parsed.params, urlencode(query), ""))
    except ValueError:
        return str(value or "").strip()


def beijing_day(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=BEIJING)
        return parsed.astimezone(BEIJING).date().isoformat()
    except ValueError:
        match = re.match(r"\d{4}-\d{2}-\d{2}", text)
        return match.group(0) if match else ""


def source_type(domain: str, title: str = "") -> str:
    haystack = f"{domain} {title}".lower()
    if any(item in haystack for item in VIDEO_DOMAINS) or "视频" in title:
        return "视频"
    if any(item in haystack for item in SHOP_DOMAINS):
        return "电商"
    if any(item in haystack for item in SOCIAL_DOMAINS):
        return "社交"
    return "文章"


def media_name(domain: str) -> str:
    clean = domain.lower().removeprefix("www.")
    for suffix, name in MEDIA_NAMES.items():
        if clean == suffix or clean.endswith("." + suffix):
            return name
    return clean or "未知媒体"


def normalize_source(raw: dict[str, Any]) -> dict[str, Any]:
    url = str(raw.get("url") or raw.get("href") or "")
    stable = canonical_url(url)
    domain = urlparse(url).netloc.lower().removeprefix("www.")
    title = str(raw.get("title") or "").strip()
    kind = str(raw.get("type") or raw.get("source_type") or "").strip() or source_type(domain, title)
    return {"title": title, "url": url, "canonical_url": stable, "domain": domain,
            "media": str(raw.get("media") or "").strip() or media_name(domain), "type": kind,
            "brand_mentions": list(raw.get("brand_mentions") or []),
            "owned_brands": list(raw.get("owned_brands") or []),
            "own_products": list(raw.get("own_products") or []),
            "own_brand": bool(raw.get("own_brand")), "brand_match_scope": str(raw.get("brand_match_scope") or "")}


def load_doubao_runs(refs_path: Path, answers_path: Path, products_path: Path | None = None) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    source_urls: dict[str, set[str]] = {}
    if answers_path.exists():
        with answers_path.open("r", encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                key = str(row.get("run_no") or "")
                grouped[key] = {"model_id": "doubao", "run_id": f"doubao-{key}", "sequence": int(key or 0),
                    "question": canonical_question(row.get("question") or "未知问题"), "finished_at": str(row.get("captured_at") or row.get("run_time") or row.get("extracted_at") or ""),
                    "day": beijing_day(row.get("captured_at") or row.get("run_time") or ""), "serial": str(row.get("source_device") or row.get("mumu_serial") or "远端豆包"),
                    "answer": str(row.get("answer_text") or ""), "status": "success", "sources": [], "products": [], "brands": []}
                source_urls[key] = set()
    if refs_path.exists():
        with refs_path.open("r", encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                key = str(row.get("run_no") or "")
                run = grouped.setdefault(key, {"model_id": "doubao", "run_id": f"doubao-{key}", "sequence": int(key or 0),
                    "question": canonical_question(row.get("question") or "未知问题"), "finished_at": str(row.get("captured_at") or row.get("run_time") or row.get("extracted_at") or ""),
                    "day": beijing_day(row.get("captured_at") or row.get("run_time") or ""), "serial": str(row.get("source_device") or row.get("mumu_serial") or "远端豆包"),
                    "answer": "", "status": "success", "sources": [], "products": [], "brands": []})
                source = normalize_source(row)
                seen = source_urls.setdefault(key, set())
                if source["canonical_url"] and source["canonical_url"] not in seen:
                    seen.add(source["canonical_url"])
                    run["sources"].append(source)
    if products_path and products_path.exists():
        with products_path.open("r", encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                run = grouped.get(str(row.get("run_no") or ""))
                if not run:
                    continue
                product = {"brand": str(row.get("brand_name") or "").strip(),
                           "product_name": str(row.get("product_name") or "").strip(),
                           "rank": int(row.get("product_index") or 0),
                           "evidence": str(row.get("evidence") or "").strip()}
                if product["brand"] or product["product_name"]:
                    run["products"].append(product)
                    if product["brand"] and product["brand"] not in run["brands"]:
                        run["brands"].append(product["brand"])
    return sorted(grouped.values(), key=lambda item: item["sequence"])


def load_generic_runs(model_id: str, stats: dict[str, Any]) -> list[dict[str, Any]]:
    output = []
    for raw in stats.get("runs") or []:
        seen = set()
        sources = []
        for item in raw.get("sources") or []:
            source = normalize_source(item)
            if source["canonical_url"] and source["canonical_url"] not in seen:
                seen.add(source["canonical_url"]); sources.append(source)
        output.append({"model_id": model_id, "run_id": str(raw.get("run_id") or f"{model_id}-{len(output)+1}"),
            "sequence": int(raw.get("sequence") or raw.get("round") or len(output)+1), "question": canonical_question(raw.get("question") or "未知问题"),
            "finished_at": str(raw.get("finished_at") or ""), "day": str(raw.get("day") or beijing_day(raw.get("finished_at") or "")),
            "serial": str(raw.get("serial") or model_id), "answer": str(raw.get("web_body") or raw.get("reply") or ""),
            "status": str(raw.get("status") or "success"), "sources": sources,
            "products": list(raw.get("products") or []), "brands": list(raw.get("brands") or [])})
    return output


@lru_cache(maxsize=50000)
def _title_terms(title: str) -> tuple[str, ...]:
    terms = {item.lower() for item in re.findall(r"[A-Za-z][A-Za-z0-9+-]{2,}", title)
             if item.lower() not in ENGLISH_STOP}
    if jieba is not None:
        for token in jieba.lcut(title, cut_all=False):
            token = token.strip().casefold()
            if re.fullmatch(r"[\u4e00-\u9fff]{2,8}", token):
                terms.add(token)
    else:
        for chunk in re.findall(r"[\u4e00-\u9fff]{2,}", title):
            if len(chunk) <= 8:
                terms.add(chunk)
            else:
                for width in (2, 3, 4):
                    terms.update(chunk[index:index+width] for index in range(len(chunk)-width+1))
    return tuple(terms)


def keyword_counts(titles: Iterable[str], limit: int = 18) -> list[dict[str, Any]]:
    counts: Counter[str] = Counter()
    for title in titles:
        per_title = _title_terms(str(title or ""))
        for term in per_title:
            if term not in KEYWORD_STOP and not any(stop in term for stop in ("推荐一", "排行榜", "怎么样")):
                counts[term] += 1
    result = []
    for term, count in counts.most_common():
        if count < 2 or term[0] in PARTICLES or term[-1] in PARTICLES:
            continue
        if any(term != longer and term in longer and count <= longer_count
               for longer, longer_count in counts.items() if len(longer) > len(term)):
            continue
        result.append({"term": term, "count": count})
        if len(result) >= limit:
            break
    return result


def _top_sources(runs: list[dict[str, Any]], kind: str) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for run in runs:
        for source in run["sources"]:
            normalized_kind = "视频" if source["type"] == "视频" else "文章"
            if normalized_kind != kind:
                continue
            row = grouped.setdefault(source["canonical_url"], {**source, "count": 0, "run_ids": set()})
            if run["run_id"] not in row["run_ids"]:
                row["run_ids"].add(run["run_id"]); row["count"] += 1
    rows = sorted(grouped.values(), key=lambda item: (-item["count"], item["title"], item["canonical_url"]))[:10]
    for row in rows:
        row.pop("run_ids", None)
    return rows


def _product_fields(raw: dict[str, Any]) -> tuple[str, str, int]:
    brand = str(raw.get("brand") or raw.get("brand_name") or "").strip()
    product = str(raw.get("product_name") or raw.get("name") or "").strip()
    compact_brand = _compact(brand)
    if compact_brand:
        prefix = re.compile(
            r"^\s*" + r"[^0-9A-Za-z\u4e00-\u9fff]*".join(re.escape(char) for char in compact_brand),
            re.I,
        )
        for _ in range(4):
            match = prefix.match(product)
            if not match:
                break
            product = product[match.end():].lstrip(" ·-_/～|（）()")
        words = re.findall(r"[A-Za-z0-9]+", brand)
        if len(words) > 1 and len(words[-1]) >= 3:
            product = re.sub(r"^\s*" + re.escape(words[-1]), "", product, count=1, flags=re.I).lstrip(" ·-_/～|（）()")
    try:
        rank = int(raw.get("rank") or raw.get("product_index") or 0)
    except (TypeError, ValueError):
        rank = 0
    return brand, product, rank


def _catalogs(runs_by_model: dict[str, list[dict[str, Any]]]) -> tuple[dict[str, set[str]], dict[str, tuple[str, str]]]:
    brand_aliases: dict[str, set[str]] = defaultdict(set)
    product_catalog: dict[str, tuple[str, str]] = {}
    canonical_by_key: dict[str, str] = {}
    raw_brands: list[str] = []

    for item in brand_vocabulary():
        canonical = str(item.get("name") or "").strip()
        if not valid_brand(canonical):
            continue
        aliases = {
            str(alias or "").strip()
            for alias in item.get("aliases") or []
            if str(alias or "").strip()
        } | {canonical}
        brand_aliases[canonical].update(aliases)
        for alias in aliases:
            key = _compact(alias)
            if key:
                canonical_by_key[key] = canonical

    for runs in runs_by_model.values():
        for run in runs:
            raw_brands.extend(str(brand or "").strip() for brand in run.get("brands") or [])
            for raw in run.get("products") or []:
                brand, _product, _rank = _product_fields(raw)
                if brand:
                    raw_brands.append(brand)

    variants: dict[str, Counter[str]] = defaultdict(Counter)
    for name in raw_brands:
        if valid_brand(name) and _compact(name) not in canonical_by_key:
            variants[_compact(name)][name] += 1
    for key, counts in variants.items():
        canonical_by_key[key] = sorted(
            counts,
            key=lambda name: (-counts[name], name.count(" "), len(name), name.casefold()),
        )[0]

    def add_brand(value: str) -> str:
        name = str(value or "").strip()
        if not valid_brand(name):
            return ""
        key = _compact(name)
        canonical = canonical_by_key.setdefault(key, name)
        brand_aliases[canonical].add(name)
        return canonical
    for runs in runs_by_model.values():
        for run in runs:
            for brand in run.get("brands") or []:
                add_brand(str(brand or ""))
            for raw in run.get("products") or []:
                brand, product, _ = _product_fields(raw)
                brand = add_brand(brand)
                label = " ".join(part for part in (brand, product) if part).strip()
                if brand and product and label:
                    product_catalog[_compact(label)] = (brand, product)
    return brand_aliases, product_catalog


def _brand_matcher(brand_aliases: dict[str, set[str]]) -> tuple[re.Pattern[str] | None, dict[str, set[str]]]:
    aliases: dict[str, set[str]] = defaultdict(set)
    for brand, values in brand_aliases.items():
        for value in values | {brand}:
            token = _compact(value)
            if len(token) >= 2:
                aliases[token].add(brand)
    if not aliases:
        return None, aliases
    pattern = re.compile("|".join(re.escape(token) for token in sorted(aliases, key=len, reverse=True)))
    return pattern, aliases


def _mentions(text: str, matcher: tuple[re.Pattern[str] | None, dict[str, set[str]]]) -> list[str]:
    pattern, aliases = matcher
    if pattern is None:
        return []
    found: set[str] = set()
    for match in pattern.finditer(_compact(text)):
        found.update(aliases.get(match.group(0), ()))
    return sorted(found)


def _enrich_runs(runs_by_model: dict[str, list[dict[str, Any]]], brand_aliases: dict[str, set[str]], product_catalog: dict[str, tuple[str, str]], matcher: tuple[re.Pattern[str] | None, dict[str, set[str]]]) -> None:
    canonical_by_fold = {alias.casefold(): brand for brand, aliases in brand_aliases.items() for alias in aliases | {brand}}
    owned_names = {
        canonical_by_fold.get(str(item.get("name") or "").strip().casefold(), str(item.get("name") or "").strip())
        for item in owned_brand_vocabulary() if str(item.get("name") or "").strip()
    }
    index = content_index()
    for runs in runs_by_model.values():
        for run in runs:
            answer = str(run.get("answer") or "")
            explicit_brands = {canonical_by_fold.get(str(item).strip().casefold(), str(item).strip()) for item in run.get("brands") or [] if valid_brand(str(item))}
            products = list(run.get("products") or [])
            explicit_brands.update(_mentions(answer, matcher))
            if not products:
                compact_answer = _compact(answer)
                for token, (brand, product) in product_catalog.items():
                    if token and token in compact_answer:
                        products.append({"brand": brand, "product_name": product, "rank": 0,
                                         "evidence": "跨模型产品词表精确命中"})
            normalized_products = []
            for raw in products:
                item = dict(raw)
                brand, _product, _rank = _product_fields(item)
                if not valid_brand(brand):
                    brand = ""
                canonical = canonical_by_fold.get(brand.casefold(), brand)
                item["brand"] = canonical
                item["brand_name"] = canonical
                normalized_products.append(item)
            run["products"] = normalized_products
            run["brands"] = sorted(explicit_brands)
            for source in run.get("sources") or []:
                title_matches = set(_mentions(source.get("title") or "", matcher))
                body_matches: set[str] = set()
                title_products = set(own_product_mentions(source.get("title") or ""))
                body_products: set[str] = set()
                entry = index.get(source.get("url")) or index.get(source.get("canonical_url")) or {}
                if source.get("type") != "视频":
                    body_matches.update(canonical_by_fold.get(str(item).strip().casefold(), str(item).strip()) for item in entry.get("brand_mentions") or [] if valid_brand(str(item)))
                    body_matches.update(canonical_by_fold.get(str(item).strip().casefold(), str(item).strip()) for item in entry.get("owned_brand_mentions") or [] if valid_brand(str(item)))
                    if not body_matches and entry.get("excerpt"):
                        body_matches.update(_mentions(str(entry.get("excerpt")), matcher))
                    if (
                        entry.get("status") == "ok"
                        and entry.get("extraction_quality") in {"high", "medium"}
                    ):
                        if entry.get("own_product_mentions"):
                            body_products.update(entry.get("own_product_mentions") or [])
                        elif int(entry.get("own_product_schema_version") or 0) == OWN_PRODUCT_SCHEMA_VERSION:
                            pass
                        else:
                            body_products.update(own_product_mentions(entry.get("excerpt") or ""))
                matches = title_matches | body_matches
                product_matches = sorted(title_products | body_products)
                title_owned = title_matches & owned_names
                body_owned = body_matches & owned_names
                owned = sorted(set(brands_for_products(product_matches)) | title_owned | body_owned)
                source["brand_mentions"] = sorted(matches)
                source["title_brand_mentions"] = sorted(title_matches)
                source["body_brand_mentions"] = sorted(body_matches)
                source["own_products"] = product_matches
                source["title_product_mentions"] = sorted(title_products)
                source["body_product_mentions"] = sorted(body_products)
                source["owned_brands"] = owned
                source["own_brand"] = bool(product_matches or owned)
                title_owned_hit = bool(title_products or title_owned)
                body_owned_hit = bool(body_products or body_owned)
                source["brand_match_scope"] = "标题+正文" if title_owned_hit and body_owned_hit else "标题" if title_owned_hit else "正文" if body_owned_hit else ""


def prepare_analytics(
    runs_by_model: dict[str, list[dict[str, Any]]],
) -> tuple[
    dict[str, set[str]],
    dict[str, tuple[str, str]],
    tuple[re.Pattern[str] | None, dict[str, set[str]]],
]:
    """Enrich one versioned run snapshot for reuse across dashboard filters."""
    brand_aliases, product_catalog = _catalogs(runs_by_model)
    matcher = _brand_matcher(brand_aliases)
    _enrich_runs(runs_by_model, brand_aliases, product_catalog, matcher)
    return brand_aliases, product_catalog, matcher


def _dense_ranks(counts: Counter[str]) -> dict[str, int]:
    rank_map: dict[str, int] = {}
    previous = None
    rank = 0
    for position, (name, count) in enumerate(sorted(counts.items(), key=lambda item: (-item[1], item[0])), 1):
        if count != previous:
            rank = position
            previous = count
        rank_map[name] = rank
    return rank_map


def _daily_mentions(runs: list[dict[str, Any]], field: str) -> list[dict[str, Any]]:
    days = sorted({run.get("day") for run in runs if run.get("day")})
    result = []
    previous_ranks: dict[str, int] = {}
    for day in days:
        day_runs = [run for run in runs if run.get("day") == day]
        counts: Counter[str] = Counter()
        rank_values: dict[str, list[int]] = defaultdict(list)
        for run in day_runs:
            seen = set()
            if field == "brands":
                values = [(str(name), 0) for name in run.get("brands") or []]
            else:
                values = []
                for raw in run.get("products") or []:
                    brand, product, rank = _product_fields(raw)
                    if brand and product:
                        values.append((" ".join(part for part in (brand, product) if part).strip(), rank))
            for name, rank in values:
                if not name or name in seen:
                    continue
                seen.add(name); counts[name] += 1
                if rank:
                    rank_values[name].append(rank)
        ranks = _dense_ranks(counts)
        rows = []
        for name, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:30]:
            current_rank = ranks[name]
            previous_rank = previous_ranks.get(name)
            rows.append({"name": name, "mentions": count, "mention_rate": round(count * 100 / len(day_runs), 2) if day_runs else 0,
                         "rank": current_rank, "rank_change": (previous_rank - current_rank) if previous_rank else None,
                         "average_position": round(sum(rank_values[name]) / len(rank_values[name]), 2) if rank_values[name] else None})
        result.append({"date": day, "runs": len(day_runs), "items": rows})
        previous_ranks = ranks
    return result


def _daily_source_analysis(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output = []
    for day in sorted({run.get("day") for run in runs if run.get("day")}):
        day_runs = [run for run in runs if run.get("day") == day]
        sources = [source for run in day_runs for source in run.get("sources") or []]
        articles = [source for source in sources if source.get("type") != "视频"]
        videos = [source for source in sources if source.get("type") == "视频"]
        branded = [source for source in sources if source.get("brand_mentions")]
        title_branded = [source for source in sources if source.get("title_brand_mentions")]
        owned = [source for source in sources if source.get("own_brand")]
        output.append({"date": day, "runs": len(day_runs), "sources": len(sources),
                       "branded_sources": len(branded), "branded_source_rate": round(len(branded) * 100 / len(sources), 2) if sources else 0,
                       "title_branded_sources": len(title_branded), "title_branded_source_rate": round(len(title_branded) * 100 / len(sources), 2) if sources else 0,
                       "owned_sources": len(owned), "owned_source_rate": round(len(owned) * 100 / len(sources), 2) if sources else 0,
                       "article_keywords": keyword_counts((item.get("title") or "" for item in articles), 12),
                       "video_keywords": keyword_counts((item.get("title") or "" for item in videos), 12)})
    return output


def _daily_source_top(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"date": day, "top_articles": _top_sources([run for run in runs if run.get("day") == day], "文章"),
             "top_videos": _top_sources([run for run in runs if run.get("day") == day], "视频")}
            for day in sorted({run.get("day") for run in runs if run.get("day")}, reverse=True)]


def build_analytics(
    model_meta: dict[str, dict[str, Any]],
    runs_by_model: dict[str, list[dict[str, Any]]],
    *,
    question: str = "",
    date: str = "",
    model: str = "",
    prepared: tuple[
        dict[str, set[str]],
        dict[str, tuple[str, str]],
        tuple[re.Pattern[str] | None, dict[str, set[str]]],
    ] | None = None,
) -> dict[str, Any]:
    if prepared is None:
        brand_aliases, product_catalog = _catalogs(runs_by_model)
        matcher = _brand_matcher(brand_aliases)
    else:
        brand_aliases, product_catalog, matcher = prepared
    selected_models = [model] if model and model in runs_by_model else list(model_meta)
    scope_runs = [
        run
        for model_id in selected_models
        for run in runs_by_model.get(model_id, [])
    ]
    all_questions = sorted({run["question"] for run in scope_runs})
    date_scope = [
        run for run in scope_runs
        if not question or run["question"] == question
    ]
    all_dates = sorted(
        {run["day"] for run in date_scope if run["day"]},
        reverse=True,
    )
    models = []
    for model_id in selected_models:
        raw_runs = runs_by_model.get(model_id, [])
        runs = [run for run in raw_runs if (not question or run["question"] == question) and (not date or run["day"] == date)]
        if prepared is None:
            _enrich_runs({model_id: runs}, brand_aliases, product_catalog, matcher)
        sources = [source for run in runs for source in run["sources"]]
        article_titles = [source["title"] for source in sources if source["type"] != "视频" and source["title"]]
        video_titles = [source["title"] for source in sources if source["type"] == "视频" and source["title"]]
        by_question = []
        for item in sorted({run["question"] for run in runs}):
            question_runs = [run for run in runs if run["question"] == item]
            question_sources = [source for run in question_runs for source in run["sources"]]
            by_question.append({"question": item, "runs": len(question_runs), "sources": len(question_sources),
                "unique_sources": len({source["canonical_url"] for source in question_sources}),
                "avg_sources": round(len(question_sources)/len(question_runs), 2) if question_runs else 0})
        daily = []
        for day in sorted({run["day"] for run in runs if run["day"]}, reverse=True):
            day_runs = [run for run in runs if run["day"] == day]
            day_sources = [source for run in day_runs for source in run["sources"]]
            daily.append({"date": day, "runs": len(day_runs), "sources": len(day_sources), "unique_sources": len({source["canonical_url"] for source in day_sources})})
        brand_daily = _daily_mentions(runs, "brands")
        product_daily = _daily_mentions(runs, "products")
        models.append({**model_meta[model_id], "runs": len(runs), "sources": len(sources), "unique_sources": len({source["canonical_url"] for source in sources}),
            "question_count": len({run["question"] for run in runs}), "device_count": len({run["serial"] for run in runs}),
            "source_types": [{"name": name, "count": count} for name, count in Counter(source["type"] for source in sources).most_common()],
            "media": [{"name": name, "count": count} for name, count in Counter(source["media"] for source in sources).most_common(15)],
            "daily": daily, "questions": by_question, "top_articles": _top_sources(runs, "文章"), "top_videos": _top_sources(runs, "视频"),
            "article_keywords": keyword_counts(article_titles), "video_keywords": keyword_counts(video_titles),
            "brand_daily": brand_daily, "product_daily": product_daily,
            "source_brand_daily": _daily_source_analysis(runs),
            "daily_source_top": _daily_source_top(runs),
            "owned_source_count": sum(bool(source.get("own_brand")) for source in sources),
            "branded_source_count": sum(bool(source.get("brand_mentions")) for source in sources),
            "recent_runs": sorted(runs, key=lambda item: item["finished_at"], reverse=True)[:30]})
    return {"generated_at": datetime.now(BEIJING).isoformat(timespec="seconds"), "filters": {"model": model, "question": question, "date": date},
            "models": models, "model_catalog": list(model_meta.values()), "questions": all_questions, "dates": all_dates,
            "analysis_method": {"brand_product": "结构化结果优先，跨模型词表精确补全", "source_brand": "视频仅标题；文章标题加已归档正文", "keywords": "本地确定性分词与停用词过滤", "llm_tokens": 0}}
