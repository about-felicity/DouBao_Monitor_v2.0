from __future__ import annotations

import csv
import hashlib
import json
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse


BEIJING = timezone(timedelta(hours=8))
TRACKING = {"refer", "from", "source", "spm", "srsltid", "share_token"}
VIDEO_DOMAINS = ("douyin.com", "iesdouyin.com", "bilibili.com", "youtube.com", "kuaishou.com", "ixigua.com")
SHOP_DOMAINS = ("jd.com", "taobao.com", "tmall.com", "yangkeduo.com")
SOCIAL_DOMAINS = ("xiaohongshu.com", "weibo.com")
MEDIA_NAMES = {"zhihu.com": "知乎", "xiaohongshu.com": "小红书", "weibo.com": "微博", "bilibili.com": "哔哩哔哩", "douyin.com": "抖音", "iesdouyin.com": "抖音", "jd.com": "京东", "taobao.com": "淘宝", "tmall.com": "天猫", "baidu.com": "百度", "qq.com": "腾讯网", "163.com": "网易", "ifeng.com": "凤凰网"}
KEYWORD_STOP = {"推荐", "一款", "什么", "怎么", "可以", "一个", "使用", "产品", "品牌", "最新", "真的", "这款", "哪些", "比较", "选择", "效果", "十大", "排行榜", "测评"}


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


def normalize_source(raw: dict[str, Any]) -> dict[str, str]:
    url = str(raw.get("url") or raw.get("href") or "")
    stable = canonical_url(url)
    domain = urlparse(url).netloc.lower().removeprefix("www.")
    title = str(raw.get("title") or "").strip()
    kind = str(raw.get("type") or raw.get("source_type") or "").strip() or source_type(domain, title)
    return {"title": title, "url": url, "canonical_url": stable, "domain": domain,
            "media": str(raw.get("media") or "").strip() or media_name(domain), "type": kind}


def load_doubao_runs(refs_path: Path, answers_path: Path) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    if answers_path.exists():
        with answers_path.open("r", encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                key = str(row.get("run_no") or "")
                grouped[key] = {"model_id": "doubao", "run_id": f"doubao-{key}", "sequence": int(key or 0),
                    "question": str(row.get("question") or "未知问题"), "finished_at": str(row.get("captured_at") or row.get("run_time") or row.get("extracted_at") or ""),
                    "day": beijing_day(row.get("captured_at") or row.get("run_time") or ""), "serial": str(row.get("source_device") or row.get("mumu_serial") or "远端豆包"),
                    "answer": str(row.get("answer_text") or ""), "status": "success", "sources": []}
    if refs_path.exists():
        with refs_path.open("r", encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                key = str(row.get("run_no") or "")
                run = grouped.setdefault(key, {"model_id": "doubao", "run_id": f"doubao-{key}", "sequence": int(key or 0),
                    "question": str(row.get("question") or "未知问题"), "finished_at": str(row.get("captured_at") or row.get("run_time") or row.get("extracted_at") or ""),
                    "day": beijing_day(row.get("captured_at") or row.get("run_time") or ""), "serial": str(row.get("source_device") or row.get("mumu_serial") or "远端豆包"),
                    "answer": "", "status": "success", "sources": []})
                source = normalize_source(row)
                if source["canonical_url"] and source["canonical_url"] not in {item["canonical_url"] for item in run["sources"]}:
                    run["sources"].append(source)
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
            "sequence": int(raw.get("sequence") or raw.get("round") or len(output)+1), "question": str(raw.get("question") or "未知问题"),
            "finished_at": str(raw.get("finished_at") or ""), "day": str(raw.get("day") or beijing_day(raw.get("finished_at") or "")),
            "serial": str(raw.get("serial") or model_id), "answer": str(raw.get("web_body") or raw.get("reply") or ""),
            "status": str(raw.get("status") or "success"), "sources": sources})
    return output


def keyword_counts(titles: Iterable[str], limit: int = 18) -> list[dict[str, Any]]:
    counts: Counter[str] = Counter()
    for title in titles:
        per_title = set()
        for english in re.findall(r"[A-Za-z][A-Za-z0-9+-]{2,}", title):
            per_title.add(english.lower())
        for chunk in re.findall(r"[\u4e00-\u9fff]{2,}", title):
            if len(chunk) <= 8:
                per_title.add(chunk)
            else:
                for width in (2, 3, 4):
                    per_title.update(chunk[index:index+width] for index in range(len(chunk)-width+1))
        for term in per_title:
            if term not in KEYWORD_STOP and not any(stop in term for stop in ("推荐一", "排行榜", "怎么样")):
                counts[term] += 1
    return [{"term": term, "count": count} for term, count in counts.most_common(limit) if count >= 2]


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


def build_analytics(model_meta: dict[str, dict[str, Any]], runs_by_model: dict[str, list[dict[str, Any]]], *, question: str = "", date: str = "", model: str = "") -> dict[str, Any]:
    selected_models = [model] if model and model in runs_by_model else list(model_meta)
    all_questions = sorted({run["question"] for runs in runs_by_model.values() for run in runs})
    all_dates = sorted({run["day"] for runs in runs_by_model.values() for run in runs if run["day"]}, reverse=True)
    models = []
    for model_id in selected_models:
        raw_runs = runs_by_model.get(model_id, [])
        runs = [run for run in raw_runs if (not question or run["question"] == question) and (not date or run["day"] == date)]
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
        models.append({**model_meta[model_id], "runs": len(runs), "sources": len(sources), "unique_sources": len({source["canonical_url"] for source in sources}),
            "question_count": len({run["question"] for run in runs}), "device_count": len({run["serial"] for run in runs}),
            "source_types": [{"name": name, "count": count} for name, count in Counter(source["type"] for source in sources).most_common()],
            "media": [{"name": name, "count": count} for name, count in Counter(source["media"] for source in sources).most_common(15)],
            "daily": daily, "questions": by_question, "top_articles": _top_sources(runs, "文章"), "top_videos": _top_sources(runs, "视频"),
            "article_keywords": keyword_counts(article_titles), "video_keywords": keyword_counts(video_titles),
            "recent_runs": sorted(runs, key=lambda item: item["finished_at"], reverse=True)[:30]})
    return {"generated_at": datetime.now(BEIJING).isoformat(timespec="seconds"), "filters": {"model": model, "question": question, "date": date},
            "models": models, "model_catalog": list(model_meta.values()), "questions": all_questions, "dates": all_dates}
