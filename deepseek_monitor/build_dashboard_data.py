"""把 DeepSeek JSONL 聚合为统一面板使用的数据结构。"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
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


def empty_payload() -> dict:
    return {"generated_at": datetime.now(BEIJING).isoformat(timespec="seconds"), "total_runs": 0, "successful_runs": 0, "total_sources": 0, "unique_sources": 0, "question_count": 0, "device_count": 0, "date_range": "等待采集", "questions": [], "devices": [], "runs": [], "daily": [], "top_media": [], "source_types": [], "brands": [], "products": []}


def build() -> dict:
    records = []
    if RESULTS.exists():
        for line in RESULTS.read_text(encoding="utf-8").splitlines():
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    runs = []
    seen = set()
    for record in records:
        if record.get("status") != "success":
            continue
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
        runs.append({"run_id": rid, "sequence": len(runs) + 1, "round": int(record.get("round") or 0), "serial": str(record.get("serial") or "DeepSeek Web"), "question": str(record.get("question") or "未知问题"), "reply": str(record.get("reply") or ""), "web_body": str(record.get("web_body") or ""), "started_at": str(record.get("started_at") or ""), "finished_at": str(record.get("finished_at") or ""), "day": day(record.get("finished_at") or record.get("started_at") or ""), "status": "success", "sources": sources, "brands": [], "products": []})
    payload = empty_payload()
    all_sources = [source for run in runs for source in run["sources"]]
    questions = []
    for question in dict.fromkeys(run["question"] for run in runs):
        selected = [run for run in runs if run["question"] == question]
        refs = [source for run in selected for source in run["sources"]]
        questions.append({"question": question, "runs": len(selected), "sources": len(refs), "unique_sources": len({item["canonical_url"] for item in refs})})
    devices = []
    for serial in dict.fromkeys(run["serial"] for run in runs):
        selected = [run for run in runs if run["serial"] == serial]
        devices.append({"serial": serial, "runs": len(selected), "sources": sum(len(run["sources"]) for run in selected), "latest": max((run["finished_at"] for run in selected), default="")})
    daily = []
    for date in sorted({run["day"] for run in runs if run["day"]}, reverse=True):
        selected = [run for run in runs if run["day"] == date]
        refs = [source for run in selected for source in run["sources"]]
        daily.append({"date": date, "runs": len(selected), "successful_runs": len(selected), "sources": len(refs), "unique_sources": len({item["canonical_url"] for item in refs}), "question_count": len({run["question"] for run in selected}), "device_count": len({run["serial"] for run in selected}), "product_mentions": 0, "brands": [], "media": counted(item["media"] for item in refs), "types": counted(item["type"] for item in refs), "questions": counted(run["question"] for run in selected)})
    times = [run["finished_at"] for run in runs if run["finished_at"]]
    date_range = "等待采集"
    if times:
        first, last = min(times)[:10], max(times)[:10]
        date_range = first if first == last else f"{first} 至 {last}"
    payload.update({"total_runs": len(records), "successful_runs": len(runs), "total_sources": len(all_sources), "unique_sources": len({item["canonical_url"] for item in all_sources}), "question_count": len(questions), "device_count": len(devices), "date_range": date_range, "questions": questions, "devices": devices, "runs": runs, "daily": daily, "top_media": counted(item["media"] for item in all_sources), "source_types": counted(item["type"] for item in all_sources)})
    return payload


def main() -> None:
    payload = build()
    OUTPUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"DeepSeek 面板数据：{payload['successful_runs']} 轮，{payload['total_sources']} 条信源")


if __name__ == "__main__":
    main()
