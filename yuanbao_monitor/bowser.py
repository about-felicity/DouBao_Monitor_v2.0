import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from selenium import webdriver
from selenium.webdriver.chrome.options import Options


def _find_chrome_executable() -> Optional[str]:
    """按常见路径查找 Chrome 可执行文件。"""
    candidates = [
        # 用户级 Chrome
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%PROGRAMFILES%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%PROGRAMFILES(x86)%\Google\Chrome\Application\chrome.exe"),
        # 系统级 Chromium / Edge 兜底
        os.path.expandvars(r"%PROGRAMFILES%\Microsoft\Edge\Application\msedge.exe"),
        os.path.expandvars(r"%PROGRAMFILES(x86)%\Microsoft\Edge\Application\msedge.exe"),
    ]
    for path in candidates:
        if path and os.path.isfile(path):
            return path
    return None


def _wait_for_port(host: str, port: int, timeout: float = 15.0) -> bool:
    """等待某个端口可连接。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=1.0):
                return True
        except OSError:
            time.sleep(0.3)
    return False


def connect_chrome(debug_port: int = 9222) -> webdriver.Chrome:
    """连接一个已经用 --remote-debugging-port 启动的 Chrome 实例。"""
    options = Options()
    options.add_experimental_option("debuggerAddress", f"127.0.0.1:{debug_port}")
    driver = webdriver.Chrome(options=options)
    return driver


def connect_or_launch_chrome(
    debug_port: int = 9222,
    chrome_path: Optional[str] = None,
    user_data_dir: Optional[str] = None,
    start_url: str = "https://yuanbao.tencent.com/chat/",
) -> webdriver.Chrome:
    """
    先尝试连接已有 Chrome；如果连不上，就自动启动一个带 remote-debugging 的 Chrome。

    参数:
        debug_port: 远程调试端口
        chrome_path: Chrome 可执行文件路径，None 则自动查找
        user_data_dir: Chrome 用户数据目录，None 则使用临时目录
        start_url: 启动后打开的页面
    """
    # 1) 先尝试连接已有实例
    try:
        driver = connect_chrome(debug_port)
        print(f"已连接到现有 Chrome（端口 {debug_port}）")
        return driver
    except Exception as e:
        print(f"未检测到现有 Chrome 调试端口: {e}")

    # 2) 自动查找 Chrome 路径
    exe = chrome_path or _find_chrome_executable()
    if not exe:
        raise RuntimeError(
            "找不到 Chrome 可执行文件。请手动指定 chrome_path，"
            "或先安装 Chrome / Edge，或先用 --remote-debugging-port=9222 启动 Chrome。"
        )
    print(f"找到浏览器: {exe}")

    # 3) 准备用户数据目录
    if user_data_dir is None:
        user_data_dir = os.path.join(os.getcwd(), "chrome_profile_auto")
    Path(user_data_dir).mkdir(parents=True, exist_ok=True)
    print(f"使用用户数据目录: {user_data_dir}")

    # 4) 启动 Chrome
    cmd = [
        exe,
        f"--remote-debugging-port={debug_port}",
        f"--user-data-dir={os.path.abspath(user_data_dir)}",
        "--no-first-run",
        "--no-default-browser-check",
        start_url,
    ]
    print(f"正在启动 Chrome: {' '.join(cmd)}")
    subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # 5) 等端口可用
    if not _wait_for_port("127.0.0.1", debug_port, timeout=20):
        raise RuntimeError(f"Chrome 调试端口 {debug_port} 未在 20 秒内就绪")

    # 6) 连接
    time.sleep(1.5)  # 给浏览器多一点时间初始化
    driver = connect_chrome(debug_port)
    print("Chrome 已启动并连接")
    return driver


if __name__ == "__main__":
    driver = connect_or_launch_chrome()
    print("当前页面标题:", driver.title)
    print("当前URL:", driver.current_url)
