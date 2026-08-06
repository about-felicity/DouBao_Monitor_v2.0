from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from monitor_core.jsonl_dashboard import build_jsonl_dashboard

BASE_DIR = Path(__file__).resolve().parent

if __name__ == "__main__":
    payload = build_jsonl_dashboard("wenxin", BASE_DIR / "wenxin_results.jsonl", BASE_DIR / "dashboard.json")
    print(f"文心面板数据：{payload['successful_runs']} 轮，{payload['total_sources']} 条信源")
