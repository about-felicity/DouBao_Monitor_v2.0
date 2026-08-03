from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
from pathlib import Path
import shutil
import sys
import time


BASE_DIR = Path(__file__).resolve().parent
MONITOR_DIR = BASE_DIR.parent
DEFAULT_OUTPUT_ROOT = MONITOR_DIR / "doubao_standalone_deploy"
SHADOWBOT_NODE = Path(
    r"C:\ProgramData\ShadowBot\support_x64\mobile\NodeJS"
)
SHADOWBOT_ANDROID_SDK = Path(
    r"C:\ProgramData\ShadowBot\support_x64\mobile\AndroidSDK"
)
SHADOWBOT_JAVA_SDK = Path(
    r"C:\ProgramData\ShadowBot\support_x64\mobile\JavaSDK"
)
PYTHON_RUNTIME_SOURCE = Path(sys.base_prefix)
PYTHON_DISTRIBUTIONS = [
    "pillow",
    "requests",
    "websocket-client",
    "lxml",
    "openpyxl",
    "certifi",
    "charset-normalizer",
    "idna",
    "urllib3",
    "et-xmlfile",
]

ROOT_FILES = [
    "doubao_env_loader.py",
    "doubao_question_aliases.py",
    "run_doubao_latest_grab.py",
    "save_doubao_refs.py",
    "doubao_brand_settings.py",
    "doubao_brand_settings.json",
    "doubao_dashboard_server.py",
    "doubao_source_content_worker.py",
    "doubao_source_ai_worker.py",
    "doubao_product_ai_worker.py",
    "rebuild_doubao_products_from_answers.py",
]
CONTROLLER_FILES = [
    "doubao_mumu_loop.py",
    "doubao_mumu_web_pipeline.py",
    "doubao_mumu_scheduled_job.py",
    "doubao_mumu_control_panel.py",
    "doubao_remote_startup.py",
    "doubao_lan_client.py",
    "doubao_remote_sync_config.json.example",
    "配置远端豆包回传.ps1",
    "远端豆包一键配置回传.bat",
    "doubao_mumu_panel_config.json",
    "requirements.txt",
    "打开豆包MuMu控制面板.bat",
    "open_control_panel.cmd",
    "run_scheduled_doubao_job.bat",
    "远端电脑一键启动.bat",
    "remote_one_click.cmd",
    "README.md",
]


def copy_required(source: Path, destination: Path) -> None:
    if not source.exists():
        raise FileNotFoundError(source)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)


def copy_portable_python(destination: Path) -> None:
    for pattern in (
        "python*.exe",
        "python*.dll",
        "vcruntime*.dll",
        "LICENSE.txt",
    ):
        for source in PYTHON_RUNTIME_SOURCE.glob(pattern):
            if source.is_file():
                copy_required(source, destination / source.name)
    for directory in ("DLLs", "tcl"):
        source = PYTHON_RUNTIME_SOURCE / directory
        if source.exists():
            shutil.copytree(
                source,
                destination / directory,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
            )
    source_lib = PYTHON_RUNTIME_SOURCE / "Lib"

    def ignore_lib(path: str, names: list[str]) -> set[str]:
        ignored = {"__pycache__"}
        if Path(path).resolve() == source_lib.resolve():
            ignored.update({"site-packages", "test", "ensurepip"})
        ignored.update(
            name for name in names if name.endswith((".pyc", ".pyo"))
        )
        return ignored

    shutil.copytree(source_lib, destination / "Lib", ignore=ignore_lib)
    source_site = source_lib / "site-packages"
    target_site = destination / "Lib" / "site-packages"
    target_site.mkdir(parents=True, exist_ok=True)
    for name in PYTHON_DISTRIBUTIONS:
        distribution = importlib.metadata.distribution(name)
        for entry in distribution.files or []:
            source = Path(distribution.locate_file(entry)).resolve()
            if not source.is_file():
                continue
            try:
                relative = source.relative_to(source_site.resolve())
            except ValueError:
                continue
            if "__pycache__" in relative.parts or source.suffix in {
                ".pyc",
                ".pyo",
            }:
                continue
            copy_required(source, target_site / relative)


def build(
    output_root: Path,
    include_appium: bool,
    include_python: bool,
    make_zip: bool,
) -> dict:
    stamp = time.strftime("%Y%m%d_%H%M%S")
    package_root = output_root / f"doubao_standalone_{stamp}"
    monitor_target = package_root / "monitor"
    controller_target = monitor_target / "doubao_mumu_controller"
    monitor_target.mkdir(parents=True, exist_ok=False)
    controller_target.mkdir(parents=True, exist_ok=True)

    for name in ROOT_FILES:
        copy_required(MONITOR_DIR / name, monitor_target / name)
    shutil.copytree(
        MONITOR_DIR / "doubao_ref_extension",
        monitor_target / "doubao_ref_extension",
    )
    shutil.copytree(MONITOR_DIR / "monitor_core", monitor_target / "monitor_core")
    shutil.copytree(MONITOR_DIR / "model_plugins", monitor_target / "model_plugins")
    for name in CONTROLLER_FILES:
        copy_required(BASE_DIR / name, controller_target / name)

    if include_appium:
        if not SHADOWBOT_NODE.exists():
            raise RuntimeError(f"找不到可打包的 Appium：{SHADOWBOT_NODE}")
        runtime = controller_target / "portable_runtime"
        shutil.copytree(SHADOWBOT_NODE, runtime / "NodeJS")
        if not SHADOWBOT_ANDROID_SDK.exists():
            raise RuntimeError(
                f"找不到可打包的 Android SDK：{SHADOWBOT_ANDROID_SDK}"
            )
        if not SHADOWBOT_JAVA_SDK.exists():
            raise RuntimeError(
                f"找不到可打包的 Java SDK：{SHADOWBOT_JAVA_SDK}"
            )
        shutil.copytree(SHADOWBOT_ANDROID_SDK, runtime / "AndroidSDK")
        shutil.copytree(SHADOWBOT_JAVA_SDK, runtime / "JavaSDK")
    if include_python:
        copy_portable_python(
            controller_target / "portable_runtime" / "Python"
        )

    launcher = package_root / "一键启动独立采集.bat"
    launcher.write_text(
        "@echo off\r\n"
        "cd /d \"%~dp0monitor\\doubao_mumu_controller\"\r\n"
        "set \"DOUBAO_NO_LAUNCHER_PAUSE=1\"\r\n"
        "call \"remote_one_click.cmd\" --panel-only\r\n",
        encoding="ascii",
    )
    config_launcher = package_root / "配置问题与定时.bat"
    config_launcher.write_text(
        "@echo off\r\n"
        "cd /d \"%~dp0monitor\\doubao_mumu_controller\"\r\n"
        "call \"open_control_panel.cmd\"\r\n",
        encoding="ascii",
    )
    readme = package_root / "使用说明.txt"
    readme.write_text(
        "1. 新电脑只需安装 Google Chrome 和 MuMu；包内已带便携 Python、"
        "Appium、Android SDK、ADB 和 Java。\n"
        "2. 启动 MuMu，在豆包 App 中登录账号并保持 MuMu 运行。\n"
        "   支持同时启动任意多个 MuMu；每台可以登录不同豆包账号。\n"
        "3. 双击“一键启动独立采集.bat”。\n"
        "4. 程序会为每个 MuMu 实例启动独立调试 Chrome 和独立用户目录；"
        "无需手动安装插件，抓取器会自动注入豆包网页。第一次请在每个"
        " Chrome 分别登录对应 MuMu 的豆包账号。\n"
        "5. 问题与重复次数可在 monitor\\doubao_mumu_controller\\"
        "doubao_mumu_panel_config.json 中修改；也可双击“配置问题与定时.bat”"
        "使用控制面板修改并安装定时任务。“实例”留空会并行运行全部"
        "已启动 MuMu，也可填写 0,1,3。问题、运行参数、定时参数、"
        "自有品牌和竞品会自动保存，下次打开无需重新输入。\n"
        "6. 若要把豆包结果汇总到主电脑统一面板，请把主电脑生成的"
        "doubao_lan_pairing.json 拖到 monitor\\doubao_mumu_controller\\"
        "远端豆包一键配置回传.bat；"
        "网络中断时结果进入离线队列，恢复后自动续传。未配置时仍可独立运行。\n",
        encoding="utf-8",
    )

    zip_path = ""
    if make_zip:
        archive = shutil.make_archive(
            str(package_root),
            "zip",
            root_dir=str(package_root.parent),
            base_dir=package_root.name,
        )
        zip_path = archive
    return {
        "package_root": str(package_root),
        "zip": zip_path,
        "include_appium": include_appium,
        "include_python": include_python,
        "mode": "remote_capable",
        "data_upload": True,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="生成新电脑完全独立运行的一键豆包采集包。"
    )
    parser.add_argument("--output-root", default=str(DEFAULT_OUTPUT_ROOT))
    parser.add_argument(
        "--without-appium",
        action="store_true",
        help="不复制便携 Appium，目标电脑必须自行安装。",
    )
    parser.add_argument(
        "--without-python",
        action="store_true",
        help="不复制精简便携 Python，目标电脑必须自行安装。",
    )
    parser.add_argument("--no-zip", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    result = build(
        Path(args.output_root),
        include_appium=not args.without_appium,
        include_python=not args.without_python,
        make_zip=not args.no_zip,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
