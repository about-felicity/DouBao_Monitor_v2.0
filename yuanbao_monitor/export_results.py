"""把 yuanbao_results.jsonl 导出为便于筛选的 Excel。"""

import json
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font


BASE_DIR = Path(__file__).resolve().parent


def main():
    source = BASE_DIR / "yuanbao_results.jsonl"
    target = BASE_DIR / "yuanbao_results.xlsx"
    records = []
    if source.exists():
        for line in source.read_text(encoding="utf-8").splitlines():
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "元宝采集结果"
    headers = ["状态", "设备", "轮次", "问题", "App可见回答", "网页完整回答", "信源数", "信源标题", "信源链接", "开始时间", "结束时间", "错误"]
    sheet.append(headers)
    for cell in sheet[1]:
        cell.font = Font(bold=True)
    for record in records:
        sources = record.get("sources") or []
        sheet.append([
            record.get("status", ""), record.get("serial", ""), record.get("round", ""),
            record.get("question", ""), record.get("reply", ""), record.get("web_body", ""),
            len(sources), "\n".join(s.get("title", "") for s in sources),
            "\n".join(s.get("url", "") for s in sources), record.get("started_at", ""),
            record.get("finished_at", ""), record.get("error") or record.get("web_error") or "",
        ])
    widths = [10, 22, 8, 30, 60, 80, 8, 50, 70, 24, 24, 30]
    for index, width in enumerate(widths, 1):
        sheet.column_dimensions[chr(64 + index)].width = width
    for row in sheet.iter_rows(min_row=2):
        for cell in row:
            cell.alignment = Alignment(vertical="top", wrap_text=True)
    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = sheet.dimensions
    workbook.save(target)
    print(target)


if __name__ == "__main__":
    main()
