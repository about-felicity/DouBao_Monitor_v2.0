from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse


ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
RESULTS_PATH = DATA_DIR / "deepseek_results.jsonl"
sys.path.insert(0, str(ROOT))
import local_receiver  # noqa: E402


INTERNAL = ("deepseek.com", "deepseek.cn", "deepseekstatic.com")


def unwrap(raw: str) -> str:
    value = str(raw or "").strip()
    try:
        parsed = urlparse(value)
    except ValueError:
        return ""
    query = parse_qs(parsed.query)
    for key in ("url", "target", "target_url", "targetUrl", "redirect", "redirect_url", "dest", "destination"):
        if query.get(key):
            nested = unquote(query[key][0])
            if urlparse(nested).scheme in {"http", "https"}:
                return nested
    return value if parsed.scheme in {"http", "https"} else ""


def likely_source(source: dict) -> bool:
    url = unwrap(str(source.get("url") or ""))
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    host = parsed.hostname.casefold().removeprefix("www.") if parsed.hostname else ""
    path = parsed.path.casefold()
    if not host or any(host == item or host.endswith("." + item) for item in INTERNAL):
        return False
    if host == "s2.zimgs.cn" or host.endswith(".zimgs.cn") or host == "w3.org" or host.endswith(".w3.org"):
        return False
    if host.endswith(".vipserver") or host.endswith(".alibaba-inc.com") or host == "space.bilibili.com":
        return False
    if host.endswith(".xiaohongshu.com") and "/user/profile/" in path:
        return False
    if re.search(r"/(favicon\.ico|[^/]+\.(?:png|jpe?g|gif|webp|svg|avif))$", path, re.I):
        return False
    if re.search(r"user-avatar|avatar/", url, re.I):
        return False
    return True


def source_count(text: str) -> int:
    values = [int(value) for value in re.findall(r"(\d{1,3})\s*篇来源", text or "")]
    return values[-1] if values else 0


def clean_reply(text: str) -> str:
    value = str(text or "").replace("\r\n", "\n").strip()
    match = re.search(r"\n\d{1,2}:\d{2}\n", value)
    if match and match.start() > len(value) * 0.35:
        value = value[:match.start()].rstrip()
    return re.sub(r"\n\d{1,3}\s*篇来源\s*$", "", value).strip()


def normalize(row: dict) -> dict:
    row = dict(row)
    raw_reply = str(row.get("reply") or row.get("web_body") or "")
    expected = source_count(raw_reply) or int(row.get("expected_source_count") or 0)
    raw_sources = list(row.get("sources") or [])
    found: dict[str, dict] = {}
    for source in raw_sources:
        if not isinstance(source, dict) or not likely_source(source):
            continue
        url = unwrap(str(source.get("url") or ""))
        if not url or url in found:
            continue
        next_source = dict(source)
        next_source["url"] = url
        found[url] = next_source
    sources = list(found.values())[:expected] if expected else list(found.values())
    reply = clean_reply(raw_reply)
    row.update({
        "reply": reply, "web_body": reply, "sources": sources,
        "expected_source_count": expected, "page_reported_source_count": expected,
        "source_capture_complete": not expected or len(sources) >= expected,
        "source_count_basis": "page_source_badge" if expected else row.get("source_count_basis", "no_page_count"),
    })
    capture = dict(row.get("capture") or {})
    capture.update({
        "raw_source_count": capture.get("raw_source_count", len(raw_sources)),
        "filtered_source_count": len(sources), "migrated_clean_version": 1,
    })
    row["capture"] = capture
    return local_receiver.normalize_source_titles(row)


def main() -> int:
    if not RESULTS_PATH.exists():
        print("No results file; nothing to normalize.")
        return 0
    rows = []
    for line in RESULTS_PATH.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(normalize(json.loads(line)))
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = DATA_DIR / f"deepseek_results.raw_{stamp}.jsonl"
    shutil.copy2(RESULTS_PATH, backup)
    descriptor, temporary_name = tempfile.mkstemp(prefix="deepseek_results.", suffix=".tmp", dir=DATA_DIR)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
        Path(temporary_name).replace(RESULTS_PATH)
    finally:
        Path(temporary_name).unlink(missing_ok=True)
    for row in rows:
        local_receiver.write_result_artifacts(row)
    local_receiver.append_event({
        "timestamp": local_receiver.now(), "level": "info", "event": "existing_results_normalized",
        "run_id": "migration", "round": 0, "prompt": "",
        "details": {"result_count": len(rows), "backup_path": str(backup), "results_path": str(RESULTS_PATH)},
    })
    print(json.dumps({"ok": True, "result_count": len(rows), "backup": str(backup), "results": str(RESULTS_PATH)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
