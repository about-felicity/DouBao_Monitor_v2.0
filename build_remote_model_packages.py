from __future__ import annotations

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import re
import shutil
import zipfile


ROOT = Path(__file__).resolve().parent
MODELS = {
    "deepseek": ("DeepSeek", ROOT / "deepseek_monitor"),
    "yuanbao": ("腾讯元宝", ROOT / "yuanbao_monitor"),
    "wenxin": ("文心", ROOT / "wenxin_monitor"),
}
COMMON_FILES = (
    "remote_model_worker.py",
    "remote_model_control_panel.py",
    "remote_worker_requirements.txt",
)
EXCLUDED_NAMES = {
    "__pycache__",
    "chrome_profile",
    "chrome_profile_auto",
    "chrome_profiles",
    "diagnostics",
    "web_results",
    "dashboard",
    "yuanbao_state",
    "runtime",
    ".venv",
    ".git",
    "node_modules",
    "deepseek_brand_ai_cache.json",
    "yuanbao_brand_ai_cache.json",
}
EXCLUDED_SUFFIXES = {".log", ".jsonl", ".tmp", ".pyc", ".png", ".xml", ".xlsx", ".html", ".env"}


def copy_source_tree(source: Path, target: Path) -> None:
    for current, directories, filenames in os.walk(source):
        directories[:] = [name for name in directories if name not in EXCLUDED_NAMES]
        current_path = Path(current)
        for filename in filenames:
            if filename in EXCLUDED_NAMES or filename.endswith("_state.json") or filename == "dashboard.json":
                continue
            path = current_path / filename
            if path.suffix.lower() in EXCLUDED_SUFFIXES:
                continue
            relative = path.relative_to(source)
            destination = target / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, destination)


def load_pairing(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    receiver_url = str(value.get("receiver_url") or "")
    token = str(value.get("token") or "")
    if not receiver_url.startswith("http://") or len(token) < 24:
        raise ValueError("主机配对文件无效，请先启动主机面板和 8791 接收器")
    return value


def clean_generated_packages(output_root: Path) -> None:
    pattern = re.compile(r"(?:deepseek|yuanbao|wenxin|afu)_remote_\d{8}_\d{6}(?:\.zip)?")
    if not output_root.exists():
        return
    for path in output_root.iterdir():
        if not pattern.fullmatch(path.name):
            continue
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()


def build_package(model: str, pairing: dict, output_root: Path, stamp: str) -> tuple[Path, Path]:
    model_name, model_source = MODELS[model]
    package_root = output_root / f"{model}_remote_{stamp}"
    package_root.mkdir(parents=True, exist_ok=False)
    for name in COMMON_FILES:
        shutil.copy2(ROOT / name, package_root / name)
    copy_source_tree(ROOT / "monitor_core", package_root / "monitor_core")
    copy_source_tree(ROOT / "model_plugins" / model, package_root / "model_plugins" / model)
    copy_source_tree(model_source, package_root / model_source.name)
    sync_path = package_root / "runtime" / "remote_workers" / f"{model}_sync.json"
    sync_path.parent.mkdir(parents=True, exist_ok=True)
    sync_path.write_text(
        json.dumps({**pairing, "enabled": True, "model": model}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    launcher = package_root / f"一键启动{model_name}远端采集.bat"
    launcher_content = (
        "@echo off\r\n"
        "setlocal\r\n"
        "cd /d \"%~dp0\"\r\n"
        "set \"PYTHONUTF8=1\"\r\n"
        "set \"PY_CMD=python\"\r\n"
        "where py >nul 2>nul && set \"PY_CMD=py -3\"\r\n"
        "%PY_CMD% -c \"import requests,selenium,uiautomator2,websocket\" >nul 2>nul\r\n"
        "if errorlevel 1 %PY_CMD% -m pip install -r remote_worker_requirements.txt --disable-pip-version-check\r\n"
        "if errorlevel 1 (echo Dependency installation failed & pause & exit /b 1)\r\n"
        f"%PY_CMD% remote_model_worker.py --model {model} --preflight\r\n"
        "if errorlevel 1 (echo Preflight failed & pause & exit /b 1)\r\n"
        f"%PY_CMD% remote_model_control_panel.py --model {model}\r\n"
        "if errorlevel 1 pause\r\n"
    )
    launcher.write_bytes(launcher_content.encode("ascii"))
    (package_root / f"start_{model}_remote.cmd").write_bytes(launcher_content.encode("ascii"))
    (package_root / "使用说明.txt").write_text(
        f"本部署包只运行 {model_name}。\n"
        f"主机回传地址已写入：{pairing['receiver_url']}\n"
        "主机 IP 变化时会通过局域网 UDP 8792 自动发现新地址，并继续补传离线队列。\n"
        f"远端电脑安装 Chrome、MuMu 和 Python 后，双击：{launcher.name}\n"
        "首次打开后先点账号检查，确认 App 与专用 Chrome 登录一致，再点启动采集。\n",
        encoding="utf-8-sig",
    )
    archive = output_root / f"{package_root.name}.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as handle:
        for path in package_root.rglob("*"):
            if path.is_file():
                handle.write(path, Path(package_root.name) / path.relative_to(package_root))
    return package_root, archive


def main() -> int:
    parser = argparse.ArgumentParser(description="生成已内置主机回传地址的远端单模型部署包")
    parser.add_argument("--model", choices=("all", *MODELS), default="all")
    parser.add_argument("--pairing", type=Path, default=ROOT / "runtime" / "lan_result_pairing.json")
    parser.add_argument("--output", type=Path, default=ROOT / "remote_model_deploy_three_models")
    args = parser.parse_args()
    pairing = load_pairing(args.pairing)
    clean_generated_packages(args.output)
    args.output.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    selected = MODELS if args.model == "all" else (args.model,)
    for model in selected:
        folder, archive = build_package(model, pairing, args.output, stamp)
        print(f"{model}: {folder}")
        print(f"{model}: {archive}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
