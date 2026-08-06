from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from monitor_core.jsonl_dashboard import build_jsonl_dashboard

BASE_DIR = Path(__file__).resolve().parent

if __name__ == "__main__":
    payload = build_jsonl_dashboard("afu", BASE_DIR / "afu_results.jsonl", BASE_DIR / "dashboard.json")
    print(f"蚂蚁阿福面板数据：{payload['successful_runs']} 轮，{payload['total_sources']} 条信源")
