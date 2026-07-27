import csv
import os
import sys
import time
from collections import OrderedDict

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
REFS_CSV = os.path.join(BASE_DIR, "doubao_refs_result.csv")
PRODUCTS_CSV = os.path.join(BASE_DIR, "doubao_products_result.csv")

sys.path.insert(0, BASE_DIR)

import run_doubao_latest_grab as grabber  # noqa: E402
import save_doubao_refs as saver  # noqa: E402


def read_csv_rows(path):
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def safe_int(value):
    try:
        return int(str(value or "").strip())
    except Exception:
        return 0


def is_question_match(row, keyword):
    text = " ".join(str(row.get(key) or "") for key in ("question", "chat_title", "title", "page_url"))
    return keyword in text or ("推荐" + keyword.replace("推荐", "")) in text


def unique_ref_runs(keyword):
    refs = read_csv_rows(REFS_CSV)
    runs = OrderedDict()
    for row in refs:
        if not is_question_match(row, keyword):
            continue
        run_no = safe_int(row.get("run_no"))
        page_url = str(row.get("page_url") or "").strip()
        if not run_no or not page_url:
            continue
        if run_no not in runs:
            runs[run_no] = {
                "run_no": run_no,
                "run_time": row.get("run_time") or "",
                "question": row.get("question") or row.get("chat_title") or keyword,
                "page_url": page_url,
            }
    return runs


def product_run_set(keyword):
    rows = read_csv_rows(PRODUCTS_CSV)
    result = set()
    for row in rows:
        if is_question_match(row, keyword):
            run_no = safe_int(row.get("run_no"))
            if run_no:
                result.add(run_no)
    return result


def navigate_and_grab(url):
    page = grabber.find_doubao_page()
    ws_url = page.get("webSocketDebuggerUrl")
    if not ws_url:
        raise RuntimeError("豆包页面缺少 webSocketDebuggerUrl")
    grabber.cdp_call(ws_url, "Page.navigate", {"url": url}, timeout=10)
    ws_url, ready_value = grabber.wait_for_doubao_ready(
        url,
        timeout=int(os.environ.get("DOUBAO_BACKFILL_READY_TIMEOUT", "45")),
        require_content=True,
        require_references=False,
    )
    print("ready:", ready_value)
    return grabber.grab_with_retry(ws_url, url)


def backfill(keyword, min_run=0, max_count=0):
    runs = unique_ref_runs(keyword)
    existing = product_run_set(keyword)
    missing = [
        item for run_no, item in runs.items()
        if run_no not in existing and run_no >= min_run
    ]
    if max_count:
        missing = missing[:max_count]

    print("keyword:", keyword)
    print("ref_runs:", len(runs), "product_runs:", len(existing), "missing:", len(missing))
    written_total = 0
    failed = 0
    for index, item in enumerate(missing, 1):
        print("\n[%s/%s] run=%s url=%s" % (index, len(missing), item["run_no"], item["page_url"]))
        try:
            payload = navigate_and_grab(item["page_url"])
            payload["question"] = item["question"] or keyword
            payload["url"] = item["page_url"]
            count = saver.append_products_csv(payload, item["run_no"], item["run_time"])
            written_total += count
            print("products_written:", count)
        except Exception as exc:
            failed += 1
            print("FAILED:", repr(exc))
        time.sleep(float(os.environ.get("DOUBAO_BACKFILL_SLEEP", "1.2")))
    print("\nDONE written_total=%s failed=%s" % (written_total, failed))


def remove_product_rows(keyword):
    rows = read_csv_rows(PRODUCTS_CSV)
    if not rows:
        print("products csv empty")
        return 0
    backup = PRODUCTS_CSV + ".before_" + keyword.replace("/", "_") + "_rebuild.bak"
    if not os.path.exists(backup):
        with open(backup, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=rows[0].keys())
            writer.writeheader()
            writer.writerows(rows)

    kept = [row for row in rows if not is_question_match(row, keyword)]
    removed = len(rows) - len(kept)
    with open(PRODUCTS_CSV, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(kept)
    print("removed_product_rows:", removed, "backup:", backup)
    return removed


if __name__ == "__main__":
    keyword = sys.argv[1] if len(sys.argv) > 1 else "面膜推荐"
    if len(sys.argv) > 2 and sys.argv[2] == "--rebuild":
        remove_product_rows(keyword)
        min_run = safe_int(sys.argv[3]) if len(sys.argv) > 3 else 0
        max_count = safe_int(sys.argv[4]) if len(sys.argv) > 4 else 0
    else:
        min_run = safe_int(sys.argv[2]) if len(sys.argv) > 2 else 0
        max_count = safe_int(sys.argv[3]) if len(sys.argv) > 3 else 0
    backfill(keyword, min_run=min_run, max_count=max_count)
